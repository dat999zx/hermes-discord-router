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


def route_discord_channel(context: Any, session_db: Any) -> Optional[str]:
    """
    Route a Discord message to a project based on channel configuration.
    
    Args:
        context: SessionContext with source.chat_id, source.parent_chat_id
        session_db: AsyncSessionDB for updating session cwd
        
    Returns:
        The resolved cwd path, or None if no routing configured
    """
    # Check for Discord channel→project mapping
    channel_projects_json = os.environ.get("DISCORD_CHANNEL_PROJECTS", "")
    if not channel_projects_json:
        return None
    
    try:
        channel_projects = json.loads(channel_projects_json)
    except json.JSONDecodeError:
        return None
    
    if not channel_projects or not isinstance(channel_projects, dict):
        return None
    
    # Get chat_id and parent_chat_id from context
    chat_id = str(getattr(context.source, "chat_id", "") or "")
    parent_chat_id = str(getattr(context.source, "parent_chat_id", "") or "")
    
    logger.debug("Discord routing: chat_id=%s parent_chat_id=%s", chat_id, parent_chat_id)
    
    # Look up project (threads use parent_chat_id which is the channel)
    project = channel_projects.get(chat_id) or channel_projects.get(parent_chat_id)
    if not project:
        return None
    
    # Resolve project to cwd
    cwd = resolve_project_to_cwd(project)
    if not cwd:
        return None
    
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
