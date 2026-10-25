"""One model per session across Desktop and the Discord gateway (model_sync.py),
plus the installer hooks that wire it in and survive `hermes update`.

Run directly: python test_model_sync.py
"""
import os
import py_compile
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import model_sync as ms

MARKER = ms.SWITCH_PREFIX + "{m} via provider {p}. From this point forward, use this runtime metadata.]"


def test_parse_switch_marker():
    assert ms.parse_switch_marker(MARKER.format(m="claude-opus-5-5", p="anthropic")) == ("claude-opus-5-5", "anthropic")
    assert ms.parse_switch_marker(MARKER.format(m="gpt-6.1-sol", p="openai-codex")) == ("gpt-6.1-sol", "openai-codex")
    assert ms.parse_switch_marker(ms.SWITCH_PREFIX + "x. From this point") is None  # no provider
    assert ms.parse_switch_marker("hello") is None


def test_gateway_switch_target():
    import json
    gw_model = json.dumps({"model": "gpt-x", "provider": "openai-codex",
                           "gateway_runtime": {"provider": "openai-codex"}})
    assert ms.gateway_switch_target(gw_model, "claude-opus-5-5", "anthropic") == ("gpt-x", "openai-codex")
    assert ms.gateway_switch_target(gw_model, "gpt-x", "openai-codex") is None  # already there
    # Desktop wrote the top-level keys (gateway still on its own provider): not a gateway switch.
    desktop = json.dumps({"model": "claude-opus-5-5", "provider": "anthropic",
                          "gateway_runtime": {"provider": "openai-codex"}})
    assert ms.gateway_switch_target(desktop, "claude-opus-5-5", "anthropic") is None
    assert ms.gateway_switch_target(None, "a", "b") is None
    assert ms.gateway_switch_target("not json", "a", "b") is None


class _Store:
    def __init__(self):
        self.meta, self.persisted = {}, None

    def peek_session_id(self, key):
        return "S1"

    def get_session_metadata(self, key, name, default=None):
        return self.meta.get(name, default)

    def set_session_metadata(self, key, name, value):
        self.meta[name] = value
        return True

    def set_model_override(self, key, override):
        self.persisted = override


class _DB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                          "content TEXT, active INTEGER, message_uid TEXT)")

    def add(self, content, uid=None, role="user"):
        self.conn.execute("INSERT INTO messages (session_id, role, content, active, message_uid) VALUES ('S1',?,?,1,?)",
                          (role, content, uid))

    def _read_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()


class _Runner:
    def __init__(self):
        self.session_store, self.db = _Store(), _DB()
        self._session_db = types.SimpleNamespace(_db=self.db)
        self.state = types.SimpleNamespace(conversation=types.SimpleNamespace(model_override=None))
        self.evicted = 0

    def _rehydrate_session_model_override(self, key):
        conv = self.state.conversation
        if conv.model_override is None and self.session_store.persisted:
            conv.model_override = dict(self.session_store.persisted, api_key="resolved")

    def _session_model_override(self, key):
        return self.state.conversation.model_override

    def _peek_session_state(self, key):
        return self.state

    def _evict_cached_agent(self, key):
        self.evicted += 1


def test_gateway_adopts_desktop_switch_once():
    r = _Runner()
    ms.adopt_desktop_switch(r, "k")  # no marker yet
    assert r.state.conversation.model_override is None and r.evicted == 0

    r.db.add(MARKER.format(m="claude-opus-5-5", p="anthropic"), uid="u1")
    ms.adopt_desktop_switch(r, "k")
    o = r.state.conversation.model_override
    assert (o["model"], o["provider"], o["api_key"]) == ("claude-opus-5-5", "anthropic", "resolved")
    assert r.session_store.persisted == {"model": "claude-opus-5-5", "provider": "anthropic"}
    assert r.evicted == 1

    # A later Discord /model wins: the same marker is never re-applied.
    r.state.conversation.model_override = {"model": "gpt-x", "provider": "openai-codex"}
    ms.adopt_desktop_switch(r, "k")
    assert r.state.conversation.model_override["model"] == "gpt-x" and r.evicted == 1

    # A compacted copy of the same marker keeps its uid: still not a new switch.
    r.db.add(MARKER.format(m="claude-opus-5-5", p="anthropic"), uid="u1")
    ms.adopt_desktop_switch(r, "k")
    assert r.state.conversation.model_override["model"] == "gpt-x"

    # A NEW Desktop switch is adopted.
    r.db.add(MARKER.format(m="claude-sonnet-5", p="anthropic"), uid="u2")
    ms.adopt_desktop_switch(r, "k")
    assert r.state.conversation.model_override["model"] == "claude-sonnet-5" and r.evicted == 2


