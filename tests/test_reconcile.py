"""Manifest reconciliation: completeness, replacement, withdrawal, recovery."""

import csv
import io

import pytest

from app import db, reconcile, service

from helpers import make_csv, tap, upload


def manifest_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["事件号", "修订号"])
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def upload_manifest(db_path, station, day, rows, name="manifest.csv", run=True):
    result = reconcile.upload_manifest(
        db_path, station, day, name, manifest_csv(rows)
    )
    if run:
        reconcile.run_pending_recon_jobs(db_path)
    return result


def recon(db_path, station, day):
    conn = db.connect(db_path)
    try:
        return reconcile.recon_detail(conn, station, day)
    finally:
        conn.close()


def recon_row(db_path, station, day):
    conn = db.connect(db_path)
    try:
        for row in reconcile.recon_list(conn):
            if (row["station"], row["date"]) == (station, day):
                return row
        return None
    finally:
        conn.close()


SEED = [
    tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in"),
    tap("e2", "c1", "2024-05-01T08:20:00Z", "国贸", "out"),
    tap("e3", "c2", "2024-05-01T09:00:00Z", "西直门", "in"),
    tap("e4", "c2", "2024-05-01T09:30:00Z", "王府井", "out"),
]


def test_exact_match_is_complete(db_path):
    upload(db_path, SEED)
    result = upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    assert result.rows == 2
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "complete"
    assert detail["checked"] is True
    assert detail["diffs"] == []
    assert detail["manifest_entries"] == 2
    assert detail["accepted_taps"] == 2


def test_missing_revision_and_unexpected_are_reported(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [
        ["e1", 1],      # accepted is revision 0 -> mismatch
        ["e3", 0],
        ["e9", 0],      # never arrived -> missing
    ])
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "mismatch"
    by_kind = {(d["kind"], d["event_no"]): d for d in detail["diffs"]}
    assert by_kind[("missing", "e9")]["manifest_rev"] == 0
    assert by_kind[("revision", "e1")] == {
        "kind": "revision", "event_no": "e1", "accepted_rev": 0,
        "manifest_rev": 1, "accepted_station": "西直门",
        "detail": "已受理修订号 0，清单为 1",
    }
    assert ("unexpected", "e2") not in by_kind   # different station


def test_extra_accepted_tap_at_station_is_unexpected(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0]])
    detail = recon(db_path, "西直门", "2024-05-01")
    kinds = {(d["kind"], d["event_no"]) for d in detail["diffs"]}
    assert kinds == {("unexpected", "e3")}


def test_station_day_without_manifest_never_complete(db_path):
    upload(db_path, SEED)
    row = recon_row(db_path, "国贸", "2024-05-01")
    assert row["status"] == "no_manifest"
    assert row["checked"] is False
    assert row["missing"] is None
    detail = recon(db_path, "国贸", "2024-05-01")
    assert detail["status"] == "no_manifest"
    assert detail["checked"] is False


def test_replacing_manifest_withdraws_old_conclusion_immediately(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"

    # Accept the replacement without running the worker: conclusion must be gone.
    replacement = upload_manifest(
        db_path, "西直门", "2024-05-01", [["e1", 0]], run=False
    )
    assert replacement.replaced_manifest_id is not None
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "pending"
    assert detail["checked"] is False
    assert detail["diffs"] == []
    assert detail["manifest"]["id"] == replacement.manifest_id

    reconcile.run_pending_recon_jobs(db_path)
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "mismatch"
    assert {(d["kind"], d["event_no"]) for d in detail["diffs"]} == {("unexpected", "e3")}


def test_only_one_active_manifest_per_station_day(db_path):
    first = upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0]])
    second = upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0]])
    conn = db.connect(db_path)
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM manifests WHERE station='西直门' AND service_date='2024-05-01' "
            "AND is_active = 1"
        ).fetchone()["n"]
        history = reconcile.manifest_history(conn, "西直门", "2024-05-01")
    finally:
        conn.close()
    assert active == 1
    assert [m["is_active"] for m in history] == [1, 0]
    assert second.replaced_manifest_id == first.manifest_id


