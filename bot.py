"""Mikasa: assigns a role and posts a welcome when someone joins, and announces
when a server's Twitch stream goes live (with its TikTok link alongside it).
Each server configures itself with the /setup commands."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import customgame
import storage
import twitch

log = logging.getLogger("mikasa")

POLL_SECONDS = 60
VOTE_TIMER_CHECK_SECONDS = 5
DATA_FILE = Path(__file__).resolve().parent / "data.json"
DEFAULT_WELCOME = "Welcome to {server}, {member}! 🎮"

TWITCH_USERNAME = re.compile(r"[a-z0-9_]{1,25}")
TIKTOK_USERNAME = re.compile(r"[a-z0-9_.]{1,24}")

# What each /setup clear choice removes. Clearing Twitch also forgets the last
# stream, so a new username announces right away.
CLEARABLE = {
    "join role": ("join_role_id",),
    "welcome": ("welcome_channel_id", "welcome_message"),
    "livenotif": ("announce_channel_id",),
    "ping role": ("ping_role_id",),
    "twitch": ("twitch_username", "stream_id", "last_seen"),
    "tiktok": ("tiktok_username",),
    "maps": ("maps",),
}


def build_announcement(settings: dict, guild: discord.Guild) -> str:
    lines = ["🔴 **We're live!**", f"Twitch: https://twitch.tv/{settings['twitch_username']}"]
    if settings.get("tiktok_username"):
        lines.append(f"TikTok: https://www.tiktok.com/@{settings['tiktok_username']}/live")
    ping_role = guild.get_role(settings["ping_role_id"]) if settings.get("ping_role_id") else None
    if ping_role:
        lines.insert(0, ping_role.mention)
    return "\n".join(lines)


def build_welcome(settings: dict, member: discord.Member) -> str:
    # Plain replace, so a stray { or } in the message can't crash it.
    template = settings.get("welcome_message", DEFAULT_WELCOME)
    return template.replace("{member}", member.mention).replace("{server}", member.guild.name)


def normalize_username(raw: str, pattern: re.Pattern) -> str | None:
    username = raw.strip().lstrip("@").lower()
    return username if pattern.fullmatch(username) else None


def channel_problem(channel: discord.TextChannel, guild: discord.Guild) -> str | None:
    permissions = channel.permissions_for(guild.me)
    if permissions.view_channel and permissions.send_messages:
        return None
    return f"❌ I can't post in {channel.mention}. Give me View Channel and Send Messages there, then try again."


def join_role_problem(role: discord.Role, interaction: discord.Interaction) -> str | None:
    guild, user = interaction.guild, interaction.user
    if role.permissions.administrator:
        return f"❌ {role.mention} has Administrator, so I won't give it to everyone who joins."
    # Otherwise someone with Manage Server could hand out roles above their own.
    if user.id != guild.owner_id and not user.top_role > role:
        return f"❌ {role.mention} is above your highest role, so you can't set it."
    if not guild.me.guild_permissions.manage_roles:
        return "❌ I need the Manage Roles permission first."
    if not role.is_assignable():
        return (
            f"❌ I can't assign {role.mention}. Drag my role above it in Server Settings > Roles, "
            "then try again. Bot-managed roles and @everyone can't be assigned at all."
        )
    return None


async def reply(interaction: discord.Interaction, message: str) -> None:
    # Public, so no mentions: confirming a ping role mustn't actually ping it.
    await interaction.response.send_message(message, allowed_mentions=discord.AllowedMentions.none())


setup_group = app_commands.Group(
    name="setup",
    description="Configure this bot for this server",
    guild_only=True,
    # Hidden from anyone without Manage Server, unless changed under Server Settings > Integrations.
    default_permissions=discord.Permissions(manage_guild=True),
)


@setup_group.command(name="joinrole", description="Set the role people get when they join")
async def setup_joinrole(interaction: discord.Interaction, role: discord.Role):
    problem = join_role_problem(role, interaction)
    if problem:
        await reply(interaction, problem)
        return
    interaction.client.store.update(interaction.guild_id, join_role_id=role.id)
    await reply(interaction, f"✅ New members will get {role.mention}.")


@setup_group.command(name="welcome", description="Set the welcome channel and, optionally, the message")
@app_commands.describe(message="Use {member} to mention the new person and {server} for the server name")
async def setup_welcome(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: app_commands.Range[str, 1, 1500] | None = None,
):
    problem = channel_problem(channel, interaction.guild)
    if problem:
        await reply(interaction, problem)
        return
    changes = {"welcome_channel_id": channel.id}
    if message:
        changes["welcome_message"] = message
    interaction.client.store.update(interaction.guild_id, **changes)
    await reply(interaction, f"✅ Welcome messages will be posted in {channel.mention}.")


@setup_group.command(name="livenotif", description="Set the channel for live announcements")
async def setup_livenotif(interaction: discord.Interaction, channel: discord.TextChannel):
    problem = channel_problem(channel, interaction.guild)
    if problem:
        await reply(interaction, problem)
        return
    interaction.client.store.update(interaction.guild_id, announce_channel_id=channel.id)
    await reply(interaction, f"✅ Live announcements will be posted in {channel.mention}.")


@setup_group.command(name="pingrole", description="Set the role that gets pinged by live announcements")
async def setup_pingrole(interaction: discord.Interaction, role: discord.Role):
    interaction.client.store.update(interaction.guild_id, ping_role_id=role.id)
    message = f"✅ Live announcements will ping {role.mention}."
    if not role.mentionable and not interaction.guild.me.guild_permissions.mention_everyone:
        message += (
            "\n⚠️ That ping won't notify anyone yet. Turn on **Allow anyone to @mention this role**, "
            "or give me the Mention Everyone permission."
        )
    await reply(interaction, message)


@setup_group.command(name="twitch", description="Set the Twitch username to watch")
async def setup_twitch(interaction: discord.Interaction, username: str):
    login = normalize_username(username, TWITCH_USERNAME)
    if login is None:
        await reply(interaction, "❌ That isn't a valid Twitch username. Enter just the name, not a link.")
        return
    store = interaction.client.store
    if store.get(interaction.guild_id).get("twitch_username") != login:
        store.update(interaction.guild_id, twitch_username=login, stream_id=None, last_seen=None)
    await reply(interaction, f"✅ Watching https://twitch.tv/{login} (check that this link is right).")


@setup_group.command(name="tiktok", description="Set the TikTok username used in announcements")
async def setup_tiktok(interaction: discord.Interaction, username: str):
    handle = normalize_username(username, TIKTOK_USERNAME)
    if handle is None:
        await reply(interaction, "❌ That isn't a valid TikTok username. Enter just the name, not a link.")
        return
    interaction.client.store.update(interaction.guild_id, tiktok_username=handle)
    await reply(interaction, f"✅ Announcements will link https://www.tiktok.com/@{handle}/live.")


@setup_group.command(name="maps", description="Set the maps custom games vote on")
@app_commands.describe(maps="Map names separated by commas, like: Bind, Haven, Lotus")
async def setup_maps(interaction: discord.Interaction, maps: str):
    parsed, problem = customgame.parse_maps(maps)
    if problem:
        await reply(interaction, problem)
        return
    interaction.client.store.update(interaction.guild_id, maps=parsed)
    await reply(interaction, f"✅ Custom games will vote on: {', '.join(parsed)}.")


@setup_group.command(name="clear", description="Turn off or remove one setting")
async def setup_clear(
    interaction: discord.Interaction,
    setting: Literal["join role", "welcome", "livenotif", "ping role", "twitch", "tiktok", "maps"],
):
    interaction.client.store.update(interaction.guild_id, **{key: None for key in CLEARABLE[setting]})
    await reply(interaction, f"✅ Cleared **{setting}**.")


@setup_group.command(name="show", description="Show this server's current settings")
async def setup_show(interaction: discord.Interaction):
    settings = interaction.client.store.get(interaction.guild_id)

    def mention(key: str, prefix: str) -> str:
        return f"<{prefix}{settings[key]}>" if settings.get(key) else "not set"

    await reply(
        interaction,
        "\n".join(
            [
                f"**Join role:** {mention('join_role_id', '@&')}",
                f"**Welcome channel:** {mention('welcome_channel_id', '#')}",
                f"**Welcome message:** {settings.get('welcome_message', DEFAULT_WELCOME)}",
                f"**Announcement channel:** {mention('announce_channel_id', '#')}",
                f"**Ping role:** {mention('ping_role_id', '@&')}",
                f"**Twitch:** {settings.get('twitch_username', 'not set')}",
                f"**TikTok:** {settings.get('tiktok_username', 'not set')}",
                f"**Custom game maps:** {', '.join(settings.get('maps', customgame.DEFAULT_MAPS))}",
            ]
        ),
    )


class MikasaBot(discord.Client):
    def __init__(self, store: storage.Store, twitch_client_id: str, twitch_client_secret: str):
        intents = discord.Intents.default()
        intents.members = True  # needed to receive member join events
        super().__init__(intents=intents)
        self.store = store
        self.twitch_client_id = twitch_client_id
        self.twitch_client_secret = twitch_client_secret
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(setup_group)
        self.tree.add_command(customgame.customgame_command)
        self.tree.add_command(customgame.leaderboard_command)
        # Registered up front so buttons on existing games still work after a restart.
        self.add_dynamic_items(customgame.GameButton, customgame.TeamSelect)
        self.session: aiohttp.ClientSession | None = None
        self.twitch: twitch.TwitchClient | None = None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.twitch = twitch.TwitchClient(self.session, self.twitch_client_id, self.twitch_client_secret)
        await self.tree.sync()
        self.check_twitch.start()
        self.check_vote_timers.start()

    async def close(self) -> None:
        self.check_twitch.cancel()
        self.check_vote_timers.cancel()
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        settings = self.store.get(member.guild.id)
        # Handled separately so a role failure doesn't also lose the welcome.
        if settings.get("join_role_id"):
            await self._assign_role(member, settings["join_role_id"])
        if settings.get("welcome_channel_id"):
            await self._send_welcome(member, settings)

    async def _assign_role(self, member: discord.Member, role_id: int) -> None:
        role = member.guild.get_role(role_id)
        if role is None:
            log.error("Join role %s no longer exists in %s", role_id, member.guild)
            return
        try:
            await member.add_roles(role, reason="Assigned automatically on join")
        except discord.Forbidden:
            log.error(
                "Can't assign %s in %s: the bot needs Manage Roles and its own role must be above it",
                role.name,
                member.guild,
            )
        except discord.HTTPException:
            log.exception("Failed to assign %s to %s", role.name, member)

    async def _send_welcome(self, member: discord.Member, settings: dict) -> None:
        try:
            channel = await self._get_channel(settings["welcome_channel_id"])
            await channel.send(
                build_welcome(settings, member),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            )
        except discord.HTTPException:
            log.exception("Failed to post the welcome for %s in %s", member, member.guild)

    async def _get_channel(self, channel_id: int):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def poll_twitch(self) -> None:
        targets = []
        for guild in self.guilds:
            settings = self.store.get(guild.id)
            if settings.get("twitch_username") and settings.get("announce_channel_id"):
                targets.append((guild, settings))
        if not targets:
            return

        live = await self.twitch.get_live_streams({settings["twitch_username"] for _, settings in targets})
        now = time.time()
        for guild, settings in targets:
            stream_id = live.get(settings["twitch_username"])
            if stream_id is None:
                continue
            # One server's broken channel mustn't stop the others being announced.
            try:
                await self._announce(guild, settings, stream_id, now)
            except Exception:
                log.exception("Failed to announce in %s; retrying next check", guild)

    async def _announce(self, guild: discord.Guild, settings: dict, stream_id: str, now: float) -> None:
        if twitch.should_announce(stream_id, settings, now):
            channel = await self._get_channel(settings["announce_channel_id"])
            await channel.send(
                build_announcement(settings, guild),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=False),
            )
            log.info("Announced stream %s in %s", stream_id, guild)
        # Saved only after a successful send, so a failed post retries next check.
        self.store.update(guild.id, stream_id=stream_id, last_seen=now)

    @tasks.loop(seconds=POLL_SECONDS)
    async def check_twitch(self) -> None:
        # An unhandled error would stop the loop for good, so log and try again.
        try:
            await self.poll_twitch()
        except Exception:
            log.exception("Twitch check failed; retrying in %s seconds", POLL_SECONDS)

    @check_twitch.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=VOTE_TIMER_CHECK_SECONDS)
    async def check_vote_timers(self) -> None:
        # An unhandled error would stop the loop for good, so log and try again.
        try:
            await customgame.expire_votes(self, time.time())
        except Exception:
            log.exception("Vote timer check failed")

    @check_vote_timers.before_loop
    async def _wait_until_ready_for_votes(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    load_dotenv()
    names = ("DISCORD_TOKEN", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit("Add these to your .env file: " + ", ".join(missing))

    token, client_id, client_secret = (os.environ[name].strip() for name in names)
    MikasaBot(storage.Store(DATA_FILE), client_id, client_secret).run(token, root_logger=True)


if __name__ == "__main__":
    main()
