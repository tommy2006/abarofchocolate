"""Zips the frozen app folder into the setup payload and writes payload.json (version, size, file count).

    python packaging/windows/pack_payload.py <app_dir> <app.zip> <payload.json>
"""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    app_dir, zip_path, info_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    from tpm import __version__

    files = [p for p in sorted(app_dir.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
    total = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            z.write(p, p.relative_to(app_dir).as_posix())
            total += p.stat().st_size
    info = {"version": __version__, "bytes": total, "files": len(files)}
    info_path.write_text(json.dumps(info), encoding="utf-8")
    print(f"packed {len(files)} files, {total / 1e6:.0f} MB -> {zip_path.stat().st_size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
