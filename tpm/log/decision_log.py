"""Append-only, hash-chained decision log in SQLite. Every inference, flag, diagnosis, check, rule,
human decision and egress event goes here. Tampering breaks the chain (verify_chain).

    log.record(actor, action, object_type, object_id, payload, evidence_ids)     one entry, one transaction
    log.record_many([(actor, action, object_type, object_id, payload, evidence_ids), ...])
                                                                                  many entries, one transaction per chunk

Both take the write lock FIRST (BEGIN IMMEDIATE) and only then read the last hash, so two writers (two Workspace
objects, the API and the pipeline, several threads) can never chain from the same predecessor: the chain stays
linear and verifiable. record_many is what a stage uses for thousands of flags or checks: one commit instead of one
per entry (on Windows every commit waits for the disk, so a loop of record() calls costs milliseconds per entry)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

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
MANY_CHUNK = 5000  # entries per transaction in record_many: bounds how long another writer waits for the lock
_INSERT = "INSERT INTO entries(ts, actor, action, object_type, object_id, payload, evidence_ids, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)"


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _entry_hash(ts: str, actor: str, action: str, object_type: str, object_id: str, payload: dict[str, Any], evidence_ids: list[str], prev: str) -> str:
    """The chained hash of one entry. record(), record_many() and verify_chain() all use this one definition."""
    body = _canon({"ts": ts, "actor": actor, "action": action, "object_type": object_type, "object_id": object_id, "payload": payload, "evidence_ids": evidence_ids, "prev": prev})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _normalize(actor: Any, action: Any, object_type: Any, object_id: Any, payload: Optional[dict[str, Any]] = None, evidence_ids: Optional[Iterable[str]] = None) -> tuple[str, str, str, str, dict[str, Any], list[str]]:
    """Text columns hold text: an int object_id would be read back as a string by verify_chain and break the chain."""
    return str(actor), str(action), str(object_type), str(object_id), (payload or {}), [str(e) for e in (evidence_ids or [])]


def _as_args(item: Any) -> tuple[str, str, str, str, dict[str, Any], list[str]]:
    """One record_many item: a tuple / list (actor, action, object_type, object_id[, payload[, evidence_ids]]) or a dict
    with those keys."""
    if isinstance(item, dict):
        return _normalize(item["actor"], item["action"], item["object_type"], item["object_id"], item.get("payload"), item.get("evidence_ids"))
    if isinstance(item, (tuple, list)) and 4 <= len(item) <= 6:
        return _normalize(*item)
    raise TypeError("record_many expects (actor, action, object_type, object_id[, payload[, evidence_ids]]) tuples or dicts with those keys")


class DecisionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tl = threading.local()  # per-thread write buffer, see buffered()
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
        actor, action, object_type, object_id, payload, ev = _normalize(actor, action, object_type, object_id, payload, evidence_ids)
        buf = getattr(self._tl, "buf", None)
        if buf is not None:  # inside buffered(): kept in order, written in bulk (no LogEntry to return yet)
            buf.append((actor, action, object_type, object_id, payload, ev))
            if len(buf) >= self._tl.flush_every:
                self._tl.buf = []
                self.record_many(buf)
            return None  # type: ignore[return-value]
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
                        h = _entry_hash(ts, actor, action, object_type, object_id, payload, ev, prev)
                        cur = self._conn.execute(_INSERT, (ts, actor, action, object_type, object_id, _canon(payload), _canon(ev), prev, h))
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

    @contextmanager
    def buffered(self, flush_every: int = MANY_CHUNK) -> Iterator[None]:
        """record() calls made by THIS thread inside the block are kept in their order and written with record_many
        (one transaction per chunk) at the end of the block, or every `flush_every` entries. Other threads write as
        usual. While buffering, record() returns None: use it only where the returned entry is not needed (a stage
        writing one or two records per object). Nested blocks join the outer one; an exception still flushes."""
        if getattr(self._tl, "buf", None) is not None:
            yield
            return
        self._tl.buf = []
        self._tl.flush_every = max(1, int(flush_every))
        try:
            yield
        finally:
            buf, self._tl.buf = self._tl.buf, None
            if buf:
                self.record_many(buf)

    def record_many(self, entries: Iterable[Any], chunk_size: int = MANY_CHUNK) -> list[LogEntry]:
        """Append many entries in their order and return them as LogEntry objects. Items are (actor, action,
        object_type, object_id[, payload[, evidence_ids]]) tuples or dicts with those keys.

        Every chunk of up to `chunk_size` entries is ONE transaction: write lock first (BEGIN IMMEDIATE), then the last
        hash, then the entries chained one after the other and inserted together. Another writer waits for the chunk
        or writes between two chunks; either way the chain stays linear. A chunk that fails is rolled back as a whole."""
        items = [_as_args(e) for e in entries]
        out: list[LogEntry] = []
        size = max(1, int(chunk_size))
        for start in range(0, len(items), size):
            with self._lock:  # released between chunks: other threads of this process may write in between
                out.extend(self._write_chunk(items[start : start + size]))
        return out

    def _write_chunk(self, chunk: list[tuple[str, str, str, str, dict[str, Any], list[str]]]) -> list[LogEntry]:
        if not chunk:
            return []
        stored = [(_canon(p), _canon(ev)) for (_, _, _, _, p, ev) in chunk]  # independent of the predecessor: done before the lock
        last: Exception | None = None
        for attempt in range(80):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    ts = now_iso()
                    prev = self._last_hash()
                    rows: list[tuple[Any, ...]] = []
                    links: list[tuple[str, str]] = []
                    for (actor, action, otype, oid, payload, ev), (p_txt, ev_txt) in zip(chunk, stored):
                        h = _entry_hash(ts, actor, action, otype, oid, payload, ev, prev)
                        rows.append((ts, actor, action, otype, oid, p_txt, ev_txt, prev, h))
                        links.append((prev, h))
                        prev = h
                    self._conn.executemany(_INSERT, rows)
                    last_seq = int(self._conn.execute("SELECT seq FROM entries ORDER BY seq DESC LIMIT 1").fetchone()[0])
                    self._conn.execute("COMMIT")
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                first = last_seq - len(chunk) + 1  # one transaction under the write lock: the rowids are consecutive
                return [LogEntry(seq=first + i, ts=ts, actor=a, action=ac, object_type=ot, object_id=oi, payload=p, evidence_ids=ev, prev_hash=ph, hash=h) for i, ((a, ac, ot, oi, p, ev), (ph, h)) in enumerate(zip(chunk, links))]
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

    def logged_ids(self, object_type: Optional[str] = None, action: Optional[str] = None) -> set[str]:
        """Distinct object_ids that have at least one entry (optionally of one object type and / or action). No payload
        is parsed, so this stays cheap on a log with hundreds of thousands of entries."""
        q = "SELECT DISTINCT object_id FROM entries WHERE 1=1"
        args: list[Any] = []
        if object_type:
            q += " AND object_type = ?"
            args.append(object_type)
        if action:
            q += " AND action = ?"
            args.append(action)
        with self._lock:
            return {str(r[0]) for r in self._conn.execute(q, args).fetchall()}

    def scan(self, payload_actions: Iterable[str] = ()) -> list[tuple[str, str, str, str, Optional[str]]]:
        """(object_type, action, object_id, evidence_ids JSON, payload JSON or None) of every entry, in order. Payloads
        are returned only for entries whose action is in `payload_actions` (the completeness audit reads a few stage
        summaries, not the whole log)."""
        acts = sorted({str(a) for a in payload_actions})
        marks = ",".join("?" for _ in acts) or "NULL"
        q = f"SELECT object_type, action, object_id, evidence_ids, CASE WHEN action IN ({marks}) THEN payload ELSE NULL END FROM entries ORDER BY seq"
        with self._lock:
            return [tuple(r) for r in self._conn.execute(q, acts).fetchall()]

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
            h = _entry_hash(e.ts, e.actor, e.action, e.object_type, e.object_id, e.payload, e.evidence_ids, prev)
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
