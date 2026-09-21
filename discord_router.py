"""
Discord Channel Router - External routing logic

This module is imported by the patched gateway/run.py to handle
Discord channel→project routing. Keeping the logic here means:
1. The run.py patch is tiny (just imports and calls this)
2. This file can be updated without re-patching run.py
3. Survives minor Hermes updates that don't touch the import line

Place this file at: %LOCALAPPDATA%/hermes/discord_router.py
"""

import os
import json
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Dict, Optional, Any

logger = logging.getLogger("hermes.discord_router")


def _hermes_home() -> Path:
    """Locate the Hermes home dir on any platform."""
    hh = os.environ.get("HERMES_HOME")
    if hh:
        return Path(hh)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "hermes"
    return Path.home() / ".local" / "share" / "hermes"


def resolve_project_to_cwd(project: str) -> str:
    """
    Resolve a project name to a folder path.
    
    If project looks like a path (contains / or \\ or :), use it directly.
    Otherwise, look it up in the projects database.
    """
    # Explicit path wins (recommended: put the real folder in config.yaml).
    if "/" in project or "\\" in project or ":" in project:
        return os.path.expanduser(project)
    
    # Try to find project in projects.db
    try:
        db_path = _hermes_home() / "projects.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cursor = conn.execute(
                "SELECT COALESCE(p.primary_path, pf.path) AS path "
                "FROM projects p "
                "LEFT JOIN project_folders pf ON pf.project_id = p.id "
                "WHERE p.slug = ? OR p.name = ? "
                "ORDER BY pf.is_primary DESC LIMIT 1",
                (project, project)
            )
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.debug("Could not look up project '%s' in DB: %s", project, e)
    
    # No match. Return empty so the session keeps its normal cwd instead of
    # being sent to an invented folder (a bogus path makes Hermes Desktop
    # auto-create a phantom project from that directory name).
    logger.warning(
        "Discord routing: project '%s' not found in projects.db — set an "
        "explicit folder path in config.yaml discord.channels[].project",
        project,
    )
    return ""


def _stored_cwd(session_id: str, session_key: str, chat_id: str) -> str:
    """The cwd already recorded for this conversation, or "".

    Tried in order of precision: session id, session key, then the chat/thread id.
    ALL THREE matter, because the caller does not always have the first one:

    * A mirror-created thread carries the project its session was started in, but
      a thread id is not in the channel map -- only its parent channel is. When
      the parent lookup misses (unmapped channel, DM, or an adapter that did not
      populate parent_chat_id) the conversation's OWN stored cwd is the answer.
    * ``context.session_id`` is empty until the session row exists, so a turn
      arriving mid-run (queued/steered while the agent is busy) can reach here
      with no id at all. ``chat_id`` is always set, and for a Discord thread the
      session row records it verbatim.

    Returning "" here is not harmless: the gateway passes the result straight into
    ``set_session_vars(cwd=...)``, which pins ``_SESSION_CWD``; empty means every
    consumer falls back to the launch directory, i.e. the conversation silently
    jumps to the Home project mid-run.
    """
    keys = [k for k in (session_id, session_key) if k]
    if not keys and not chat_id:
        return ""
    try:
        db_path = _hermes_home() / "state.db"
        if not db_path.exists():
            return ""
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for key in keys:
                row = conn.execute(
                    "SELECT cwd FROM sessions WHERE session_key = ? OR id = ? LIMIT 1",
                    (key, key),
                ).fetchone()
                if row and row[0]:
                    return str(row[0])
            if chat_id:
                # Most recent session in this chat/thread: a thread hosts the
                # desktop session the mirror opened it for AND the gateway's own,
                # so prefer the newest rather than an arbitrary match.
                row = conn.execute(
                    "SELECT cwd FROM sessions WHERE chat_id = ? AND COALESCE(cwd,'') <> '' "
                    "ORDER BY started_at DESC LIMIT 1",
                    (chat_id,),
                ).fetchone()
                if row and row[0]:
                    return str(row[0])
        finally:
            conn.close()
    except Exception as e:
        logger.debug("Could not read stored cwd (%s/%s/%s): %s", session_id, session_key, chat_id, e)
    return ""


def route_discord_channel(context: Any, session_db: Any) -> Optional[str]:
    """
    Route a Discord message to a project based on channel configuration.
    
    Args:
        context: SessionContext with source.chat_id, source.parent_chat_id
        session_db: AsyncSessionDB for updating session cwd
        
    Returns:
        The resolved cwd path, or None if no routing configured
    """
    session_id = str(getattr(context, "session_id", "") or "")
    session_key = str(getattr(context, "session_key", "") or "")
    chat_id = str(getattr(context.source, "chat_id", "") or "")
    parent_chat_id = str(getattr(context.source, "parent_chat_id", "") or "")

    def _fallback() -> Optional[str]:
        return _stored_cwd(session_id, session_key, chat_id) or None

    # Check for Discord channel→project mapping
    channel_projects_json = os.environ.get("DISCORD_CHANNEL_PROJECTS", "")
    if not channel_projects_json:
        return _fallback()

    try:
        channel_projects = json.loads(channel_projects_json)
    except json.JSONDecodeError:
        return _fallback()

    if not channel_projects or not isinstance(channel_projects, dict):
        return _fallback()

    logger.debug("Discord routing: chat_id=%s parent_chat_id=%s", chat_id, parent_chat_id)

    # Look up project (threads use parent_chat_id which is the channel)
    project = channel_projects.get(chat_id) or channel_projects.get(parent_chat_id)
    if not project:
        # Unmapped channel, or a thread whose parent could not be read. Keep the
        # session where it already is rather than blanking it.
        return _fallback()

    # Resolve project to cwd
    cwd = resolve_project_to_cwd(project)
    if not cwd:
        return _fallback()
    
    logger.info("Discord routing: channel %s → project '%s' → cwd '%s'",
               parent_chat_id or chat_id, project, cwd)
    
    # Update session cwd in database (async, fire-and-forget)
    if cwd and hasattr(context, 'session_id') and context.session_id and session_db is not None:
        async def _update_cwd():
            try:
                await session_db.update_session_cwd(context.session_id, cwd)
                logger.debug("Discord routing: updated session %s cwd in DB", context.session_id)
            except Exception as e:
                logger.debug("Discord routing: failed to update session cwd: %s", e)
        
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_update_cwd())
        except RuntimeError:
            pass  # No running loop
    
    return cwd
