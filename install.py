#!/usr/bin/env python3
"""
Installer for Hermes Discord Router + Desktop mirror.

What it does (idempotent -- safe to re-run):
  1. Locates your Hermes home and the hermes-agent source tree.
  2. Copies discord_router.py into Hermes home.
  3. Patches the two gateway files that cannot be reached from a plugin:
       - plugins/platforms/discord/adapter.py  (read discord.channels config)
       - gateway/run.py                        (apply the channel -> cwd route)
     Every patch is anchored on a unique string, backed up first, and skipped
     if already applied.
  4. Writes mirror.config.yaml from your answers (or --channel flags).
  5. Optionally installs the desktop supervisor to autostart.

Usage
-----
    python install.py                 # interactive
    python install.py --uninstall     # revert patches, remove autostart
    python install.py --no-autostart  # patch + config only
"""

from __future__ import annotations

import os
import sys
import shutil
import argparse
import platform
import subprocess
from pathlib import Path

from desktop_live_refresh import apply_desktop_live_refresh, revert_desktop_live_refresh

REPO = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"


# ----------------------------------------------------------------- discovery

def hermes_home() -> Path:
    hh = os.environ.get("HERMES_HOME")
    if hh and Path(hh).exists():
        return Path(hh)
    candidates = []
    if IS_WINDOWS:
        la = os.environ.get("LOCALAPPDATA")
        if la:
            candidates.append(Path(la) / "hermes")
    candidates += [
        Path.home() / ".hermes",
        Path.home() / ".local" / "share" / "hermes",
        Path.home() / "Library" / "Application Support" / "hermes",
    ]
    for c in candidates:
        if (c / "config.yaml").exists() or (c / "hermes-agent").exists():
            return c
    raise SystemExit(
        "Could not find your Hermes home. Set HERMES_HOME and re-run:\n"
        "  Windows: set HERMES_HOME=%LOCALAPPDATA%\\hermes\n"
        "  macOS/Linux: export HERMES_HOME=~/.hermes"
    )


def agent_dir(home: Path) -> Path:
    d = home / "hermes-agent"
    if not d.exists():
        raise SystemExit(f"hermes-agent not found under {home}")
    return d


def venv_python(home: Path) -> Path:
    a = agent_dir(home)
    p = a / "venv" / ("Scripts" if IS_WINDOWS else "bin") / ("python.exe" if IS_WINDOWS else "python")
    return p if p.exists() else Path(sys.executable)


# -------------------------------------------------------------------- patch

ADAPTER_ANCHOR = '    _gate("no_thread_channels", "DISCORD_NO_THREAD_CHANNELS", from_platform_extra=False)\n'
ADAPTER_MARKER = "# channel_projects: route channels to specific Hermes projects."
ADAPTER_PATCH = '''
    # channel_projects: route channels to specific Hermes projects.
    # Each entry has 'id' (channel ID) and either 'project' (a name resolved
    # against projects.db) or 'folder' (an explicit path, used as-is).
    channel_projects = discord_cfg.get("channels")
    if isinstance(channel_projects, list):
        channel_project_map = {}
        for entry in channel_projects:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            target = entry.get("folder") or entry.get("project")
            if target:
                channel_project_map[str(entry["id"])] = str(target)
        if channel_project_map:
            seeded_extra["channel_projects"] = channel_project_map
            # config.yaml is the source of truth and ALWAYS overwrites: a
            # gateway restart re-spawns from the old process and inherits its
            # environment, so an `if not os.getenv()` guard would make the new
            # gateway silently keep the PREVIOUS run's stale mapping.
            import json
            _new_map = json.dumps(channel_project_map)
            _old_map = os.environ.get("DISCORD_CHANNEL_PROJECTS")
            if _old_map != _new_map:
                os.environ["DISCORD_CHANNEL_PROJECTS"] = _new_map
                logger.info("Set DISCORD_CHANNEL_PROJECTS: %s", _new_map)
'''

