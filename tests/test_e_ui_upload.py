"""Agent E: big-file upload. ``POST /api/runs`` streams the data file to ``workspace/<run_id>/uploads/<name>``
in chunks and never holds it in memory; the Runs view sends it with progress events and can cancel.

    .venv\\Scripts\\python.exe -m pytest tests/test_e_ui_upload.py -q
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import time
import tracemalloc
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"
MB = 1024 * 1024


# ------------------------------------------------------------------------------------------ helpers
def _write_csv(path: Path, target_bytes: int) -> tuple[int, str]:
    """A CSV of about ``target_bytes`` whose rows all differ (a reordered or dropped chunk changes the hash)."""
    h = hashlib.sha256()
    size = 0
    i = 0
    with open(path, "wb") as f:
        head = b"t,group,a,b,c,d\n"
        f.write(head)
        h.update(head)
        size += len(head)
        while size < target_bytes:
            rows = "".join(f"{j},{j // 500},{(j * 37) % 1000 / 10:.1f},{(j * 91) % 977 / 7:.3f},{j % 13},{(j * j) % 10007}\n" for j in range(i, i + 20000)).encode()
            i += 20000
            f.write(rows)
            h.update(rows)
            size += len(rows)
    return size, h.hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(MB), b""):
            h.update(block)
    return h.hexdigest()


@pytest.fixture()
def calls(monkeypatch):
    """Replace the pipeline with a stub that finishes at once and records how the job was started (the real
    pipeline on a 30 MB file would run for minutes in a background thread)."""
    import tpm.pipeline as pipeline
    from tpm.workspace import Workspace

    seen: list[dict] = []

    def fake_run_pipeline(source_path, run_id=None, profile=None, options=None, progress_cb=None, settings=None, **kw):
        seen.append({"source_path": str(source_path), "run_id": run_id, "profile": profile, "options": dict(options or {})})
        ws = Workspace(run_id=run_id, settings=settings)
        st = ws.status()
        st.state = "done"
        for sg in st.stages:
            sg.state = "done"
            sg.progress = 1.0
        ws.set_status(st)
        ws.close()
        return st

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    return seen


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from tpm.api.server import create_app

    app = create_app(workspace_dir=tmp_path / "workspace")
    with TestClient(app) as c:
        c.workspace = tmp_path / "workspace"
        yield c


def _wait_job(client, rid: str) -> dict:
    st: dict = {}
    for _ in range(200):
        try:
            r = client.get(f"/api/runs/{rid}/status")
            st = r.json() if r.status_code == 200 else st
        except Exception:
            pass
        if st.get("job") and st["job"].get("state") in ("done", "failed"):
            break
        time.sleep(0.05)
    return st


def _incoming(workspace: Path) -> list[Path]:
    d = workspace / ".incoming"
    return list(d.iterdir()) if d.exists() else []


# ------------------------------------------------------------------------------------------ the upload itself
def test_30mb_upload_creates_run_with_identical_file(client, calls, tmp_path):
    src = tmp_path / "big process file.csv"
    size, digest = _write_csv(src, 30 * MB)
    assert size >= 30 * MB
    with open(src, "rb") as fh:
        r = client.post("/api/runs", data={"has_header": "true", "transposed": "false", "group_columns": "group, t", "language": "en"}, files={"file": (src.name, fh, "text/csv")})
    assert r.status_code == 202, r.text
    d = r.json()
    rid = d["run_id"]
    stored = client.workspace / rid / "uploads" / src.name
    assert Path(d["source_path"]) == stored
    assert stored.is_file() and stored.stat().st_size == size
    assert _sha256(stored) == digest
    assert _incoming(client.workspace) == [], "the staging folder must be empty after a finished upload"
    st = _wait_job(client, rid)
    assert st["run_id"] == rid and len(st["stages"]) == 7
    # the rest of create_run is intact: options parsed, job started on the uploaded file
    assert d["options"]["has_header"] is True and d["options"]["transposed"] is False and d["options"]["group_columns"] == ["group", "t"]
    assert len(calls) == 1 and calls[0]["source_path"] == str(stored) and calls[0]["run_id"] == rid
    assert calls[0]["options"]["group_columns"] == ["group", "t"]
    assert any(x["run_id"] == rid for x in client.get("/api/runs").json()["runs"])
    assert all(x["run_id"] != ".incoming" for x in client.get("/api/runs").json()["runs"])


def _multipart(boundary: str, before: list[tuple[str, str]], filename: str, blocks, after: list[tuple[str, str]] = (), rules: bytes | None = None):
    b = boundary
    for k, v in before:
        yield f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    yield f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode()
    for block in blocks:
        yield block
    yield b"\r\n"
    if rules is not None:
        yield f'--{b}\r\nContent-Disposition: form-data; name="rules_file"; filename="rules.md"\r\nContent-Type: text/markdown\r\n\r\n'.encode() + rules + b"\r\n"
    for k, v in after:
        yield f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    yield f"--{b}--\r\n".encode()


def test_fields_after_the_file_and_rules_file(client, calls):
    body = b"".join(_multipart("XBOUNDX", [("has_header", "true")], "..\\..\\evil name.csv", [b"a,b\n1,2\n3,4\n"], after=[("run_id", "upload_order_check"), ("domain_hint", "two columns")], rules=b"S01 must stay below 5\n"))
    r = client.post("/api/runs", content=body, headers={"content-type": "multipart/form-data; boundary=XBOUNDX"})
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["run_id"] == "upload_order_check", "run_id sent after the file part must still name the run"
    stored = Path(d["source_path"])
    assert stored == client.workspace / "upload_order_check" / "uploads" / "evil name.csv", "only the base name of the upload is used"
    assert stored.read_bytes() == b"a,b\n1,2\n3,4\n"
    assert d["options"]["rules_text"].startswith("S01 must stay below 5") and d["options"]["domain_hint"] == "two columns"
    _wait_job(client, d["run_id"])
    assert _incoming(client.workspace) == []


def test_path_runs_and_bad_requests_still_work(client, calls, tmp_path):
    src = tmp_path / "local.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    r = client.post("/api/runs", data={"path": str(src)}, files={"rules_file": ("r.md", b"", "text/markdown")})
    assert r.status_code == 202, r.text
    assert r.json()["source_path"] == str(src), "a file chosen by path is read in place"
    assert not (client.workspace / r.json()["run_id"] / "uploads").exists(), "nothing is copied for a path run"
    _wait_job(client, r.json()["run_id"])
    assert client.post("/api/runs", data={"has_header": "true"}, files={"other": ("x.bin", b"zz", "application/octet-stream")}).status_code == 400
    # an unknown profile is refused and the uploaded file does not stay behind
    bad = client.post("/api/runs", data={"profile": "no-such-profile"}, files={"file": ("x.csv", b"a\n1\n", "text/csv")})
    assert bad.status_code == 400 and "profile" in bad.text
    assert _incoming(client.workspace) == []


# ------------------------------------------------------------------------------------------ memory
def _fake_request(chunks, boundary: str = "MEMB", content_length: int | None = None, disconnect_after: int | None = None):
    from starlette.requests import Request

    it = iter(chunks)
    sent = [0]

    async def receive():
        if disconnect_after is not None and sent[0] >= disconnect_after:
            return {"type": "http.disconnect"}
        try:
            chunk = next(it)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent[0] += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    headers = [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return Request({"type": "http", "method": "POST", "path": "/api/runs", "headers": headers, "query_string": b""}, receive)


def test_receiver_memory_stays_bounded_for_a_large_stream(tmp_path):
    """96 MB through the receiver in 64 KB network chunks, then 40 MB as ONE chunk: the peak of the Python heap
    stays near the 8 MB write buffer, far below the file size."""
    from tpm.api.upload import CHUNK_BYTES, receive_multipart

    def blocks(n_bytes: int, size: int, h):
        i = 0
        while i < n_bytes:
            blk = (f"{i:016d}," * (size // 17 + 1)).encode()[:size]
            h.update(blk)
            i += size
            yield blk

    for total, size, limit in ((96 * MB, 64 * 1024, 3 * CHUNK_BYTES),):
        h = hashlib.sha256()
        req = _fake_request(_multipart("MEMB", [("has_header", "true")], "stream.csv", blocks(total, size, h)))
        tracemalloc.start()
        try:
            got = asyncio.run(receive_multipart(req, tmp_path / "ws"))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert got.size == total and got.path.stat().st_size == total
        assert _sha256(got.path) == h.hexdigest()
        assert peak < limit, f"peak heap {peak / MB:.1f} MB for a {total / MB:.0f} MB upload (limit {limit / MB:.0f} MB)"
        assert got.fields == [("has_header", "true")]
        got.discard()
        assert _incoming(tmp_path / "ws") == []

    # one huge network chunk (what Starlette's TestClient sends): only the parser slices and the write buffer are added
    big = os.urandom(MB) * 40
    body = b"".join(_multipart("MEMB", [], "one.bin", [big]))
    del big
    req = _fake_request([body])
    tracemalloc.start()
    try:
        got = asyncio.run(receive_multipart(req, tmp_path / "ws"))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got.size == 40 * MB
    assert peak < 3 * CHUNK_BYTES, f"peak heap {peak / MB:.1f} MB on top of a single 40 MB chunk"
    got.discard()


def test_handler_no_longer_reads_the_upload_into_memory():
    server = (ROOT / "tpm" / "api" / "server.py").read_text(encoding="utf-8")
    m = re.search(r"async def create_run\(.*?(?=\n    @app\.)", server, flags=re.S)
    assert m, "create_run not found"
    body = m.group(0)
    assert "receive_multipart" in body
    assert not re.search(r"\.read\(\s*\)", body), "create_run must not call .read() without a size"
    assert "request.form()" not in body, "request.form() spools the whole upload to the temp directory first"
    assert "write_bytes(" not in body
    upload = (ROOT / "tpm" / "api" / "upload.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in upload.splitlines() if not line.strip().startswith("#"))
    assert not re.search(r"\.read\(", code.split('"""', 2)[2]), "the receiver streams; it never reads a file or body whole"
    assert "request.stream()" in upload and "CHUNK_BYTES = 8 * 1024 * 1024" in upload


