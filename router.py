#!/usr/bin/env python3
"""
Hermes Discord Router

Routes Discord channels to Hermes projects. Each channel maps to a project folder,
so messages in #knowl go to your 'knowl' project's Knowl memory.

Usage:
    python router.py              # Uses config.yaml
    python router.py --config /path/to/config.yaml
"""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord.ext import commands
import yaml

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("hermes-router")


@dataclass
class ChannelMapping:
    """Maps a Discord channel to a Hermes project."""
    id: str
    project: str
    folder: Optional[str] = None
    
    def get_folder(self, projects_root: str) -> Path:
        """Get the resolved folder path for this channel."""
        if self.folder:
            return Path(os.path.expanduser(self.folder))
        return Path(os.path.expanduser(projects_root)) / self.project


@dataclass
class Config:
    """Router configuration."""
    discord_token: str
    channels: List[ChannelMapping]
    hermes_cli: str = "hermes"
    projects_root: str = "~/projects"
    timeout: int = 300
    react_on_route: bool = True
    use_threads: bool = False
    require_mention: bool = False
    ignore_bots: bool = True
    
    # Lookup cache
    _channel_map: Dict[str, ChannelMapping] = field(default_factory=dict, repr=False)
    
    def __post_init__(self):
        self._channel_map = {ch.id: ch for ch in self.channels}
    
    def get_channel_mapping(self, channel_id: str) -> Optional[ChannelMapping]:
        """Get mapping for a channel ID."""
        return self._channel_map.get(channel_id)
    
    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load config from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        
        channels = [
            ChannelMapping(
                id=str(ch["id"]),
                project=ch["project"],
                folder=ch.get("folder")
            )
            for ch in data.get("channels", [])
        ]
        
        hermes_cfg = data.get("hermes", {})
        behavior = data.get("behavior", {})
        
        return cls(
            discord_token=data.get("discord", {}).get("token", ""),
            channels=channels,
            hermes_cli=hermes_cfg.get("cli", "hermes"),
            projects_root=hermes_cfg.get("projects_root", "~/projects"),
            timeout=hermes_cfg.get("timeout", 300),
            react_on_route=behavior.get("react_on_route", True),
            use_threads=behavior.get("use_threads", False),
            require_mention=behavior.get("require_mention", False),
            ignore_bots=behavior.get("ignore_bots", True),
        )


class HermesRouter:
    """Routes Discord messages to Hermes projects."""
    
    def __init__(self, config: Config):
        self.config = config
        self._active_sessions: Dict[str, asyncio.subprocess.Process] = {}
    
    async def route_message(
        self,
        channel_id: str,
        author: str,
        content: str,
        attachments: List[str] = None
    ) -> Optional[str]:
        """
        Route a message to the appropriate Hermes project.
        
        Returns the response from Hermes, or None if routing failed.
        """
        mapping = self.config.get_channel_mapping(channel_id)
        if not mapping:
            return None
        
        folder = mapping.get_folder(self.config.projects_root)
        
        if not folder.exists():
            logger.warning(f"Project folder does not exist: {folder}")
            # Create it? Or fail?
            folder.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created project folder: {folder}")
        
        # Build the prompt with context
        prompt = f"[Discord from @{author}]: {content}"
        
        logger.info(f"Routing to project '{mapping.project}' at {folder}")
        
        try:
            response = await self._invoke_hermes(folder, prompt)
            return response
        except Exception as e:
            logger.error(f"Hermes invocation failed: {e}")
            return f"❌ Error routing to project: {e}"
    
    async def _invoke_hermes(self, folder: Path, prompt: str) -> str:
        """Invoke Hermes CLI with the given prompt in the project folder."""
        
        # Build command
        # hermes -p "prompt" runs a single prompt
        # --cwd sets the working directory (project context)
        cmd = [
            self.config.hermes_cli,
            "--cwd", str(folder),
            "-p", prompt
        ]
        
        logger.debug(f"Invoking: {' '.join(cmd)}")
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(folder)
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return "⏱️ Response timed out"
        
        if proc.returncode != 0:
            logger.warning(f"Hermes exited with code {proc.returncode}")
            if stderr:
                logger.warning(f"stderr: {stderr.decode()}")
        
        return stdout.decode().strip() if stdout else "(no response)"


class RouterBot(commands.Bot):
    """Discord bot that routes messages to Hermes projects."""
    
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.config = config
        self.router = HermesRouter(config)
    
    async def on_ready(self):
        logger.info(f"✓ Bot logged in as {self.user}")
        logger.info(f"✓ Routing {len(self.config.channels)} channels")
        for ch in self.config.channels:
            folder = ch.get_folder(self.config.projects_root)
            logger.info(f"  #{ch.id} → {ch.project} ({folder})")
    
    async def on_message(self, message: discord.Message):
        # Ignore own messages
        if message.author == self.user:
            return
        
        # Ignore bots if configured
        if self.config.ignore_bots and message.author.bot:
            return
        
        # Only process in mapped channels
        channel_id = str(message.channel.id)
        mapping = self.config.get_channel_mapping(channel_id)
        
        if not mapping:
            # Not a mapped channel, process commands normally
            await self.process_commands(message)
            return
        
        # Check mention requirement
        if self.config.require_mention:
            if not self.user.mentioned_in(message):
                return
        
        # React to show we're routing
        if self.config.react_on_route:
            try:
                await message.add_reaction("📚")
            except discord.errors.Forbidden:
                pass
        
        # Route the message
        logger.info(f"[{message.channel.name} → {mapping.project}] {message.author}: {message.content[:60]}")
        
        async with message.channel.typing():
            response = await self.router.route_message(
                channel_id=channel_id,
                author=str(message.author),
                content=message.content,
                attachments=[a.url for a in message.attachments]
            )
        
        if response:
            # Send response (in thread if configured)
            if self.config.use_threads and isinstance(message.channel, discord.TextChannel):
                thread = await message.create_thread(
                    name=f"Response to {message.author.display_name}",
                    auto_archive_duration=60
                )
                await thread.send(response[:2000])  # Discord limit
            else:
                await message.reply(response[:2000])
        
        # Mark as handled (remove routing reaction, add done)
        if self.config.react_on_route:
            try:
                await message.remove_reaction("📚", self.user)
                await message.add_reaction("✅")
            except discord.errors.Forbidden:
                pass


async def main():
    parser = argparse.ArgumentParser(description="Route Discord channels to Hermes projects")
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config file (default: config.yaml)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Load config
    if not args.config.exists():
        logger.error(f"Config file not found: {args.config}")
        logger.error("Copy config.example.yaml to config.yaml and fill in your values")
        sys.exit(1)
    
    config = Config.from_yaml(args.config)
    
    if not config.discord_token or config.discord_token == "YOUR_BOT_TOKEN_HERE":
        logger.error("Discord token not configured in config.yaml")
        sys.exit(1)
    
    if not config.channels:
        logger.warning("No channels configured — bot will not route any messages")
    
    # Start bot
    bot = RouterBot(config)
    
    logger.info("Starting Hermes Discord Router...")
    await bot.start(config.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