RUN_ANCHOR = '        return set_session_vars(\n            platform=context.source.platform.value,\n'
RUN_MARKER = "# Discord channel-project routing (external module)"
RUN_PATCH = '''        # Discord channel-project routing (external module)
        _channel_cwd = ""
        if context.source.platform == Platform.DISCORD:
            try:
                import sys as _sys
                _hh = os.environ.get("HERMES_HOME") or os.path.join(
                    os.environ.get("LOCALAPPDATA")
                    or os.path.join(os.path.expanduser("~"), ".local", "share"),
                    "hermes",
                )
                if _hh not in _sys.path:
                    _sys.path.insert(0, _hh)
                from discord_router import route_discord_channel
                _channel_cwd = route_discord_channel(context, self._session_db) or ""
            except ImportError:
                pass
            except Exception as _e:
                logger.debug("Discord routing failed: %s", _e)

'''
RUN_CWD_OLD = "            cron_session=\"\")"
RUN_CWD_NEW = "            cron_session=\"\",\n            cwd=_channel_cwd)"


def patch_file(path: Path, marker: str, anchor: str, insert: str, *, after=True) -> str:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return "already applied"
    if anchor not in text:
        return f"ANCHOR NOT FOUND -- patch manually (looked for: {anchor[:60]!r})"
    backup = path.with_suffix(path.suffix + ".hdr-backup")
    # No router marker at all = a pristine file (fresh install, or a `hermes update`
    # reset it). A backup left from before the update would restore OLD Hermes
    # code on --uninstall, so refresh it.
    if not backup.exists() or not any(m in text for m in _router_markers()):
        backup.write_text(text, encoding="utf-8")
    text = text.replace(anchor, (anchor + insert) if after else (insert + anchor), 1)
    path.write_text(text, encoding="utf-8")
    return "patched"


def apply_patches(home: Path) -> None:
    a = agent_dir(home)
    adapter = a / "plugins" / "platforms" / "discord" / "adapter.py"
    run = a / "gateway" / "run.py"

    for f in (adapter, run):
        if not f.exists():
            raise SystemExit(f"expected file missing: {f}")

    print("  adapter.py :", patch_file(adapter, ADAPTER_MARKER, ADAPTER_ANCHOR, ADAPTER_PATCH))

    r = patch_file(run, RUN_MARKER, RUN_ANCHOR, RUN_PATCH, after=False)
    print("  run.py     :", r)
    if r == "patched":
        t = run.read_text(encoding="utf-8")
        if "cwd=_channel_cwd" not in t:
            if RUN_CWD_OLD in t:
                t = t.replace(RUN_CWD_OLD, RUN_CWD_NEW, 1)
                run.write_text(t, encoding="utf-8")
                print("  run.py     : wired cwd= into set_session_vars")
            else:
                print("  run.py     : WARNING could not wire cwd= -- add "
                      "`cwd=_channel_cwd,` to the set_session_vars(...) call")


# Desktop-mirror threads: admit unmentioned replies and route them to the
# Desktop session, without a gateway restart (lookups go to discord_router).
HDR_IMPORT = (
    "        import sys as _hdr_sys\n"
    "        _hdr_home = os.environ.get(\"HERMES_HOME\") or os.path.join(\n"
    "            os.environ.get(\"LOCALAPPDATA\") or os.path.join(os.path.expanduser(\"~\"), \".local\", \"share\"),\n"
    "            \"hermes\")\n"
    "        if _hdr_home not in _hdr_sys.path:\n"
    "            _hdr_sys.path.insert(0, _hdr_home)\n"
)
MIRROR_ADAPTER_ANCHOR = '        thread_require_mention (multi-bot threads) gates threads like channels."""\n'
MIRROR_ADAPTER_MARKER = "# hermes-discord-router: mirror threads are bot threads"
MIRROR_ADAPTER_PATCH = (
    "        # hermes-discord-router: mirror threads are bot threads\n"
    "        try:\n"
    "            _hdr_tid = str(message.channel.id)\n"
    "            if isinstance(message.channel, discord.Thread) and _hdr_tid not in self._threads:\n"
    + HDR_IMPORT.replace("        ", "                ", 1).replace("\n        ", "\n                ") +
    "                from discord_router import mirror_session_for_thread\n"
    "                if mirror_session_for_thread(_hdr_tid):\n"
    "                    self._threads._remember(_hdr_tid)\n"
    "        except Exception as _hdr_e:\n"
    "            logger.debug(\"mirror-thread check failed: %s\", _hdr_e)\n"
)
MIRROR_SESSION_ANCHOR = "            observed = self._entries.get(session_key)\n"
MIRROR_SESSION_MARKER = "# hermes-discord-router: adopt mirror thread"
MIRROR_SESSION_PATCH = (
    "            if observed is None and not force_new:  # hermes-discord-router: adopt mirror thread\n"
    "                try:\n"
    + HDR_IMPORT.replace("        ", "                    ", 1).replace("\n        ", "\n                    ") +
    "                    from discord_router import adopt_mirror_route\n"
    "                    observed = adopt_mirror_route(self, session_key)\n"
    "                except Exception as _hdr_e:\n"
    "                    logger.debug(\"mirror-route adopt failed: %s\", _hdr_e)\n"
)


