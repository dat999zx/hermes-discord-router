"""Mirrored turns are laid out as separated blocks and round-trip for dedupe."""
import desktop_mirror as dm


def test_user_turn_has_divider_header_and_plain_body():
    out = dm.format_turn("user", "first line\n\nthird line")
    assert out.splitlines() == [dm._DIVIDER, dm._USER_HEADER, "first line", "", "third line"]


def test_reply_opens_with_spacer_and_header_only_on_first_of_a_run():
    assert dm.format_turn("assistant", "hi", "user") == f"{dm._SPACER}\n{dm._ASSISTANT_HEADER}\nhi"
    assert dm.format_turn("assistant", "hi", None) == f"{dm._SPACER}\n{dm._ASSISTANT_HEADER}\nhi"
    assert dm.format_turn("assistant", "more", "assistant") == "more"


def test_unformat_round_trips():
    for role, text, prev in [("user", "a\n\nb", None), ("assistant", "x\ny", "user"),
                             ("assistant", "z", "assistant")]:
        assert dm.unformat_turn(dm.format_turn(role, text, prev)) == text
    assert dm.unformat_turn("**You:** legacy") == "legacy"
    quoted = f"{dm._DIVIDER}\n{dm._USER_HEADER}\n> a\n>\n> b"  # earlier quoted layout
    assert dm.unformat_turn(quoted) == "a\n\nb"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
