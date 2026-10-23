"""Natively enrichment: derived series passed to lookup, results stamped, misses
retried (not matches overwritten), manual entries untouched, orphans dropped."""
from datetime import datetime, timedelta, timezone

import pytest

from epublib import enrich, pipeline
from epublib.index_store import load_json, save_json

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


MISS = {"natively_level": None, "match_type": None}
Q = {"series": "S", "order": 2}


@pytest.mark.parametrize("entry, query, kw, want", [
    (None, Q, {}, True),                                                        # never looked up
    ({"natively_level": 20, "match_type": "item"}, Q, {}, False),               # a real match is final
    ({"natively_level": 20, "match_type": "manual_url"}, Q, {"retry_unmatched": True}, False),
    ({**MISS, "match_type": "manual_url"}, Q, {"retry_unmatched": True}, False),
    # misses
    ({**MISS, "fetched_at": iso(1), "query": Q}, Q, {}, False),                 # fresh, same inputs
    ({**MISS, "fetched_at": iso(1), "query": Q}, Q, {"retry": False}, False),
    ({**MISS, "fetched_at": iso(1), "query": Q}, Q, {"retry_unmatched": True}, True),  # forced
    ({**MISS, "fetched_at": iso(20), "query": Q}, Q, {}, True),                 # older than RETRY_DAYS
    ({**MISS, "fetched_at": iso(20), "query": Q}, Q, {"retry": False}, False),  # chain: never
    ({**MISS, "fetched_at": iso(1), "query": {"series": "", "order": 0}}, Q, {}, True),  # series learned since
    ({**MISS}, Q, {}, True),                                                    # cached before stamping
    ({**MISS, "fetched_at": "garbage"}, Q, {}, True),
    # series-level matches can be upgraded once, when the volume is known
    ({"natively_level": 22, "match_type": "series"}, Q, {}, True),
    ({"natively_level": 22, "match_type": "series"}, {"series": "S", "order": 0}, {}, False),
    ({"natively_level": 22, "match_type": "series", "query": Q}, Q, {}, False),  # already tried with an order
    ({"natively_level": 22, "match_type": "series"}, Q, {"retry": False}, False),
])
def test_wants_lookup(entry, query, kw, want):
    assert enrich.wants_lookup(entry, query, NOW, **kw) is want


def _setup(cfg, monkeypatch, books, cache=None, results=None):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {b["book_id"]: {"language": "ja", "title": b["book_id"], **b} for b in books})
    save_json(cfg.natively_index, cache or {})
    calls = []

    def fake(title, series="", order=0, **kw):
        calls.append((title, series, order))
        return (results or {}).get(title, {"natively_level": None, "match_type": None})

    monkeypatch.setattr(enrich.natively, "lookup", fake)
    return calls


def test_lookup_gets_the_derived_series_and_volume_and_the_result_is_stamped(cfg, monkeypatch):
    calls = _setup(cfg, monkeypatch, [
        {"book_id": "a", "series_name": "Derived", "series_order": "03", "series": "calibre"},
        {"book_id": "b", "series": "OnlyCalibre"},
        {"book_id": "c", "series_name": "Odd", "series_order": "2.5"},
        {"book_id": "d"},
        {"book_id": "en", "language": "en"},
    ], results={"a": {"natively_level": 21, "match_type": "item"}})
    cache = enrich.enrich(cfg)
    assert sorted(calls) == [("a", "Derived", 3), ("b", "OnlyCalibre", 0), ("c", "Odd", 0), ("d", "", 0)]
    assert "en" not in cache
    assert cache["a"]["natively_level"] == 21 and cache["a"]["query"] == {"series": "Derived", "order": 3}
    assert datetime.fromisoformat(cache["a"]["fetched_at"]).tzinfo is not None
    assert load_json(cfg.natively_index, {}) == cache


def test_a_second_run_leaves_fresh_misses_alone_but_a_forced_retry_does_not(cfg, monkeypatch):
    calls = _setup(cfg, monkeypatch, [{"book_id": "a"}, {"book_id": "b", "series_name": "S", "series_order": "1"}],
                   results={"b": {"natively_level": 18, "match_type": "item"}})
    enrich.enrich(cfg)
    calls.clear()
    enrich.enrich(cfg)
    assert calls == []
    enrich.enrich(cfg, retry_unmatched=True)
    assert calls == [("a", "", 0)]                           # only the miss; b stays matched


def test_a_retry_never_turns_a_match_into_a_miss_and_manual_entries_survive_refresh(cfg, monkeypatch):
    good = {"natively_level": 22, "match_type": "series", "natively_url": "u"}
    manual = {"natively_level": 30, "match_type": "manual_url", "natively_url": "hand"}
    calls = _setup(cfg, monkeypatch,
                   [{"book_id": "a", "series_name": "S", "series_order": "2"}, {"book_id": "m"}],
                   cache={"a": good, "m": manual})
    cache = enrich.enrich(cfg)                               # a is an upgradable series match; m is manual
    assert calls == [("a", "S", 2)]
    assert cache["a"]["natively_level"] == 22 and cache["a"]["natively_url"] == "u"   # kept, lookup found nothing
    assert cache["a"]["query"] == {"series": "S", "order": 2}                          # …but not retried forever
    calls.clear()
    cache = enrich.enrich(cfg, refresh=True)
    assert [c[0] for c in calls] == ["a"] and cache["m"] == manual                    # refresh skips the hand-set one


