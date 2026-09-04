#!/usr/bin/env python3
"""
Hermes Desktop lifecycle supervisor.

Binds the background services to the Hermes Desktop app instead of to the
Windows login session:

    Hermes Desktop opens  -> start gateway + Desktop->Discord mirror
    Hermes Desktop closes -> stop  gateway + mirror

Only this supervisor autostarts at login; it idles at ~0% CPU until the
desktop app appears. Without it, `Hermes_Gateway.vbs` / `Hermes_Desktop_Mirror.vbs`
would keep both services alive for the whole login session.

Notes
-----
* Electron runs several processes named Hermes.exe (main + renderers + GPU).
  We treat "any Hermes.exe alive" as "desktop is open", so closing a window
  that leaves a background process running still counts as open.
* Shutdown is debounced: the app must be gone for STOP_GRACE_POLLS consecutive
  polls before we tear services down, so an app restart/update doesn't thrash
  the gateway.
"""

from __future__ import annotations

import os
import sys
import time
import signal
import logging
import subprocess
from pathlib import Path
from typing import Optional, List

import psutil

REPO_DIR = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
VENV_PY = HERMES_HOME / "hermes-agent" / "venv" / "Scripts" / "python.exe"
MIRROR_PY = REPO_DIR / "desktop_mirror.py"

DESKTOP_PROCESS_NAMES = {"hermes.exe"}
IS_WINDOWS = os.name == "nt"

POLL_SECONDS = 3.0
STOP_GRACE_POLLS = 3          # ~9s of "desktop gone" before stopping services
START_SETTLE_SECONDS = 1.5

LOG_PATH = HERMES_HOME / "logs" / "desktop_supervisor.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("supervisor")


# ---------------------------------------------------------------- detection

def _iter_procs(attrs: List[str]):
    for p in psutil.process_iter(attrs):
        try:
            yield p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def desktop_running() -> bool:
    me = os.getpid()
    for p in _iter_procs(["pid", "name"]):
        try:
            if p.pid == me:
                continue
            if (p.info.get("name") or "").lower() in DESKTOP_PROCESS_NAMES:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _cmdline(p) -> str:
    try:
        return " ".join(p.info.get("cmdline") or [])
    except Exception:
        return ""


def gateway_procs() -> List[psutil.Process]:
    out = []
    me = os.getpid()
    for p in _iter_procs(["pid", "name", "cmdline"]):
        if p.pid == me:
            continue
        cl = _cmdline(p)
        if "hermes_cli.main" in cl and " gateway " in f" {cl} ":
            out.append(p)
    return out


def mirror_procs() -> List[psutil.Process]:
    out = []
    me = os.getpid()
    for p in _iter_procs(["pid", "name", "cmdline"]):
        if p.pid == me:
            continue
        cl = _cmdline(p)
        if "desktop_mirror.py" in cl:
            out.append(p)
    return out


# ------------------------------------------------------------------ actions

def _hermes_env() -> dict:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["PYTHONIOENCODING"] = "utf-8"
    # Never let a stale mapping leak into the child: config.yaml is the source
    # of truth and the adapter re-derives this on startup.
    env.pop("DISCORD_CHANNEL_PROJECTS", None)
    return env


CREATE_NO_WINDOW = 0x08000000


def _hermes_startup_entry() -> Optional[Path]:
    """Path of Hermes's OWN autostart entry, if this platform has one."""
    if not IS_WINDOWS:
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / "Hermes_Gateway.vbs")


def suppress_hermes_autostart() -> None:
    """Remove Hermes's own login autostart entry.

    `hermes gateway start` re-registers `Hermes_Gateway.vbs` in the Startup
    folder every time it runs (hermes_cli/gateway_windows.py install()), so
    deleting it once is not enough -- this supervisor starts the gateway, which
    puts the entry straight back. Left alone, the gateway would then ALSO start
    at login, defeating the point of tying it to the desktop app.

    We re-remove it after every start. Hermes only uses the entry for login
    autostart; deleting it does not affect the running gateway.
    """
    entry = _hermes_startup_entry()
    if entry is None:
        return
    try:
        if entry.exists():
            entry.unlink()
            log.info("removed Hermes's own autostart entry (%s)", entry.name)
    except Exception as e:
        log.debug("could not remove %s: %s", entry, e)