def apply_mirror_patches(home: Path) -> None:
    a = agent_dir(home)
    adapter = a / "plugins" / "platforms" / "discord" / "adapter.py"
    session = a / "gateway" / "session.py"
    print("  adapter.py :", patch_file(adapter, MIRROR_ADAPTER_MARKER, MIRROR_ADAPTER_ANCHOR, MIRROR_ADAPTER_PATCH))
    print("  session.py :", patch_file(session, MIRROR_SESSION_MARKER, MIRROR_SESSION_ANCHOR, MIRROR_SESSION_PATCH))


PT_ANCHOR_1 = "    # Snapshot after the model sync: a deferred switch's history mutation belongs to this turn.\n"
PT_INSERT_1 = ("    from tui_gateway.history_resync import adopt_foreign_writes\n"
               "    adopt_foreign_writes(session, _session_db)  # rows another process (Discord) appended\n")
PT_ANCHOR_2 = "            status_note = _commit_turn_history(session, result, st.history, st.history_version)\n"
PT_INSERT_2 = ("            from tui_gateway.history_resync import record_tail\n"
               "            record_tail(session, _session_db)\n")


def apply_history_resync(home: Path) -> None:
    """Desktop adopts rows the Discord gateway wrote to the shared session."""
    tg = agent_dir(home) / "tui_gateway"
    pt = tg / "prompt_turn.py"
    if not pt.exists():
        print("  prompt_turn.py: MISSING -- skipped history resync")
        return
    text = pt.read_text(encoding="utf-8")
    if ("def _adopt_out_of_band_turns(" in text
            and "\n    _adopt_out_of_band_turns(session)\n" in text):
        # Native adoption excludes the submit-time user row. Our older reload
        # includes it, so running both sends the current prompt to the model twice.
        repaired = text.replace(PT_INSERT_1, "").replace(PT_INSERT_2, "")
        if repaired != text:
            pt.write_text(repaired, encoding="utf-8")
            print("  prompt_turn.py: removed obsolete history hooks; reopen Desktop to activate")
        else:
            print("  prompt_turn.py: native history adoption; no extra hooks needed")
        return
    shutil.copy2(Path(__file__).parent / "history_resync.py", tg / "history_resync.py")
    print("  history_resync.py: copied")
    print("  prompt_turn.py :", patch_file(pt, "adopt_foreign_writes(session", PT_ANCHOR_1, PT_INSERT_1, after=False))
    print("  prompt_turn.py :", patch_file(pt, "record_tail(session", PT_ANCHOR_2, PT_INSERT_2))


def _model_sync_hook(indent: str, call: str) -> str:
    """A turn-start call into HERMES_HOME/model_sync.py that can never break the turn."""
    body = [
        "try:  # hermes-discord-router: model sync",
        "    import os as _hdr_os, sys as _hdr_sys",
        "    _hdr_home = _hdr_os.environ.get(\"HERMES_HOME\") or _hdr_os.path.join(",
        "        _hdr_os.environ.get(\"LOCALAPPDATA\") or _hdr_os.path.join(_hdr_os.path.expanduser(\"~\"), \".local\", \"share\"),",
        "        \"hermes\")",
        "    if _hdr_home not in _hdr_sys.path:",
        "        _hdr_sys.path.insert(0, _hdr_home)",
        f"    from model_sync import {call.split('(')[0]}",
        f"    {call}",
        "except ImportError:",
        "    pass",
    ]
    return "".join(f"{indent}{line}\n" for line in body)


