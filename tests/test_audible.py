"""The Audible client (audible.py): stores and links, ASINs from pasted text,
search with its ten-minute cache and fallback, and errors a person can read.
`requests.get` is faked throughout: nothing here touches the network."""
import pytest
import requests

from epublib import audible


class Reply:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body if body is not None else {}, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def web(monkeypatch):
    """Fake requests.get: `web.replies` is consumed in order (an Exception is raised);
    every call is recorded in `web.calls` as (url, params, headers)."""
    class Web:
        replies: list = []
        calls: list = []

    def fake_get(url, params=None, timeout=None, headers=None):
        Web.calls.append((url, dict(params or {}), headers or {}, timeout))
        out = Web.replies.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    Web.replies, Web.calls = [], []
    monkeypatch.setattr(audible.requests, "get", fake_get)
    monkeypatch.setattr(audible, "_cache", {})
    return Web


def book(asin, title="本", runtime=60):
    return {"asin": asin, "title": title, "runtime_length_min": runtime}


# ── stores and links ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("code, api, site", [
    ("jp", "https://api.audible.co.jp/1.0/catalog/products", "https://www.audible.co.jp"),
    ("us", "https://api.audible.com/1.0/catalog/products", "https://www.audible.com"),
    ("uk", "https://api.audible.co.uk/1.0/catalog/products", "https://www.audible.co.uk"),
    ("au", "https://api.audible.com.au/1.0/catalog/products", "https://www.audible.com.au"),
])
def test_each_store_has_its_own_api_and_site(code, api, site):
    assert audible.market(code).api == api and audible.market(code).site == site


def test_the_japanese_store_is_the_default_and_a_code_is_case_blind():
    assert audible.pd_url("B0CCN9M612") == "https://www.audible.co.jp/pd/B0CCN9M612"
    assert audible.pd_url("B0CCN9M612", "US") == "https://www.audible.com/pd/B0CCN9M612"


def test_an_unknown_store_is_a_bad_argument():
    with pytest.raises(ValueError, match="Unknown Audible marketplace"):
        audible.market("zz")


def test_every_store_is_offered_by_the_settings_page():
    from epublib import settings

    key = settings.BY_NAME["audible.marketplace"]
    assert [c for c, _ in key.choices] == list(audible.MARKETPLACES)
    assert settings.coerce(key, " UK ", None) == "uk"
    with pytest.raises(settings.SettingError, match="must be one of"):
        settings.coerce(key, "moon", None)


def test_the_store_comes_from_the_file_the_overlay_or_the_environment(tmp_path, monkeypatch):
    from epublib import settings

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[audible]\nmarketplace = "uk"\n')
    monkeypatch.setenv("HONDANA_CONFIG", str(cfg_file))
    assert settings.load().audible_marketplace == "uk"
    monkeypatch.setenv("HONDANA_AUDIBLE_MARKETPLACE", "DE")
    loaded = settings.load()
    assert (loaded.audible_marketplace, loaded.sources["audible_marketplace"]) == ("de", "env")
    cfg_file.write_text('[audible]\nmarketplace = "moon"\n')
    monkeypatch.delenv("HONDANA_AUDIBLE_MARKETPLACE")
    with pytest.raises(SystemExit, match="must be one of"):
        settings.load()


# ── ASINs from what was pasted ───────────────────────────────────────────────

@pytest.mark.parametrize("text, asin", [
    ("B0CCN9M612", "B0CCN9M612"),
    ("  b0ccn9m612 ", "B0CCN9M612"),
    ("https://www.audible.co.jp/pd/%E5%A4%9C%E3%81%AE%E3%83%94%E3%82%AF%E3%83%8B%E3%83%83%E3%82%AF/B0CCN9M612?ref=a_x", "B0CCN9M612"),
    ("https://www.audible.com/pd/B07QMQHLVH", "B07QMQHLVH"),
    ("4101234567", "4101234567"),
    ("048650206X", "048650206X"),
])
def test_an_asin_is_found_in_a_bare_id_or_a_link(text, asin):
    assert audible.parse_asin(text) == asin


@pytest.mark.parametrize("text", ["", None, "夜のピクニック", "B0SHORT", "https://www.audible.com/series/12345678901234", "B0CCN9M6123456"])
def test_other_text_has_no_asin(text):
    assert audible.parse_asin(text) is None


# ── one product ──────────────────────────────────────────────────────────────

def test_a_product_is_fetched_from_the_chosen_store_with_an_honest_user_agent(web):
    web.replies = [Reply(body={"product": book("B0AAAAAAAA")})]
    assert audible.fetch_product("B0AAAAAAAA", "uk")["asin"] == "B0AAAAAAAA"
    url, params, headers, timeout = web.calls[0]
    assert url == "https://api.audible.co.uk/1.0/catalog/products/B0AAAAAAAA"
    assert params == {"response_groups": audible.RESPONSE_GROUPS}
    assert headers["User-Agent"].startswith("Hondana/") and "github.com/bry5an/hondana" in headers["User-Agent"]
    assert timeout == audible.TIMEOUT


