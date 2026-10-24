# PyInstaller spec for the desktop app (onedir).
#
#   uv sync --extra desktop --group build
#   uv run python packaging/make_icons.py packaging/build
#   uv run pyinstaller packaging/hondana.spec --noconfirm
#
# macOS gets dist/Hondana.app (a menu-bar app, no Dock icon). Windows gets dist/Hondana/
# with hondana.exe (windowed, the tray app) and hondana-cli.exe (console, for the command
# line and `--self-test`, whose output a windowed program has nowhere to print).
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import (collect_all, collect_submodules, copy_metadata)

ROOT = Path(SPECPATH).parent
BUILD = ROOT / "packaging" / "build"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
MAC, WIN = sys.platform == "darwin", sys.platform == "win32"

datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]
binaries = []
# `uvicorn.run("epublib.server:app")` names the app by string, so the analysis can't see it.
hiddenimports = collect_submodules("epublib") + collect_submodules("uvicorn") + ["pystray"]
hiddenimports += ["pystray._win32"] if WIN else ["pystray._darwin", "AppKit", "Foundation", "objc"] if MAC else []

# Packages with data files, compiled extensions or imports by name: take everything.
# (spaCy and its English model are the bulk of the download.)
for package in ("spacy", "thinc", "en_core_web_sm", "srsly", "cymem", "preshed", "murmurhash",
                "catalogue", "wasabi", "weasel", "confection", "langcodes", "language_data",
                "fugashi", "unidic_lite", "ipadic", "MeCab", "pykakasi", "wordfreq",
                "zeroconf", "mutagen", "genanki", "segno"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:  # a platform without it (the build still fails loudly at --self-test)
        print(f"collect_all({package}) skipped: {exc}")
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
datas += copy_metadata("hondana")  # `importlib.metadata.version("hondana")` is the About row

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "ruff"],
)
pyz = PYZ(a.pure)

icon = str(BUILD / ("hondana.icns" if MAC else "hondana.ico"))
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Hondana" if MAC else "hondana",
          console=False, icon=icon)
exes = [gui]
if WIN:
    exes.append(EXE(pyz, a.scripts, [], exclude_binaries=True, name="hondana-cli", console=True, icon=icon))
coll = COLLECT(*exes, a.binaries, a.datas, name="Hondana")

if MAC:
    app = BUNDLE(
        coll, name="Hondana.app", icon=icon, bundle_identifier="io.github.bry5an.hondana",
        info_plist={
            "CFBundleName": "Hondana",
            "CFBundleDisplayName": "Hondana",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # a menu-bar app: no Dock icon
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )
