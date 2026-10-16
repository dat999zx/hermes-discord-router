"""Drive the real polling loop against compaction-style SQLite rows; no Discord writes."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import desktop_mirror as dm


class StopPolling(Exception):
    pass


def check(with_uid=True, role="user"):
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
        home = Path(tmp)
        db = sqlite3.connect(home / "state.db")
        db.execute("CREATE TABLE sessions (id TEXT, source TEXT, cwd TEXT, chat_id TEXT, archived INT)")
        db.execute("INSERT INTO sessions VALUES ('chat', 'desktop', 'project', 'thread', 0)")
        uid_column = ", message_uid TEXT" if with_uid else ""
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                   "content TEXT, timestamp REAL, platform_message_id TEXT, active INT" + uid_column + ")")

        def add(row_id, text, uid, active=1):
            values = (row_id, "chat", role, text, 123, None, active)
            if with_uid:
                values += (uid,)
            db.execute("INSERT INTO messages VALUES (" + ",".join("?" * len(values)) + ")", values)

        add(1, "already delivered", "old", 0)
        add(2, "already delivered", "old")  # retained copy, new row id
        add(3, "already delivered", "new")  # legitimate repeated prompt, even same timestamp
        add(4, "no uid", None)
        add(5, "not consumed yet", "pending", 0)
        add(6, "not consumed yet", "pending")  # original was never past cursor: deliver it
        db.commit()
        db.close()

        m = dm.Mirror.__new__(dm.Mirror)
        m.state_db = home / "state.db"
        m.cursor_path = home / "cursor.json"
        m.cursor = 1
        m.listable_sql = "1"
        m.cwd_to_channel = {"project": "channel"}
        m.session_threads = {"chat": "thread"}
        m.discord_turn_threads = set()
        m.poll_s = 0
        m._sweep_deletions = lambda: None
        m._target_channel = lambda turn: "channel"
        m._thread_for_turn = lambda *args: "thread"
        sent = []
        m._send = lambda thread, content: sent.append(content)
        with patch.object(dm.time, "sleep", side_effect=StopPolling):
            try:
                m.run()
            except StopPolling:
                pass
        expected = ["**You:** already delivered", "**You:** no uid", "**You:** not consumed yet"]
        if not with_uid:
            expected.insert(0, "**You:** already delivered")
        if role == "assistant":
            expected = [text.removeprefix("**You:** ") for text in expected]
        assert sent == expected, (sent, expected)
        assert json.loads(m.cursor_path.read_text())["last_id"] == 6
        # A restart must not replay the rows just consumed.
        m.cursor = m._load_cursor()
        assert m._fetch_new_turns() == []

        # A batch consisting ONLY of a retained copy must still advance the cursor.
        if with_uid:
            db = sqlite3.connect(m.state_db)
            db.execute("UPDATE messages SET active=0 WHERE id=3")
            db.execute("INSERT INTO messages VALUES (7,'chat','user','already delivered',123,NULL,1,'new')")
            db.commit()
            db.close()
            sent.clear()
            with patch.object(dm.time, "sleep", side_effect=StopPolling):
                try:
                    m.run()
                except StopPolling:
                    pass
            assert sent == [], sent
            assert json.loads(m.cursor_path.read_text())["last_id"] == 7


if __name__ == "__main__":
    check()
    check(role="assistant")
    check(with_uid=False)
    print("ok: compaction replay, repeated prompts, pending turns, restart, legacy schema")
