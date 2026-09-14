"""Mirror listing filter + deletion sweep.

Run directly: <hermes venv>/python.exe test_sweep_and_filter.py
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import desktop_mirror as dm

HERMES_HOME = Path(dm.os.environ.get("LOCALAPPDATA", "")) / "hermes"


def _db(path: Path):
    """Real sessions schema (Hermes's listable predicate touches parent columns),
    minimal messages table."""
    src = sqlite3.connect(f"file:{HERMES_HOME / 'state.db'}?mode=ro", uri=True)
    ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()[0]
    src.close()
    c = sqlite3.connect(path)
    c.execute(ddl)
    c.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                 content TEXT, timestamp REAL, platform_message_id TEXT, active INT DEFAULT 1)""")
    rows = [
        ("root", "desktop", None, 0),
        ("child", "subagent", "root", 0),   # subagent run
        ("krow", "kanban", None, 0),        # machine source
        ("old", "desktop", None, 1),        # archived
    ]
    c.executemany(
        "INSERT INTO sessions (id, source, cwd, parent_session_id, archived, started_at) "
        "VALUES (?,?,'C:/proj',?,?,0)",
        rows)
    c.executemany(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,0)",
        [(i, sid, "user", "hi") for i, sid in enumerate(["root", "child", "krow", "old"], start=1)],
    )
    c.commit()
    c.close()


class _FakeResp:
    def __init__(self, code): self.status_code, self.ok = code, code < 300


class _FakeHTTP:
    """404 for 'dead' threads, 200 otherwise; records deletes."""
    def __init__(self, dead): self.dead, self.deleted = dead, []
    def get(self, url, **kw): return _FakeResp(404 if url.rsplit("/", 1)[-1] in self.dead else 200)
    def delete(self, url, **kw):
        self.deleted.append(url.rsplit("/", 1)[-1])
        return _FakeResp(204)


def _mirror(home: Path) -> dm.Mirror:
    m = dm.Mirror.__new__(dm.Mirror)
    m.hermes_home, m.state_db = home, home / "state.db"
    m.listable_sql = dm._listable_child_sql(HERMES_HOME)
    m.cursor, m.session_threads = 0, {}
    m.thread_map_path = home / "threads.json"
    m._guild_cache = ""  # no guild -> idle-archive pass is a no-op in tests
    m.cwd_to_channel = {dm.Mirror._norm("C:/proj"): "CH"}
    return m


def demo():
    home = Path(tempfile.mkdtemp())
    _db(home / "state.db")
    m = _mirror(home)

    # 1. Only the human-visible session's turns are fetched.
    got = {t["session_id"] for t in m._fetch_new_turns()}
    assert got == {"root"}, got

    # 2. Sweep: session gone/not-listable -> its thread is deleted.
    m.session_threads = {"root": "T_ROOT", "child": "T_CHILD", "vanished": "T_GONE"}
    m.http = _FakeHTTP(dead=set())
    m._sweep_deletions()
    assert sorted(m.http.deleted) == ["T_CHILD", "T_GONE"], m.http.deleted
    assert m.session_threads == {"root": "T_ROOT"}, m.session_threads
    assert json.loads(m.thread_map_path.read_text()) == {"root": "T_ROOT"}

    # 3. Sweep: thread 404s in Discord -> the Hermes session is deleted.
    calls = []
    m._delete_session = calls.append
    m.http = _FakeHTTP(dead={"T_ROOT"})
    m._sweep_deletions()
    assert calls == ["root"], calls
    assert m.session_threads == {}, m.session_threads

    # 4. A network error is NOT a 404: nothing is deleted.
    class Boom(_FakeHTTP):
        def get(self, *a, **k): raise RuntimeError("network")
    m.session_threads, calls[:] = {"root": "T_ROOT"}, []
    m.http = Boom(dead=set())
    m._sweep_deletions()
    assert calls == [] and m.session_threads == {"root": "T_ROOT"}

    # 5. Idle picker: only unarchived, in-channel, stale threads are selected.
    def tid(days_ago): return str((int((dm.time.time() - days_ago * 86400) * 1000) - 1420070400000) << 22)
    threads = [
        {"id": tid(10), "parent_id": "CH", "last_message_id": tid(10), "thread_metadata": {}},
        {"id": tid(10), "parent_id": "OTHER", "last_message_id": tid(10), "thread_metadata": {}},
        {"id": tid(10), "parent_id": "CH", "last_message_id": tid(10), "thread_metadata": {"archived": True}},
        {"id": tid(1), "parent_id": "CH", "last_message_id": tid(1), "thread_metadata": {}},
        {"id": tid(9), "parent_id": "CH", "last_message_id": None, "thread_metadata": {}},  # empty, uses id
    ]
    cutoff = dm.time.time() - dm._IDLE_ARCHIVE_DAYS * 86400
    picked = dm._idle_thread_ids(threads, {"CH"}, cutoff)
    assert picked == [threads[0]["id"], threads[4]["id"]], picked
    print("ok")


if __name__ == "__main__":
    demo()
