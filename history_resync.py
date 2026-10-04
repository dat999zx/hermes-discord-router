"""Adopt transcript rows written by OTHER processes (Discord/Telegram gateway, cron).

A Desktop session keeps its history in memory. The messaging gateway shares the same
``state.db`` session but is a separate process, so turns it appends never reach that
memory and the next Desktop turn runs blind to them. The durable transcript is the
authority: before each turn, compare the DB tail row id with the one we last saw and
reload when it moved.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_TAIL_KEY = "_db_tail_row_id"


def _db_tail(db, session_key: str):
    row = db._read_one("SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1", (session_key,))
    return row[0] if row and row[0] is not None else None


def _stamped_tail(history: list):
    ids = []
    for m in history:
        raw = m.get("_row_id", m.get("row_id")) if isinstance(m, dict) else None
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            pass
    return max(ids) if ids else None


def record_tail(session: dict, session_db_ctx) -> None:
    """Remember the DB tail after our own turn so our writes are not mistaken for foreign ones."""
    key = session.get("session_key")
    if not key:
        return
    try:
        with session_db_ctx(session) as db:
            if db is not None:
                session[_TAIL_KEY] = _db_tail(db, key)
    except Exception:
        logger.debug("history_resync: record_tail failed", exc_info=True)


def adopt_foreign_writes(session: dict, session_db_ctx) -> bool:
    """Reload ``session['history']`` from the DB if another process appended to it."""
    key = session.get("session_key")
    if not key:
        return False
    try:
        with session_db_ctx(session) as db:
            if db is None:
                return False
            tail = _db_tail(db, key)
            with session["history_lock"]:
                seen = session.get(_TAIL_KEY)
                if seen is None:
                    seen = _stamped_tail(session.get("history", []))
                if tail is None or seen is None or tail <= seen:
                    session[_TAIL_KEY] = tail if tail is not None else seen
                    return False
                fresh = db.get_messages_as_conversation(
                    key, repair_alternation=True, include_row_ids=True)
                if not isinstance(fresh, list) or not fresh:
                    return False
                before = len(session.get("history", []))
                session["history"] = fresh
                session["history_version"] = int(session.get("history_version", 0)) + 1
                session[_TAIL_KEY] = tail
        logger.info("history_resync: session %s adopted foreign writes (tail %s -> %s, history %d -> %d)",
                    key, seen, tail, before, len(fresh))
        return True
    except Exception:
        logger.warning("history_resync: adopt failed", exc_info=True)
        return False
