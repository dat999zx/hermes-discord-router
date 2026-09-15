"""update_in_progress(): the supervisor stands down while Hermes updates.

Run directly: .venv/Scripts/python.exe test_update_marker.py
"""
import os
import subprocess
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

    _no_start_during_update(home)
    print("ok")


def _no_start_during_update(home: Path):
    """The exit-2 race: desktop kills the gateway ~1s BEFORE claiming the marker.

    start_gateway() must re-check the marker itself, or the heal that was decided
    while no marker existed still spawns a gateway the updater then trips over.
    """
    spawned = []
    real_run, real_procs = subprocess.run, ds.gateway_procs
    try:
        subprocess.run = lambda *a, **k: spawned.append(a)
        ds.gateway_procs = lambda: []                 # gateway is down

        _write(home, os.getpid(), time.time())        # marker appeared meanwhile
        ds.start_gateway()
        assert spawned == [], f"started the gateway mid-update: {spawned!r}"

        (home / MARKER).unlink()                      # update finished
        ds.start_gateway()
        assert spawned, "must still heal a genuinely crashed gateway"
    finally:
        subprocess.run, ds.gateway_procs = real_run, real_procs


if __name__ == "__main__":
    demo()
