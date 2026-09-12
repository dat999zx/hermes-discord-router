"""Routing must never BLANK a session's cwd.

The gateway passes this function's return straight into set_session_vars(cwd=...),
which sets the ContextVar that every project-aware consumer (knowl, context files,
terminal) reads. Returning None/"" for an unmapped channel therefore does not mean
"leave it alone" -- it means "this session has no project", and a Discord thread
whose parent channel is not in the map silently loses the project it was started in.
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discord_router as dr


class _Source:
    def __init__(self, chat_id="", parent_chat_id=""):
        self.chat_id = chat_id
        self.parent_chat_id = parent_chat_id


class _Ctx:
    def __init__(self, chat_id="", parent_chat_id="", session_id=""):
        self.source = _Source(chat_id, parent_chat_id)
        self.session_id = session_id


def _make_state_db(home: Path, session_key: str, cwd: str) -> None:
    conn = sqlite3.connect(str(home / "state.db"))
    conn.execute("CREATE TABLE sessions (id TEXT, session_key TEXT, cwd TEXT)")
    conn.execute(
        "INSERT INTO sessions (id, session_key, cwd) VALUES (?, ?, ?)",
        (session_key, session_key, cwd),
    )
    conn.commit()
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    proj = "D:/Unity/projects/House Moving Company"
    other = "D:/coding/knowl"
    sess = "agent:main:discord:thread:999:999"
    _make_state_db(tmp, sess, proj)
    os.environ["HERMES_HOME"] = str(tmp)

    # channel 111 -> knowl; the thread 999 and its parent 222 are NOT mapped
    os.environ["DISCORD_CHANNEL_PROJECTS"] = json.dumps({"111": other})

    # 1. A mapped channel routes to its project. The whole point of the tool.
    assert dr.route_discord_channel(_Ctx(chat_id="111", session_id=sess), None) == other

    # 2. A thread inherits its PARENT channel's project.
    assert dr.route_discord_channel(
        _Ctx(chat_id="999", parent_chat_id="111", session_id=sess), None) == other

    # 3. THE BUG: an unmapped channel must NOT blank the session's cwd.
    #    Before the fix this returned None, and the gateway then called
    #    set_session_vars(cwd="") -- so knowl fell back to os.getcwd() (the
    #    hermes source tree or the user's home) and wrote to the global store.
    assert dr.route_discord_channel(
        _Ctx(chat_id="999", parent_chat_id="222", session_id=sess), None) == proj

    # 4. Same when routing is not configured at all.
    os.environ.pop("DISCORD_CHANNEL_PROJECTS")
    assert dr.route_discord_channel(_Ctx(chat_id="999", session_id=sess), None) == proj

    # 5. Malformed config must not blank it either.
    os.environ["DISCORD_CHANNEL_PROJECTS"] = "{not json"
    assert dr.route_discord_channel(_Ctx(chat_id="999", session_id=sess), None) == proj

    # 6. A genuinely unknown session still yields None, so a brand-new session
    #    keeps Hermes's own default rather than inheriting someone else's project.
    assert dr.route_discord_channel(_Ctx(chat_id="999", session_id="nope"), None) is None
    assert dr.route_discord_channel(_Ctx(chat_id="999"), None) is None

    print("all ok")


if __name__ == "__main__":
    main()
