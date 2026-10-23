"""End-to-end smoke test: scan -> analyze -> pair over the real
tests/fixtures/epubs/ fixtures (4 tiny metadata-only stub files, including a
deliberately malformed broken_book.epub and an EN/JP Harry Potter pair).

This isn't a numeric-behavior pin like the other test modules -- it exercises
the real wiring between scanner.py, analyze.py and pairing.py end to end, and
would catch e.g. a broken zip crashing the pipeline instead of degrading
gracefully.

All 4 fixtures are metadata-only stubs with an empty <spine/> or no
extractable text, so `analyze` never actually tokenizes anything here --
every book lands on the `no_text` path. That's what keeps this test fast.
"""
from __future__ import annotations

from epublib import analyze, pairing, scanner

from conftest import SAMPLE_EPUBS


def test_scan_analyze_pair_smoke(cfg):
    cfg.epub_dir = SAMPLE_EPUBS

    library = scanner.scan(cfg)
    assert len(library) == 4
    languages = {b["language"] for b in library.values()}
    assert languages == {"ja", "en"}
    # The malformed EPUB doesn't crash the scan -- it still gets an entry
    # (falling back to the filename), just with no metadata.
    broken = next(b for b in library.values() if b["path"] == "broken_book.epub")
    assert broken["language"] == "en"

    vocab = analyze.analyze(cfg)
    assert len(vocab) == 4
    # No real spine text in any of these stub fixtures -- every book hits the
    # no_text path, and the bad zip doesn't raise.
    assert {e["status"] for e in vocab.values()} == {"no_text"}
    assert all(e.get("error") is None for e in vocab.values())

    pairings = pairing.pair(cfg)
    # Author score (J.K. Rowling / J.K. ローリング) doesn't clear the strict
    # AUTHOR_AUTO auto-pair threshold (0.9) -- confirmed real behavior, not
    # an assumption -- so nothing auto-pairs here.
    assert pairings == {}

    # But it does clear the review-page's lower SUGGEST_MIN (0.45) bar, so
    # the /pair UI would surface it as the JP book's top (and only)
    # candidate for manual confirmation.
    suggestions = pairing.compute_suggestions(cfg)
    jp_hp = next(
        s for s in suggestions["suggestions"]
        if s["jp"]["title"] == "ハリー・ポッターと賢者の石"
    )
    assert len(jp_hp["candidates"]) == 1
    assert jp_hp["candidates"][0]["title"] == "Harry Potter and the Philosopher's Stone"
    assert jp_hp["candidates"][0]["author_score"] >= pairing.SUGGEST_MIN
