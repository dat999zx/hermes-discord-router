"""Keep one model per session across Desktop and the Discord gateway.

A Discord thread opened in Hermes Desktop is ONE session driven by two
processes: Desktop's tui_gateway (its own live agent) and the messaging
gateway. Hermes keeps each side's /model choice to itself, so switching the
model in Desktop left Discord on the old one, and vice versa.

Both sides already write their switch into state.db; these hooks read the
other side's write at the start of a turn:

* Desktop -> Discord. Desktop records a switch as one durable marker message
  ("[System: The active model for this chat has changed to M via provider P.").
  ``adopt_desktop_switch`` (gateway, per turn) adopts the newest marker ONCE as
  the session's /model override, so a later Discord /model still wins.
* Discord -> Desktop. A gateway /model writes the route to both the top-level
  ``model_config`` keys and ``gateway_runtime``, while Desktop writes only the
  top-level keys. Top-level provider == gateway_runtime provider and a model
  that differs from Desktop's live agent therefore means "switched elsewhere".
  ``adopt_gateway_switch`` (Desktop, per turn) applies it as a session-only
  switch, at most once per target.

Each switch is applied through Hermes's own switch path, so the other side then
sees a value equal to its own and does nothing. That is why the sync can't
loop. Installed into HERMES_HOME by install.py; the hooks are two
one-line calls patched into gateway/run_turn.py and tui_gateway/prompt_turn.py.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("hermes-discord-router.model_sync")

SWITCH_PREFIX = "[System: The active model for this chat has changed to "
_SWITCH_RE = re.compile(
    re.escape(SWITCH_PREFIX) + r"(?P<model>\S+?)(?: via provider (?P<provider>\S+?))?\. From this point")


def parse_switch_marker(content: str):
    """(model, provider) named by a Desktop switch marker, or None."""
    m = _SWITCH_RE.match(content or "")
    if not m or not m.group("provider"):
        return None
    return m.group("model"), m.group("provider")


def _model_config(raw) -> dict:
    try:
        cfg = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def gateway_switch_target(model_config_raw, live_model: str, live_provider: str):
    """(model, provider) a gateway /model set that Desktop's live agent lacks, else None."""
    cfg = _model_config(model_config_raw)
    model, provider = str(cfg.get("model") or ""), str(cfg.get("provider") or "")
    runtime = cfg.get("gateway_runtime") if isinstance(cfg.get("gateway_runtime"), dict) else {}
    if not model or not provider or runtime.get("provider") != provider:
        return None
    if (live_model, live_provider) == (model, provider):
        return None
    return model, provider


# ------------------------------------------------------------- gateway side

def adopt_desktop_switch(runner, session_key: str) -> None:
    """Gateway turn start: follow a model switch Desktop made for this session."""
    store = getattr(runner, "session_store", None)
    db = getattr(getattr(runner, "_session_db", None), "_db", None)
    if store is None or db is None or not session_key:
        return
    try:
        session_id = store.peek_session_id(session_key)
        if not session_id:
            return
        row = db._read_one(
            "SELECT COALESCE(NULLIF(message_uid, ''), CAST(id AS TEXT)), content FROM messages "
            "WHERE session_id = ? AND active = 1 AND role = 'user' AND content LIKE ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, SWITCH_PREFIX + "%"),
        )
        if not row:
            return
        # message_uid survives compaction, so a compacted copy is not a new switch.
        marker_key = str(row[0])
        if store.get_session_metadata(session_key, "adopted_model_switch") == marker_key:
            return
        target = parse_switch_marker(str(row[1] or ""))
        if target:
            model, provider = target
            runner._rehydrate_session_model_override(session_key)
            current = runner._session_model_override(session_key) or {}
            if (current.get("model"), current.get("provider")) != (model, provider):
                store.set_model_override(session_key, {"model": model, "provider": provider})
                state = runner._peek_session_state(session_key)
                if state is not None:
                    state.conversation.model_override = None  # rehydrate re-resolves credentials
                runner._rehydrate_session_model_override(session_key)
                runner._evict_cached_agent(session_key)
                logger.info("Adopted Desktop model switch for session=%s: model=%s provider=%s",
                            session_key, model, provider)
        store.set_session_metadata(session_key, "adopted_model_switch", marker_key)
    except Exception:
        logger.warning("Failed to adopt Desktop model switch for session=%s", session_key, exc_info=True)


# ------------------------------------------------------------- Desktop side

def adopt_gateway_switch(sid: str, session: dict) -> None:
    """Desktop turn start: follow a /model switch the gateway made for this session."""
    agent = session.get("agent")
    key = str(session.get("session_key") or "").strip()
    if agent is None or not key:
        return
    try:
        db = getattr(agent, "_session_db", None)
        if db is None:
            from tui_gateway.server import _get_db
            db = _get_db()
        row = db.get_session(key) if db is not None else None
        if not row:
            return
        target = gateway_switch_target(
            row.get("model_config"), getattr(agent, "model", "") or "", getattr(agent, "provider", "") or "")
        if not target:
            return
        model, provider = target
        signature = f"{provider}/{model}"
        if session.get("hdr_adopted_gateway_model") == signature:
            return  # tried already; a failed switch must not retry every turn
        session["hdr_adopted_gateway_model"] = signature
        from tui_gateway.model_switch import _apply_model_switch
        _apply_model_switch(sid, session, f"{model} --provider {provider}", confirm_expensive_model=True,
                            persist_override=False, count_switch=False)
        logger.info("Adopted gateway model switch for session=%s: model=%s provider=%s", key, model, provider)
    except Exception:
        logger.warning("Failed to adopt gateway model switch for session=%s", key, exc_info=True)
