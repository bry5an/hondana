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