def start_gateway() -> None:
    if gateway_procs():
        log.info("gateway already running")
        return
    log.info("starting gateway")
    try:
        subprocess.run(
            [str(VENV_PY), "-m", "hermes_cli.main", "gateway", "start"],
            cwd=str(HERMES_HOME), env=_hermes_env(),
            capture_output=True, text=True, timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log.error("gateway start failed: %s", e)
    # `gateway start` re-adds Hermes's login autostart entry; strip it again so
    # the gateway stays bound to the desktop app.
    suppress_hermes_autostart()


def stop_gateway() -> None:
    if not gateway_procs():
        return
    log.info("stopping gateway")
    try:
        subprocess.run(
            [str(VENV_PY), "-m", "hermes_cli.main", "gateway", "stop"],
            cwd=str(HERMES_HOME), env=_hermes_env(),
            capture_output=True, text=True, timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log.error("gateway stop failed: %s", e)
    # Backstop: anything still alive after a clean stop gets terminated.
    for p in gateway_procs():
        try:
            p.terminate()
        except Exception:
            pass
    gone, alive = psutil.wait_procs(gateway_procs(), timeout=10)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass


def start_mirror() -> None:
    if mirror_procs():
        log.info("mirror already running")
        return
    if not MIRROR_PY.exists():
        log.warning("mirror script missing: %s", MIRROR_PY)
        return
    log.info("starting mirror")
    try:
        subprocess.Popen(
            [str(VENV_PY), str(MIRROR_PY)],
            cwd=str(REPO_DIR), env=_hermes_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log.error("mirror start failed: %s", e)


def stop_mirror() -> None:
    procs = mirror_procs()
    if not procs:
        return
    log.info("stopping mirror (%d proc)", len(procs))
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=8)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    # The lock file is released with the process handle; remove the stale file
    # so a later start isn't confused by leftovers on some filesystems.
    try:
        (HERMES_HOME / "desktop_mirror.lock").unlink(missing_ok=True)
    except Exception:
        pass


def start_services() -> None:
    start_gateway()
    time.sleep(START_SETTLE_SECONDS)
    start_mirror()


def stop_services() -> None:
    stop_mirror()
    stop_gateway()


# -------------------------------------------------------------- single-inst

def acquire_lock():
    lock_path = HERMES_HOME / "desktop_supervisor.lock"
    try:
        if not lock_path.exists():
            lock_path.write_text("0", encoding="utf-8")
        fh = open(lock_path, "r+")
        if os.path.getsize(lock_path) == 0:
            fh.write("0"); fh.flush(); fh.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                log.info("another supervisor holds the lock — exiting")
                sys.exit(0)
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                log.info("another supervisor holds the lock — exiting")
                sys.exit(0)
        try:
            fh.seek(1); fh.truncate(1)
            fh.write(f" pid={os.getpid()}\n"); fh.flush()
        except Exception:
            pass
        return fh
    except SystemExit:
        raise
    except Exception as e:
        log.debug("lock unavailable (%s) — continuing", e)
        return None


# ------------------------------------------------------------------- main

def main() -> None:
    _lock = acquire_lock()  # noqa: F841 — held for process lifetime

    stopping = False

    def _bye(*_a):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        log.info("supervisor exiting — stopping services")
        stop_services()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    # Adopt reality on boot instead of assuming a clean slate.
    suppress_hermes_autostart()
    desktop_up = desktop_running()
    services_up = bool(gateway_procs())
    log.info("supervisor started (desktop=%s, gateway=%s, mirror=%s)",
             desktop_up, services_up, bool(mirror_procs()))
    if desktop_up:
        start_services()
        services_up = True
    elif services_up:
        log.info("desktop closed but services running — stopping")
        stop_services()
        services_up = False

    missing_polls = 0
    try:
        while True:
            time.sleep(POLL_SECONDS)
            up = desktop_running()

            if up:
                missing_polls = 0
                if not services_up:
                    log.info("desktop opened -> starting services")
                    start_services()
                    services_up = True
                else:
                    # Self-heal a service that died on its own.
                    if not gateway_procs():
                        log.warning("gateway vanished — restarting")
                        start_gateway()
                    if not mirror_procs():
                        log.warning("mirror vanished — restarting")
                        start_mirror()
            else:
                if services_up:
                    missing_polls += 1
                    if missing_polls >= STOP_GRACE_POLLS:
                        log.info("desktop closed -> stopping services")
                        stop_services()
                        services_up = False
                        missing_polls = 0
    except KeyboardInterrupt:
        _bye()


if __name__ == "__main__":
    main()
