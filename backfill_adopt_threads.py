"""One-shot backfill: adopt threads the mirror created BEFORE _adopt_thread existed.

Same two writes as desktop_mirror._adopt_thread, applied to the existing map.
Idempotent: skips threads already registered / sessions already linked.
Run with the mirror stopped (it holds no lock on these files, but avoid races).
"""
import json, os, sqlite3, sys
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or (Path(os.environ["LOCALAPPDATA"]) / "hermes"))
threads_map = HOME / "desktop_mirror_threads.json"
tracker = HOME / "discord_threads.json"
state_db = HOME / "state.db"

mapping = json.loads(threads_map.read_text(encoding="utf-8"))
ids = json.loads(tracker.read_text(encoding="utf-8")) if tracker.exists() else []
if not isinstance(ids, list):
    ids = []
known = {str(i) for i in ids}

added = [t for t in mapping.values() if str(t) not in known]
for t in added:
    ids.append(str(t))
if added:
    tmp = tracker.with_suffix(".tmp")
    tmp.write_text(json.dumps(ids[-500:]), encoding="utf-8")
    os.replace(tmp, tracker)
print(f"tracker: registered {len(added)} thread(s)")

conn = sqlite3.connect(str(state_db), timeout=10)
linked = 0
for sid, tid in mapping.items():
    cur = conn.execute(
        "UPDATE sessions SET chat_id=? WHERE id=? AND COALESCE(chat_id,'')=''",
        (str(tid), sid),
    )
    if cur.rowcount:
        linked += 1
        print(f"  linked {sid} -> {tid}")
conn.commit()

print(f"sessions: linked {linked}")

# verify
known2 = {str(i) for i in json.loads(tracker.read_text(encoding='utf-8'))}
missing = [t for t in mapping.values() if str(t) not in known2]
print("\nVERIFY tracker missing:", missing or "none")
rows = conn.execute(
    "SELECT id, chat_id FROM sessions WHERE id IN (%s)" % ",".join("?" * len(mapping)),
    list(mapping),
).fetchall()
for sid, chat in rows:
    exp = str(mapping[sid])
    print(f"  {sid}: chat_id={chat!r} {'OK' if str(chat or '')==exp else 'MISMATCH/absent-session'}")
conn.close()
