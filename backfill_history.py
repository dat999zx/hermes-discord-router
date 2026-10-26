"""One-shot replay of Desktop history into Discord threads (the live mirror is forward-only).

Replays every Desktop-origin turn up to the mirror's current cursor, no cap. Sessions with
no thread yet get one. Turns already in a thread (matched by text on Discord) are skipped,
so it is safe to re-run. Default is a dry run; pass --apply to post.
Discord threads are append-only: replayed turns land AFTER whatever the thread already holds.
"""
import re, sqlite3, sys, time
import desktop_mirror as dm

apply = "--apply" in sys.argv
m = dm.Mirror(dm.load_config())
conn = sqlite3.connect(f"file:{m.state_db}?mode=ro", uri=True); conn.row_factory = sqlite3.Row

def existing_texts(tid):
    seen, before = set(), None
    while True:
        r = m.http.get(f"{dm.DISCORD_API}/channels/{tid}/messages", params={"limit": 100, **({"before": before} if before else {})}, timeout=30)
        if not r.ok: return seen
        page = r.json()
        for x in page: seen.add(dm.unformat_turn(x["content"])[:80])
        if len(page) < 100: return seen
        before = page[-1]["id"]

sessions = conn.execute(f"""SELECT s.id, s.source, s.cwd, s.chat_id FROM sessions s
    WHERE {m.listable_sql} AND s.archived=0
    AND LOWER(COALESCE(s.source,'')) NOT IN ({dm._sql_placeholders(dm._DENY_SOURCES)})""", dm._DENY_SOURCES).fetchall()
total = 0
for s in sessions:
    s = dict(s)
    ch = m._target_channel(s)
    if not ch: continue
    rows = conn.execute("""SELECT id, role, content, platform_message_id FROM messages WHERE session_id=? AND id<=?
        AND role IN ('user','assistant') AND active=1 AND COALESCE(content,'')<>'' ORDER BY id""", (s["id"], m.cursor)).fetchall()
    out, discord_turn = [], False
    for r in rows:
        c = r["content"]
        if c.lstrip().startswith(dm._NOISE_PREFIXES): continue
        if r["role"] == "user":
            discord_turn = dm.is_discord_turn(dict(r))
            if discord_turn: continue
        elif discord_turn: continue  # gateway already delivered this reply
        out.append((r["role"], c))
    if not out: continue
    tid = m.session_threads.get(s["id"]) or (s["chat_id"] if s["chat_id"] and m._parent_channel(s["chat_id"]) == ch else None)
    have = existing_texts(tid) if tid else set()
    todo = [(ro, c) for ro, c in out if c[:80] not in have]
    print(f"{s['id']}: {len(todo)}/{len(out)} to post -> {'thread '+tid if tid else 'NEW thread'}")
    total += len(todo)
    if apply and todo:
        tid = tid or m._ensure_thread(ch, s["id"], next((c for ro, c in todo if ro == "user"), todo[0][1]))
        if not tid: print("  no thread, skipped"); continue
        prev = None
        for ro, c in todo:
            m._send(tid, dm.format_turn(ro, c, prev))
            prev = ro
            time.sleep(0.4)
print(("posted" if apply else "would post"), total, "turns")
