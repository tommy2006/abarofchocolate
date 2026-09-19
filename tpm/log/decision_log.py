"""Append-only, hash-chained decision log in SQLite. Every inference, flag, diagnosis, check, rule,
human decision and egress event goes here. Tampering breaks the chain (verify_chain)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from ..contracts import LogEntry, now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_object ON entries(object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_entries_action ON entries(action);
"""

GENESIS = "0" * 64


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class DecisionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Several connections open the same log at the same moment (the API answers /status and /events for a
        # run while the pipeline thread opens its own Workspace). On a fresh database that race ended in
        # "database is locked" and killed the run before its first stage. Autocommit mode + busy timeout +
        # retried initialisation make opening safe; record() takes the write lock explicitly.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0, isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout=30000")
        last: Exception | None = None
        for attempt in range(60):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.executescript(_SCHEMA)
                last = None
                break
            except sqlite3.OperationalError as e:  # locked / busy: another connection is initialising
                last = e
                time.sleep(0.05 + 0.01 * attempt)
        if last is not None:
            raise last

    def _last_hash(self) -> str:
        row = self._conn.execute("SELECT hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def record(
        self,
        actor: str,
        action: str,
        object_type: str,
        object_id: str,
        payload: Optional[dict[str, Any]] = None,
        evidence_ids: Optional[Iterable[str]] = None,
    ) -> LogEntry:
        payload = payload or {}
        ev = list(evidence_ids or [])
        with self._lock:
            last: Exception | None = None
            for attempt in range(80):
                try:
                    # write lock first, THEN read the last hash: two connections can never chain from the same
                    # predecessor, so the hash chain stays linear with several writers
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        ts = now_iso()
                        prev = self._last_hash()
                        body = _canon({"ts": ts, "actor": actor, "action": action, "object_type": object_type, "object_id": object_id, "payload": payload, "evidence_ids": ev, "prev": prev})
                        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
                        cur = self._conn.execute(
                            "INSERT INTO entries(ts, actor, action, object_type, object_id, payload, evidence_ids, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                            (ts, actor, action, object_type, object_id, _canon(payload), _canon(ev), prev, h),
                        )
                        seq = cur.lastrowid
                        self._conn.execute("COMMIT")
                    except Exception:
                        try:
                            self._conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        raise
                    return LogEntry(seq=seq, ts=ts, actor=actor, action=action, object_type=object_type, object_id=object_id, payload=payload, evidence_ids=ev, prev_hash=prev, hash=h)
                except sqlite3.OperationalError as e:  # locked / busy
                    last = e
                    time.sleep(0.02 + 0.005 * attempt)
            raise last if last is not None else RuntimeError("decision log write failed")

    def _row_to_entry(self, r: tuple) -> LogEntry:
        return LogEntry(seq=r[0], ts=r[1], actor=r[2], action=r[3], object_type=r[4], object_id=r[5], payload=json.loads(r[6]), evidence_ids=json.loads(r[7]), prev_hash=r[8], hash=r[9])

    def entries(
        self,
        object_type: Optional[str] = None,
        object_id: Optional[str] = None,
        action: Optional[str] = None,
        actor_prefix: Optional[str] = None,
        since_seq: int = 0,
        limit: int = 10_000,
    ) -> list[LogEntry]:
        q = "SELECT seq, ts, actor, action, object_type, object_id, payload, evidence_ids, prev_hash, hash FROM entries WHERE seq > ?"
        args: list[Any] = [since_seq]
        if object_type:
            q += " AND object_type = ?"
            args.append(object_type)
        if object_id:
            q += " AND object_id = ?"
            args.append(object_id)
        if action:
            q += " AND action = ?"
            args.append(action)
        if actor_prefix:
            q += " AND actor LIKE ?"
            args.append(actor_prefix + "%")
        q += " ORDER BY seq ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    def verify_chain(self) -> dict[str, Any]:
        """Recompute every hash. Returns {'ok': bool, 'checked': n, 'first_bad_seq': seq|None}."""
        prev = GENESIS
        checked = 0
        with self._lock:
            rows = self._conn.execute("SELECT seq, ts, actor, action, object_type, object_id, payload, evidence_ids, prev_hash, hash FROM entries ORDER BY seq ASC").fetchall()
        for r in rows:
            e = self._row_to_entry(r)
            body = _canon({"ts": e.ts, "actor": e.actor, "action": e.action, "object_type": e.object_type, "object_id": e.object_id, "payload": e.payload, "evidence_ids": e.evidence_ids, "prev": prev})
            h = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if e.prev_hash != prev or e.hash != h:
                return {"ok": False, "checked": checked, "first_bad_seq": e.seq}
            prev = e.hash
            checked += 1
        return {"ok": True, "checked": checked, "first_bad_seq": None}

    def export_jsonl(self, out_path: str | Path) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for e in self.entries(limit=10_000_000):
                f.write(e.model_dump_json() + "\n")
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
