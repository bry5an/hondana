"""`hondana --self-test`: does this installation work?

The release workflow runs it on every packaged artifact before publishing, and it is
the first thing to try when a user reports that the app opens and shows nothing. It
works in a scratch home folder (never the user's library) and checks, in order:

* the bundled `templates/` and `static/` are there;
* `hondana serve` starts on a free port and answers `/healthz`, `/` and a static file;
* a Japanese and an English EPUB scan and analyze (fugashi + UniDic, spaCy);
* wordfreq's Japanese tokenizer (MeCab + ipadic) and pykakasi's readings work.

Prints one line per check and returns 0 when all pass, else 1.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from . import desktop, procs
from .config import ROOT

# Enough text that neither book lands on analyze's `no_text` path (< 500 characters).
JA_TEXT = ("吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。"
           "何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している。") * 8
EN_TEXT = ("It was a bright cold day in April, and the clocks were striking thirteen. "
           "Winston Smith slipped quickly through the glass doors of Victory Mansions. ") * 8

_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
  <dc:language>{lang}</dc:language><dc:identifier id="id">urn:uuid:{uid}</dc:identifier>
 </metadata>
 <manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="c"/></spine>
</package>"""
_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def make_epub(path: Path, *, title: str, author: str, lang: str, text: str) -> None:
    body = f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>{text}</p></body></html>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", _OPF.format(title=title, author=author, lang=lang, uid=lang))
        zf.writestr("OEBPS/c.xhtml", body)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def check_assets() -> str:
    for rel in ("templates/index.html", "templates/partials/shell.html", "static/app.css", "static/ui.js"):
        if not (ROOT / rel).is_file():
            raise AssertionError(f"missing {rel} under {ROOT}")
    return str(ROOT)


def check_server(home: Path) -> str:
    port = _free_port()
    env = {**os.environ, "HONDANA_HOME": str(home), "HONDANA_HOST": "127.0.0.1", "HONDANA_PORT": str(port),
           "HONDANA_DISCOVERY_ENABLED": "0", "HONDANA_SUPERVISED": "1"}
    log = home / "serve.log"
    with open(log, "wb") as logf:
        proc = subprocess.Popen(desktop.server_command(), env=env, stdin=subprocess.DEVNULL, stdout=logf,
                                stderr=subprocess.STDOUT, **procs.popen_flags(hide_window=True))
    url = f"http://127.0.0.1:{port}"
    try:
        end = time.monotonic() + 90
        while not desktop.healthy(url):
            if proc.poll() is not None or time.monotonic() > end:
                tail = log.read_text(encoding="utf-8", errors="replace")[-600:]
                raise AssertionError(f"the server did not come up (exit {proc.poll()}): {tail}")
            time.sleep(0.3)
        for path in ("/", "/settings", "/static/app.css"):
            status, body = _get(url + path)
            if status != 200 or not body:
                raise AssertionError(f"GET {path} gave {status}")
    finally:
        procs.shutdown_process(proc, 8)
    return url


def check_analysis(home: Path) -> str:
    from . import analyze, scanner, settings

    books = home / "selftest-books"
    make_epub(books / "ja.epub", title="吾輩は猫である", author="夏目漱石", lang="ja", text=JA_TEXT)
    make_epub(books / "en.epub", title="Nineteen Eighty-Four", author="George Orwell", lang="en", text=EN_TEXT)
    data = home / "selftest-data"
    with mock.patch.dict(os.environ, {"HONDANA_HOME": str(home), "HONDANA_EPUB_DIR": str(books),
                                      "HONDANA_DATA_DIR": str(data)}):
        cfg = settings.load()
    library = scanner.scan(cfg)
    vocab = analyze.analyze(cfg)
    langs = sorted(b["language"] for b in library.values())
    if langs != ["en", "ja"]:
        raise AssertionError(f"scan read the languages as {langs}")
    bad = {library[i]["language"]: e.get("status") for i, e in vocab.items() if e.get("status") != "ok"}
    if bad or len(vocab) != 2:
        raise AssertionError(f"analyze did not finish both books: {bad or vocab}")
    return "fugashi/UniDic and spaCy"


def check_tokenizers() -> str:
    import wordfreq

    from . import romaji

    if wordfreq.zipf_frequency("猫", "ja") <= 0:
        raise AssertionError("wordfreq (MeCab + ipadic) found no Japanese frequency for 猫")
    if not any("murakami" in form for form in romaji.reading_forms("村上春樹")):
        raise AssertionError("pykakasi did not read 村上春樹 as murakami")
    return "wordfreq/ipadic and pykakasi"


def run() -> int:
    """Run every check; the exit code is 0 only if all passed."""
    failed = 0
    with tempfile.TemporaryDirectory(prefix="hondana-selftest-") as tmp:
        home = Path(tmp)
        checks: list[tuple[str, Callable[[], str]]] = [
            ("bundled files", check_assets),
            ("server", lambda: check_server(home)),
            ("analysis", lambda: check_analysis(home)),
            ("tokenizers", check_tokenizers),
        ]
        for name, check in checks:
            try:
                detail = check()
            except Exception as exc:  # report, then go on to the next check
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)
            else:
                print(f"ok   {name}: {detail}", flush=True)
    print("self-test passed" if not failed else f"self-test failed ({failed})", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
