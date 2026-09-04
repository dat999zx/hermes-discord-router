# Hermes Discord Router

Route Discord channels to Hermes projects. Each channel maps to a project folder — messages in `#knowl` go to your `knowl` project's memory, messages in `#my-app` go to `my-app`, etc.

```
Discord #knowl  ──────►  Hermes 'knowl' project  ──────►  Knowl memory stored there
Discord #my-app ──────►  Hermes 'my-app' project ──────►  Knowl memory stored there
```

## Why

By default, all Discord messages land in your Home project. This means project-specific context gets mixed together. This router keeps each project's memory isolated to its channel.

## Installation

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/hermes-discord-router.git
cd hermes-discord-router

# Install dependencies
pip install -r requirements.txt

# Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your Discord bot token and channel mappings
```

## Configuration

Edit `config.yaml`:

```yaml
discord:
  token: "YOUR_BOT_TOKEN"
  
channels:
  - id: "1234567890123456789"  # Discord channel ID
    project: "knowl"           # Hermes project name
    folder: "D:/projects/knowl"  # Optional: explicit path
    
  - id: "9876543210987654321"
    project: "my-app"
    folder: "D:/projects/my-app"

hermes:
  # Path to Hermes CLI (optional, auto-detected)
  cli: "hermes"
  # Or for dev builds:
  # cli: "C:/Users/Admin/AppData/Local/hermes/knowl-dev.cmd"
```

### Getting Channel IDs

1. Enable Developer Mode in Discord (Settings → App Settings → Advanced → Developer Mode)
2. Right-click a channel → Copy Channel ID

## Usage

### As a standalone bot

```bash
python router.py
```

The bot will:
1. Connect to Discord
2. Watch configured channels
3. Route messages to the correct Hermes project
4. Store context in each project's Knowl memory

### As a Hermes plugin (coming soon)

Native integration with Hermes gateway — no separate bot needed.

## How It Works

1. **Message arrives** in Discord channel `#knowl` (ID: 1234...)
2. **Router looks up** channel ID → finds project `knowl` at `D:/projects/knowl`
3. **Spawns Hermes** with `--cwd D:/projects/knowl` so the session runs in that project
4. **Knowl stores** the conversation in that project's `.knowl/` directory
5. **Response** goes back to Discord

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Discord Bot    │────►│  Router Process  │────►│  Hermes CLI     │
│  (discord.py)   │     │  (channel→proj)  │     │  (per-project)  │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                          │
                        ┌─────────────────────────────────┼─────────────────────────────────┐
                        ▼                                 ▼                                 ▼
                 ┌─────────────┐                  ┌─────────────┐                  ┌─────────────┐
                 │ Project A   │                  │ Project B   │                  │ Project C   │
                 │ .knowl/     │                  │ .knowl/     │                  │ .knowl/     │
                 │ (memory)    │                  │ (memory)    │                  │ (memory)    │
                 └─────────────┘                  └─────────────┘                  └─────────────┘
```

## Requirements

- Python 3.10+
- discord.py 2.0+
- Hermes Agent (with Knowl plugin)
- A Discord bot with Message Content Intent enabled

## Creating a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create New Application → name it
3. Go to Bot → Reset Token → copy it
4. Enable **Message Content Intent** under Privileged Gateway Intents
5. Go to OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Permissions: Send Messages, Read Message History, Add Reactions
6. Copy the URL and invite bot to your server

## License

MIT

## Credits

Inspired by:
- [Kimaki](https://github.com/remorses/kimaki) — OpenCode Discord orchestrator
- [picord](https://pi.dev/packages/@venthezone/picord) — pi Discord integration
- [discord-ops](https://glama.ai/mcp/servers/bookedsolidtech/discord-ops) — MCP project routing

Built for [Hermes Agent](https://github.com/nousresearch/hermes-agent).
