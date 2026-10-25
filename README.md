# Hermes Discord Router

Turn Discord into a second UI for [Hermes](https://github.com/NousResearch/hermes-agent).

Map each Discord channel to a project folder, so a message in `#myproject` runs
with that project's working directory — its own memory, its own `AGENTS.md`, its
own files. Then mirror your **Hermes Desktop** conversations back into Discord as
per-session threads, so both surfaces show the same work.

```
#home    ->  C:/Users/you              Discord msg  ->  routed to project cwd
#myproj  ->  D:/code/myproj            Desktop turn ->  mirrored to a thread
```

## What you get

| Piece | What it does |
|---|---|
| **Channel routing** | A message in `#channel` runs in that channel's project folder |
| **Desktop mirror** | Prompts typed in Hermes Desktop appear in the mapped channel as a thread |
| **Lifecycle supervisor** | Gateway + mirror start when you open Hermes Desktop, stop when you close it |
| **Model sync** | A `/model` switch in Desktop reaches the same session's Discord thread, and the other way round |
| **Self-repair** | Re-applies the gateway patches by itself after a `hermes update` wipes them |

Hermes already handles Discord→Hermes messaging. This adds the *project routing*
and the *Desktop→Discord* direction.

## Requirements

- Hermes Agent with the Discord platform working (`DISCORD_BOT_TOKEN` in your
  Hermes `.env`, bot in your server)
- Python 3.9+ (the installer uses Hermes's own venv)
- Windows for the autostart supervisor; routing and mirroring work anywhere

## Install

```bash
git clone https://github.com/YOURNAME/hermes-discord-router
cd hermes-discord-router
python install.py
```

The installer:
1. finds your Hermes home (`HERMES_HOME`, or the usual per-OS locations)
2. copies `discord_router.py` into it
3. patches two gateway files (backed up first, skipped if already applied)
4. writes `mirror.config.yaml`
5. installs the desktop supervisor to autostart

It is **idempotent** — re-run it any time (e.g. after a Hermes update).

Then add the printed block to your Hermes `config.yaml`:

```yaml
discord:
  channels:
    - id: '1234567890123456789'
      folder: C:/Users/you
    - id: '9876543210987654321'
      folder: D:/code/myproject
```

Restart: `hermes gateway restart`

> Get a channel ID: enable **Settings → Advanced → Developer Mode**, then
> right-click a channel → **Copy Channel ID**.

### Uninstall

```bash
python install.py --uninstall
```

Reverts both patches from backups and removes the autostart entry.

## Configuration

**`folder:`** (recommended) is an absolute path used as-is.
**`project:`** is a project *name* looked up in Hermes's `projects.db`; if that
DB is empty the lookup fails and routing is skipped for that channel. Prefer
`folder:` unless you know your projects are registered.

`mirror.config.yaml` controls the Desktop→Discord direction:

```yaml
bot_token: ${DISCORD_BOT_TOKEN}   # or omit to read Hermes's .env
hermes_home: C:/Users/you/AppData/Local/hermes
poll_seconds: 2.0
channels:
  - id: '9876543210987654321'
    cwd: D:/code/myproject        # must match the session's cwd
```

## How it works

**Routing.** The Discord adapter reads `discord.channels` and bridges it to an
env var; the gateway resolves the channel (or its parent, for threads) to a
folder and sets the session's cwd — which is also how Hermes Desktop groups
sessions into projects.

**Mirroring.** Hermes Desktop and the gateway are separate processes that share
`state.db`. The mirror tails that DB for turns typed in Desktop and posts them
to Discord over the **REST API** — not a second gateway connection, since
Discord allows only one WebSocket per bot token and the gateway owns it.

Origin is decided **per message**, not per session: a session's `source` stays
`discord` even for turns typed in Desktop, so a per-session filter would mirror
nothing. Messages relayed from Discord (marked by a platform message ID or the
gateway's `[Name] ` prefix) are skipped, which is also what prevents echo loops.

**Model sync.** A Discord thread opened in Desktop is one session driven by two
processes, and Hermes keeps each one's `/model` choice to itself. `model_sync.py`
is called at the start of every turn on both sides. A Desktop switch is stored
as a marker message, and the gateway adopts the newest one once as the session's
`/model` override, so a later Discord `/model` still wins. A gateway `/model`
writes the route to both the top-level `model_config` keys and `gateway_runtime`,
and Desktop applies it as a session-only switch. Each switch goes through
Hermes's own switch path, so the other side sees its own value and does nothing.

**Supervisor.** Polls for the Hermes Desktop process and starts/stops both
services with it. Shutdown is debounced ~9s so an app restart doesn't thrash the
gateway; a service that dies while the app is open is restarted automatically.

## Why it patches the gateway

Routing has to set the session cwd *before* the session is created. Hermes's
plugin/hook system fires after that point and can't modify session context, so
there is no plugin-only path. The patches are two small anchored inserts, backed
up and reversible.

**A Hermes update overwrites them.** The supervisor notices within a minute
(immediately when the update marker clears), runs `python install.py --repair`
and restarts a running gateway so it loads the patches. Desktop picks its patch
up the next time it is reopened. If Hermes moved the code a patch anchors to,
the repair is attempted once and the reason is logged to
`logs/desktop_supervisor.log`. You can still run `python install.py --repair`
(or plain `python install.py`) by hand any time.

## Troubleshooting

**Messages go to the wrong project.** Check the gateway log:
`grep "DISCORD_CHANNEL_PROJECTS" gateway.log` — it should show your folders. If
it shows old values, the gateway inherited a stale env var from a previous
process; do a full `hermes gateway stop && hermes gateway start`.

**Nothing mirrors to Discord.** Confirm the session's cwd matches a `cwd:` in
`mirror.config.yaml` exactly:
`sqlite3 state.db "SELECT cwd FROM sessions ORDER BY started_at DESC LIMIT 3"`

**A phantom project appeared in the sidebar.** A channel pointed at a folder
that isn't a real project; Hermes auto-creates a project from any cwd. Fix the
`folder:` value and repoint affected sessions.

**Desktop prompts appear twice.** On Hermes versions with native out-of-band
history adoption, the installer removes this repo's older history-resync hooks.
Running both reloaded the current submit-time user row into history before Hermes
added the prompt for the turn. Re-run `python install.py`, then close and reopen
Desktop so its backend loads the repaired code. Restarting only the Discord gateway
is not enough. Older Hermes versions still receive the legacy hook.

**Desktop only shows Discord turns after reopening.** The installer removes a
sidebar-cache shortcut from Desktop's tile refresh. A change event could arrive
before the sidebar list finished refreshing, causing another open pane to skip
its transcript read. This repair updates both the TypeScript source and recognized
unpacked packaged-renderer code, preserving the existing transcript-signature and
live-turn guards. Close and reopen Desktop once after repair; gateway restart alone
does not reload the renderer. Chat data is not changed. Backups are restored by
`python install.py --uninstall`; unknown source/bundle shapes are not guessed.

For the optional real Hermes hook regression (requires its installed JS test dependencies):
`python test_desktop_live_refresh.py --desktop PATH/TO/hermes-agent/apps/desktop`.
It runs a temporary copy of the upstream background-sync suite with the obsolete
metadata-shortcut assertion adapted and the delayed-publication regression added.

**Old turns repeat after compression.** The mirror skips retained storage copies
using their stable `message_uid` when an earlier copy is already behind its saved
cursor. Separate prompts with the same text still mirror. Restart the mirror after
updating this repo; no Hermes core patch or cursor reset is needed. Older Hermes
schemas without `message_uid` retain the original row-based behavior.

**Services don't start with the app.** Check `logs/desktop_supervisor.log`.

## Files

| File | Role |
|---|---|
| `install.py` | Installer / uninstaller |
| `discord_router.py` | Channel → folder resolution (copied into Hermes home) |
| `desktop_mirror.py` | Desktop → Discord mirror service |
| `desktop_supervisor.py` | Starts/stops services with Hermes Desktop; re-applies patches after updates |
| `model_sync.py` | One model per session across Desktop and Discord (copied into Hermes home) |

## License

MIT