# ------------------------------------------------------------------------------------------ failures
def test_disk_full_before_the_upload_is_reported_clearly(client, calls, monkeypatch):
    import tpm.api.upload as upload

    class _Usage:
        total, used, free = 100 * MB, 99 * MB, 1 * MB

    monkeypatch.setattr(upload.shutil, "disk_usage", lambda p: _Usage)
    r = client.post("/api/runs", files={"file": ("big.csv", b"x" * (3 * MB), "text/csv")})
    assert r.status_code == 507, r.text
    detail = r.json()["detail"]
    assert "Not enough disk space" in detail and "1.0 MB are free" in detail and "path" in detail
    assert _incoming(client.workspace) == [] and calls == []
    assert client.get("/api/runs").json()["runs"] == []


def test_disk_full_while_writing_removes_the_partial_file(client, calls, monkeypatch):
    import tpm.api.upload as upload

    real = upload.run_in_threadpool

    async def failing(fn, *a, **kw):
        if getattr(fn, "__name__", "") == "write":
            raise OSError(errno.ENOSPC, "No space left on device")
        return await real(fn, *a, **kw)

    monkeypatch.setattr(upload, "run_in_threadpool", failing)
    r = client.post("/api/runs", files={"file": ("big.csv", b"y" * (2 * MB), "text/csv")})
    assert r.status_code == 507, r.text
    assert "Not enough disk space" in r.json()["detail"] and "Nothing was kept" in r.json()["detail"]
    assert _incoming(client.workspace) == [] and calls == []


