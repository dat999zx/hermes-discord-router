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

**Supervisor.** Polls for the Hermes Desktop process and starts/stops both
services with it. Shutdown is debounced ~9s so an app restart doesn't thrash the
gateway; a service that dies while the app is open is restarted automatically.

## Why it patches the gateway

Routing has to set the session cwd *before* the session is created. Hermes's
plugin/hook system fires after that point and can't modify session context, so
there is no plugin-only path. The patches are two small anchored inserts, backed
up and reversible.

**A Hermes update overwrites them.** Re-run `python install.py` afterwards — it
detects what is already applied and only fixes what is missing.

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

**Services don't start with the app.** Check `logs/desktop_supervisor.log`.

## Files

| File | Role |
|---|---|
| `install.py` | Installer / uninstaller |
| `discord_router.py` | Channel → folder resolution (copied into Hermes home) |
| `desktop_mirror.py` | Desktop → Discord mirror service |
| `desktop_supervisor.py` | Starts/stops services with Hermes Desktop |
| `router.py` | Standalone bot (superseded; kept for reference) |

## License

MIT