def test_a_missing_product_is_none_not_an_error(web):
    web.replies = [Reply(404)]
    assert audible.fetch_product("B0AAAAAAAA") is None


@pytest.mark.parametrize("reply, message", [
    (requests.Timeout(), "didn't answer in time"),
    (requests.ConnectionError(), "Couldn't reach Audible"),
    (Reply(429), "limiting requests"),
    (Reply(503), r"error \(503\)"),
    (Reply(200, bad_json=True), "can't be read"),
])
def test_failures_say_what_happened(web, reply, message):
    web.replies = [reply]
    with pytest.raises(audible.AudibleError, match=message):
        audible.fetch_product("B0AAAAAAAA")


# ── search ───────────────────────────────────────────────────────────────────

def test_a_search_sends_what_was_given_and_drops_series_pages(web):
    web.replies = [Reply(body={"products": [book("B0AAAAAAAA"), book("B0BBBBBBBB", runtime=None),
                                            {"asin": "B0CCCCCCCC", "runtime_length_min": 5}]})]
    found = audible.search("夜の  ピクニック", "恩田 陸", marketplace="jp")
    assert [p["asin"] for p in found] == ["B0AAAAAAAA"]       # no runtime / no title: not books
    url, params, *_ = web.calls[0]
    assert url == "https://api.audible.co.jp/1.0/catalog/products"
    assert params["title"] == "夜の ピクニック" and params["author"] == "恩田 陸"
    assert "keywords" not in params and params["products_sort_by"] == "Relevance"
    assert params["num_results"] == audible.SEARCH_LIMIT


def test_no_hits_for_title_and_author_retries_with_the_title_alone(web):
    web.replies = [Reply(body={"products": []}), Reply(body={"products": [book("B0AAAAAAAA")]})]
    assert [p["asin"] for p in audible.search("題名", "表記ゆれ")] == ["B0AAAAAAAA"]
    assert "author" in web.calls[0][1] and "author" not in web.calls[1][1]
    assert len(web.calls) == 2


def test_a_title_nothing_matches_is_finally_tried_as_keywords(web):
    web.replies = [Reply(body={"products": []}), Reply(body={"products": []}),
                   Reply(body={"products": [book("B0AAAAAAAA")]})]
    assert [p["asin"] for p in audible.search("題名 (シリーズ 1)", "著者")] == ["B0AAAAAAAA"]
    assert [("title" in c[1], "author" in c[1], c[1].get("keywords")) for c in web.calls] == [
        (True, True, None), (True, False, None), (False, False, "題名 (シリーズ 1)")]


def test_keywords_given_by_hand_are_not_second_guessed(web):
    web.replies = [Reply(body={"products": []})]
    assert audible.search(keywords="何か") == [] and len(web.calls) == 1
    web.replies = [Reply(body={"products": []})]
    assert audible.search("題名", keywords="何か") == [] and len(web.calls) == 2   # no author to drop, no fallback


def test_an_empty_search_is_a_bad_argument_and_asks_nothing(web):
    with pytest.raises(ValueError, match="Give a title"):
        audible.search("  ", "", "")
    assert web.calls == []


def test_a_search_is_cached_for_ten_minutes_per_store_and_query(web, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(audible, "_now", lambda: clock[0])
    web.replies = [Reply(body={"products": [book("B0AAAAAAAA")]}) for _ in range(3)]
    audible.search("題名")
    audible.search("題名")                                 # served from the cache
    assert len(web.calls) == 1
    audible.search("題名", marketplace="us")               # another store is another query
    assert len(web.calls) == 2
    clock[0] += audible.CACHE_SECONDS - 1
    audible.search("題名")
    assert len(web.calls) == 2
    clock[0] += 2
    audible.search("題名")                                 # expired
    assert len(web.calls) == 3


def test_a_failed_search_is_not_cached(web):
    web.replies = [Reply(503), Reply(body={"products": [book("B0AAAAAAAA")]})]
    with pytest.raises(audible.AudibleError):
        audible.search("題名")
    assert [p["asin"] for p in audible.search("題名")] == ["B0AAAAAAAA"]


def test_the_cache_stays_bounded(web, monkeypatch):
    monkeypatch.setattr(audible, "CACHE_SIZE", 8)
    web.replies = [Reply(body={"products": []}) for _ in range(60)]   # a miss is asked twice
    for i in range(30):
        audible.search(f"題名{i}")
    assert len(audible._cache) <= 8
