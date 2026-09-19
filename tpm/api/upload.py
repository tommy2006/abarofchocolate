"""Streaming receiver for the multipart upload of ``POST /api/runs``.

A process file can be several gigabytes. ``request.form()`` spools file parts into the system temp directory
and the old handler then read the whole file into memory; neither survives a 6 GB upload. This receiver parses
the multipart stream itself (python-multipart, the parser Starlette uses) and writes the data file straight to
the workspace volume in bounded chunks:

* memory stays bounded by ``CHUNK_BYTES`` whatever the file size (the network chunks are cut into
  ``FEED_BYTES`` pieces before they reach the parser, so one huge chunk cannot blow the bound either);
* nothing is rejected by size; the only size-related failure is a full disk, reported as HTTP 507 with the
  numbers a person needs (needed, free, where), after the partial file is removed;
* the file is staged in ``<workspace>/.incoming/<token>/`` because form fields such as ``run_id`` may arrive
  after the file part; ``Received.move_into`` renames it into ``<workspace>/<run_id>/uploads/`` (same volume,
  so nothing is copied). ``.incoming`` holds no status.json and its name starts with a dot, so it is never
  listed or addressed as a run;
* a client that goes away (Cancel in the UI aborts the request) leaves nothing behind.

When the upload fails half-way the rest of the request body is read and thrown away before the error is sent:
a browser that is still sending would otherwise see a connection reset instead of the explanation.
"""
from __future__ import annotations

import errno
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request

try:  # the package was renamed; Starlette accepts both
    import python_multipart as _mp
    from python_multipart.multipart import parse_options_header
except ModuleNotFoundError:  # pragma: no cover
    import multipart as _mp  # type: ignore[no-redef]
    from multipart.multipart import parse_options_header  # type: ignore[no-redef]

CHUNK_BYTES = 8 * 1024 * 1024  # file data buffered before one write in the threadpool
FEED_BYTES = 1024 * 1024  # largest slice handed to the parser at once
MAX_FIELD_BYTES = 1024 * 1024  # a plain form field
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024  # a side file kept in memory (rules file)
STAGING_DIR = ".incoming"
STALE_STAGING_S = 24 * 3600


