"""update_in_progress(): the supervisor stands down while Hermes updates.

Run directly: .venv/Scripts/python.exe test_update_marker.py
"""
import os
import tempfile
import time
from pathlib import Path

import desktop_supervisor as ds

MARKER = ".hermes-update-in-progress"


def _write(home: Path, pid, started_at):
    (home / MARKER).write_text(f"{pid}\n{started_at}\n", encoding="utf-8")


def demo():
    home = Path(tempfile.mkdtemp())
    ds.HERMES_HOME = home

    assert ds.update_in_progress() is False                      # no marker

    _write(home, os.getpid(), time.time())                       # live update
    assert ds.update_in_progress() is True

    _write(home, os.getpid(), time.time() - (21 * 60))           # past the ceiling
    assert ds.update_in_progress() is False, "stale marker must not wedge services off"

    _write(home, 999_999_999, time.time())                       # dead pid
    assert ds.update_in_progress() is False, "crashed updater must not wedge services off"

    (home / MARKER).write_text("garbage\n", encoding="utf-8")    # malformed
    assert ds.update_in_progress() is False
    print("ok")


if __name__ == "__main__":
    demo()
