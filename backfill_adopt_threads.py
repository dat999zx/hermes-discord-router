"""One-shot backfill: adopt threads the mirror created BEFORE _adopt_thread existed.

Same two writes as desktop_mirror._adopt_thread, applied to the existing map.
Idempotent: skips threads already registered / sessions already linked.
Run with the mirror stopped (it holds no lock on these files, but avoid races).
"""
import json, os, sqlite3, sys, time
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

# gateway_routing: the row that actually decides which session a reply continues
scope = str(HOME / "sessions")
claimed = 0
for sid, tid in mapping.items():
    key = f"agent:main:discord:thread:{tid}:{tid}"
    iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry = {
        "session_key": key, "session_id": sid,
        "created_at": iso, "updated_at": iso,
        "platform": "discord", "chat_type": "thread", "metadata": {},
        "origin": {"platform": "discord", "chat_id": str(tid),
                   "chat_type": "thread", "thread_id": str(tid)},
    }
    cur = conn.execute(
        "INSERT OR IGNORE INTO gateway_routing (scope, session_key, entry_json, updated_at)"
        " VALUES (?,?,?,?)",
        (scope, key, json.dumps(entry), time.time()),
    )
    if cur.rowcount:
        claimed += 1
        print(f"  claimed routing {key} -> {sid}")
conn.commit()
print(f"routing: claimed {claimed} (existing gateway-owned keys left alone)")

# verify
known2 = {str(i) for i in json.loads(tracker.read_text(encoding='utf-8'))}
missing = [t for t in mapping.values() if str(t) not in known2]
print("\nVERIFY tracker missing:", missing or "none")
bad = []
for sid, tid in mapping.items():
    row = conn.execute(
        "SELECT entry_json FROM gateway_routing WHERE session_key=?",
        (f"agent:main:discord:thread:{tid}:{tid}",),
    ).fetchone()
    if not row:
        bad.append((sid, tid, "NO ROUTING ROW"))
    elif json.loads(row[0]).get("session_id") != sid:
        bad.append((sid, tid, "routed to " + str(json.loads(row[0]).get("session_id"))))
print("VERIFY routing mismatches:", bad or "none")
conn.close()
print("\nRESTART THE GATEWAY: routing + thread tracker are read once at startup.")
