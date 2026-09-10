"""hermes_channels(): mirror takes its channel list from hermes's config.yaml."""
import tempfile
from pathlib import Path

from desktop_mirror import hermes_channels

CFG = """
discord:
  channels:
    - id: '111'
      project: C:/Users/Admin
    - id: 222
      folder: D:/coding/knowl
    - {no_id: true}
"""


def demo():
    d = Path(tempfile.mkdtemp())
    assert hermes_channels(d) == []  # no config.yaml -> empty, caller falls back

    (d / "config.yaml").write_text(CFG, encoding="utf-8")
    got = hermes_channels(d)
    assert got == [
        {"id": "111", "cwd": None, "project": "C:/Users/Admin"},
        {"id": "222", "cwd": "D:/coding/knowl", "project": None},
    ], got

    (d / "config.yaml").write_text("discord: {}\n", encoding="utf-8")
    assert hermes_channels(d) == []
    print("ok")


if __name__ == "__main__":
    demo()
