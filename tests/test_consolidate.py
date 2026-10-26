"""consolidate's destination paths: the catalog's `new_path`, or the same layout derived."""
from epublib.consolidate import _dest_rel


def test_a_row_with_new_path_keeps_it():
    row = {"new_path": "[A] S/[01] T [B0X].m4b", "title": "ignored"}
    assert _dest_rel("B0X", row) == "[A] S/[01] T [B0X].m4b"


def test_a_row_without_new_path_gets_the_same_layout():
    row = {"title": "都会のトム＆ソーヤ 2", "author": "はやみね かおる", "series": "都会のトム＆ソーヤ", "order": "02"}
    assert _dest_rel("B01LA6G11E", row) == "[はやみね かおる] 都会のトム＆ソーヤ/[02] 都会のトム＆ソーヤ 2 [B01LA6G11E].m4b"


def test_a_standalone_has_no_order_prefix_and_unsafe_characters_are_dropped():
    row = {"title": "Who/What?", "author": "", "series": None, "order": None}
    assert _dest_rel("B0X", row) == "Who What/Who What [B0X].m4b"


def test_a_book_without_a_key_has_no_bracketed_key():
    row = {"title": "T", "author": "A", "series": "S", "order": "01"}
    assert _dest_rel(None, row) == "[A] S/[01] T.m4b"


def test_the_same_row_can_name_a_folder_or_another_extension():
    row = {"title": "T", "author": "A", "series": "S", "order": "01"}
    assert _dest_rel("B0X", row, folder=True) == "[A] S/[01] T [B0X]"
    assert _dest_rel("B0X", row, ext=".flac") == "[A] S/[01] T [B0X].flac"
    # new_path is the catalog's own and wins; only its extension follows the file.
    hand = {"new_path": "Hand/Made [B0X].m4b", "title": "ignored"}
    assert _dest_rel("B0X", hand, folder=True) == "Hand/Made [B0X]"
    assert _dest_rel("B0X", hand, ext=".opus") == "Hand/Made [B0X].opus"


def test_portable_names_fit_every_filesystem():
    long_jp = "長" * 120  # 360 bytes: over the 255 most filesystems allow a name
    parts = _dest_rel("B0X", {"title": long_jp, "author": "著" * 90, "series": long_jp}, portable=True).split("/")
    assert all(len(p.encode()) <= 255 for p in parts) and parts[1].endswith(" [B0X].m4b")
    # No dot-name (the scanner skips them), no device name, no trailing dot, never empty or `..`.
    for title, expected in [(".hack", "_.hack/_.hack.m4b"), ("CON", "_CON/_CON.m4b"), ("..", "_/_...m4b"),
                            ("Ends.", "Ends/Ends..m4b")]:
        assert _dest_rel(None, {"title": title}, portable=True) == expected, title
    # Without the flag nothing about a name changes.
    assert _dest_rel(None, {"title": ".hack"}) == ".hack/.hack.m4b"
