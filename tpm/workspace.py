"""Run workspace: one directory per run holding every artifact, plus evidence/inference registries,
the decision log and a DuckDB handle over dataset.parquet."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import Settings, get_settings
from .contracts import Evidence, Inference, RunStatus, StageStatus, now_iso
from .log.decision_log import DecisionLog
from .memory import duckdb_memory_limit, duckdb_threads

ARTIFACTS = {
    "meta": "meta.json",
    "status": "status.json",
    "dataset": "dataset.parquet",
    "schema": "schema.json",
    "signals": "signals.json",
    "relations": "relations.json",
    "domain": "domain.json",
    "evidence": "evidence.jsonl",
    "inferences": "inferences.jsonl",
    "checks": "checks.jsonl",
    "trust": "trust.jsonl",
    "batches": "batches.json",
    "rules": "rules.json",
    "scores": "scores.parquet",
    "flags": "flags.jsonl",
    "patterns": "patterns.json",
    "baseline": "baseline.json",
    "detect_meta": "detect_meta.json",
    "evaluation": "evaluation.json",
    "diagnoses": "diagnoses.jsonl",
    "assessor": "assessor.json",
    "decision_log": "decision_log.sqlite",
    "egress_ledger": "egress_ledger.jsonl",
    "chat": "chat.jsonl",
}


def _json_default(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "item"):
        try:
            return o.item()
        except Exception:
            pass
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def dumps(obj: Any, indent: Optional[int] = None) -> str:
    return json.dumps(obj, default=_json_default, indent=indent, ensure_ascii=False, allow_nan=True)


class _Registry:
    """Append-only JSONL registry with sequential IDs (EV-000001 / INF-000001)."""

    def __init__(self, path: Path, prefix: str, model: type):
        self.path = path
        self.prefix = prefix
        self.model = model
        self._lock = threading.RLock()
        self._n = 0
        self._cache: dict[str, Any] = {}
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = self.model(**json.loads(line))
                    self._cache[obj.id] = obj
                    self._n = max(self._n, int(obj.id.split("-")[-1]))

    def next_id(self) -> str:
        with self._lock:
            self._n += 1
            return f"{self.prefix}-{self._n:06d}"

    def add_obj(self, obj: Any) -> Any:
        with self._lock:
            self._cache[obj.id] = obj
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(obj.model_dump_json() + "\n")
        return obj

    def get(self, id_: str) -> Optional[Any]:
        return self._cache.get(id_)

    def all(self) -> list[Any]:
        return list(self._cache.values())

    def update(self, obj: Any) -> Any:
        """Rewrite the file with an updated object (rare: human status changes)."""
        with self._lock:
            self._cache[obj.id] = obj
            with open(self.path, "w", encoding="utf-8") as f:
                for o in self._cache.values():
                    f.write(o.model_dump_json() + "\n")
        return obj

    def __len__(self) -> int:
        return len(self._cache)


class EvidenceRegistry(_Registry):
    def __init__(self, path: Path):
        super().__init__(path, "EV", Evidence)

    def add(self, kind: str, statement: str, signals: Optional[Iterable[str]] = None, values: Optional[dict[str, Any]] = None, computed_by: str = "", n_samples: Optional[int] = None, group_id: Optional[str] = None, batch_id: Optional[str] = None) -> Evidence:
        ev = Evidence(id=self.next_id(), kind=kind, signals=list(signals or []), statement=statement, values=values or {}, computed_by=computed_by, n_samples=n_samples, group_id=group_id, batch_id=batch_id)
        return self.add_obj(ev)


class InferenceRegistry(_Registry):
    def __init__(self, path: Path):
        super().__init__(path, "INF", Inference)

    def add(self, subject: str, claim: str, status: str = "inferred", confidence: float = 0.5, evidence_ids: Optional[Iterable[str]] = None, reasoning: str = "", source: str = "code", alternatives: Optional[Iterable[str]] = None, stage: str = "") -> Inference:
        inf = Inference(id=self.next_id(), subject=subject, claim=claim, status=status, confidence=float(confidence), evidence_ids=list(evidence_ids or []), reasoning=reasoning, source=source, alternatives=list(alternatives or []), stage=stage)
        return self.add_obj(inf)


class Workspace:
    def __init__(self, run_id: Optional[str] = None, settings: Optional[Settings] = None, root: Optional[str | Path] = None):
        self.settings = settings or get_settings()
        self.root = Path(root) if root else self.settings.workspace_path
        self.run_id = run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        self.dir = self.root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.evidence = EvidenceRegistry(self.path("evidence"))
        self.inferences = InferenceRegistry(self.path("inferences"))
        self.log = DecisionLog(self.path("decision_log"))
        self._lock = threading.RLock()
        self._duck = None

    # ---------- paths / json ----------
    def path(self, artifact: str) -> Path:
        return self.dir / ARTIFACTS.get(artifact, artifact)

    def exists(self, artifact: str) -> bool:
        return self.path(artifact).exists()

    def write_json(self, artifact: str, obj: Any) -> Path:
        p = self.path(artifact)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(dumps(obj, indent=1))
            tmp.replace(p)
        return p

    def read_json(self, artifact: str, default: Any = None) -> Any:
        p = self.path(artifact)
        if not p.exists():
            return default
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    def append_jsonl(self, artifact: str, obj: Any) -> None:
        p = self.path(artifact)
        with self._lock:
            with open(p, "a", encoding="utf-8") as f:
                f.write(dumps(obj) + "\n")

    def read_jsonl(self, artifact: str) -> list[Any]:
        p = self.path(artifact)
        if not p.exists():
            return []
        out = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def rewrite_jsonl(self, artifact: str, objs: Iterable[Any]) -> None:
        p = self.path(artifact)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                for o in objs:
                    f.write(dumps(o) + "\n")
            tmp.replace(p)

    # ---------- typed helpers ----------
    def schema(self):
        from .contracts import DatasetSchema

        d = self.read_json("schema")
        return DatasetSchema(**d) if d else None

    def signals(self):
        from .contracts import SignalDescriptor

        d = self.read_json("signals", [])
        return [SignalDescriptor(**x) for x in d]

    def flags(self):
        from .contracts import Flag

        return [Flag(**x) for x in self.read_jsonl("flags")]

    def diagnoses(self):
        from .contracts import Diagnosis

        return [Diagnosis(**x) for x in self.read_jsonl("diagnoses")]

    def checks(self):
        from .contracts import CheckResult

        return [CheckResult(**x) for x in self.read_jsonl("checks")]

    def trust(self):
        from .contracts import TrustVerdict

        return [TrustVerdict(**x) for x in self.read_jsonl("trust")]

    def rules(self):
        from .contracts import Rule

        return [Rule(**x) for x in self.read_json("rules", [])]

    def patterns(self):
        from .contracts import FaultPattern

        return [FaultPattern(**x) for x in self.read_json("patterns", [])]

    # ---------- status ----------
    def status(self) -> RunStatus:
        d = self.read_json("status")
        if d:
            return RunStatus(**d)
        return RunStatus(run_id=self.run_id, source_path="", profile=self.settings.profile)

    def set_status(self, status: RunStatus) -> None:
        status.updated_at = now_iso()
        self.write_json("status", status)

    def update_stage(self, stage: str, state: Optional[str] = None, progress: Optional[float] = None, message: Optional[str] = None, error: Optional[str] = None) -> RunStatus:
        with self._lock:
            st = self.status()
            found = None
            for s in st.stages:
                if s.stage == stage:
                    found = s
            if found is None:
                found = StageStatus(stage=stage, state="pending")
                st.stages.append(found)
            if state:
                found.state = state
                if state == "running" and not found.started_at:
                    found.started_at = now_iso()
                if state in ("done", "failed", "skipped"):
                    found.finished_at = now_iso()
            if progress is not None:
                found.progress = float(progress)
            if message is not None:
                found.message = message
            if error is not None:
                found.error = error
            self.set_status(st)
            return st

    # ---------- duckdb ----------
    def duckdb(self):
        """Shared DuckDB connection with a memory limit; `dataset` view points at dataset.parquet."""
        import duckdb

        with self._lock:
            if self._duck is None:
                con = duckdb.connect(database=":memory:")
                con.execute(f"SET memory_limit='{duckdb_memory_limit()}'")
                con.execute(f"SET threads={duckdb_threads()}")
                con.execute(f"SET temp_directory='{(self.dir / 'duck_tmp').as_posix()}'")
                self._duck = con
            if self.exists("dataset"):
                self._duck.execute(f"CREATE OR REPLACE VIEW dataset AS SELECT * FROM read_parquet('{self.path('dataset').as_posix()}')")
            if self.exists("scores"):
                self._duck.execute(f"CREATE OR REPLACE VIEW scores AS SELECT * FROM read_parquet('{self.path('scores').as_posix()}')")
            return self._duck

    def close(self) -> None:
        with self._lock:
            if self._duck is not None:
                self._duck.close()
                self._duck = None
        self.log.close()

    # ---------- discovery ----------
    @classmethod
    def list_runs(cls, settings: Optional[Settings] = None) -> list[dict[str, Any]]:
        settings = settings or get_settings()
        root = settings.workspace_path
        out = []
        if not root.exists():
            return out
        for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir() and (d / ARTIFACTS["status"]).exists():
                try:
                    with open(d / ARTIFACTS["status"], "r", encoding="utf-8") as f:
                        out.append(json.load(f))
                except Exception:
                    continue
        return out

    @classmethod
    def open(cls, run_id: str, settings: Optional[Settings] = None) -> "Workspace":
        return cls(run_id=run_id, settings=settings)
