"""Station-day manifest reconciliation ("清单核收").

Operations hands over a *complete* list of the event numbers (and their
revision numbers) that a station uploaded for one UTC service day.  We compare
that list with the taps we have accepted for the same station and day:

* **缺失 (missing)** – listed by the manifest, no accepted tap at this
  station-day (it never arrived, or a correction moved it elsewhere);
* **修订不符 (revision mismatch)** – accepted, but the winning revision differs;
* **清单外 (unexpected)** – accepted at this station-day, absent from the list.

Only when all three lists are empty is the station-day *核齐* (complete).  A
station-day without an active manifest can never be complete.

Publication protocol (same fencing idea as the journey recompute)
------------------------------------------------------------------
1. Every change that can invalidate a conclusion runs in the writer
   transaction that caused it: a manifest upload, or accepting tap
   revisions.  It bumps that station-day's ``recon_epochs`` counter, sets
   ``recon_state`` to ``pending``, **deletes the stored diff** and ensures a
   pending job.  The old conclusion is gone before the change is acknowledged.
2. ``run_pending_recon_jobs`` computes from a read snapshot, captures the
   epoch and active manifest id, then re-takes the write lock.  It publishes
   only if both are still current; a concurrent manifest replacement or tap
   correction therefore cannot be overwritten by an older check.
3. A failed check leaves the state non-conclusive (``failed``) with the error
   recorded; it never falls back to the previous conclusion.
4. ``recover_recon`` runs at startup: interrupted jobs are re-queued, any
   conclusion whose epoch/manifest is not current is demoted to ``pending``
   with its diff deleted, and pending states without a job get one.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import db

MAX_MANIFEST_ROWS = 200_000
MAX_JOB_ATTEMPTS = 5

MANIFEST_HEADER_ALIASES = {
    "event_no": {"事件号", "event_no", "eventno", "event id", "event_id"},
    "revision": {"修订号", "revision", "rev", "revision_no"},
}
MANIFEST_REQUIRED = ("event_no", "revision")

KIND_MISSING = "missing"
KIND_REVISION = "revision"
KIND_UNEXPECTED = "unexpected"

STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"
STATUS_MISMATCH = "mismatch"
STATUS_FAILED = "failed"
STATUS_NO_MANIFEST = "no_manifest"

CONCLUSION_STATUSES = (STATUS_COMPLETE, STATUS_MISMATCH)

STATUS_LABELS = {
    STATUS_PENDING: "待核",
    STATUS_COMPLETE: "核齐",
    STATUS_MISMATCH: "不一致",
    STATUS_FAILED: "核收失败",
    STATUS_NO_MANIFEST: "无清单",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


class ManifestError(ValueError):
    """A user-correctable manifest problem (reported with HTTP 400)."""


class StaleReconError(RuntimeError):
    """The snapshot used for a check was overtaken by newer data."""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _validate_pair(station: str, day: str) -> tuple[str, str]:
    station = (station or "").strip()
    if not station:
        raise ManifestError("站点不能为空")
    if len(station) > 200:
        raise ManifestError("站点名称过长（最多 200 字符）")
    if any(ord(ch) < 32 for ch in station):
        raise ManifestError("站点名称包含控制字符")
    try:
        parsed = date.fromisoformat((day or "").strip())
    except ValueError:
        raise ManifestError("日期格式应为 YYYY-MM-DD")
    return station, parsed.isoformat()


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManifestEntry:
    event_no: str
    revision: int
    line: int


def _norm_header(name: str) -> str:
    return name.strip().lstrip("﻿").strip().lower().replace(" ", "")


def _map_header(raw: list[str]) -> dict[int, str]:
    normalised = {_norm_header(h): i for i, h in enumerate(raw) if h is not None}
    mapping: dict[int, str] = {}
    for field, aliases in MANIFEST_HEADER_ALIASES.items():
        for alias in aliases:
            idx = normalised.get(_norm_header(alias))
            if idx is not None:
                mapping[idx] = field
                break
    present = set(mapping.values())
    missing = [f for f in MANIFEST_REQUIRED if f not in present]
    if missing:
        raise ManifestError(
            "清单表头缺少必需列：" + ", ".join(missing) + "（需要 事件号/修订号）"
        )
    return mapping


def parse_manifest_csv(payload: bytes, filename: str = "manifest.csv") -> list[ManifestEntry]:
    """Parse 事件号/修订号; duplicate event numbers keep the highest revision."""
    if not payload.strip():
        raise ManifestError("文件为空")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = payload.decode("gb18030")
        except UnicodeDecodeError:
            raise ManifestError("无法解码文件，请使用 UTF-8 或 GB18030 编码")

    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration:
        raise ManifestError("文件为空")
    mapping = _map_header(raw_header)

    best: dict[str, ManifestEntry] = {}
    rows = 0
    for offset, row in enumerate(reader, start=2):
        if not row or all(not (c or "").strip() for c in row):
            continue
        rows += 1
        if rows > MAX_MANIFEST_ROWS:
            raise ManifestError(f"清单超过 {MAX_MANIFEST_ROWS} 行上限，请拆分后上传")
        fields = {name: (row[i].strip() if i < len(row) else "") for i, name in mapping.items()}
        event_no = fields["event_no"]
        if not event_no:
            raise ManifestError(f"第 {offset} 行：事件号为空")
        try:
            revision = int(fields["revision"])
        except (TypeError, ValueError):
            raise ManifestError(f"第 {offset} 行：修订号 {fields['revision']!r} 不是整数")
        if revision < 0:
            raise ManifestError(f"第 {offset} 行：修订号不能为负")
        entry = ManifestEntry(event_no=event_no, revision=revision, line=offset)
        prior = best.get(event_no)
        if prior is None or revision > prior.revision:
            best[event_no] = entry

    if not best:
        raise ManifestError("文件没有有效数据行")
    return [best[key] for key in sorted(best)]


def count_manifest_rows(payload: bytes) -> int:
    """Raw data-row count (before duplicate collapsing), for the audit trail."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("gb18030")
    reader = csv.reader(io.StringIO(text))
    next(reader, None)
    return sum(1 for row in reader if row and any((cell or "").strip() for cell in row))


