import importlib

import pytest
from fastapi.testclient import TestClient

from helpers import make_csv, tap


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "api.sqlite3"
    monkeypatch.setenv("TAPS_DB", str(db_file))
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def upload(client, rows, name="taps.csv"):
    return client.post(
        "/api/uploads",
        files={"file": (name, make_csv(rows), "text/csv")},
    )


def test_index_and_health(client):
    assert client.get("/healthz").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200
    assert "地铁站间客流" in page.text


def test_upload_then_view_day(client):
    res = upload(client, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "B", "out"),
        tap("e3", "c2", "2024-05-01T08:30:00Z", "C", "in"),
    ])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["day_states"] == {"2024-05-01": "current"}

    day = client.get("/api/days/2024-05-01").json()
    assert day["status"] == "current"
    assert day["station_pairs"][0] == {
        "from_station": "A", "to_station": "B", "trips": 1,
        "computed_at": day["computed_at"],
    }
    assert day["pending_total"] == 1

    days = client.get("/api/days").json()["days"]
    assert [d["date"] for d in days] == ["2024-05-01"]


def test_bad_csv_returns_400(client):
    res = client.post(
        "/api/uploads",
        files={"file": ("bad.csv", b"foo,bar\n1,2\n", "text/csv")},
    )
    assert res.status_code == 400
    assert "表头" in res.json()["detail"]


def test_unknown_day_404(client):
    assert client.get("/api/days/2030-01-01").status_code == 404


def test_bad_day_format(client):
    assert client.get("/api/days/not-a-date").status_code == 400


def test_recompute_endpoint(client):
    upload(client, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "B", "out"),
    ])
    res = client.post("/api/recompute?days=2024-05-01")
    assert res.status_code == 200
    assert res.json()["day_states"] == {"2024-05-01": "current"}
    assert client.post("/api/recompute").status_code == 400


def test_cross_midnight_correction_via_api(client):
    upload(client, [
        tap("in1", "c1", "2024-05-01T23:50:00Z", "A", "in"),
        tap("out1", "c1", "2024-05-02T00:20:00Z", "B", "out"),
    ])
    res = upload(client, [tap("out1", "c1", "2024-05-02T00:20:00Z", "D", "out", revision=1)])
    assert set(res.json()["affected_days"]) == {"2024-05-01", "2024-05-02"}
    day1 = client.get("/api/days/2024-05-01").json()
    day2 = client.get("/api/days/2024-05-02").json()
    assert day1["status"] == day2["status"] == "current"
    assert [(p["from_station"], p["to_station"]) for p in day1["station_pairs"]] == [("A", "D")]
    assert day2["trip_total"] == 0


# ---------------------------------------------------------------------------
# manifest reconciliation endpoints
# ---------------------------------------------------------------------------

def manifest_bytes(rows):
    return ("事件号,修订号\n" + "".join(f"{e},{r}\n" for e, r in rows)).encode("utf-8")


def post_manifest(client, station, day, rows, name="manifest.csv"):
    return client.post(
        "/api/manifests",
        data={"station": station, "date": day},
        files={"file": (name, manifest_bytes(rows), "text/csv")},
    )


def test_manifest_upload_complete_and_mismatch(client):
    upload(client, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "国贸", "out"),
    ])
    res = post_manifest(client, "西直门", "2024-05-01", [["e1", 0]])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reconciliation"]["status"] == "complete"
    assert body["reconciliation"]["counts"] == {"missing": 0, "revision": 0, "unexpected": 0}

    res = post_manifest(client, "西直门", "2024-05-01", [["e1", 2], ["e8", 0]])
    assert res.status_code == 200
    rec = res.json()["reconciliation"]
    assert rec["status"] == "mismatch"
    assert rec["counts"] == {"missing": 1, "revision": 1, "unexpected": 0}
    assert res.json()["replaced_manifest_id"] is not None

    listing = client.get("/api/recon").json()
    row = next(r for r in listing["reconciliation"]
               if (r["station"], r["date"]) == ("西直门", "2024-05-01"))
    assert row["status"] == "mismatch"
    assert row["missing"] == 1 and row["revision_mismatch"] == 1
    assert listing["summary"]["mismatch"] >= 1


def test_manifest_without_taps_reports_missing(client):
    upload(client, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "国贸", "out"),
    ])
    body = post_manifest(client, "国贸", "2024-05-01", [["e2", 0], ["e7", 0]]).json()
    assert body["reconciliation"]["status"] == "mismatch"
    assert body["reconciliation"]["counts"]["missing"] == 1


def test_station_day_without_manifest_is_not_complete(client):
    upload(client, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "国贸", "out"),
    ])
    detail = client.get("/api/recon/国贸/2024-05-01").json()
    assert detail["status"] == "no_manifest"
    assert detail["checked"] is False
    assert detail["diffs"] == []
    # The station-day still appears in the list (so it cannot be overlooked).
    rows = client.get("/api/recon").json()["reconciliation"]
    assert ("国贸", "2024-05-01") in {(r["station"], r["date"]) for r in rows}


def test_manifest_validation_errors(client):
    assert post_manifest(client, "", "2024-05-01", [["e1", 0]]).status_code == 400
    assert post_manifest(client, "西直门", "2024-13-01", [["e1", 0]]).status_code == 400
    res = client.post(
        "/api/manifests",
        data={"station": "西直门", "date": "2024-05-01"},
        files={"file": ("bad.csv", b"foo,bar\n1,2\n", "text/csv")},
    )
    assert res.status_code == 400
    assert "表头" in res.json()["detail"]


def test_recon_retry_without_manifest_is_400(client):
    res = client.post("/api/recon/西直门/2024-05-01/retry")
    assert res.status_code == 400


def test_index_shows_reconciliation_section(client):
    upload(client, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "国贸", "out"),
    ])
    post_manifest(client, "西直门", "2024-05-01", [["e1", 0]])
    page = client.get("/")
    assert page.status_code == 200
    assert "站点日核收" in page.text
    assert "核齐" in page.text
    assert "上传站点日完整清单" in page.text


def test_correction_withdraws_conclusion_via_api(client):
    upload(client, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "国贸", "out"),
    ])
    assert post_manifest(client, "西直门", "2024-05-01", [["e1", 0]]).json()[
        "reconciliation"]["status"] == "complete"
    # Move e1 to 国贸 with a higher revision: 西直门 loses it.
    res = upload(client, [tap("e1", "c1", "2024-05-01T08:00:00Z", "国贸", "in", revision=1)])
    assert res.status_code == 200
    detail = client.get("/api/recon/西直门/2024-05-01").json()
    assert detail["status"] == "mismatch"
    assert detail["counts"]["missing"] == 1
    guomao = client.get("/api/recon/国贸/2024-05-01").json()
    assert guomao["status"] == "no_manifest"   # has taps, still not a conclusion