def test_correction_withdraws_old_and_new_station_day(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    upload_manifest(db_path, "国贸", "2024-05-01", [["e2", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"
    assert recon(db_path, "国贸", "2024-05-01")["status"] == "complete"

    # Correct e2 from 国贸 to 西直门 (same day).  Both station-days lose
    # consistency and must be withdrawn at acceptance time.
    accepted = service.accept_upload(db_path, "fix.csv", make_csv([
        tap("e2", "c1", "2024-05-01T08:20:00Z", "西直门", "out", revision=1),
    ]))
    assert ("西直门", "2024-05-01") in accepted.recon_pairs
    assert ("国贸", "2024-05-01") in accepted.recon_pairs
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "pending"
    assert recon(db_path, "国贸", "2024-05-01")["status"] == "pending"

    reconcile.run_pending_recon_jobs(db_path)
    west = recon(db_path, "西直门", "2024-05-01")
    guomao = recon(db_path, "国贸", "2024-05-01")
    assert west["status"] == "mismatch"
    assert ("unexpected", "e2") in {(d["kind"], d["event_no"]) for d in west["diffs"]}
    assert guomao["status"] == "mismatch"
    assert ("missing", "e2") in {(d["kind"], d["event_no"]) for d in guomao["diffs"]}


def test_correction_moving_date_withdraws_both_days(db_path):
    upload(db_path, [
        tap("e1", "c1", "2024-05-01T23:00:00Z", "西直门", "in"),
        tap("e2", "c1", "2024-05-01T23:30:00Z", "国贸", "out"),
    ])
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0]])
    upload_manifest(db_path, "西直门", "2024-05-02", [["e1", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"
    # Day 2 has no e1 yet -> mismatch; the point is the withdrawal below.
    assert recon(db_path, "西直门", "2024-05-02")["status"] == "mismatch"

    accepted = service.accept_upload(db_path, "move.csv", make_csv([
        tap("e1", "c1", "2024-05-02T00:10:00Z", "西直门", "in", revision=1),
    ]))
    assert ("西直门", "2024-05-01") in accepted.recon_pairs
    assert ("西直门", "2024-05-02") in accepted.recon_pairs
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "pending"
    assert recon(db_path, "西直门", "2024-05-02")["status"] == "pending"

    reconcile.run_pending_recon_jobs(db_path)
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "mismatch"
    assert ("missing", "e1") in {
        (d["kind"], d["event_no"])
        for d in recon(db_path, "西直门", "2024-05-01")["diffs"]
    }
    # Day 2 now has e1, but at revision 1 while the list says 0.
    day2 = recon(db_path, "西直门", "2024-05-02")
    assert day2["status"] == "mismatch"
    assert {(d["kind"], d["event_no"]) for d in day2["diffs"]} == {("revision", "e1")}

    upload_manifest(db_path, "西直门", "2024-05-02", [["e1", 1]])
    assert recon(db_path, "西直门", "2024-05-02")["status"] == "complete"


def test_lower_revision_ignored_does_not_withdraw(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"
    accepted = service.accept_upload(db_path, "old.csv", make_csv([
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in", revision=0),
    ]))
    assert accepted.inserted == 0
    assert accepted.recon_pairs == []
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"


def test_failed_recheck_does_not_restore_old_conclusion(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"

    service.accept_upload(db_path, "fix.csv", make_csv([
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in", revision=1),
    ]))
    original = reconcile._publish_recon

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    reconcile._publish_recon = boom
    try:
        reconcile.run_pending_recon_jobs(db_path)
    finally:
        reconcile._publish_recon = original

    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "failed"
    assert detail["checked"] is False
    assert detail["diffs"] == []
    assert "disk full" in detail["last_error"]

    # Manual retry is the repair path and produces the real conclusion.
    reconcile.request_recon_retry(db_path, "西直门", "2024-05-01")
    reconcile.run_pending_recon_jobs(db_path)
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "mismatch"
    assert {(d["kind"], d["event_no"]) for d in detail["diffs"]} == {("revision", "e1")}


def test_restart_with_pending_job_never_restores_old_conclusion(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"

    accepted = service.accept_upload(db_path, "fix.csv", make_csv([
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in", revision=1),
    ]))
    conn = db.connect(db_path)
    try:
        # Simulate a crash mid-check: job claimed running, state left pending,
        # and a corrupt "complete" claim from the previous epoch.
        conn.execute("UPDATE recon_jobs SET status='running' WHERE station='西直门'")
        conn.execute(
            "UPDATE recon_state SET status='complete', data_epoch=0 WHERE station='西直门'"
        )
        conn.execute("INSERT INTO recon_diffs (station, service_date, kind, event_no) "
                     "VALUES ('西直门', '2024-05-01', 'missing', 'stale')")
    finally:
        conn.close()
    assert accepted.recon_pairs

    reconcile.recover_recon(db_path)
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "pending"
    assert detail["checked"] is False
    assert detail["diffs"] == []

    reconcile.run_pending_recon_jobs(db_path)
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "mismatch"
    assert {(d["kind"], d["event_no"]) for d in detail["diffs"]} == {("revision", "e1")}


def test_restart_demotes_conclusion_if_manifest_was_lost(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"
    conn = db.connect(db_path)
    try:
        conn.execute(
            "UPDATE manifests SET is_active = 0 WHERE station='西直门' AND service_date='2024-05-01'"
        )
    finally:
        conn.close()
    reconcile.recover_recon(db_path)
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "no_manifest"
    assert detail["checked"] is False


def test_slow_check_cannot_overwrite_new_manifest(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"

    # New tap revision withdraws, then a check starts.
    accepted = service.accept_upload(db_path, "fix.csv", make_csv([
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in", revision=1),
    ]))
    stale_job = conn_job(db_path, "西直门")

    replaced = {"id": None}

    def hook(stage, conn, info):
        if stage == "after_compute" and info["job_id"] == stale_job:
            # Competing manifest replacement lands while the check is running.
            replaced["id"] = reconcile.upload_manifest(
                db_path, "西直门", "2024-05-01", "new.csv", manifest_csv([["e1", 1], ["e3", 0]])
            ).manifest_id

    reconcile.run_pending_recon_jobs(db_path, hook=hook)
    reconcile.run_pending_recon_jobs(db_path)

    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["manifest"]["id"] == replaced["id"]
    assert detail["status"] == "complete"
    assert detail["checked"] is True
    assert accepted.recon_pairs


def conn_job(db_path, station):
    conn = db.connect(db_path)
    try:
        return conn.execute(
            "SELECT id FROM recon_jobs WHERE station = ? AND status = 'pending' ORDER BY id LIMIT 1",
            (station,),
        ).fetchone()["id"]
    finally:
        conn.close()


def test_concurrent_manifest_replacement_and_correction_converge(db_path):
    """A slow check must never publish over a newer manifest or tap revision."""
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])

    # Two revisions accepted before the check runs.
    accepted = service.accept_upload(db_path, "fix.csv", make_csv([
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in", revision=3),
    ]))
    assert accepted.recon_pairs
    job_id = conn_job(db_path, "西直门")

    raced = {}

    def hook(stage, conn, info):
        if stage == "after_compute" and info["job_id"] == job_id:
            # Newer manifest and newer revision both land mid-check.
            raced["manifest"] = reconcile.upload_manifest(
                db_path, "西直门", "2024-05-01", "new.csv", manifest_csv([["e1", 4], ["e3", 0]])
            ).manifest_id
            service.accept_upload(db_path, "again.csv", make_csv([
                tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in", revision=4),
            ]))

    reconcile.run_pending_recon_jobs(db_path, hook=hook)
    reconcile.run_pending_recon_jobs(db_path)

    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["manifest"]["id"] == raced["manifest"]
    # Both the manifest and the accepted revision are 4 -> complete, and it is
    # a conclusion from the *new* data, not the abandoned snapshot.
    assert detail["status"] == "complete"
    assert detail["checked"] is True


def test_recover_requeues_pending_state_without_job(db_path):
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])
    conn = db.connect(db_path)
    try:
        conn.execute("DELETE FROM recon_jobs")
        conn.execute(
            "UPDATE recon_state SET status='pending', checked_at=NULL WHERE station='西直门'"
        )
        conn.execute("DELETE FROM recon_diffs")
    finally:
        conn.close()
    reconcile.recover_recon(db_path)
    reconcile.run_pending_recon_jobs(db_path)
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"


def test_manifest_duplicate_events_keep_highest_revision(db_path):
    upload(db_path, SEED)
    result = upload_manifest(db_path, "西直门", "2024-05-01", [
        ["e1", 2], ["e1", 0], ["e3", 0], ["e3", 1],
    ])
    assert result.rows == 4
    assert result.duplicate_rows == 2
    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "mismatch"
    kinds = {(d["kind"], d["event_no"], d["manifest_rev"]) for d in detail["diffs"]}
    assert kinds == {("revision", "e1", 2), ("revision", "e3", 1)}


def test_stale_job_failure_does_not_clobber_newer_conclusion(db_path):
    """A retryable old job must not erase a conclusion published by a newer one.

    Two *distinct* queued jobs are needed: the older one fails its epoch check
    after the newer manifest has already been published.
    """
    upload(db_path, SEED)
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 0], ["e3", 0]])

    # A tap revision withdraws the conclusion and queues job A.
    service.accept_upload(db_path, "fix.csv", make_csv([
        tap("e1", "c1", "2024-05-01T08:00:00Z", "西直门", "in", revision=1),
    ]))
    conn = db.connect(db_path)
    try:
        job_a = conn.execute(
            "SELECT id FROM recon_jobs WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
    finally:
        conn.close()

    # A newer manifest (matching revision 1) publishes a "complete" conclusion.
    upload_manifest(db_path, "西直门", "2024-05-01", [["e1", 1], ["e3", 0]])
    assert recon(db_path, "西直门", "2024-05-01")["status"] == "complete"

    # Now the older job A fails its epoch check.  Its retry bookkeeping must not
    # erase the conclusion or the diffs that the newer job published.
    conn = db.connect(db_path)
    try:
        reconcile._fail_or_retry(
            conn, job_a, "西直门", "2024-05-01", attempts=1,
            error="重算期间收到新数据，放弃本次发布并重试", retry=True,
        )
        job = conn.execute("SELECT status FROM recon_jobs WHERE id = ?", (job_a,)).fetchone()
        assert job["status"] == "pending"
    finally:
        conn.close()

    detail = recon(db_path, "西直门", "2024-05-01")
    assert detail["status"] == "complete"
    assert detail["checked"] is True
    assert detail["diffs"] == []