MODEL_SYNC_MARKER = "# hermes-discord-router: model sync"
# Gateway: right after a persisted /model override is rehydrated, before it is read.
RT_ANCHOR = "            self._rehydrate_session_model_override(skey)\n"
RT_INSERT = _model_sync_hook("            ", "adopt_desktop_switch(self, skey)")
# Desktop: right after the config-model sync, inside the "no /model --once active" guard.
PT_SYNC_ANCHOR = "        _sync_agent_model_with_config(sid, session)\n"
PT_SYNC_INSERT = _model_sync_hook("        ", "adopt_gateway_switch(sid, session)")


def apply_model_sync(home: Path) -> None:
    """One model per session: a /model on Desktop reaches Discord and vice versa."""
    shutil.copy2(REPO / "model_sync.py", home / "model_sync.py")
    print(f"  -> {home / 'model_sync.py'}")
    a = agent_dir(home)
    for f, anchor, insert in ((a / "gateway" / "run_turn.py", RT_ANCHOR, RT_INSERT),
                              (a / "tui_gateway" / "prompt_turn.py", PT_SYNC_ANCHOR, PT_SYNC_INSERT)):
        if not f.exists():
            print(f"  {f.name}: MISSING -- skipped model sync")
            continue
        print(f"  {f.name} :", patch_file(f, MODEL_SYNC_MARKER, anchor, insert))


def _router_markers() -> tuple[str, ...]:
    """Every string the router's patches leave behind (history hooks included)."""
    return (ADAPTER_MARKER, RUN_MARKER, MIRROR_ADAPTER_MARKER, MIRROR_SESSION_MARKER,
            MODEL_SYNC_MARKER, "adopt_foreign_writes(session", "record_tail(session")


def missing_patches(home: Path) -> list[str]:
    """Patched Hermes files whose router hook is gone. Every `hermes update`
    resets the hermes-agent tree, which silently drops all of these.

    The history-resync hooks are not listed: they are intentionally absent on
    Hermes builds that adopt foreign turns natively.
    """
    a = agent_dir(home)
    expected = [
        (a / "plugins" / "platforms" / "discord" / "adapter.py", ADAPTER_MARKER),
        (a / "plugins" / "platforms" / "discord" / "adapter.py", MIRROR_ADAPTER_MARKER),
        (a / "gateway" / "run.py", RUN_MARKER),
        (a / "gateway" / "session.py", MIRROR_SESSION_MARKER),
        (a / "gateway" / "run_turn.py", MODEL_SYNC_MARKER),
        (a / "tui_gateway" / "prompt_turn.py", MODEL_SYNC_MARKER),
    ]
    missing = []
    for f, marker in expected:
        try:
            if marker not in f.read_text(encoding="utf-8"):
                missing.append(f"{f.name}:{marker.lstrip('# ')[:40]}")
        except OSError:
            missing.append(f"{f.name}:file missing")
    for module in ("discord_router.py", "model_sync.py"):
        installed, source = home / module, REPO / module
        if not installed.exists() or installed.read_bytes() != source.read_bytes():
            missing.append(f"{module}:stale")
    return missing


def revert_patches(home: Path) -> None:
    a = agent_dir(home)
    for f in (a / "plugins" / "platforms" / "discord" / "adapter.py",
              a / "gateway" / "run.py", a / "gateway" / "session.py",
              a / "gateway" / "run_turn.py",
              a / "tui_gateway" / "prompt_turn.py"):
        b = f.with_suffix(f.suffix + ".hdr-backup")
        if b.exists():
            shutil.copy2(b, f)
            b.unlink()
            print(f"  reverted {f.name}")
        else:
            print(f"  no backup for {f.name} (skipped)")


# ------------------------------------------------------------------ configs

