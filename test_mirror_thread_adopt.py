"""Mirror threads stay reachable while the gateway runs: the patched gateway asks
discord_router on a miss instead of trusting its startup snapshot.

Run with Hermes's Python so gateway.session imports:
  <hermes python> test_mirror_thread_adopt.py
"""
import json, os, sys, tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
home = Path(tempfile.mkdtemp(prefix="hdr_adopt_"))
os.environ["HERMES_HOME"] = str(home)
agent = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent"
sys.path.insert(1, str(agent))

import discord_router as dr
from gateway.session_persistence import SessionPersistenceMixin  # noqa: F401  (import check)


class FakeStore:
    def __init__(self):
        self._entries = {}

    from gateway.session_persistence import SessionPersistenceMixin as _M
    _routing_entry_from_json = staticmethod(_M._routing_entry_from_json)


def main() -> None:
    # no map file -> nothing adopted
    assert dr.mirror_session_for_thread("111") is None

    (home / "desktop_mirror_threads.json").write_text(json.dumps({"sess_desk": "111"}))
    assert dr.mirror_session_for_thread("111") == "sess_desk"
    assert dr.mirror_session_for_thread("222") is None

    s = FakeStore()
    key = "agent:main:discord:thread:111:111"
    e = dr.adopt_mirror_route(s, key)
    assert e is not None and e.session_id == "sess_desk", e
    assert s._entries[key] is e
    assert e.origin.thread_id == "111" and e.origin.chat_type == "thread"

    # unknown thread, non-thread keys, mismatched ids -> untouched
    for k in ("agent:main:discord:thread:222:222", "agent:main:discord:group:111",
              "agent:main:discord:thread:9:111"):
        assert dr.adopt_mirror_route(s, k) is None, k
    assert list(s._entries) == [key]

    # a thread the mirror creates later is seen without restarting (mtime reload)
    import time; time.sleep(0.05)
    p = home / "desktop_mirror_threads.json"
    p.write_text(json.dumps({"sess_desk": "111", "sess_two": "333"}))
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert dr.mirror_session_for_thread("333") == "sess_two"

    # installer anchors still exist in the live Hermes files exactly once (or are applied)
    import install
    for f, marker, anchor in (
        (agent / "plugins/platforms/discord/adapter.py", install.MIRROR_ADAPTER_MARKER, install.MIRROR_ADAPTER_ANCHOR),
        (agent / "gateway/session.py", install.MIRROR_SESSION_MARKER, install.MIRROR_SESSION_ANCHOR),
    ):
        t = f.read_text(encoding="utf-8")
        assert marker in t or t.count(anchor) == 1, f"anchor drift in {f.name}"
    print("ok")


if __name__ == "__main__":
    main()
