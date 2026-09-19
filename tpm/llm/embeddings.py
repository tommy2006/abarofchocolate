"""LocalIndex: retrieval over workspace text items (evidence statements, inferences, flags, diagnoses,
checks, rules, patterns, docs/*.md). Ollama embeddings when the embedding model is pulled, otherwise a
TF-IDF index (scikit-learn). Vectors persist under <ws.dir>/index/. Nothing here ever leaves the machine."""
from __future__ import annotations

import hashlib
import json
import time
import re
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..config import ROOT, Settings, get_settings
from .providers import OllamaProvider

DOC_CHUNK_CHARS = 900


def _chunk_markdown(text: str, source: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    parts = re.split(r"(?m)^(?=#{1,3} )", text)
    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        for j in range(0, len(part), DOC_CHUNK_CHARS):
            piece = part[j: j + DOC_CHUNK_CHARS]
            head = piece.splitlines()[0][:80]
            chunks.append({"id": f"doc:{source}#{i}.{j // DOC_CHUNK_CHARS}", "type": "doc", "title": head, "text": piece})
    return chunks


def collect_items(ws: Any, include_docs: bool = True, docs_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every text item worth searching: {id, type, text, meta}."""
    items: list[dict[str, Any]] = []
    if ws is not None:
        try:
            for e in ws.evidence.all():
                items.append({"id": e.id, "type": "evidence", "text": e.statement, "meta": {"kind": e.kind, "signals": e.signals, "batch_id": e.batch_id, "group_id": e.group_id}})
        except Exception:
            pass
        try:
            for inf in ws.inferences.all():
                items.append({"id": inf.id, "type": "inference", "text": f"{inf.subject}: {inf.claim}. {inf.reasoning}", "meta": {"status": inf.status, "confidence": inf.confidence, "evidence_ids": inf.evidence_ids}})
        except Exception:
            pass
        for getter, typ, fn in (
            (lambda: ws.flags(), "flag", lambda f: (f.id, f.statement, {"kind": f.kind, "severity": f.severity, "batch_id": f.batch_id, "evidence_ids": f.evidence_ids, "signals": [c.signal for c in f.signals_ranked]})),
            (lambda: ws.diagnoses(), "diagnosis", lambda d: (d.id, d.summary + " " + " ".join(d.steps), {"cause_class": d.cause_class, "confidence": d.confidence, "evidence_ids": d.evidence_ids, "flag_ids": d.flag_ids})),
            (lambda: ws.checks(), "check", lambda c: (c.check_id, c.statement, {"status": c.status, "batch_id": c.batch_id, "signals": c.signals, "evidence_ids": c.evidence_ids})),
            (lambda: ws.rules(), "rule", lambda r: (r.id, r.text + " " + r.compile_explanation, {"status": r.status})),
            (lambda: ws.patterns(), "pattern", lambda p: (p.id, (p.name or "") + " " + p.description, {"n_events": p.n_events, "evidence_ids": p.evidence_ids})),
            (lambda: ws.signals(), "signal", lambda s: (s.id, f"{s.id} {s.structural_role} {s.instrument_hypothesis or ''} {s.unit_operation_hypothesis or ''} cluster {s.cluster_id or ''}", {"role": s.structural_role, "evidence_ids": s.evidence_ids})),
        ):
            try:
                for obj in getter():
                    _id, text, meta = fn(obj)
                    items.append({"id": _id, "type": typ, "text": text, "meta": meta})
            except Exception:
                continue
    if include_docs:
        d = docs_dir or (ROOT / "docs")
        try:
            for p in sorted(d.glob("*.md")):
                try:
                    items.extend({**c, "meta": {"path": str(p)}} for c in _chunk_markdown(p.read_text(encoding="utf-8"), p.name))
                except Exception:
                    continue
        except Exception:
            pass
    return [it for it in items if it.get("text")]


MAX_EMBED_ITEMS = 600   # larger workspaces use TF-IDF (instant) instead of per-item model embeddings
EMBED_BUDGET_S = 20.0   # total time an index build may spend on model embeddings before falling back


class LocalIndex:
    def __init__(self, ws: Any = None, settings: Optional[Settings] = None, include_docs: bool = True, force_tfidf: bool = False):
        self.ws = ws
        self.settings = settings or get_settings()
        self.include_docs = include_docs
        self.force_tfidf = force_tfidf
        self.items: list[dict[str, Any]] = []
        self.method = "none"
        self.model = ""
        self._vecs: Optional[np.ndarray] = None
        self._tfidf = None
        self._matrix = None
        self.dir: Optional[Path] = (Path(ws.dir) / "index") if ws is not None else None

    # ---- persistence ----
    def _embedding_model_now(self) -> str:
        try:
            return OllamaProvider(self.settings).embedding_model() or self.settings.local_llm.embedding_model
        except Exception:
            return self.settings.local_llm.embedding_model

    def _signature(self, items: list[dict[str, Any]]) -> str:
        h = hashlib.sha256()
        for it in items:
            h.update(it["id"].encode("utf-8"))
            h.update(hashlib.sha1(it["text"].encode("utf-8")).digest())
        return h.hexdigest()

    def _load(self, signature: str) -> bool:
        if not self.dir or not (self.dir / "items.jsonl").exists() or not (self.dir / "meta.json").exists():
            return False
        try:
            meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
            if meta.get("signature") != signature:
                return False
            items = [json.loads(l) for l in (self.dir / "items.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            self.items = items
            self.method = meta.get("method", "tfidf")
            self.model = meta.get("model", "")
            if self.method == "ollama" and (self.dir / "vectors.npz").exists():
                if self.model and self.model != self._embedding_model_now():
                    return False  # built with another embedding model: its vectors do not match new queries
                self._vecs = np.load(self.dir / "vectors.npz")["vectors"].astype(np.float32)
                return self._vecs.shape[0] == len(items)
            if self.method == "tfidf":
                self._fit_tfidf()
                return True
        except Exception:
            return False
        return False

    def _save(self, signature: str) -> None:
        if not self.dir:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / "items.jsonl", "w", encoding="utf-8") as f:
                for it in self.items:
                    f.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")
            if self.method == "ollama" and self._vecs is not None:
                np.savez_compressed(self.dir / "vectors.npz", vectors=self._vecs)
            (self.dir / "meta.json").write_text(json.dumps({"signature": signature, "method": self.method, "model": self.model, "n_items": len(self.items)}), encoding="utf-8")
        except Exception:
            pass

    # ---- build ----
    def _fit_tfidf(self) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer

        texts = [it["text"] for it in self.items]
        self._tfidf = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True, token_pattern=r"(?u)\b[\w\-]+\b")
        self._matrix = self._tfidf.fit_transform(texts)
        self.method = "tfidf"
        self.model = "sklearn-tfidf"

    def build(self, force: bool = False) -> "LocalIndex":
        items = collect_items(self.ws, include_docs=self.include_docs)
        sig = self._signature(items)
        if not force and self._load(sig):
            return self
        self.items = items
        if not items:
            self.method = "none"
            return self
        used_ollama = False
        if not self.force_tfidf:
            try:
                prov = OllamaProvider(self.settings)
                # Embedding every item through the local model is only worth it for a modest index and while it
                # stays fast; otherwise the instant TF-IDF index is used so a chat answer never waits on it.
                if len(items) <= MAX_EMBED_ITEMS and prov.is_available() and prov.has_embedding_model():
                    vecs = []
                    texts = [it["text"][:2000] for it in items]
                    t_embed = time.time()
                    for i in range(0, len(texts), 32):
                        if time.time() - t_embed > EMBED_BUDGET_S:
                            raise TimeoutError("embedding budget exceeded")
                        vecs.append(prov.embed(texts[i: i + 32]))
                    self._vecs = np.vstack(vecs).astype(np.float32)
                    norms = np.linalg.norm(self._vecs, axis=1, keepdims=True) + 1e-9
                    self._vecs = self._vecs / norms
                    self.method = "ollama"
                    self.model = prov.embedding_model() or self.settings.local_llm.embedding_model
                    used_ollama = self._vecs.shape[0] == len(items)
            except Exception:
                used_ollama = False
        if not used_ollama:
            self._fit_tfidf()
        self._save(sig)
        return self

    # ---- search ----
    def search(self, query: str, k: int = 5, types: Optional[list[str]] = None) -> list[dict[str, Any]]:
        if not self.items:
            self.build()
        if not self.items or not query:
            return []
        if self.method == "ollama" and self._vecs is not None:
            try:
                q = OllamaProvider(self.settings).embed([query[:2000]], model=self.model or None)[0].astype(np.float32)  # same model as the index
                q = q / (np.linalg.norm(q) + 1e-9)
                scores = self._vecs @ q
            except Exception:
                self._fit_tfidf()
                scores = self._tfidf_scores(query)
        else:
            if self._tfidf is None:
                self._fit_tfidf()
            scores = self._tfidf_scores(query)
        order = np.argsort(-scores)
        out: list[dict[str, Any]] = []
        for idx in order:
            it = self.items[int(idx)]
            if types and it["type"] not in types:
                continue
            sc = float(scores[int(idx)])
            if sc <= 0 and out:
                break
            out.append({"id": it["id"], "type": it["type"], "text": it["text"][:400], "score": round(sc, 4), "meta": it.get("meta", {})})
            if len(out) >= k:
                break
        return out

    def _tfidf_scores(self, query: str) -> np.ndarray:
        qv = self._tfidf.transform([query])
        return np.asarray((self._matrix @ qv.T).todense()).ravel()

    def info(self) -> dict[str, Any]:
        return {"method": self.method, "model": self.model, "n_items": len(self.items), "dir": str(self.dir) if self.dir else None}