def write_mirror_config(home: Path, channels: list[tuple[str, str]]) -> None:
    cfg = REPO / "mirror.config.yaml"
    if cfg.exists():
        print(f"  {cfg.name} exists -- leaving it alone")
        return
    lines = [
        "# Desktop -> Discord mirror config",
        "bot_token: ${DISCORD_BOT_TOKEN}",
        f"hermes_home: {home.as_posix()}",
        "poll_seconds: 2.0",
        "channels:",
    ]
    for cid, folder in channels:
        lines += [f"  - id: '{cid}'", f"    cwd: {folder}"]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote {cfg.name}")


def print_gateway_config(channels: list[tuple[str, str]]) -> None:
    print("\nAdd this to your Hermes config.yaml (top level):\n")
    print("discord:")
    print("  channels:")
    for cid, folder in channels:
        print(f"    - id: '{cid}'")
        print(f"      folder: {folder}")


# ---------------------------------------------------------------- autostart

def ensure_service_python() -> Path:
    """Return the pythonw the services should run on, deps installed.

    NOT Hermes's venv: `hermes update` refuses to start while any process holds
    that venv's native .pyd files, so a supervisor and mirror living there made
    every update abort with "another Hermes process is using this installation".

    NOT a venv of our own either: a venv is a shim over a base interpreter, and
    deleting that base (an uninstalled miniconda, here) breaks every launch with
    "did not find executable at ...\\pythonw.exe". Requirements are three pure
    packages, so `pip install --user` on a system Python needs no venv at all.
    """
    py = Path(os.environ.get("ROUTER_PYTHON") or sys.executable)
    if "hermes" in str(py).lower():
        raise SystemExit(
            f"refusing to install services onto Hermes's own python ({py}).\n"
            "  Re-run with a system Python, e.g.:\n"
            "    py -m install.py       (or set ROUTER_PYTHON=<path to python.exe>)")
    subprocess.run([str(py), "-m", "pip", "install", "--user", "-q",
                    "-r", str(REPO / "requirements.txt")], check=True)
    print(f"  services will run on {py}")
    pyw = py.with_name("pythonw.exe")
    return pyw if pyw.exists() else py


def install_autostart(home: Path) -> None:
    if not IS_WINDOWS:
        print("  autostart: only wired for Windows; on macOS/Linux run "
              "`python desktop_supervisor.py` from your session manager")
        return
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    pyw = ensure_service_python()
    vbs = startup / "Hermes_Desktop_Supervisor.vbs"
    vbs.write_text(
        "Option Explicit\n"
        "Dim sh, env\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        'Set env = sh.Environment("PROCESS")\n'
        f'env.Item("HERMES_HOME") = "{home}"\n'
        'env.Item("PYTHONIOENCODING") = "utf-8"\n'
        f'sh.CurrentDirectory = "{REPO}"\n'
        f'sh.Run "{pyw} ""{REPO / "desktop_supervisor.py"}""", 0, False\n',
        encoding="utf-8",
    )
    # Remove superseded entries from earlier versions.
    for old in ("Hermes_Gateway.vbs", "Hermes_Desktop_Mirror.vbs"):
        p = startup / old
        if p.exists():
            p.unlink()
            print(f"  removed old autostart: {old}")
    print(f"  installed {vbs.name}")


def remove_autostart() -> None:
    if not IS_WINDOWS:
        return
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    for name in ("Hermes_Desktop_Supervisor.vbs", "Hermes_Gateway.vbs", "Hermes_Desktop_Mirror.vbs"):
        p = startup / name
        if p.exists():
            p.unlink()
            print(f"  removed {name}")


# --------------------------------------------------------------------- main

def existing_channels(home: Path) -> list[tuple[str, str]]:
    """Channels already mapped in hermes's config.yaml.

    Re-running the installer is the NORMAL repair path: a Hermes update
    overwrites gateway/run.py and silently removes the patch, so the fix is to
    run install.py again. Asking for the whole channel map again at that point
    is how a repair turns into a reconfiguration.
    """
    try:
        import yaml  # optional: only needed to reuse an existing map
    except ImportError:
        return []
    cfg = home / "config.yaml"
    if not cfg.exists():
        return []
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    out = []
    for e in (data.get("discord") or {}).get("channels") or []:
        if isinstance(e, dict) and e.get("id"):
            folder = e.get("folder") or e.get("project") or ""
            if folder:
                out.append((str(e["id"]), str(folder).replace("\\", "/")))
    return out