def test_cancelled_upload_leaves_nothing_behind(tmp_path):
    from tpm.api.upload import UploadFailed, receive_multipart

    chunks = list(_multipart("MEMB", [("run_id", "cancel_me")], "half.csv", [b"z" * (256 * 1024) for _ in range(64)]))
    req = _fake_request(chunks, disconnect_after=40)
    with pytest.raises(UploadFailed) as e:
        asyncio.run(receive_multipart(req, tmp_path / "ws", chunk_bytes=MB))
    assert e.value.status_code == 400 and "cancel" in e.value.detail
    assert _incoming(tmp_path / "ws") == [], "a cancelled upload must not leave a partial file"
    assert not (tmp_path / "ws" / "cancel_me").exists()


def test_malformed_and_oversized_parts(tmp_path):
    from tpm.api.upload import MAX_FIELD_BYTES, UploadFailed, receive_multipart

    huge_field = f'--MEMB\r\nContent-Disposition: form-data; name="domain_hint"\r\n\r\n'.encode() + b"h" * (MAX_FIELD_BYTES + 10) + b"\r\n--MEMB--\r\n"
    with pytest.raises(UploadFailed) as e:
        asyncio.run(receive_multipart(_fake_request([huge_field]), tmp_path / "ws"))
    assert e.value.status_code == 413
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    no_boundary = Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", b"multipart/form-data")], "query_string": b""}, receive)
    with pytest.raises(UploadFailed) as e2:
        asyncio.run(receive_multipart(no_boundary, tmp_path / "ws"))
    assert e2.value.status_code == 400


def test_safe_filename():
    from tpm.api.upload import safe_filename

    assert safe_filename("C:\\data\\plant A\\te process.csv") == "te process.csv"
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..") == "upload.csv" and safe_filename("") == "upload.csv" and safe_filename(None) == "upload.csv"
    assert safe_filename('we"ird<name>.csv') == "weirdname.csv"


# ------------------------------------------------------------------------------------------ client side
def test_runs_view_uploads_with_progress_and_cancel():
    src = (STATIC / "js" / "views" / "runs.js").read_text(encoding="utf-8")
    assert "new XMLHttpRequest()" in src and "xhr.upload" in src, "fetch has no upload progress events"
    assert ".abort()" in src, "Cancel must abort the request"
    assert "fd.append('file'" in src and src.index("fd.append('run_id'") < src.index("fd.append('file'"), "fields go before the file so the server knows them first"
    en = json.loads((STATIC / "i18n" / "en.json").read_text(encoding="utf-8"))
    for k in ("upload.title", "upload.sent", "upload.speed", "upload.remaining", "upload.cancel", "upload.complete", "upload.inPlace", "upload.staysLocal", "upload.cancelled", "upload.failed"):
        assert k in en, k
        assert f"'{k}'" in src, f"runs.js does not use {k}"
    assert "nothing is copied" in en["upload.inPlace"].lower()
    assert "this machine" in en["upload.staysLocal"].lower()