def test_entries_for_books_no_longer_in_the_library_are_dropped(cfg, monkeypatch):
    _setup(cfg, monkeypatch, [{"book_id": "a"}], cache={"a": {**MISS, "fetched_at": iso(0), "query": {"series": "", "order": 0}},
                                                        "gone": {"natively_level": 20}})
    assert set(enrich.enrich(cfg)) == {"a"}


def test_an_empty_library_does_not_wipe_the_cache(cfg, monkeypatch):
    _setup(cfg, monkeypatch, [], cache={"x": {"natively_level": 20}})
    assert set(enrich.enrich(cfg)) == {"x"}


# ── pipeline wiring ──────────────────────────────────────────────────────────

def test_retry_stage_is_a_standalone_action_not_part_of_all(monkeypatch):
    stage = pipeline.STAGE_BY_ID["enrich-unmatched"]
    assert not stage.in_chain and "enrich-unmatched" not in pipeline.CHAIN
    seen = []
    monkeypatch.setattr(pipeline.enrich_mod, "enrich", lambda cfg, *a, **k: seen.append(k))
    stage.run(None)
    assert seen == [{"retry_unmatched": True}]
    seen.clear()
    pipeline.STAGE_BY_ID["enrich"].run(None)                 # the ordinary stage retries aged misses
    assert seen == [{"refresh": False}]
    # (test_pipeline.test_run_all_… pins that `all` runs `enrich` exactly once)


def test_the_per_book_chain_only_looks_up_books_with_no_result(cfg, monkeypatch):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {"a": {"book_id": "a", "language": "ja"}})
    seen = []
    monkeypatch.setattr(pipeline.enrich_mod, "enrich", lambda c, *a, **k: seen.append(k))
    for mod, fn in ((pipeline.analyze_mod, "analyze"), (pipeline.pairing_mod, "pair"),
                    (pipeline.xref_mod, "xref_one"), (pipeline.build_mod, "build"),
                    (pipeline.epub_catalog_mod, "fetch_missing")):
        monkeypatch.setattr(mod, fn, lambda *a, **k: None)
    assert pipeline.run_book_chain(cfg, "a") == []
    assert seen == [{"retry": False}]


# ── audiobook catalog ──────────────────────────────────────────────────

LEGACY_MATCH = {"natively_level": 22, "match_type": "series", "natively_url": "u"}   # old scraper: no fetched_at
LEGACY_MISS = {"natively_level": None, "match_type": None, "natively_url": None}


def _catalog(cfg, monkeypatch, catalog, cache):
    save_json(cfg.audiobook_data, catalog)
    save_json(cfg.audiobook_natively, cache)
    calls = []

    def fake(title, series="", order=0, **kw):
        calls.append((title, series, order))
        return {"natively_level": 18, "match_type": "item", "natively_url": "new"}

    monkeypatch.setattr(enrich.natively, "lookup", fake)
    return calls


CATALOG = {
    "new": {"title": "[3巻] シリーズ", "series": "シリーズ", "order": "03"},
    "old_match": {"title": "A", "series": "A", "order": "02"},
    "old_miss": {"title": "B", "series": "", "order": ""},
}


def test_audiobook_enrich_looks_up_new_entries_and_old_misses_but_never_old_matches(cfg, monkeypatch):
    calls = _catalog(cfg, monkeypatch, CATALOG, {"old_match": LEGACY_MATCH, "old_miss": LEGACY_MISS})
    cache = enrich.enrich_audiobooks(cfg)
    assert sorted(calls) == [("B", "", 0), ("[3巻] シリーズ", "シリーズ", 3)]
    assert cache["old_match"] == LEGACY_MATCH                        # the scraper's match is final
    assert cache["new"]["natively_level"] == 18 and cache["new"]["query"] == {"series": "シリーズ", "order": 3}
    assert load_json(cfg.audiobook_natively, {}) == cache
    calls.clear()
    enrich.enrich_audiobooks(cfg)                                    # everything is now stamped, matched
    assert calls == []


def test_audiobook_refresh_refetches_everything_but_hand_set_entries(cfg, monkeypatch):
    manual = {"natively_level": 30, "match_type": "manual_url", "natively_url": "hand"}
    calls = _catalog(cfg, monkeypatch, {**CATALOG, "m": {"title": "M"}},
                     {"old_match": LEGACY_MATCH, "m": manual})
    cache = enrich.enrich_audiobooks(cfg, refresh=True)
    assert {c[0] for c in calls} == {"A", "B", "[3巻] シリーズ"}   # M is hand-set
    assert cache["m"] == manual


def test_audiobook_enrich_without_a_catalog_is_a_no_op(cfg):
    assert enrich.enrich_audiobooks(cfg) == {}


def test_audiobook_stage_is_standalone_and_takes_refresh(monkeypatch):
    stage = pipeline.STAGE_BY_ID["enrich-audiobooks"]
    assert not stage.in_chain and stage.refresh_help and "enrich-audiobooks" not in pipeline.CHAIN
    seen = []
    monkeypatch.setattr(pipeline.enrich_mod, "enrich_audiobooks", lambda c, **k: seen.append(k))
    stage.run(None, True)
    assert seen == [{"refresh": True}]
