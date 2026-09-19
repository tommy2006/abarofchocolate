# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build of the Windows app: one folder with NorrinTPM.exe (windowed) and NorrinTPM-cli.exe (console).
# Build with packaging\windows\build.ps1 (runs this from the repository root).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
HERE = Path(SPECPATH).resolve()


def tpm_modules():
    """Pipeline stages are resolved by dotted path at run time (tpm.pipeline.STAGES): name every module."""
    out = []
    for p in (ROOT / "tpm").rglob("*.py"):
        rel = p.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out.append(".".join(parts))
    return sorted(set(out))


def tpm_data():
    """Static UI, prompt templates, report templates, translations: every non-Python file of the package."""
    out = []
    for p in (ROOT / "tpm").rglob("*"):
        if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts:
            out.append((str(p), str(p.parent.relative_to(ROOT))))
    return out


datas = tpm_data()
# every config file the app reads: settings.yaml, failure_signatures.yaml (live monitor), rules.example.md (demo rules)
datas += [(str(p), "config") for p in (ROOT / "config").iterdir() if p.is_file() and p.suffix in (".yaml", ".yml", ".md")]
datas += [(str(p), "samples") for p in (ROOT / "samples").iterdir() if p.is_file()]
datas += [(str(p), "samples/labels") for p in (ROOT / "samples" / "labels").glob("*") if p.is_file()]
datas += [(str(ROOT / ".env.example"), "."), (str(HERE / "norrin_tpm.ico"), "packaging/windows")]
for doc in ("README.md", "DATAFLOW.md"):
    if (ROOT / doc).exists():
        datas.append((str(ROOT / doc), "."))
datas += collect_data_files("pptx") + collect_data_files("reportlab") + collect_data_files("ruptures")

hiddenimports = tpm_modules()
hiddenimports += collect_submodules("uvicorn") + collect_submodules("ruptures") + collect_submodules("anthropic")
hiddenimports += ["multipart", "python_multipart", "dotenv", "yaml", "openpyxl", "httptools", "websockets", "h11",
                  "lightgbm", "duckdb", "pyarrow", "pyarrow.parquet", "pyarrow.csv", "pyarrow.dataset", "pyarrow.compute",
                  "sklearn.ensemble._iforest", "sklearn.covariance", "sklearn.neural_network", "sklearn.decomposition",
                  "sklearn.utils._typedefs", "sklearn.neighbors._partition_nodes", "scipy.special.cython_special",
                  "scipy.ndimage", "scipy.signal", "scipy.stats", "psutil", "jinja2.ext", "email.mime.multipart",
                  "email.mime.application", "smtplib", "sqlite3", "tkinter", "tkinter.ttk", "tkinter.messagebox"]

# plotly.min.js ships inside tpm/api/static/vendor (build.ps1 makes sure it exists): the Python package is not needed
excludes = ["plotly", "pytest", "_pytest", "IPython", "matplotlib", "notebook", "PyInstaller", "pypdf", "tkinter.test",
            "pandas.tests", "numpy.tests", "scipy.tests", "sklearn.tests"]

a = Analysis(
    [str(HERE / "entry_desktop.py"), str(HERE / "entry_cli.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

desktop = EXE(
    pyz,
    [s for s in a.scripts if "entry_cli" not in s[0]],
    [],
    exclude_binaries=True,
    name="NorrinTPM",
    icon=str(HERE / "norrin_tpm.ico"),
    console=False,
    upx=False,
)
cli = EXE(
    pyz,
    [s for s in a.scripts if "entry_desktop" not in s[0]],
    [],
    exclude_binaries=True,
    name="NorrinTPM-cli",
    icon=str(HERE / "norrin_tpm.ico"),
    console=True,
    upx=False,
)
coll = COLLECT(desktop, cli, a.binaries, a.datas, strip=False, upx=False, name="NorrinTPM")
