"""Private persistence bridge between Render's local SQLite cache and Supabase.

The application keeps SQLite as the fast local working database, while this
module mirrors rows to Supabase and can hydrate recent history after a Render
restart. Tables are not exposed publicly; writes go through the private
tracker-sync Edge Function.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("supabase_sync")

SYNC_URL = os.getenv("SUPABASE_SYNC_URL", "").rstrip("/")
SYNC_SECRET = os.getenv("SUPABASE_TRACKER_SECRET", "")
HYDRATE_DAYS = int(os.getenv("SUPABASE_HYDRATE_DAYS", "120"))

TABLE_TIME_COLUMNS = {
    "auction_snapshot": "observed_at",
    "sale_observation": "observed_at",
    "valuation_report": "computed_at",
    "price_deviation_alert": "created_at",
    "community_signal": "collected_at",
    "patch_signal": "published_at",
    "manual_prices": "updated_at",
    "meta": "updated_at",
}

REMOTE_TABLES = tuple(TABLE_TIME_COLUMNS)


def enabled() -> bool:
    return bool(SYNC_URL and SYNC_SECRET)


def _post(payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    if not enabled():
        return {"ok": False, "disabled": True}
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        SYNC_URL,
        data=data,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-tracker-secret": SYNC_SECRET,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body or "{}")


class SupabaseMirror:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=20000)
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        if not enabled() or self._started:
            return
        with self._lock:
            if self._started:
                return
            self._started = True
            threading.Thread(target=self._worker, daemon=True, name="supabase-mirror").start()
            log.info("Supabase persistence mirror enabled")

    def enqueue(self, table: str, row: dict[str, Any]) -> None:
        if not enabled() or table not in REMOTE_TABLES:
            return
        self.start()
        clean = {k: v for k, v in row.items() if k != "id"}
        try:
            self._queue.put_nowait((table, clean))
        except queue.Full:
            log.error("Supabase mirror queue full; dropping %s row", table)

    def _worker(self) -> None:
        pending: dict[str, list[dict[str, Any]]] = {}
        last_flush = time.monotonic()
        while True:
            wait = max(0.2, 2.0 - (time.monotonic() - last_flush))
            try:
                table, row = self._queue.get(timeout=wait)
                pending.setdefault(table, []).append(row)
            except queue.Empty:
                pass

            total = sum(len(v) for v in pending.values())
            if total < 100 and time.monotonic() - last_flush < 2.0:
                continue

            for table, rows in list(pending.items()):
                if not rows:
                    continue
                try:
                    result = _post({"action": "write", "table": table, "rows": rows})
                    if not result.get("ok"):
                        raise RuntimeError(result.get("error") or "remote write failed")
                    pending[table] = []
                except Exception as exc:
                    log.warning("Supabase mirror write failed for %s (%d rows): %s", table, len(rows), exc)
            last_flush = time.monotonic()

    def read_recent(self, table: str, since: float | None = None, page_size: int = 1000) -> list[dict[str, Any]]:
        if not enabled() or table not in REMOTE_TABLES:
            return []
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload: dict[str, Any] = {
                "action": "read",
                "table": table,
                "limit": page_size,
                "offset": offset,
            }
            if since is not None:
                payload["since"] = since
            result = _post(payload, timeout=30.0)
            page = result.get("rows") or []
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return rows


mirror = SupabaseMirror()


def hydrate_sqlite(conn) -> dict[str, int]:
    """Load recent persistent rows into an already-initialized SQLite DB."""
    if not enabled():
        return {}

    cutoff = time.time() - max(1, HYDRATE_DAYS) * 86400
    counts: dict[str, int] = {}
    for table in REMOTE_TABLES:
        since = None if table in {"manual_prices", "meta"} else cutoff
        try:
            rows = mirror.read_recent(table, since=since)
        except Exception as exc:
            log.warning("Supabase hydration failed for %s: %s", table, exc)
            continue

        inserted = 0
        for row in rows:
            row = {k: v for k, v in row.items() if k != "id"}
            if not row:
                continue
            cols = list(row)
            placeholders = ",".join("?" for _ in cols)
            sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
            try:
                before = conn.total_changes
                conn.execute(sql, [row[c] for c in cols])
                if conn.total_changes > before:
                    inserted += 1
            except Exception as exc:
                log.debug("Hydration row skipped for %s: %s", table, exc)
        counts[table] = inserted
    conn.commit()
    log.info("Hydrated SQLite from Supabase: %s", counts)
    mirror.start()
    return counts
