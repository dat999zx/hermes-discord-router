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
# name, e.g. "[IsntDatEpik?] hello". Some relayed rows land in state.db with no
# platform_message_id, so this prefix is the second signal for "came from
# Discord, don't mirror it back".
_RELAYED_PREFIX_RE = re.compile(r"^\s*\[[^\]\n]{1,64}\]\s")

# Gateway-injected system noise that should never be mirrored as a turn.
_NOISE_PREFIXES = (
    "Operation interrupted:",
    "[Triggering message id:",
    "[CONTEXT COMPACTION",
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
        log.info("created thread '%s' (%s) for session %s", name, tid, session_id)
        return tid

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
        """
        conn = sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT m.id, m.session_id, m.role, m.content, m.timestamp,
                       m.platform_message_id, s.source, s.cwd
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

    def _channel_for_cwd(self, cwd: Optional[str]) -> Optional[str]:
        if not cwd:
            return None
        return self.cwd_to_channel.get(self._norm(cwd))

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

                    channel_id = self._channel_for_cwd(t.get("cwd"))
                    if not channel_id:
                        continue  # session's cwd isn't a mapped project

                    # Only a user turn may open a thread -- that way the thread
                    # is named from your actual prompt instead of a generic
                    # placeholder. An assistant turn with no thread yet belongs
                    # to a conversation that started before the mirror did.
                    if role == "assistant" and sid not in self.session_threads:
                        continue

                    thread_id = self._ensure_thread(channel_id, sid, content)
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
