"""cwd -> channel mapping, including the NULL-cwd (Home project) case.

Hermes stores NULL cwd for Desktop sessions that never explicitly picked a
workspace, so a bare `if not cwd: return None` silently dropped every Home
session from the mirror.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desktop_mirror as dm


def main() -> None:
    n = dm.Mirror._norm
    home = os.path.expanduser("~")
    # A project folder that is not the home dir, on whatever platform runs this.
    proj = os.path.join(os.path.sep, "srv", "code", "myproject")
    other = os.path.join(os.path.sep, "srv", "code", "unmapped")

    m = dm.Mirror.__new__(dm.Mirror)
    m.cwd_to_channel = {n(home): "home-chan", n(proj): "proj-chan"}

    # explicit project folder still wins
    assert m._channel_for_cwd(proj) == "proj-chan"
    assert m._channel_for_cwd(proj.replace("/", "\\")) == "proj-chan"  # either separator
    # the home dir named explicitly
    assert m._channel_for_cwd(home) == "home-chan"
    # THE BUG: NULL/empty cwd is a Desktop launch-dir session == Home project
    assert m._channel_for_cwd(None) == "home-chan"
    assert m._channel_for_cwd("") == "home-chan"
    # an unmapped folder is still unmapped (no accidental catch-all)
    assert m._channel_for_cwd(other) is None

    # with no home mapping configured, NULL must NOT fall into another channel
    m2 = dm.Mirror.__new__(dm.Mirror)
    m2.cwd_to_channel = {n(proj): "proj-chan"}
    assert m2._channel_for_cwd(None) is None
    assert m2._channel_for_cwd(proj) == "proj-chan"

    print("all ok")


if __name__ == "__main__":
    main()
