# PyInstaller spec for Zachary (Phase 8): one folder, one executable ("Zachary").
#   pyinstaller packaging/zachary.spec --noconfirm
# The executable runs the assistant by default; "Zachary indexer ..." / "Zachary cli ..."
# run the other parts (zachary/__main__.py, zachary/launch.py).
# Models are not bundled: they download on first use into the data folder.
# ruff: noqa
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH is provided by PyInstaller)
WIN = sys.platform == "win32"

datas = [(str(ROOT / "zachary" / "ui" / "assets"), "zachary/ui/assets")]
datas += collect_data_files("model2vec")
datas += collect_data_files("vosk")
import sqlite_vec

# sqlite-vec loads its extension from its own package folder: put it exactly there.
binaries = [(sqlite_vec.loadable_path() + (".dll" if WIN else ".so"), "sqlite_vec")]
for pkg in ("vosk", "pywhispercpp", "_sounddevice_data"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass
hiddenimports = collect_submodules("zachary") + [
    "sqlite_vec", "model2vec", "send2trash", "websockets", "httpx", "anthropic",
    "sklearn.cluster", "sklearn.utils._typedefs", "docx", "pptx", "openpyxl", "pymupdf",
    "watchdog.observers",
] + ([] if WIN else ["jeepney", "jeepney.io.blocking"])
excludes = [
    "tkinter", "pytest", "IPython", "matplotlib", "pandas.tests", "numpy.tests", "onnxruntime",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "zachary" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Zachary",
    console=not WIN,  # Windows: a windowed app (no console window)
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Zachary", upx=False)  # noqa: F821