def prompt_channels() -> list[tuple[str, str]]:
    # Non-interactive (CI, a pipe, an agent shell): returning empty leaves any
    # existing config untouched, which beats crashing on EOFError after the
    # gateway patches have already been written.
    if not sys.stdin or not sys.stdin.isatty():
        print("\nNo channel map given and stdin is not a terminal -- keeping any existing config.")
        print("Pass --channel ID=FOLDER to set one non-interactively.")
        return []
    print("\nMap Discord channels to project folders (blank ID to finish).")
    print("Channel ID: right-click a channel in Discord > Copy Channel ID")
    print("(Discord > Settings > Advanced > Developer Mode must be on)\n")
    out = []
    while True:
        try:
            cid = input("  Channel ID: ").strip()
        except EOFError:
            break
        if not cid:
            break
        try:
            folder = input("  Project folder (absolute path): ").strip()
        except EOFError:
            break
        if folder:
            out.append((cid, folder.replace("\\", "/")))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-autostart", action="store_true")
    ap.add_argument("--repair", action="store_true",
                    help="re-apply modules + patches only (no prompts); exit 1 if any is still missing")
    ap.add_argument("--channel", action="append", default=[],
                    metavar="ID=FOLDER", help="e.g. --channel 123456=D:/code/proj")
    args = ap.parse_args()

    home = hermes_home()
    print(f"Hermes home: {home}")
    print(f"Platform   : {platform.system()}")

    if args.uninstall:
        print("\nUninstalling...")
        revert_patches(home)
        revert_desktop_live_refresh(home)
        remove_autostart()
        rp = home / "discord_router.py"
        if rp.exists():
            rp.unlink()
            print("  removed discord_router.py")
        ms = home / "model_sync.py"
        if ms.exists():
            ms.unlink()
            print("  removed model_sync.py")
        print("\nDone. Restart the gateway: hermes gateway restart")
        return

    print("\n1) Installing router module")
    shutil.copy2(REPO / "discord_router.py", home / "discord_router.py")
    print(f"  -> {home / 'discord_router.py'}")

    print("\n2) Patching gateway")
    apply_patches(home)
    apply_mirror_patches(home)
    apply_history_resync(home)
    apply_desktop_live_refresh(home)
    apply_model_sync(home)

    if args.repair:
        # Non-interactive re-apply (the supervisor runs this after `hermes update`).
        missing = missing_patches(home)
        if missing:
            print("\nStill missing:", ", ".join(missing))
            sys.exit(1)
        print("\nRepaired. Restart the gateway and reopen Desktop to load the patches.")
        return

    channels = []
    for spec in args.channel:
        if "=" in spec:
            cid, folder = spec.split("=", 1)
            channels.append((cid.strip(), folder.strip().replace("\\", "/")))
    if not channels:
        # Reuse what hermes already has before asking. Repairing a patch that an
        # update wiped must not require retyping the map.
        channels = existing_channels(home)
        if channels:
            print(f"\n  reusing {len(channels)} channel(s) already in config.yaml")
    if not channels:
        channels = prompt_channels()

    if channels:
        print("\n3) Writing mirror config")
        write_mirror_config(home, channels)
        print_gateway_config(channels)

    if not args.no_autostart:
        print("\n4) Installing autostart")
        install_autostart(home)

    print("\nDone. Next:")
    print("  1. Add the discord: block above to your Hermes config.yaml")
    print("  2. Ensure DISCORD_BOT_TOKEN is in your Hermes .env")
    print("  3. hermes gateway restart")
    print("  4. Start the supervisor now (or just log out/in):")
    print(f"     {sys.executable} {REPO / 'desktop_supervisor.py'}")


if __name__ == "__main__":
    main()