def test_gateway_marker_matching_current_override_is_a_noop():
    r = _Runner()
    r.state.conversation.model_override = {"model": "m", "provider": "p"}
    r.db.add(MARKER.format(m="m", p="p"), uid="u1")
    ms.adopt_desktop_switch(r, "k")
    assert r.evicted == 0 and r.session_store.meta["adopted_model_switch"] == "u1"


def test_desktop_adopts_gateway_switch_once():
    import json
    calls = []
    fake = types.ModuleType("tui_gateway.model_switch")
    fake._apply_model_switch = lambda sid, session, raw, **kw: calls.append((sid, raw, kw))
    pkg = types.ModuleType("tui_gateway")
    saved = {k: sys.modules.get(k) for k in ("tui_gateway", "tui_gateway.model_switch")}
    sys.modules["tui_gateway"], sys.modules["tui_gateway.model_switch"] = pkg, fake
    try:
        row = {"model_config": json.dumps({"model": "gpt-x", "provider": "openai-codex",
                                           "gateway_runtime": {"provider": "openai-codex"}})}
        db = types.SimpleNamespace(get_session=lambda key: row)
        agent = types.SimpleNamespace(model="claude-opus-5-5", provider="anthropic", _session_db=db)
        session = {"agent": agent, "session_key": "S1"}
        ms.adopt_gateway_switch("sid", session)
        assert calls == [("sid", "gpt-x --provider openai-codex",
                          {"confirm_expensive_model": True, "persist_override": False, "count_switch": False})]
        ms.adopt_gateway_switch("sid", session)  # failed/ignored switch must not retry every turn
        assert len(calls) == 1
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _hermes_agent_dir():
    home = Path(os.environ.get("HERMES_HOME") or Path(os.environ.get("LOCALAPPDATA", "")) / "hermes")
    a = home / "hermes-agent"
    return a if (a / "gateway" / "run_turn.py").exists() else None


def test_install_hooks_land_on_real_hermes_files_and_compile():
    import install
    real = _hermes_agent_dir()
    if real is None:
        print("  (skipped: no Hermes install)")
        return
    tmp = Path(tempfile.mkdtemp())
    try:
        for rel, anchor, insert in (("gateway/run_turn.py", install.RT_ANCHOR, install.RT_INSERT),
                                    ("tui_gateway/prompt_turn.py", install.PT_SYNC_ANCHOR, install.PT_SYNC_INSERT)):
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            text = (real / rel).read_text(encoding="utf-8")
            if install.MODEL_SYNC_MARKER in text:  # live install already patched: use its backup
                text = (real / (rel + ".hdr-backup")).read_text(encoding="utf-8")
            dst.write_text(text, encoding="utf-8")
            assert install.patch_file(dst, install.MODEL_SYNC_MARKER, anchor, insert) == "patched", rel
            assert install.patch_file(dst, install.MODEL_SYNC_MARKER, anchor, insert) == "already applied"
            py_compile.compile(str(dst), doraise=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_backup_is_refreshed_after_an_update_reset():
    import install
    tmp = Path(tempfile.mkdtemp())
    try:
        f = tmp / "run_turn.py"
        backup = f.with_suffix(".py.hdr-backup")
        backup.write_text("OLD HERMES VERSION\n", encoding="utf-8")
        f.write_text("x = 1\n" + install.RT_ANCHOR, encoding="utf-8")  # update reset: no marker
        install.patch_file(f, install.MODEL_SYNC_MARKER, install.RT_ANCHOR, install.RT_INSERT)
        assert backup.read_text(encoding="utf-8") == "x = 1\n" + install.RT_ANCHOR
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_supervisor_self_repair_restarts_once_and_never_spams():
    import desktop_supervisor as ds
    real = {k: getattr(ds, k) for k in ("patches_missing", "repair_patches",
                                        "restart_gateway_after_repair", "desktop_running")}
    calls = {"repair": 0, "restart": 0}
    try:
        ds.desktop_running = lambda: False
        ds.restart_gateway_after_repair = lambda: calls.__setitem__("restart", calls["restart"] + 1)

        # Broken in a way repair can't fix (anchor moved): one attempt, then quiet.
        ds.patches_missing = lambda: ["run_turn.py:model sync"]
        ds.repair_patches = lambda missing: calls.__setitem__("repair", calls["repair"] + 1) or False
        state = {}
        ds.self_repair(state)
        ds.self_repair(state)
        assert calls == {"repair": 1, "restart": 0}

        # Repairable: patches back, gateway restarted to load them.
        ds.repair_patches = lambda missing: calls.__setitem__("repair", calls["repair"] + 1) or True
        ds.patches_missing = lambda: ["prompt_turn.py:model sync"]
        ds.self_repair(state)
        assert calls == {"repair": 2, "restart": 1}

        ds.patches_missing = lambda: []
        ds.self_repair(state)
        assert calls == {"repair": 2, "restart": 1} and state == {}
    finally:
        for k, v in real.items():
            setattr(ds, k, v)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