# ---------------------------------------------------------------------------
# epochs, withdrawal and job queue
# ---------------------------------------------------------------------------

def _bump_epoch(conn: sqlite3.Connection, station: str, day: str) -> int:
    conn.execute(
        "INSERT INTO recon_epochs (station, service_date, epoch) VALUES (?, ?, 1) "
        "ON CONFLICT(station, service_date) DO UPDATE SET epoch = epoch + 1",
        (station, day),
    )
    row = conn.execute(
        "SELECT epoch FROM recon_epochs WHERE station = ? AND service_date = ?",
        (station, day),
    ).fetchone()
    return int(row["epoch"])


def _epoch(conn: sqlite3.Connection, station: str, day: str) -> int:
    row = conn.execute(
        "SELECT epoch FROM recon_epochs WHERE station = ? AND service_date = ?",
        (station, day),
    ).fetchone()
    return int(row["epoch"]) if row else 0


def _active_manifest(conn: sqlite3.Connection, station: str, day: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM manifests WHERE station = ? AND service_date = ? AND is_active = 1",
        (station, day),
    ).fetchone()


def _ensure_pending_job(
    conn: sqlite3.Connection, station: str, day: str, manifest_id: int | None
) -> int:
    row = conn.execute(
        "SELECT id FROM recon_jobs WHERE station = ? AND service_date = ? AND status = 'pending' "
        "ORDER BY id LIMIT 1",
        (station, day),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE recon_jobs SET trigger_manifest_id = ?, created_at = ? WHERE id = ?",
            (manifest_id, utcnow(), row["id"]),
        )
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO recon_jobs (created_at, status, station, service_date, trigger_manifest_id) "
        "VALUES (?, 'pending', ?, ?, ?)",
        (utcnow(), station, day, manifest_id),
    )
    return int(cursor.lastrowid)


