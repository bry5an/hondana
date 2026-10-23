"""Shared test fixtures.

No config.toml is loaded — Config is a plain dataclass (epublib/config.py),
so tests build one directly rooted at a pytest tmp_path. All paths that real
stages read via index_store.load_json default missing files to {} (or the
given default), so unused indices simply need not exist on disk.
"""
from __future__ import annotations

import importlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

from epublib.config import Config

SAMPLE_EPUBS = Path(__file__).resolve().parent / "fixtures" / "epubs"

_CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_CONTENT_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Test Book</dc:title>
  <dc:language>ja</dc:language>
  <dc:identifier id="bookid">urn:uuid:test</dc:identifier>
 </metadata>
 <manifest>
  <item id="doc1" href="doc1.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine><itemref idref="doc1"/></spine>
</package>"""


def write_epub(path: Path, paragraphs: list[str]) -> None:
    """A minimal but real EPUB (single spine document, one <p> per
    paragraph) so tests can exercise epub_text.extract_text() against real
    zip/XHTML parsing instead of hand-faking extracted text."""
    body = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    doc = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + "</body></html>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", _CONTENT_OPF)
        zf.writestr("OEBPS/doc1.xhtml", doc)


def make_cfg(tmp_path: Path, **overrides) -> Config:
    data_dir = tmp_path / "data"
    defaults = dict(
        epub_dir=tmp_path / "epubs",
        data_dir=data_dir,
        audiobook_data=tmp_path / "data.json",
        audiobook_dirs=[],
        audiobook_natively=tmp_path / "natively_levels.json",
        host="127.0.0.1",
        port=8000,
        natively_delay=0.0,
        natively_min_score=0.55,
        pairing_min_score=0.5,
        xref_min_score=0.62,
        data_mirror_publish=False,
        data_mirror_path=tmp_path / "mirror",
        active_user="default",
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_cfg(tmp_path)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """epublib.server imported fresh over a throwaway config (data, epubs and
    catalog all under tmp_path — never the real data.json)."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'epub_dir = "{tmp_path / "epubs"}"\ndata_dir = "{tmp_path / "data"}"\n'
        # Never default to the real data.json: tests write the catalog.
        f'audiobook_data = "{tmp_path / "catalog.json"}"\n'
    )
    # The genre vocabulary is whatever the catalog carries.
    (tmp_path / "catalog.json").write_text(json.dumps({
        f"B0GENRE{i:04d}": {"title": f"t{i}", "genre": g}
        for i, g in enumerate(("ライトノベル", "ノンフィクション", "ホラー", "エロ"))}))
    monkeypatch.setenv("EPUB_LIBRARY_CONFIG", str(cfg_file))
    sys.modules.pop("epublib.server", None)
    mod = importlib.import_module("epublib.server")
    yield mod
    sys.modules.pop("epublib.server", None)
