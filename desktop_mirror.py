#!/usr/bin/env python3
"""
Hermes Desktop -> Discord mirror.

The Discord gateway already mirrors Discord <-> Hermes bidirectionally and (via
discord_router.py) routes each channel to a project. This service completes the
loop: when you prompt in the Hermes DESKTOP app, it mirrors that turn into the
mapped Discord channel as a per-session thread -- so Discord becomes a true
second view of a project's sessions.

Why a separate REST service (not a second gateway client):
  * Discord allows only ONE gateway (WebSocket) connection per bot token. The
    Hermes gateway already holds it. So we talk to Discord over the plain REST
    API (HTTPS + Bot token) -- no socket, no conflict.
  * Desktop and the gateway are separate processes but share state.db. We tail
    the shared DB for Desktop-authored turns instead of hooking into either.

Echo safety: we only mirror sessions whose `source` is a Desktop/local source
(tui/desktop/webui/local/cli). Gateway-owned Discord sessions are skipped, so a
mirrored post can never re-trigger a Discord-side agent run.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import sqlite3
import logging
import datetime
from pathlib import Path
from typing import Dict, Optional, Any, List

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("desktop-mirror")

DISCORD_API = "https://discord.com/api/v10"

# Desktop / local session sources we mirror. Anything that resolves to a
# messaging-gateway platform (discord, telegram, ...) is deliberately excluded:
# the gateway already renders those, and re-posting them would loop.
DESKTOP_SOURCES = {"", "tui", "desktop", "webui", "local", "cli"}

# The gateway prefixes relayed platform messages with the sender's display
# name, e.g. "[SomeUser] hello". Some relayed rows land in state.db with no
# platform_message_id, so this prefix is the second signal for "came from
# Discord, don't mirror it back".
_RELAYED_PREFIX_RE = re.compile(r"^\s*\[[^\]\n]{1,64}\]\s")

# Gateway-injected system noise that should never be mirrored as a turn.
_NOISE_PREFIXES = (
    "Operation interrupted:",
    "[Triggering message id:",
    "[CONTEXT COMPACTION",
    "[System:",
    "[OUT-OF-BAND USER MESSAGE",
)


class Mirror:
    def __init__(self, cfg: dict):
        hermes_home = Path(
            cfg.get("hermes_home")
            or os.environ.get("LOCALAPPDATA", "")
        ) / "hermes" if not cfg.get("hermes_home") else Path(cfg["hermes_home"])
        # If hermes_home was given explicitly, use it as-is.
        if cfg.get("hermes_home"):
            hermes_home = Path(cfg["hermes_home"])
        self.hermes_home = hermes_home
        self.state_db = hermes_home / "state.db"
        self.token = cfg["bot_token"].strip()
        self.poll_s = float(cfg.get("poll_seconds", 2.0))

        # cwd (normalized) -> channel_id
        self.cwd_to_channel: Dict[str, str] = {}
        for entry in cfg.get("channels", []):
            ch_id = str(entry.get("id") or "").strip()
            cwd = entry.get("cwd")
            project = entry.get("project")
            if not ch_id:
                continue
            resolved = self._resolve_cwd(cwd, project)
            if resolved:
                self.cwd_to_channel[self._norm(resolved)] = ch_id
                log.info("map cwd %s -> channel %s", resolved, ch_id)

        # session_id -> thread_id (persisted so restarts reuse threads)
        self.thread_map_path = hermes_home / "desktop_mirror_threads.json"
        self.session_threads: Dict[str, str] = self._load_thread_map()

        # Sessions whose CURRENT turn came in over Discord -- used to skip the
        # assistant reply that follows a Discord-origin prompt (the gateway
        # already delivers it). Runtime-only; rebuilt as turns stream in.
        self.discord_turn_sessions: set = set()

        # chat_id -> mapped parent channel (or None). Avoids a REST call per turn.
        self._parent_cache: Dict[str, Optional[str]] = {}

        # last mirrored messages.id (cursor), persisted
        self.cursor_path = hermes_home / "desktop_mirror_cursor.json"
        self.cursor = self._load_cursor()

        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f"Bot {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesDesktopMirror (https://github.com, 1.0)",
        })

    # ---- persistence helpers -------------------------------------------

    def _load_thread_map(self) -> Dict[str, str]:
        try:
            return json.loads(self.thread_map_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_thread_map(self) -> None:
        try:
            self.thread_map_path.write_text(
                json.dumps(self.session_threads, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.debug("thread map save failed: %s", e)

    def _load_cursor(self) -> int:
        try:
            return int(json.loads(self.cursor_path.read_text(encoding="utf-8"))["last_id"])
        except Exception:
            return 0

    def _save_cursor(self) -> None:
        try:
            self.cursor_path.write_text(
                json.dumps({"last_id": self.cursor}), encoding="utf-8"
            )
        except Exception as e:
            log.debug("cursor save failed: %s", e)

    # ---- project / cwd resolution --------------------------------------

    @staticmethod
    def _norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(p.strip())) if p else ""

    def _resolve_cwd(self, cwd: Optional[str], project: Optional[str]) -> str:
        """Resolve an explicit cwd, else a project name via projects.db."""
        if cwd:
            return os.path.expanduser(cwd)
        if not project:
            return ""
        if any(c in project for c in "/\\:"):
            return os.path.expanduser(project)
        # look up in projects.db (same DB the desktop groups sessions by)
        try:
            pdb = self.hermes_home / "projects.db"
            if pdb.exists():
                conn = sqlite3.connect(str(pdb))
                row = conn.execute(
                    "SELECT COALESCE(p.primary_path, pf.path) AS path "
                    "FROM projects p "
                    "LEFT JOIN project_folders pf ON pf.project_id = p.id "
                    "WHERE p.slug = ? OR p.name = ? "
                    "ORDER BY pf.is_primary DESC LIMIT 1",
                    (project, project),
                ).fetchone()
                conn.close()
                if row and row[0]:
                    return row[0]
        except Exception as e:
            log.debug("projects.db lookup failed for %s: %s", project, e)
        return str(Path.home() / ".knowl" / "workspaces" / project)

    # ---- discord REST --------------------------------------------------

    def _post(self, url: str, payload: dict) -> Optional[dict]:
        for attempt in range(4):
            r = self.http.post(url, data=json.dumps(payload))
            if r.status_code == 429:  # rate limited
                retry = float(r.json().get("retry_after", 1.0))
                log.warning("rate limited, sleeping %.2fs", retry)
                time.sleep(retry + 0.25)
                continue
            if r.status_code in (200, 201):
                return r.json()
            log.warning("POST %s -> %s: %s", url, r.status_code, r.text[:300])
            return None
        return None

    def _derive_thread_name(self, content: str) -> str:
        """Mirror the gateway's _derive_auto_thread_name behaviour."""
        content = (content or "").strip()
        content = re.sub(r"<@[!&]?\d+>", "", content)
        content = re.sub(r"<#\d+>", "", content)
        content = re.sub(r"\s+", " ", content).strip()
        if not content:
            return "Hermes"
        if len(content) > 80:
            return content[:77] + "..."
        return content

    def _ensure_thread(self, channel_id: str, session_id: str, seed_text: str) -> Optional[str]:
        """Return an existing thread id for the session, or create one."""
        tid = self.session_threads.get(session_id)
        if tid:
            return tid
        name = self._derive_thread_name(seed_text)
        # Create a thread WITHOUT a starter message (type 11 = public thread).
        created = self._post(
            f"{DISCORD_API}/channels/{channel_id}/threads",
            {"name": name, "type": 11, "auto_archive_duration": 1440},
        )
        if not created or "id" not in created:
            log.error("thread create failed in channel %s", channel_id)
            return None
        tid = str(created["id"])
        self.session_threads[session_id] = tid
        self._save_thread_map()
        self._adopt_thread(tid, session_id)
        log.info("created thread '%s' (%s) for session %s", name, tid, session_id)
        return tid

    def _adopt_thread(self, thread_id: str, session_id: str) -> None:
        """Make a mirror-created thread a real two-way session.

        Without this the thread is write-only: you see the conversation in
        Discord but replying there does nothing. Two separate gates have to be
        satisfied, and missing EITHER one silently drops the message:

        1. ``discord_threads.json`` -- the gateway's ThreadParticipationTracker.
           ``require_mention`` defaults to true, and ``_in_bot_thread()`` only
           waives it for threads listed here. A thread the bot did not create
           itself is absent, so the reply is dropped at ingress and never even
           reaches the log.
        2. ``gateway_routing`` (state.db) -- the routing index. This is what
           actually decides which session an incoming Discord message continues.
           ``build_session_key()`` turns the message into
           ``agent:main:discord:thread:<channel>:<thread>`` and looks THAT up; a
           missing entry means the gateway starts a brand-new session, so the
           reply lands in a different conversation with none of the history.
           (``sessions.chat_id`` is NOT this mechanism -- it only feeds
           ``gateway/mirror.py``'s delivery mirroring. It is set too, since it
           costs nothing and keeps that path consistent.)

        Everything written here is in the gateway's own formats -- only what it
        would have written had it created the thread itself.

        NOTE: the gateway reads BOTH the thread tracker and the routing index
        once at startup and never re-reads them, so a thread adopted while it is
        running stays unreachable until the gateway restarts.
        """
        # 1) register with the bot's thread tracker (plain JSON list of ids)
        try:
            p = self.hermes_home / "discord_threads.json"
            ids = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
            if not isinstance(ids, list):
                ids = []
            if thread_id not in (str(i) for i in ids):
                ids.append(thread_id)
                # ponytail: mirrors ThreadParticipationTracker's 500-entry cap;
                # raise both together if the gateway's cap ever changes.
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(ids[-500:]), encoding="utf-8")
                os.replace(tmp, p)
                log.info("registered thread %s with the bot thread tracker", thread_id)
        except Exception as e:
            log.warning("could not register thread %s: %s", thread_id, e)

        # 2) claim the routing key so replies CONTINUE this session
        self._claim_routing(thread_id, session_id)

        # 3) point the session's chat_id at the thread (delivery-mirror path)
        try:
            conn = sqlite3.connect(str(self.state_db), timeout=10)
            try:
                cur = conn.execute(
                    "UPDATE sessions SET chat_id=? WHERE id=? AND COALESCE(chat_id,'')=''",
                    (thread_id, session_id),
                )
                conn.commit()
                if cur.rowcount:
                    log.info("linked session %s -> chat_id %s", session_id, thread_id)
            finally:
                conn.close()
        except Exception as e:
            log.warning("could not link session %s: %s", session_id, e)

    def _claim_routing(self, thread_id: str, session_id: str) -> None:
        """Write the gateway_routing row that maps this thread to this session.

        The gateway routes an incoming message by SESSION KEY, not by chat_id:
        `build_session_key()` (gateway/session.py) produces
        `agent:main:discord:thread:<chat_id>:<thread_id>` and looks it up in the
        `gateway_routing` table. A mirror-created thread has no such row, so the
        gateway falls through to "no existing session" and creates a new one --
        the reply appears to go to a stranger with no history.

        A thread's chat_id IS its own id for a message posted in that thread, so
        both slots carry thread_id (matches every real Discord routing row).

        Never overwrites an existing key: if the gateway already owns this
        thread, its entry is authoritative.
        """
        key = f"agent:main:discord:thread:{thread_id}:{thread_id}"
        # entry_json timestamps are ISO strings; the updated_at COLUMN is a REAL epoch.
        iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entry = {
            "session_key": key, "session_id": session_id,
            "created_at": iso, "updated_at": iso,
            "platform": "discord", "chat_type": "thread", "metadata": {},
            "origin": {
                "platform": "discord", "chat_id": thread_id,
                "chat_type": "thread", "thread_id": thread_id,
            },
        }
        try:
            conn = sqlite3.connect(str(self.state_db), timeout=10)
            try:
                scope = str(self.hermes_home / "sessions")
                cur = conn.execute(
                    "INSERT OR IGNORE INTO gateway_routing (scope, session_key, entry_json, updated_at)"
                    " VALUES (?,?,?,?)",
                    (scope, key, json.dumps(entry), time.time()),
                )
                conn.commit()
                if cur.rowcount:
                    log.info("claimed routing %s -> %s (restart gateway to load it)", key, session_id)
            finally:
                conn.close()
        except Exception as e:
            log.warning("could not claim routing for %s: %s", thread_id, e)

    def _send(self, thread_id: str, content: str) -> None:
        # Discord hard limit 2000 chars/message -> chunk.
        for chunk in self._chunk(content, 1900):
            self._post(
                f"{DISCORD_API}/channels/{thread_id}/messages",
                {"content": chunk},
            )

    @staticmethod
    def _chunk(text: str, size: int) -> List[str]:
        text = text or ""
        if len(text) <= size:
            return [text] if text.strip() else []
        out, cur = [], ""
        for line in text.splitlines(keepends=True):
            if len(cur) + len(line) > size:
                if cur:
                    out.append(cur)
                cur = line
                while len(cur) > size:
                    out.append(cur[:size])
                    cur = cur[size:]
            else:
                cur += line
        if cur.strip():
            out.append(cur)
        return out

    # ---- DB tail -------------------------------------------------------

    def _fetch_new_turns(self) -> List[dict]:
        """Return new user/assistant messages since the cursor.

        Desktop-vs-Discord is decided PER MESSAGE, not per session: a session's
        `source` stays 'discord' even for turns you type in the Desktop app, so
        filtering on the session would skip exactly what we want to mirror.
        A user message that arrived over Discord carries a platform_message_id;
        one typed in Desktop does not.

        ``chat_id`` is selected alongside ``cwd`` so a session with no cwd (one
        created before channel routing existed, or whose channel was mapped
        later) can still be matched by its Discord channel.
        """
        conn = sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT m.id, m.session_id, m.role, m.content, m.timestamp,
                       m.platform_message_id, s.source, s.cwd, s.chat_id
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE m.id > ?
                  AND m.role IN ('user','assistant')
                  AND m.active = 1
                  AND COALESCE(m.content,'') <> ''
                ORDER BY m.id ASC
                LIMIT 200
                """,
                (self.cursor,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def _thread_for_turn(self, turn: dict, channel_id: str, seed_text: str) -> Optional[str]:
        """Return the thread id to post this turn into.

        If the session already lives in a Discord thread (you were chatting
        there and switched to Desktop), post into THAT thread -- the whole
        point is that both surfaces show one conversation. Only when a session
        has no Discord thread of its own do we create one.
        """
        chat_id = str(turn.get("chat_id") or "")
        if chat_id and chat_id != channel_id:
            # chat_id is a thread; use it when it hangs off a mapped channel.
            if self._parent_channel(chat_id) == channel_id:
                return chat_id
        return self._ensure_thread(channel_id, turn["session_id"], seed_text)

    def _channel_for_cwd(self, cwd: Optional[str]) -> Optional[str]:
        """Map a session's cwd to a channel; an unset cwd means the home dir.

        Hermes deliberately stores NULL cwd for Desktop sessions that never
        explicitly chose a workspace (`_persisted_session_cwd` +
        `_LAUNCH_CWD_NOT_A_WORKSPACE` in tui_gateway/session_workdir.py): the
        app's launch directory is an artifact, not a folder the user picked, so
        it is not written to the DB. That launch directory IS the home dir, and
        the Desktop sidebar shows those sessions under the Home project — so
        falling back to the home dir's channel restores exactly the mapping the
        user sees. Projects with a real chosen folder (a repo) are unaffected:
        they persist a cwd and match on the line above.
        """
        key = self._norm(cwd) if cwd else self._norm(os.path.expanduser("~"))
        return self.cwd_to_channel.get(key)

    def _parent_channel(self, chat_id: str) -> Optional[str]:
        """Resolve a Discord chat_id to its mapped parent channel.

        A session's chat_id is the THREAD id when the conversation lives in a
        thread, and threads are not in the config map -- only their parent
        channel is. One REST lookup per id, cached.
        """
        if not chat_id:
            return None
        if chat_id in self.cwd_to_channel.values():
            return chat_id  # already a mapped channel
        if chat_id in self._parent_cache:
            return self._parent_cache[chat_id]
        parent = None
        try:
            r = self.http.get(f"{DISCORD_API}/channels/{chat_id}", timeout=15)
            if r.ok:
                pid = str(r.json().get("parent_id") or "")
                if pid and pid in self.cwd_to_channel.values():
                    parent = pid
        except Exception as e:
            log.debug("parent lookup failed for %s: %s", chat_id, e)
        self._parent_cache[chat_id] = parent
        return parent

    def _target_channel(self, turn: dict) -> Optional[str]:
        """Pick the channel to mirror a turn into.

        cwd is the primary key (it is what channel routing sets). Falling back
        to the session's Discord channel keeps sessions working when they have
        no cwd -- created before routing existed, or mapped after the fact.
        """
        ch = self._channel_for_cwd(turn.get("cwd"))
        if ch:
            return ch
        return self._parent_channel(str(turn.get("chat_id") or ""))

    def run(self) -> None:
        if not self.state_db.exists():
            log.error("state.db not found at %s", self.state_db)
            sys.exit(1)
        # On first run, don't replay all history -- start at current max id.
        if self.cursor == 0:
            conn = sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True)
            try:
                mx = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
            finally:
                conn.close()
            self.cursor = int(mx)
            self._save_cursor()
            log.info("first run: starting at messages.id=%d (no backfill)", self.cursor)

        log.info("watching %s | %d channel mapping(s)", self.state_db, len(self.cwd_to_channel))
        while True:
            try:
                turns = self._fetch_new_turns()
                for t in turns:
                    self.cursor = max(self.cursor, int(t["id"]))
                    sid = t["session_id"]
                    role = t["role"]
                    content = t["content"] or ""
                    source = (t.get("source") or "").strip().lower()

                    # Drop gateway/system noise outright.
                    if content.lstrip().startswith(_NOISE_PREFIXES):
                        continue

                    # Decide whether THIS user turn arrived over Discord.
                    # Two signals, because neither alone is reliable:
                    #  * platform_message_id -- set for most relayed messages
                    #  * "[Name] ..." prefix -- the gateway stamps relayed text
                    #    with the sender's display name (shared/multi-user
                    #    sessions), and some relayed rows carry no platform id.
                    if role == "user":
                        from_discord = bool(t.get("platform_message_id")) or bool(
                            _RELAYED_PREFIX_RE.match(content)
                        )
                        if from_discord:
                            self.discord_turn_sessions.add(sid)
                            continue
                        self.discord_turn_sessions.discard(sid)
                    elif sid in self.discord_turn_sessions:
                        continue  # reply to a Discord-origin prompt

                    channel_id = self._target_channel(t)
                    if not channel_id:
                        continue  # not a mapped project or channel

                    # Only a user turn may open a NEW thread, so a fresh thread
                    # is named from your actual prompt. A session that already
                    # lives in a Discord thread posts there regardless of role.
                    _existing = self.session_threads.get(sid) or (
                        str(t.get("chat_id") or "") not in ("", channel_id)
                    )
                    if role == "assistant" and not _existing:
                        continue

                    thread_id = self._thread_for_turn(t, channel_id, content)
                    if not thread_id:
                        continue
                    prefix = "**You:** " if role == "user" else ""
                    self._send(thread_id, f"{prefix}{content}")
                    log.info("mirrored %s turn (msg %s) session %s -> thread %s",
                             role, t["id"], sid, thread_id)
                if turns:
                    self._save_cursor()
            except Exception as e:
                log.exception("poll loop error: %s", e)
            time.sleep(self.poll_s)


def load_config() -> dict:
    here = Path(__file__).parent
    cfg_path = here / "mirror.config.yaml"
    if not cfg_path.exists():
        log.error("missing %s (copy mirror.config.example.yaml)", cfg_path)
        sys.exit(1)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    # Token: allow ${ENV} or a .env in hermes_home.
    tok = str(cfg.get("bot_token") or "").strip()
    if tok.startswith("${") and tok.endswith("}"):
        tok = os.environ.get(tok[2:-1], "")
    if not tok:
        # fall back to hermes .env
        home = cfg.get("hermes_home") or (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes")
        envp = Path(home) / ".env"
        if envp.exists():
            m = re.search(r"^DISCORD_BOT_TOKEN=(.+)$", envp.read_text(encoding="utf-8", errors="ignore"), re.M)
            if m:
                tok = m.group(1).strip().strip('"').strip("'")
    if not tok:
        log.error("no bot_token resolved (config, env, or hermes .env)")
        sys.exit(1)
    cfg["bot_token"] = tok
    return cfg


def _acquire_single_instance_lock(hermes_home: Path):
    """Ensure only one mirror runs. Returns the lock handle (keep it alive).

    Two copies would double-post every turn. The Windows autostart path can
    launch more than one process, so the guard lives here rather than in the
    launcher.
    """
    lock_path = hermes_home / "desktop_mirror.lock"
    try:
        # Open r+ (create if needed) and ensure at least 1 byte exists BEFORE
        # locking: msvcrt.locking() locks a byte RANGE, and locking byte 0 of a
        # zero-length file succeeds for every process -- silently defeating the
        # guard. Write a placeholder first, then lock that byte.
        if not lock_path.exists():
            lock_path.write_text("0", encoding="utf-8")
        fh = open(lock_path, "r+")
        if os.path.getsize(lock_path) == 0:
            fh.write("0")
            fh.flush()
            fh.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                log.info("another desktop_mirror instance holds the lock — exiting")
                sys.exit(0)
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                log.info("another desktop_mirror instance holds the lock — exiting")
                sys.exit(0)
        # Record our pid AFTER the lock byte (never truncate the locked byte).
        try:
            fh.seek(1)
            fh.truncate(1)
            fh.write(f" pid={os.getpid()}\n")
            fh.flush()
        except Exception:
            pass
        return fh
    except SystemExit:
        raise
    except Exception as e:
        log.debug("single-instance lock unavailable (%s) — continuing", e)
        return None


if __name__ == "__main__":
    _cfg = load_config()
    _home = Path(_cfg.get("hermes_home") or (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"))
    _lock = _acquire_single_instance_lock(_home)  # noqa: F841 (held for process life)
    Mirror(_cfg).run()