def withdraw_pairs(
    conn: sqlite3.Connection,
    pairs: Iterable[tuple[str, str]],
    reason: str,
    enqueue: bool = True,
) -> list[tuple[str, str]]:
    """Invalidate conclusions for ``(station, day)`` pairs inside a transaction.

    The epoch is bumped, the state becomes ``pending``, and the previously
    stored diff is deleted in the *same* transaction, so nothing can serve the
    old conclusion once the caller commits.
    """
    now = utcnow()
    touched: list[tuple[str, str]] = []
    for station, day in sorted(set(pairs)):
        manifest = _active_manifest(conn, station, day)
        existing = conn.execute(
            "SELECT 1 FROM recon_state WHERE station = ? AND service_date = ?",
            (station, day),
        ).fetchone()
        if manifest is None and existing is None:
            continue
        epoch = _bump_epoch(conn, station, day)
        conn.execute(
            "INSERT INTO recon_state "
            "(station, service_date, status, manifest_id, data_epoch, checked_at, last_error, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?, NULL, ?, ?) "
            "ON CONFLICT(station, service_date) DO UPDATE SET "
            "  status = 'pending', manifest_id = excluded.manifest_id, "
            "  data_epoch = excluded.data_epoch, checked_at = NULL, "
            "  last_error = excluded.last_error, updated_at = excluded.updated_at",
            (station, day, manifest["id"] if manifest else None, epoch, reason, now),
        )
        conn.execute(
            "DELETE FROM recon_diffs WHERE station = ? AND service_date = ?",
            (station, day),
        )
        if enqueue and manifest is not None:
            _ensure_pending_job(conn, station, day, int(manifest["id"]))
        touched.append((station, day))
    return touched


# ---------------------------------------------------------------------------
# accepting / replacing manifests
# ---------------------------------------------------------------------------

@dataclass
class ManifestResult:
    manifest_id: int
    station: str
    service_date: str
    rows: int
    duplicate_rows: int
    replaced_manifest_id: int | None
    job_id: int | None


