"""Check _adopt_thread against a throwaway HERMES_HOME. Touches no real state."""
import json, os, sqlite3, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desktop_mirror as dm


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="adopt_"))
    db = tmp / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, chat_id TEXT)")
    con.execute("INSERT INTO sessions VALUES ('sess_new', NULL)")        # desktop session
    con.execute("INSERT INTO sessions VALUES ('sess_taken', '999999')")  # already a discord session
    con.commit(); con.close()

    m = dm.Mirror.__new__(dm.Mirror)
    m.hermes_home, m.state_db = tmp, db

    # fresh thread on an unlinked session
    m._adopt_thread("111", "sess_new")
    ids = json.loads((tmp / "discord_threads.json").read_text())
    assert ids == ["111"], ids
    con = sqlite3.connect(db)
    assert con.execute("SELECT chat_id FROM sessions WHERE id='sess_new'").fetchone()[0] == "111"

    # idempotent: no duplicate id, no second link
    m._adopt_thread("111", "sess_new")
    assert json.loads((tmp / "discord_threads.json").read_text()) == ["111"]

    # must NOT clobber a session that already has a chat_id
    m._adopt_thread("222", "sess_taken")
    assert con.execute("SELECT chat_id FROM sessions WHERE id='sess_taken'").fetchone()[0] == "999999"
    # ...but the thread is still registered, so replies there are admitted
    assert "222" in json.loads((tmp / "discord_threads.json").read_text())

    # appends beside threads the gateway registered itself
    (tmp / "discord_threads.json").write_text(json.dumps(["gw1", "gw2"]))
    m._adopt_thread("333", "sess_new")
    assert json.loads((tmp / "discord_threads.json").read_text()) == ["gw1", "gw2", "333"]

    con.close()
    print("all ok")


if __name__ == "__main__":
    main()
