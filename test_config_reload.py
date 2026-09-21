"""config_channels_mtime(): a config edit must recycle the mirror.

The mirror reads discord.channels ONCE at startup. Adding a channel to
config.yaml therefore routes it INBOUND (the gateway re-reads config) while
Desktop turns in that folder silently never mirror OUT, because the running
mirror still holds the old map. Nothing restarted it -- that gap is this bug.

Run directly: <system python> test_config_reload.py
"""
import tempfile
import time
from pathlib import Path

import desktop_supervisor as ds


def demo():
    home = Path(tempfile.mkdtemp())
    ds.HERMES_HOME = home
    cfg = home / "config.yaml"

    # No config yet -> 0.0, and never raises.
    assert ds.config_channels_mtime() == 0.0

    cfg.write_text("discord:\n  channels:\n    - id: '1'\n      project: D:/a\n", encoding="utf-8")
    first = ds.config_channels_mtime()
    assert first > 0.0

    # Unchanged config must NOT look changed, or the mirror restart-loops.
    assert ds.config_channels_mtime() == first

    # Adding a channel changes the mtime -> the poll loop recycles the mirror.
    time.sleep(0.01)
    cfg.write_text(
        "discord:\n  channels:\n    - id: '1'\n      project: D:/a\n"
        "    - id: '2'\n      project: D:/coding/reins\n", encoding="utf-8")
    assert ds.config_channels_mtime() != first, (
        "a config edit went undetected — a new channel would route inbound but "
        "never mirror out")
    print("ok")


if __name__ == "__main__":
    demo()