def upload_manifest(
    db_path: str | Path, station: str, day: str, filename: str, payload: bytes
) -> ManifestResult:
    station, day = _validate_pair(station, day)
    entries = parse_manifest_csv(payload, filename)   # validated before any write
    raw_rows = count_manifest_rows(payload)
    conn = db.connect(db_path)
    try:
        with db.transaction(conn):
            now = utcnow()
            prior = _active_manifest(conn, station, day)
            replaced_id: int | None = None
            if prior is not None:
                conn.execute(
                    "UPDATE manifests SET is_active = 0, superseded_at = ? WHERE id = ?",
                    (now, prior["id"]),
                )
                replaced_id = int(prior["id"])
            cursor = conn.execute(
                "INSERT INTO manifests (station, service_date, filename, uploaded_at, row_count, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (station, day, filename, now, raw_rows),
            )
            manifest_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO manifest_entries (manifest_id, event_no, revision) VALUES (?, ?, ?)",
                [(manifest_id, e.event_no, e.revision) for e in entries],
            )
            # Replacing a list is itself an invalidation: the old conclusion is
            # withdrawn immediately, even if the new list ends up identical.
            withdraw_pairs(
                conn,
                [(station, day)],
                f"清单 #{manifest_id} 受理，旧核收结论撤回",
            )
            job = conn.execute(
                "SELECT id FROM recon_jobs WHERE station = ? AND service_date = ? "
                "AND status = 'pending' ORDER BY id LIMIT 1",
                (station, day),
            ).fetchone()
            return ManifestResult(
                manifest_id=manifest_id,
                station=station,
                service_date=day,
                rows=raw_rows,
                duplicate_rows=raw_rows - len(entries),
                replaced_manifest_id=replaced_id,
                job_id=int(job["id"]) if job else None,
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

def _accepted_taps(
    conn: sqlite3.Connection, station: str, day: str
) -> dict[str, sqlite3.Row]:
    """Winning revision per event for one station-day."""
    next_day = _next_day(day)
    rows = conn.execute(
        "SELECT e.event_no, e.revision, e.station, e.tap_time "
        "FROM events e JOIN (SELECT event_no, MAX(revision) AS rev FROM events GROUP BY event_no) m "
        "ON m.event_no = e.event_no AND m.rev = e.revision "
        "WHERE e.station = ? AND e.tap_time >= ? AND e.tap_time < ?",
        (station, day + "T00:00:00Z", next_day + "T00:00:00Z"),
    ).fetchall()
    return {row["event_no"]: row for row in rows}


def compute_diffs(
    manifest: dict[str, int], accepted: dict[str, sqlite3.Row]
) -> list[dict]:
    diffs: list[dict] = []
    for event_no, revision in manifest.items():
        row = accepted.get(event_no)
        if row is None:
            diffs.append({
                "kind": KIND_MISSING,
                "event_no": event_no,
                "accepted_rev": None,
                "manifest_rev": revision,
                "accepted_station": None,
                "detail": "清单列出但该站日没有已受理的刷卡",
            })
        elif int(row["revision"]) != revision:
            diffs.append({
                "kind": KIND_REVISION,
                "event_no": event_no,
                "accepted_rev": int(row["revision"]),
                "manifest_rev": revision,
                "accepted_station": row["station"],
                "detail": f"已受理修订号 {row['revision']}，清单为 {revision}",
            })
    for event_no, row in accepted.items():
        if event_no not in manifest:
            diffs.append({
                "kind": KIND_UNEXPECTED,
                "event_no": event_no,
                "accepted_rev": int(row["revision"]),
                "manifest_rev": None,
                "accepted_station": row["station"],
                "detail": "已受理但不在清单内",
            })
    order = {KIND_MISSING: 0, KIND_REVISION: 1, KIND_UNEXPECTED: 2}
    diffs.sort(key=lambda d: (order[d["kind"]], d["event_no"]))
    return diffs


# ---------------------------------------------------------------------------
# check worker
# ---------------------------------------------------------------------------

def run_pending_recon_jobs(
    db_path: str | Path,
    limit: int = 500,
    hook: Callable[[str, sqlite3.Connection, dict], None] | None = None,
) -> int:
    conn = db.connect(db_path)
    attempted = 0
    try:
        while attempted < limit:
            row = conn.execute(
                "SELECT id FROM recon_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                break
            attempted += 1
            _run_recon_job(conn, int(row["id"]), hook)
    finally:
        conn.close()
    return attempted


def _run_recon_job(
    conn: sqlite3.Connection,
    job_id: int,
    hook: Callable[[str, sqlite3.Connection, dict], None] | None,
) -> None:
    with db.transaction(conn):
        claimed = conn.execute(
            "UPDATE recon_jobs SET status = 'running', started_at = ?, attempts = attempts + 1 "
            "WHERE id = ? AND status = 'pending'",
            (utcnow(), job_id),
        ).rowcount
    if claimed != 1:
        return
    job = conn.execute("SELECT * FROM recon_jobs WHERE id = ?", (job_id,)).fetchone()
    station, day = job["station"], job["service_date"]
    attempts = int(job["attempts"])

    try:
        with db.snapshot(conn):
            manifest = _active_manifest(conn, station, day)
            if manifest is None:
                # Withdrawn/replaced away; a later upload will enqueue its own
                # job, so retrying this one can never succeed.
                raise LookupError("该站日没有现行清单，无法核收")
            manifest_id = int(manifest["id"])
            manifest_entries = {
                row["event_no"]: int(row["revision"])
                for row in conn.execute(
                    "SELECT event_no, revision FROM manifest_entries WHERE manifest_id = ?",
                    (manifest_id,),
                )
            }
            accepted = _accepted_taps(conn, station, day)
            captured_epoch = _epoch(conn, station, day)
            diffs = compute_diffs(manifest_entries, accepted)

        if hook:
            hook("after_compute", conn, {
                "job_id": job_id, "station": station, "day": day,
                "manifest_id": manifest_id, "epoch": captured_epoch,
            })

        with db.transaction(conn):
            current = _active_manifest(conn, station, day)
            if (
                current is None
                or int(current["id"]) != manifest_id
                or _epoch(conn, station, day) != captured_epoch
            ):
                raise StaleReconError("核收期间收到新清单或新刷卡，放弃本次结论并重试")
            _publish_recon(conn, job, manifest_id, captured_epoch, diffs)

        if hook:
            hook("after_publish", conn, {
                "job_id": job_id, "station": station, "day": day, "diffs": len(diffs),
            })
    except StaleReconError as exc:
        _fail_or_retry(conn, job_id, station, day, attempts, str(exc), retry=True)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI for triage
        _fail_or_retry(
            conn, job_id, station, day, attempts, f"{type(exc).__name__}: {exc}", retry=False
        )


def _publish_recon(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    manifest_id: int,
    epoch: int,
    diffs: list[dict],
) -> None:
    station, day = job["station"], job["service_date"]
    now = utcnow()
    status = STATUS_COMPLETE if not diffs else STATUS_MISMATCH
    conn.execute("DELETE FROM recon_diffs WHERE station = ? AND service_date = ?", (station, day))
    for diff in diffs:
        conn.execute(
            "INSERT INTO recon_diffs "
            "(station, service_date, kind, event_no, accepted_rev, manifest_rev, accepted_station, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                station, day, diff["kind"], diff["event_no"], diff["accepted_rev"],
                diff["manifest_rev"], diff["accepted_station"], diff["detail"],
            ),
        )
    conn.execute(
        "INSERT INTO recon_state "
        "(station, service_date, status, manifest_id, data_epoch, checked_at, last_error, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?) "
        "ON CONFLICT(station, service_date) DO UPDATE SET "
        "  status = excluded.status, manifest_id = excluded.manifest_id, "
        "  data_epoch = excluded.data_epoch, checked_at = excluded.checked_at, "
        "  last_error = NULL, updated_at = excluded.updated_at",
        (station, day, status, manifest_id, epoch, now, now),
    )
    conn.execute(
        "UPDATE recon_jobs SET status = 'done', finished_at = ?, last_error = NULL WHERE id = ?",
        (now, job["id"]),
    )


def _fail_or_retry(
    conn: sqlite3.Connection,
    job_id: int,
    station: str,
    day: str,
    attempts: int,
    error: str,
    retry: bool,
) -> None:
    now = utcnow()
    with db.transaction(conn):
        # Only touch the state row if it still belongs to *this* attempt.  A
        # newer successful job may already have published a conclusion for the
        # same station-day; its state and diffs must survive our failure.
        claimable = conn.execute(
            "SELECT 1 FROM recon_state WHERE station = ? AND service_date = ? "
            "AND status IN ('pending', 'failed')",
            (station, day),
        ).fetchone()
        if retry and attempts < MAX_JOB_ATTEMPTS:
            conn.execute(
                "UPDATE recon_jobs SET status = 'pending', started_at = NULL, last_error = ? "
                "WHERE id = ?",
                (error, job_id),
            )
            if claimable is not None:
                # A retry must not leave a half-trusted state: keep it pending
                # and delete any diff while the re-check is outstanding.
                conn.execute(
                    "UPDATE recon_state SET status = 'pending', checked_at = NULL, "
                    "last_error = ?, updated_at = ? WHERE station = ? AND service_date = ?",
                    (error, now, station, day),
                )
                conn.execute(
                    "DELETE FROM recon_diffs WHERE station = ? AND service_date = ?",
                    (station, day),
                )
            return
        conn.execute(
            "UPDATE recon_jobs SET status = 'failed', finished_at = ?, last_error = ? WHERE id = ?",
            (now, error, job_id),
        )
        if claimable is not None:
            conn.execute(
                "UPDATE recon_state SET status = 'failed', checked_at = NULL, "
                "last_error = ?, updated_at = ? WHERE station = ? AND service_date = ?",
                (error, now, station, day),
            )
            conn.execute(
                "DELETE FROM recon_diffs WHERE station = ? AND service_date = ?",
                (station, day),
            )


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------

def recover_recon(db_path: str | Path) -> None:
    """Make persisted reconciliation state safe to serve after a restart."""
    conn = db.connect(db_path)
    try:
        with db.transaction(conn):
            now = utcnow()
            for row in conn.execute(
                "SELECT id FROM recon_jobs WHERE status = 'running'"
            ).fetchall():
                conn.execute(
                    "UPDATE recon_jobs SET status = 'pending', started_at = NULL, "
                    "last_error = '服务重启，核收任务重新排队' WHERE id = ?",
                    (row["id"],),
                )

            states = conn.execute("SELECT * FROM recon_state").fetchall()
            for state in states:
                station, day = state["station"], state["service_date"]
                manifest = _active_manifest(conn, station, day)
                trusted = (
                    manifest is not None
                    and state["status"] in CONCLUSION_STATUSES
                    and int(state["manifest_id"] or -1) == int(manifest["id"])
                    and int(state["data_epoch"]) == _epoch(conn, station, day)
                )
                if state["status"] in CONCLUSION_STATUSES and not trusted:
                    conn.execute(
                        "UPDATE recon_state SET status = 'pending', checked_at = NULL, "
                        "last_error = ?, updated_at = ? "
                        "WHERE station = ? AND service_date = ?",
                        (
                            "数据或清单已更新，旧核收结论不可用"
                            if manifest is not None
                            else "该站日没有现行清单，不能判为核齐",
                            now, station, day,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM recon_diffs WHERE station = ? AND service_date = ?",
                        (station, day),
                    )
                elif state["status"] in (STATUS_PENDING, STATUS_FAILED):
                    # Diffs are never evidence unless a conclusion was published.
                    conn.execute(
                        "DELETE FROM recon_diffs WHERE station = ? AND service_date = ?",
                        (station, day),
                    )

            # Every active manifest must have a state row; every pending state
            # must have a runnable job.
            for manifest in conn.execute(
                "SELECT * FROM manifests WHERE is_active = 1"
            ).fetchall():
                station, day = manifest["station"], manifest["service_date"]
                state = conn.execute(
                    "SELECT 1 FROM recon_state WHERE station = ? AND service_date = ?",
                    (station, day),
                ).fetchone()
                if state is None:
                    epoch = _epoch(conn, station, day)
                    conn.execute(
                        "INSERT INTO recon_state "
                        "(station, service_date, status, manifest_id, data_epoch, checked_at, last_error, updated_at) "
                        "VALUES (?, ?, 'pending', ?, ?, NULL, '服务启动补建核收任务', ?) "
                        "ON CONFLICT(station, service_date) DO UPDATE SET "
                        "  status = 'pending', manifest_id = excluded.manifest_id, "
                        "  data_epoch = excluded.data_epoch, checked_at = NULL, "
                        "  last_error = excluded.last_error, updated_at = excluded.updated_at",
                        (station, day, manifest["id"], epoch, now),
                    )
                _ensure_pending_job_if_missing(conn, station, day, int(manifest["id"]))

            for state in conn.execute(
                "SELECT station, service_date FROM recon_state WHERE status = 'pending'"
            ).fetchall():
                manifest = _active_manifest(conn, state["station"], state["service_date"])
                if manifest is not None:
                    _ensure_pending_job_if_missing(
                        conn, state["station"], state["service_date"], int(manifest["id"])
                    )
    finally:
        conn.close()


def _ensure_pending_job_if_missing(
    conn: sqlite3.Connection, station: str, day: str, manifest_id: int
) -> None:
    existing = conn.execute(
        "SELECT 1 FROM recon_jobs WHERE station = ? AND service_date = ? "
        "AND status IN ('pending', 'running') LIMIT 1",
        (station, day),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO recon_jobs (created_at, status, station, service_date, trigger_manifest_id) "
            "VALUES (?, 'pending', ?, ?, ?)",
            (utcnow(), station, day, manifest_id),
        )


# ---------------------------------------------------------------------------
# manual retry
# ---------------------------------------------------------------------------

def request_recon_retry(db_path: str | Path, station: str, day: str) -> int | None:
    station, day = _validate_pair(station, day)
    conn = db.connect(db_path)
    try:
        with db.transaction(conn):
            manifest = _active_manifest(conn, station, day)
            if manifest is None:
                raise ManifestError("该站日没有清单，请先上传完整清单")
            withdraw_pairs(conn, [(station, day)], "手动请求重新核收")
            row = conn.execute(
                "SELECT id FROM recon_jobs WHERE station = ? AND service_date = ? "
                "AND status = 'pending' ORDER BY id LIMIT 1",
                (station, day),
            ).fetchone()
            return int(row["id"]) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# read models
# ---------------------------------------------------------------------------

def _status_from_row(
    state: sqlite3.Row | None,
    manifest: sqlite3.Row | None,
    epoch: int,
) -> str:
    if manifest is None:
        return STATUS_NO_MANIFEST
    if state is None:
        return STATUS_PENDING
    if int(state["manifest_id"] or -1) != int(manifest["id"]):
        return STATUS_PENDING
    if int(state["data_epoch"]) != epoch:
        return STATUS_PENDING
    if state["status"] in CONCLUSION_STATUSES:
        return state["status"]
    if state["status"] == STATUS_FAILED:
        return STATUS_FAILED
    return STATUS_PENDING


def _diff_counts(
    conn: sqlite3.Connection, service_date: str | None = None
) -> dict[tuple[str, str], dict[str, int]]:
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {KIND_MISSING: 0, KIND_REVISION: 0, KIND_UNEXPECTED: 0}
    )
    sql = (
        "SELECT station, service_date, kind, COUNT(*) AS n FROM recon_diffs "
    )
    params: tuple = ()
    if service_date:
        sql += "WHERE service_date = ? "
        params = (service_date,)
    sql += "GROUP BY station, service_date, kind"
    for row in conn.execute(sql, params):
        counts[(row["station"], row["service_date"])][row["kind"]] = int(row["n"])
    return counts


def recon_list(conn: sqlite3.Connection, service_date: str | None = None) -> list[dict]:
    """All station-days that hold a manifest or accepted taps.

    A station-day without an active manifest is reported as ``no_manifest``
    and can never be shown as checked.
    """
    manifests: dict[tuple[str, str], sqlite3.Row] = {}
    sql = "SELECT * FROM manifests WHERE is_active = 1"
    params: tuple = ()
    if service_date:
        sql += " AND service_date = ?"
        params = (service_date,)
    for row in conn.execute(sql, params):
        manifests[(row["station"], row["service_date"])] = row

    states: dict[tuple[str, str], sqlite3.Row] = {}
    state_sql = "SELECT * FROM recon_state"
    state_params: tuple = ()
    if service_date:
        state_sql += " WHERE service_date = ?"
        state_params = (service_date,)
    for row in conn.execute(state_sql, state_params):
        states[(row["station"], row["service_date"])] = row

    # Station-days with accepted (winning-revision) taps but no manifest must
    # be visible precisely so they are not silently treated as checked.
    tap_sql = (
        "SELECT e.station AS station, substr(e.tap_time, 1, 10) AS service_date, "
        "       COUNT(*) AS taps "
        "FROM events e JOIN (SELECT event_no, MAX(revision) AS rev FROM events GROUP BY event_no) m "
        "ON m.event_no = e.event_no AND m.rev = e.revision "
    )
    tap_params: tuple = ()
    if service_date:
        tap_sql += "WHERE substr(e.tap_time, 1, 10) = ? "
        tap_params = (service_date,)
    tap_sql += "GROUP BY e.station, service_date"
    tap_counts = {
        (row["station"], row["service_date"]): int(row["taps"])
        for row in conn.execute(tap_sql, tap_params)
    }

    diff_counts = _diff_counts(conn, service_date)
    keys = sorted(set(manifests) | set(states) | set(tap_counts))
    out: list[dict] = []
    for station, day in keys:
        manifest = manifests.get((station, day))
        state = states.get((station, day))
        epoch = _epoch(conn, station, day)
        status = _status_from_row(state, manifest, epoch)
        trusted = status in CONCLUSION_STATUSES
        counts = diff_counts.get((station, day), {})
        out.append({
            "station": station,
            "date": day,
            "status": status,
            "checked": trusted,
            "manifest_id": int(manifest["id"]) if manifest else None,
            "manifest_filename": manifest["filename"] if manifest else None,
            "manifest_rows": int(manifest["row_count"]) if manifest else None,
            "manifest_uploaded_at": manifest["uploaded_at"] if manifest else None,
            "accepted_taps": tap_counts.get((station, day), 0),
            "missing": counts.get(KIND_MISSING, 0) if trusted else None,
            "revision_mismatch": counts.get(KIND_REVISION, 0) if trusted else None,
            "unexpected": counts.get(KIND_UNEXPECTED, 0) if trusted else None,
            "checked_at": state["checked_at"] if trusted else None,
            "last_error": state["last_error"] if state else None,
        })
    return out


def recon_detail(conn: sqlite3.Connection, station: str, day: str) -> dict:
    station, day = _validate_pair(station, day)
    manifest = _active_manifest(conn, station, day)
    state = conn.execute(
        "SELECT * FROM recon_state WHERE station = ? AND service_date = ?", (station, day)
    ).fetchone()
    epoch = _epoch(conn, station, day)
    status = _status_from_row(state, manifest, epoch)
    trusted = status in CONCLUSION_STATUSES
    diffs = []
    if trusted:
        diffs = [dict(row) for row in conn.execute(
            "SELECT kind, event_no, accepted_rev, manifest_rev, accepted_station, detail "
            "FROM recon_diffs WHERE station = ? AND service_date = ? "
            "ORDER BY CASE kind WHEN 'missing' THEN 0 WHEN 'revision' THEN 1 ELSE 2 END, event_no",
            (station, day),
        )]
    entries = 0
    if manifest is not None:
        entries = int(conn.execute(
            "SELECT COUNT(*) AS n FROM manifest_entries WHERE manifest_id = ?",
            (manifest["id"],),
        ).fetchone()["n"])
    return {
        "station": station,
        "date": day,
        "status": status,
        "checked": trusted,
        "manifest": dict(manifest) if manifest else None,
        "manifest_entries": entries,
        "accepted_taps": len(_accepted_taps(conn, station, day)),
        "checked_at": state["checked_at"] if trusted else None,
        "last_error": state["last_error"] if state else None,
        "diffs": diffs,
        "counts": {
            "missing": sum(1 for d in diffs if d["kind"] == KIND_MISSING),
            "revision": sum(1 for d in diffs if d["kind"] == KIND_REVISION),
            "unexpected": sum(1 for d in diffs if d["kind"] == KIND_UNEXPECTED),
        },
    }


def recon_summary(conn: sqlite3.Connection, service_date: str | None = None) -> dict:
    rows = recon_list(conn, service_date)
    summary = {
        "total": len(rows),
        "complete": 0,
        "mismatch": 0,
        "pending": 0,
        "failed": 0,
        "no_manifest": 0,
    }
    for row in rows:
        status = row["status"]
        if status == STATUS_NO_MANIFEST:
            summary["no_manifest"] += 1
        elif status in summary:
            summary[status] += 1
        else:
            summary["pending"] += 1
    return summary


def manifest_history(conn: sqlite3.Connection, station: str, day: str, limit: int = 10) -> list[dict]:
    station, day = _validate_pair(station, day)
    return [dict(row) for row in conn.execute(
        "SELECT id, filename, uploaded_at, row_count, is_active, superseded_at FROM manifests "
        "WHERE station = ? AND service_date = ? ORDER BY id DESC LIMIT ?",
        (station, day, limit),
    ).fetchall()]


def open_recon_jobs(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["status"]: int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM recon_jobs GROUP BY status"
        ).fetchall()
    }
