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
from pathlib import Path

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
    if not backup.exists():
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


def revert_patches(home: Path) -> None:
    a = agent_dir(home)
    for f in (a / "plugins" / "platforms" / "discord" / "adapter.py",
              a / "gateway" / "run.py"):
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

def install_autostart(home: Path) -> None:
    if not IS_WINDOWS:
        print("  autostart: only wired for Windows; on macOS/Linux run "
              "`python desktop_supervisor.py` from your session manager")
        return
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    pyw = agent_dir(home) / "venv" / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        pyw = Path(sys.executable)
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
    ap.add_argument("--channel", action="append", default=[],
                    metavar="ID=FOLDER", help="e.g. --channel 123456=D:/code/proj")
    args = ap.parse_args()

    home = hermes_home()
    print(f"Hermes home: {home}")
    print(f"Platform   : {platform.system()}")

    if args.uninstall:
        print("\nUninstalling...")
        revert_patches(home)
        remove_autostart()
        rp = home / "discord_router.py"
        if rp.exists():
            rp.unlink()
            print("  removed discord_router.py")
        print("\nDone. Restart the gateway: hermes gateway restart")
        return

    print("\n1) Installing router module")
    shutil.copy2(REPO / "discord_router.py", home / "discord_router.py")
    print(f"  -> {home / 'discord_router.py'}")

    print("\n2) Patching gateway")
    apply_patches(home)

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
    print(f"     {venv_python(home)} {REPO / 'desktop_supervisor.py'}")


if __name__ == "__main__":
    main()