class UploadFailed(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _human(n: float) -> str:
    n = float(max(0, n))
    for unit in ("bytes", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def safe_filename(name: Optional[str], default: str = "upload.csv") -> str:
    """Base name only (no directories from either OS), never empty, never a dot entry."""
    base = Path(str(name or "").replace("\\", "/")).name.strip().strip(".").strip()
    base = "".join(ch for ch in base if ch >= " " and ch not in '<>:"|?*')
    return base or default


def _is_disk_full(e: BaseException) -> bool:
    return isinstance(e, OSError) and (e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112))


def _disk_full_message(root: Path, needed: Optional[int], written: int) -> str:
    try:
        free = shutil.disk_usage(root).free
        free_txt = f"{_human(free)} are free"
    except Exception:
        free_txt = "the free space could not be read"
    need_txt = f"the upload needs about {_human(needed)}" if needed else f"{_human(written)} had been written"
    return (f"Not enough disk space for this upload: {need_txt} and {free_txt} on the disk that holds the workspace ({root}). "
            "Nothing was kept. Free some space, or enter the path of the file instead of uploading it: a file chosen by path is read in place and nothing is copied.")


@dataclass
class Received:
    """What one multipart request carried. ``path`` is the staged data file (None when no file part came)."""

    fields: list[tuple[str, str]] = field(default_factory=list)
    small_files: dict[str, bytes] = field(default_factory=dict)
    path: Optional[Path] = None
    filename: Optional[str] = None
    size: int = 0
    seconds: float = 0.0
    _staging: Optional[Path] = None

    def move_into(self, dest_dir: Path) -> Path:
        """Rename the staged file into ``dest_dir`` (same volume: instant, no copy) and drop the staging folder."""
        assert self.path is not None
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / safe_filename(self.filename)
        try:
            os.replace(self.path, target)
        except OSError:  # another volume (workspace_dir is a junction, ...): fall back to a chunked move
            shutil.move(str(self.path), str(target))
        self.path = target
        self.discard(keep_file=True)
        return target

    def discard(self, keep_file: bool = False) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
            self._staging = None
        if not keep_file:
            self.path = None


def _sweep_stale(staging_root: Path) -> None:
    try:
        now = time.time()
        for d in staging_root.iterdir():
            if d.is_dir() and now - d.stat().st_mtime > STALE_STAGING_S:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


class _Part:
    __slots__ = ("name", "filename", "kind", "data")

    def __init__(self) -> None:
        self.name = ""
        self.filename: Optional[str] = None
        self.kind = "field"  # field | stream | small
        self.data = bytearray()


async def receive_multipart(request: Request, workspace_root: Path, *, stream_fields: tuple[str, ...] = ("file", "upload"), chunk_bytes: int = CHUNK_BYTES) -> Received:
    """Parse a multipart/form-data request. The first file part named in ``stream_fields`` is written to disk in
    chunks of ``chunk_bytes``; other file parts (rules file) are kept in memory up to ``MAX_SMALL_FILE_BYTES``;
    plain fields are returned in order. Raises ``UploadFailed`` (400 malformed / cancelled, 413 oversized side
    part, 507 disk full)."""
    _, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if not boundary:
        raise UploadFailed(400, "multipart request without a boundary")
    charset = params.get(b"charset", b"utf-8")
    charset = charset.decode("latin-1") if isinstance(charset, bytes) else str(charset)

    def dec(b: bytes | bytearray) -> str:
        try:
            return bytes(b).decode(charset)
        except (UnicodeDecodeError, LookupError):
            return bytes(b).decode("latin-1")

    workspace_root = Path(workspace_root)
    staging_root = workspace_root / STAGING_DIR
    staging_root.mkdir(parents=True, exist_ok=True)
    _sweep_stale(staging_root)
    out = Received()
    started = time.time()
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0

    # parser state (callbacks are synchronous: they only move bytes between buffers, disk I/O happens below)
    cur = _Part()
    hname = bytearray()
    hvalue = bytearray()
    disposition: list[bytes] = [b""]
    pending = bytearray()  # file bytes not yet on disk
    failure: list[UploadFailed] = []

    def on_part_begin() -> None:
        nonlocal cur
        cur = _Part()
        disposition[0] = b""

    def on_header_field(data: bytes, start: int, end: int) -> None:
        hname.extend(data[start:end])

    def on_header_value(data: bytes, start: int, end: int) -> None:
        hvalue.extend(data[start:end])

    def on_header_end() -> None:
        if bytes(hname).lower() == b"content-disposition":
            disposition[0] = bytes(hvalue)
        hname.clear()
        hvalue.clear()

    def on_headers_finished() -> None:
        _, opts = parse_options_header(disposition[0])
        if b"name" not in opts:
            failure.append(UploadFailed(400, 'a multipart part has no "name"'))
            return
        cur.name = dec(opts[b"name"])
        if b"filename" in opts:
            cur.filename = dec(opts[b"filename"])
            if cur.name in stream_fields and cur.filename and out.filename is None:
                cur.kind = "stream"
                out.filename = cur.filename
            else:
                cur.kind = "small"

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if failure:
            return
        if cur.kind == "stream":
            pending.extend(data[start:end])
            return
        limit = MAX_SMALL_FILE_BYTES if cur.kind == "small" else MAX_FIELD_BYTES
        if len(cur.data) + (end - start) > limit:
            failure.append(UploadFailed(413, f"the form part {cur.name!r} is larger than {_human(limit)}; only the data file ('file') may be large"))
            return
        cur.data.extend(data[start:end])

    def on_part_end() -> None:
        if failure:
            return
        if cur.kind == "field":
            out.fields.append((cur.name, dec(cur.data)))
        elif cur.kind == "small" and cur.filename:
            out.small_files[cur.name] = bytes(cur.data)

    parser = _mp.MultipartParser(boundary, {
        "on_part_begin": on_part_begin, "on_part_data": on_part_data, "on_part_end": on_part_end,
        "on_header_field": on_header_field, "on_header_value": on_header_value, "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
    })

    fh: Any = None

    async def flush(force: bool) -> None:
        """Write the buffered file bytes once a chunk is full (or at the end). Opens the staged file lazily."""
        nonlocal fh, pending
        if out.filename is None or (not pending and fh is not None) or (len(pending) < chunk_bytes and not force):
            return
        if fh is None:
            if declared:  # the body is a little larger than the file; refuse before filling the disk to the brim
                try:
                    free = shutil.disk_usage(staging_root).free
                except Exception:
                    free = None
                if free is not None and declared > free:
                    raise UploadFailed(507, _disk_full_message(workspace_root, declared, 0))
            out._staging = staging_root / uuid.uuid4().hex
            out._staging.mkdir(parents=True, exist_ok=True)
            out.path = out._staging / safe_filename(out.filename)
            fh = await run_in_threadpool(open, out.path, "wb")
        if pending:
            block, pending = pending, bytearray()  # hand the buffer over instead of copying it
            await run_in_threadpool(fh.write, block)
            out.size += len(block)

    async def close_file() -> None:
        nonlocal fh
        if fh is not None:
            f, fh = fh, None
            await run_in_threadpool(f.close)

    error: Optional[UploadFailed] = None
    try:
        async for chunk in request.stream():
            if error is not None:
                continue  # drain: the browser only reads the answer once its upload is through
            try:
                for i in range(0, len(chunk), FEED_BYTES):
                    parser.write(chunk[i:i + FEED_BYTES])
                    if failure:
                        raise failure[0]
                    await flush(False)
            except UploadFailed as e:
                error = e
            except OSError as e:
                error = UploadFailed(507, _disk_full_message(workspace_root, declared or None, out.size)) if _is_disk_full(e) else UploadFailed(500, f"could not store the upload: {e}")
            except Exception as e:  # malformed multipart (python-multipart raises its own error types)
                error = UploadFailed(400, f"malformed multipart upload: {e}")
            if error is not None:
                pending.clear()
                await close_file()
                out.discard()
        if error is None:
            try:
                parser.finalize()
                if failure:
                    raise failure[0]
                await flush(True)
            except UploadFailed as e:
                error = e
            except OSError as e:
                error = UploadFailed(507, _disk_full_message(workspace_root, declared or None, out.size)) if _is_disk_full(e) else UploadFailed(500, f"could not store the upload: {e}")
            except Exception as e:
                error = UploadFailed(400, f"malformed multipart upload: {e}")
    except ClientDisconnect:
        error = UploadFailed(400, "the upload was cancelled before it finished; nothing was kept")
    finally:
        await close_file()
    if error is not None:
        out.discard()
        raise error
    out.seconds = time.time() - started
    return out
