"""Custom games: one message that moves through picking teams, voting on a map,
playing and reporting the result, plus a per-server leaderboard.

Games are saved by message ID and the buttons are dynamic items that look the
game up when clicked, so they keep working after the bot restarts.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Literal

import discord
from discord import app_commands

log = logging.getLogger("mikasa.customgame")

# Stops two updates at once from each posting a new scoreboard message.
_scoreboard_lock = asyncio.Lock()

# Every map on playvalorant.com as of patch 13.09. Servers can change it with /setup maps.
DEFAULT_MAPS = [
    "Abyss",
    "Ascent",
    "Bind",
    "Breeze",
    "Corrode",
    "Fracture",
    "Haven",
    "Icebox",
    "Lotus",
    "Pearl",
    "Split",
    "Summit",
    "Sunset",
]
MAX_MAPS = 20  # four rows of five buttons, leaving the last row for the admin buttons
MAX_MAP_NAME_LENGTH = 40
MAX_TEAM_SIZE = 5
LEADERBOARD_SIZE = 20
VOTES_PER_PLAYER = 2
VOTE_SECONDS = 2 * 60

QUEUE_SIZE = 10
MIN_PLAYERS_TO_START = 2
QUEUE_EMOJI = "✅"
MAX_POINTS = 1_000_000

TEAM_NAMES = {"a": "Team 1", "b": "Team 2"}

# Which phases each button is valid in, so stale or double clicks do nothing.
ACTION_PHASES = {
    "startearly": {"queue"},
    "start": {"teams"},
    "cancel": {"queue", "teams", "vote", "playing"},
    "ballot": {"vote"},
    "endvote": {"vote"},
    "won": {"playing"},
    "undo": {"finished"},
}


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.permissions.manage_guild


def players(game: dict) -> list[int]:
    return game["team_a"] + game["team_b"]


def parse_maps(raw: str) -> tuple[list[str] | None, str | None]:
    """Turns "Bind, Haven" into a list of maps, or returns an error message."""
    maps: list[str] = []
    for name in (part.strip() for part in raw.split(",")):
        if name and name.lower() not in (existing.lower() for existing in maps):
            maps.append(name)
    if not 2 <= len(maps) <= MAX_MAPS:
        return None, f"❌ Enter between 2 and {MAX_MAPS} different maps, separated by commas."
    if any(len(name) > MAX_MAP_NAME_LENGTH for name in maps):
        return None, f"❌ Map names can be at most {MAX_MAP_NAME_LENGTH} characters."
    return maps, None


def votes_allowed(game: dict) -> int:
    # Always below the number of maps, or voting for everything would force a tie.
    return min(VOTES_PER_PLAYER, len(game["maps"]) - 1)


def count_votes(maps: list[str], votes: dict[str, list[int]]) -> list[int]:
    counts = [0] * len(maps)
    for choices in votes.values():
        for index in choices:
            counts[index] += 1
    return counts


def pick_map(maps: list[str], votes: dict[str, list[int]]) -> tuple[str, str | None]:
    """The winning map and, if chance decided it, a note saying how."""
    counts = count_votes(maps, votes)
    top = max(counts)
    if top == 0:
        return random.choice(maps), "No votes, so it was picked at random."
    tied = [name for name, count in zip(maps, counts) if count == top]
    return random.choice(tied), ("Tied vote, so it was picked at random." if len(tied) > 1 else None)


def apply_result(leaderboard: dict, game: dict, change: int) -> None:
    """Adds this game to the leaderboard (change=1) or takes it back off (change=-1)."""
    winner = game["winner"]
    loser = "b" if winner == "a" else "a"
    for team, stat in ((winner, "wins"), (loser, "losses")):
        for user_id in game[f"team_{team}"]:
            entry = leaderboard.setdefault(str(user_id), {"wins": 0, "losses": 0})
            entry[stat] = max(0, entry[stat] + change)
            if not entry["wins"] and not entry["losses"]:
                del leaderboard[str(user_id)]


def finish_vote(game: dict) -> None:
    # Only confirmed votes count; unconfirmed picks are thrown away.
    game.pop("picks", None)
    game["map"], game["map_note"] = pick_map(game["maps"], game["votes"])
    game["phase"] = "playing"


def remove_from_queue(game: dict, user_id: int) -> bool:
    """Takes a player out of a queue, reopening it if it had filled up."""
    if user_id not in game.get("queue", []):
        return False
    game["queue"].remove(user_id)
    game["team_a"] = [member for member in game["team_a"] if member != user_id]
    game["team_b"] = [member for member in game["team_b"] if member != user_id]
    game.get("names", {}).pop(str(user_id), None)
    game["phase"] = "queue"
    game.pop("started_early", None)
    return True


def needs_full_teams(game: dict) -> bool:
    return not game.get("started_early")


def teams_ready(game: dict) -> bool:
    if needs_full_teams(game):
        return len(game["team_a"]) == MAX_TEAM_SIZE and len(game["team_b"]) == MAX_TEAM_SIZE
    return bool(game["team_a"] and game["team_b"])


def _team_text(game: dict, team: str) -> str:
    return "\n".join(f"<@{user_id}>" for user_id in game[f"team_{team}"]) or "No players yet"


def _base_embed(title: str, game: dict, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name=TEAM_NAMES["a"], value=_team_text(game, "a"))
    embed.add_field(name=TEAM_NAMES["b"], value=_team_text(game, "b"))
    return embed


def _add_vote_results(embed: discord.Embed, game: dict) -> None:
    """Reveals the totals, which stay hidden until voting has ended."""
    if not game.get("votes"):
        return
    counts = count_votes(game["maps"], game["votes"])
    ranked = sorted(zip(game["maps"], counts), key=lambda pair: -pair[1])
    embed.add_field(
        name="Votes",
        value="\n".join(f"{name}: **{count}**" for name, count in ranked if count),
        inline=False,
    )


def _button(
    action: str,
    label: str,
    style: discord.ButtonStyle,
    arg: str | None = None,
    disabled: bool = False,
    row: int | None = None,
) -> "GameButton":
    custom_id = f"mikasa:game:{action}" + (f":{arg}" if arg is not None else "")
    return GameButton(discord.ui.Button(label=label, style=style, custom_id=custom_id, disabled=disabled, row=row))


def _queue_options(game: dict, selected: list[int] = ()) -> list[discord.SelectOption]:
    """One dropdown option per queued player, so nobody else can be picked."""
    names = game.get("names", {})
    return [
        discord.SelectOption(
            label=names.get(str(user_id), f"Player {user_id}")[:100],
            value=str(user_id),
            default=user_id in selected,
        )
        for user_id in game["queue"]
    ]


def _queue_team_select(game: dict, team: str, row: int) -> "QueueTeamSelect":
    return QueueTeamSelect(
        discord.ui.Select(
            custom_id=f"mikasa:queueteam:{team}",
            placeholder=f"Pick {TEAM_NAMES[team]}",
            min_values=0,
            max_values=min(MAX_TEAM_SIZE, len(game["queue"])),
            options=_queue_options(game, game[f"team_{team}"]),
            row=row,
        )
    )


def _kick_select(game: dict, row: int) -> "KickSelect":
    return KickSelect(
        discord.ui.Select(
            custom_id="mikasa:queuekick",
            placeholder="Admins: kick a player from the queue",
            min_values=1,
            max_values=1,
            options=_queue_options(game),
            row=row,
        )
    )


def render(game: dict) -> tuple[discord.Embed, discord.ui.View]:
    """The embed and components for the game's current phase."""
    view = discord.ui.View(timeout=None)
    phase = game["phase"]

    if phase == "queue":
        embed = discord.Embed(
            title=f"🎮 Custom Game Queue ({len(game['queue'])}/{QUEUE_SIZE})",
            description=f"React with {QUEUE_EMOJI} to join, and remove your reaction to leave.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Players", value="\n".join(f"<@{user_id}>" for user_id in game["queue"]) or "Nobody yet", inline=False
        )
        if game["queue"]:  # Discord refuses a dropdown with no options
            view.add_item(_kick_select(game, 0))
        too_few = len(game["queue"]) < MIN_PLAYERS_TO_START
        view.add_item(_button("startearly", "Start early", discord.ButtonStyle.success, disabled=too_few, row=1))
        view.add_item(_button("cancel", "Cancel", discord.ButtonStyle.secondary, row=1))

    elif phase == "teams":
        embed = _base_embed("🎮 Custom Game: pick the teams", game, discord.Color.blurple())
        view.add_item(_queue_team_select(game, "a", 0))
        view.add_item(_queue_team_select(game, "b", 1))
        unassigned = [user_id for user_id in game["queue"] if user_id not in players(game)]
        if game.get("started_early"):
            embed.description = "The queue was started early. Admins: put at least one player on each team, then confirm."
        else:
            embed.description = f"The queue is full. Admins: put {MAX_TEAM_SIZE} players on each team, then confirm."
        if unassigned:
            embed.add_field(
                name="Not on a team yet", value=" ".join(f"<@{user_id}>" for user_id in unassigned), inline=False
            )
        view.add_item(_kick_select(game, 2))
        view.add_item(_button("start", "Confirm teams", discord.ButtonStyle.success, disabled=not teams_ready(game), row=3))
        view.add_item(_button("cancel", "Cancel", discord.ButtonStyle.secondary, row=3))

    elif phase == "vote":
        votes = game["votes"]
        waiting = [user_id for user_id in players(game) if str(user_id) not in votes]
        confirmed = len(players(game)) - len(waiting)
        embed = _base_embed(
            f"🗺️ Vote for a map ({confirmed}/{len(players(game))} confirmed)", game, discord.Color.gold()
        )
        embed.description = (
            f"Click **Vote** to pick up to {votes_allowed(game)} maps, then confirm. "
            "The votes stay hidden until voting ends.\n"
            f"Voting ends <t:{int(game['vote_ends_at'])}:R>."
        )
        embed.add_field(name="Maps", value=", ".join(game["maps"]), inline=False)
        embed.add_field(name="Waiting on", value=" ".join(f"<@{user_id}>" for user_id in waiting), inline=False)
        view.add_item(_button("ballot", "Vote", discord.ButtonStyle.primary))
        view.add_item(_button("endvote", "End vote", discord.ButtonStyle.success))
        view.add_item(_button("cancel", "Cancel game", discord.ButtonStyle.secondary))

    elif phase == "playing":
        embed = _base_embed(f"⚔️ Map: {game['map']}", game, discord.Color.red())
        if game.get("map_note"):
            embed.description = game["map_note"]
        _add_vote_results(embed, game)
        if game.get("call_ids"):
            embed.add_field(
                name="Team calls",
                value=" ".join(f"<#{channel_id}>" for channel_id in game["call_ids"]),
                inline=False,
            )
        elif game.get("calls_failed"):
            embed.add_field(
                name="Team calls", value="⚠️ I couldn't create the calls. I need Manage Channels and Connect.", inline=False
            )
        view.add_item(_button("won", "Team 1 won", discord.ButtonStyle.success, arg="a"))
        view.add_item(_button("won", "Team 2 won", discord.ButtonStyle.success, arg="b"))
        view.add_item(_button("cancel", "Cancel game", discord.ButtonStyle.secondary))

    else:  # finished
        title = f"🏆 {TEAM_NAMES[game['winner']]} won on {game['map']}!"
        embed = _base_embed(title, game, discord.Color.green())
        _add_vote_results(embed, game)
        view.add_item(_button("undo", "Undo result", discord.ButtonStyle.secondary))

    return embed, view


BALLOT_BUTTON_ID = "mikasa:game:ballot"


def render_public(game: dict) -> tuple[discord.Embed, discord.ui.View]:
    """What players see: the game's progress, with no admin controls."""
    embed, view = render(game)
    for item in list(view.children):
        if item.custom_id != BALLOT_BUTTON_ID:
            view.remove_item(item)
    if game["phase"] == "teams":
        embed = discord.Embed(
            title=f"🎮 Custom Game Queue ({len(game['queue'])}/{QUEUE_SIZE})",
            description="The queue is closed. Admins are picking the teams.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Players", value="\n".join(f"<@{user_id}>" for user_id in game["queue"]), inline=False)
    elif game["phase"] == "playing":
        # The totals, the map and the calls go in their own new message instead.
        embed = discord.Embed(
            title="🗳️ Voting has closed",
            description="The results are in the message below.",
            color=discord.Color.dark_grey(),
        )
    # Permission warnings are for the admins, and the totals are in the results message.
    for index, field in reversed(list(enumerate(embed.fields))):
        if field.name == "Votes" or field.value.startswith("⚠️"):
            embed.remove_field(index)
    return embed, view


def render_admin(game: dict) -> tuple[discord.Embed, discord.ui.View]:
    """The control panel: every admin control, without the players' Vote button."""
    embed, view = render(game)
    for item in list(view.children):
        if item.custom_id == BALLOT_BUTTON_ID:
            view.remove_item(item)
    return embed, view


def _render_for(game: dict, message_id: int) -> tuple[discord.Embed, discord.ui.View]:
    if "panel_id" not in game:
        return render(game)  # a game from before panels, on a single message
    return render_admin(game) if game["panel_id"] == message_id else render_public(game)


def _find_game(games: dict, message_id: int) -> tuple[str | None, dict | None]:
    """Finds a game from either its public message or its admin panel."""
    key = str(message_id)
    if key in games:
        return key, games[key]
    for key, game in games.items():
        if game.get("panel_id") == message_id:
            return key, game
    return None, None


async def _show_update(interaction: discord.Interaction, key: str, game: dict) -> None:
    """Updates the clicked message through the interaction, then the game's other message."""
    embed, view = _render_for(game, interaction.message.id)
    await interaction.response.edit_message(embed=embed, view=view)
    await _update_game_message(interaction.client, key, game, skip=interaction.message.id)


async def _deny(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.send_message(message, ephemeral=True)


async def _set_team(interaction: discord.Interaction, team: str, chosen: list[int]) -> None:
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can set the teams.")
        return

    store = interaction.client.store
    games = store.get(interaction.guild_id).get("games", {})
    key, game = _find_game(games, interaction.message.id)
    if game is None or game["phase"] != "teams":
        await _deny(interaction, "❌ The teams for this game can't be changed anymore.")
        return

    if any(user_id not in game["queue"] for user_id in chosen):
        await _deny(interaction, "❌ Only players in the queue can be put on a team.")
        return
    other = "b" if team == "a" else "a"
    # Picking someone already on the other team moves them, so nobody is on both.
    game[f"team_{other}"] = [user_id for user_id in game[f"team_{other}"] if user_id not in chosen]
    game[f"team_{team}"] = chosen
    store.update(interaction.guild_id, games=games)
    await _show_update(interaction, key, game)


async def handle_button(interaction: discord.Interaction, action: str, arg: str | None) -> None:
    store = interaction.client.store
    settings = store.get(interaction.guild_id)
    games = settings.get("games", {})
    key, game = _find_game(games, interaction.message.id)

    if game is None:
        await _deny(interaction, "❌ This game no longer exists.")
        return
    if action != "ballot" and not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can do that.")
        return
    if game["phase"] not in ACTION_PHASES[action]:
        # An old click or a message that missed an update: just show the current step.
        await _show_update(interaction, key, game)
        return

    leaderboard = settings.get("leaderboard", {})
    calls_to_delete: list[int] = []

    if action == "ballot":
        if interaction.user.id not in players(game):
            await _deny(interaction, "❌ Only the players in this game can vote.")
            return
        if str(interaction.user.id) in game["votes"]:
            await _deny(interaction, _already_voted_text(game, interaction.user.id))
            return
        text, view = render_ballot(game, key, interaction.user.id)
        await interaction.response.send_message(text, view=view, ephemeral=True)
        return
    elif action == "startearly":
        if len(game["queue"]) < MIN_PLAYERS_TO_START:
            await _deny(interaction, f"❌ The queue needs at least {MIN_PLAYERS_TO_START} players to start.")
            return
        game["phase"] = "teams"
        game["started_early"] = True
    elif action == "start":
        if not teams_ready(game):
            needed = f"exactly {MAX_TEAM_SIZE} players" if needs_full_teams(game) else "at least one player"
            await _deny(interaction, f"❌ Both teams need {needed}.")
            return
        game["maps"] = settings.get("maps", DEFAULT_MAPS)
        game["votes"] = {}
        game["picks"] = {}
        game["vote_ends_at"] = time.time() + VOTE_SECONDS
        game["phase"] = "vote"
    elif action == "endvote":
        finish_vote(game)
    elif action == "won":
        game["winner"] = arg
        game["phase"] = "finished"
        apply_result(leaderboard, game, 1)
        calls_to_delete = game.pop("call_ids", [])
    elif action == "undo":
        apply_result(leaderboard, game, -1)
        del game["winner"]
        game["phase"] = "playing"
    elif action == "cancel":
        del games[key]
        store.update(interaction.guild_id, games=games)
        embed = _base_embed("🚫 Custom game cancelled", game, discord.Color.dark_grey())
        await interaction.response.edit_message(embed=embed, view=None)
        await _edit_game_messages(
            interaction.client,
            key,
            game,
            lambda _renderer: (embed, None),
            skip=interaction.message.id,
        )
        await _delete_team_calls(interaction.client, game.get("call_ids", []))
        return

    # Saved together, so a game and its leaderboard change can't get out of step.
    store.update(interaction.guild_id, games=games, leaderboard=leaderboard)
    await _show_update(interaction, key, game)
    await _delete_team_calls(interaction.client, calls_to_delete)
    if game["phase"] == "playing":
        await open_team_calls(interaction.client, interaction.guild_id, key, game)
    if action == "startearly":
        await announce_queue_started(interaction.client, game)
    if action in ("won", "undo"):
        await refresh_scoreboard(interaction.client, interaction.guild_id)


async def handle_kick(interaction: discord.Interaction, user_id: int) -> None:
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can kick players from the queue.")
        return

    store = interaction.client.store
    games = store.get(interaction.guild_id).get("games", {})
    key, game = _find_game(games, interaction.message.id)
    if game is None or game["phase"] not in ("queue", "teams"):
        await _deny(interaction, "❌ This queue has already closed.")
        return
    if not remove_from_queue(game, user_id):
        await _deny(interaction, "❌ That player isn't in the queue.")
        return
    game.setdefault("kicked", []).append(user_id)

    store.update(interaction.guild_id, games=games)
    await _show_update(interaction, key, game)
    message = interaction.client.get_partial_messageable(game["channel_id"]).get_partial_message(int(key))
    try:
        await message.remove_reaction(QUEUE_EMOJI, discord.Object(id=user_id))
    except discord.HTTPException:
        log.warning("Couldn't remove the queue reaction of %s; the bot needs Manage Messages", user_id)


async def handle_queue_reaction(client: discord.Client, payload: discord.RawReactionActionEvent, adding: bool) -> None:
    if payload.guild_id is None or str(payload.emoji) != QUEUE_EMOJI or payload.user_id == client.user.id:
        return
    games = client.store.get(payload.guild_id).get("games", {})
    key = str(payload.message_id)
    game = games.get(key)
    if game is None or "queue" not in game:
        return

    if not adding:
        if game["phase"] in ("queue", "teams") and remove_from_queue(game, payload.user_id):
            client.store.update(payload.guild_id, games=games)
            await _update_game_message(client, key, game)
        return

    if payload.user_id in game["queue"] or (payload.member is not None and payload.member.bot):
        return
    if game["phase"] != "queue" or payload.user_id in game.get("kicked", []):
        # Closed or kicked, so this reaction didn't get them in. Don't leave it looking like it did.
        message = client.get_partial_messageable(game["channel_id"]).get_partial_message(payload.message_id)
        try:
            await message.remove_reaction(QUEUE_EMOJI, discord.Object(id=payload.user_id))
        except discord.HTTPException:
            log.warning("Couldn't remove a late queue reaction; the bot needs Manage Messages")
        return

    game["queue"].append(payload.user_id)
    if payload.member is not None:
        game.setdefault("names", {})[str(payload.user_id)] = payload.member.display_name
    if len(game["queue"]) >= QUEUE_SIZE:
        game["phase"] = "teams"
    client.store.update(payload.guild_id, games=games)
    await _update_game_message(client, key, game)
    if game["phase"] == "teams":
        await announce_queue_started(client, game)


async def open_team_calls(client: discord.Client, guild_id: int, game_id: str, game: dict) -> None:
    """Creates a voice channel per team that only that team's players can join."""
    guild = client.get_guild(guild_id)
    if guild is None or game.get("call_ids"):
        return

    channel = guild.get_channel(game["channel_id"])
    created = []
    failed = False
    try:
        for team in ("a", "b"):
            overwrites = {guild.default_role: discord.PermissionOverwrite(connect=False)}
            for user_id in game[f"team_{team}"]:
                overwrites[discord.Object(id=user_id)] = discord.PermissionOverwrite(view_channel=True, connect=True)
            created.append(
                await guild.create_voice_channel(
                    f"{TEAM_NAMES[team]} Call",
                    overwrites=overwrites,
                    category=getattr(channel, "category", None),
                    reason="Custom game team call",
                )
            )
    except discord.HTTPException:
        log.exception("Couldn't create the team calls in %s", guild)
        failed = True

    # Fresh copy: the game may have changed or been cancelled while creating them.
    games = client.store.get(guild_id).get("games", {})
    current = games.get(game_id)
    if failed or current is None or current["phase"] != "playing":
        await _delete_team_calls(client, [call.id for call in created], created)
        if failed and current is not None:
            current["calls_failed"] = True
            client.store.update(guild_id, games=games)
            await _update_game_message(client, game_id, current)
            await _announce_vote_result(client, current)
        return

    current.pop("calls_failed", None)
    current["call_ids"] = [call.id for call in created]
    client.store.update(guild_id, games=games)
    await _update_game_message(client, game_id, current)
    await _announce_vote_result(client, current)


def vote_result_text(game: dict) -> str:
    """The player channel's message: vote totals, the map, and who joins which call."""
    counts = count_votes(game["maps"], game["votes"])
    ranked = sorted(zip(game["maps"], counts), key=lambda pair: -pair[1])
    totals = [f"{name}: **{count}**" for name, count in ranked if count]
    lines = ["🗳️ **Map vote results**", *(totals or ["No votes were cast."]), "", f"🗺️ **Map: {game['map']}**"]
    if game.get("map_note"):
        lines.append(game["map_note"])
    if game.get("call_ids"):
        lines += ["", "🔊 **Join your team's call:**"]
        for team, channel_id in zip(("a", "b"), game["call_ids"]):
            mentions = " ".join(f"<@{user_id}>" for user_id in game[f"team_{team}"])
            lines.append(f"**{TEAM_NAMES[team]}** → <#{channel_id}>: {mentions}")
    return "\n".join(lines)


async def _announce_vote_result(client: discord.Client, game: dict) -> None:
    try:
        await client.get_partial_messageable(game["channel_id"]).send(
            vote_result_text(game),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
    except discord.HTTPException:
        log.exception("Couldn't post the vote result in channel %s", game["channel_id"])


async def _delete_team_calls(client: discord.Client, channel_ids: list[int], known: list | None = None) -> None:
    channels = known if known is not None else [client.get_channel(channel_id) for channel_id in channel_ids]
    for channel in channels:
        if channel is None:
            continue  # already deleted by someone
        try:
            await channel.delete(reason="Custom game finished")
        except discord.HTTPException:
            log.exception("Couldn't delete team call %s", channel.id)


async def _edit_game_messages(client: discord.Client, game_id: str, game: dict, content, skip: int | None = None) -> None:
    """Edits the public message and, if the game has one, the admin panel."""
    targets = [(game["channel_id"], int(game_id), render_public if "panel_id" in game else render)]
    if "panel_id" in game:
        targets.append((game["panel_channel_id"], game["panel_id"], render_admin))
    for channel_id, message_id, renderer in targets:
        if message_id == skip:
            continue
        embed, view = content(renderer)
        # The game is already saved, and clicking the old message refreshes it.
        try:
            await client.get_partial_messageable(channel_id).get_partial_message(message_id).edit(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Couldn't update the custom game message %s", message_id)


async def announce_queue_started(client: discord.Client, game: dict) -> None:
    """Posts a new message in the player channel, since edits don't notify anyone."""
    text = f"🎮 **The queue has started with {len(game['queue'])} players!** Admins are picking the teams."
    try:
        await client.get_partial_messageable(game["channel_id"]).send(
            text, allowed_mentions=discord.AllowedMentions.none()
        )
    except discord.HTTPException:
        log.exception("Couldn't announce the queue start in channel %s", game["channel_id"])


async def _update_game_message(client: discord.Client, game_id: str, game: dict, skip: int | None = None) -> None:
    await _edit_game_messages(client, game_id, game, lambda renderer: renderer(game), skip=skip)


def render_ballot(game: dict, game_id: str, user_id: int) -> tuple[str, discord.ui.View]:
    """A player's private ballot, showing their unconfirmed picks."""
    picks = game.get("picks", {}).get(str(user_id), [])
    allowed = votes_allowed(game)
    view = discord.ui.View(timeout=None)
    view.add_item(
        BallotSelect(
            discord.ui.Select(
                custom_id=f"mikasa:ballot:{game_id}",
                placeholder=f"Pick up to {allowed} maps",
                min_values=1,
                max_values=allowed,
                options=[
                    discord.SelectOption(label=name, value=str(index), default=index in picks)
                    for index, name in enumerate(game["maps"])
                ],
                row=0,
            )
        )
    )
    view.add_item(
        ConfirmVote(
            discord.ui.Button(
                label="Confirm vote",
                style=discord.ButtonStyle.success,
                custom_id=f"mikasa:confirm:{game_id}",
                disabled=not picks,
                row=1,
            )
        )
    )
    text = (
        f"Pick up to {allowed} maps, then click **Confirm vote**. You can only vote once, "
        "and picks you don't confirm won't count.\n"
        f"Voting ends <t:{int(game['vote_ends_at'])}:R>."
    )
    return text, view


def _already_voted_text(game: dict, user_id: int) -> str:
    names = ", ".join(game["maps"][index] for index in game["votes"][str(user_id)])
    return f"❌ You already voted for **{names}**. Votes can't be changed."


async def _open_ballot_game(interaction: discord.Interaction, game_id: str) -> tuple[dict | None, dict | None]:
    """The games and this game, if the user can still vote in it. Otherwise says why."""
    games = interaction.client.store.get(interaction.guild_id).get("games", {})
    game = games.get(game_id)
    if game is None or game["phase"] != "vote":
        await interaction.response.edit_message(content="🗳️ Voting has closed.", view=None)
        return None, None
    if interaction.user.id not in players(game):
        await _deny(interaction, "❌ Only the players in this game can vote.")
        return None, None
    if str(interaction.user.id) in game["votes"]:
        # A second ballot left open from before this player confirmed.
        await interaction.response.edit_message(content=_already_voted_text(game, interaction.user.id), view=None)
        return None, None
    return games, game


async def handle_ballot_select(interaction: discord.Interaction, game_id: str, values: list[str]) -> None:
    games, game = await _open_ballot_game(interaction, game_id)
    if game is None:
        return
    game.setdefault("picks", {})[str(interaction.user.id)] = [int(value) for value in values]
    interaction.client.store.update(interaction.guild_id, games=games)
    text, view = render_ballot(game, game_id, interaction.user.id)
    await interaction.response.edit_message(content=text, view=view)


async def handle_confirm_vote(interaction: discord.Interaction, game_id: str) -> None:
    games, game = await _open_ballot_game(interaction, game_id)
    if game is None:
        return
    uid = str(interaction.user.id)
    picks = game.get("picks", {}).pop(uid, [])
    if not picks:
        await _deny(interaction, "❌ Pick at least one map first.")
        return

    game["votes"][uid] = picks
    if all(str(user_id) in game["votes"] for user_id in players(game)):
        finish_vote(game)
    interaction.client.store.update(interaction.guild_id, games=games)

    names = ", ".join(game["maps"][index] for index in picks)
    await interaction.response.edit_message(content=f"✅ Your vote is in: **{names}**.", view=None)
    await _update_game_message(interaction.client, game_id, game)
    if game["phase"] == "playing":
        await open_team_calls(interaction.client, interaction.guild_id, game_id, game)


async def expire_votes(client: discord.Client, now: float) -> None:
    """Closes every vote whose time has run out. The deadline is saved with the
    game, so a vote that ended while the bot was offline closes when it returns."""
    for guild in client.guilds:
        games = client.store.get(guild.id).get("games", {})
        expired = [
            (key, game) for key, game in games.items() if game["phase"] == "vote" and game["vote_ends_at"] <= now
        ]
        if not expired:
            continue
        for _, game in expired:
            finish_vote(game)
        client.store.update(guild.id, games=games)

        for key, game in expired:
            await _update_game_message(client, key, game)
            await open_team_calls(client, guild.id, key, game)


class GameButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mikasa:game:(?P<action>[a-z]+)(?::(?P<arg>[a-z0-9]+))?"):
    action: str
    arg: str | None

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        button = cls(item)
        button.action = match["action"]
        button.arg = match["arg"]
        return button

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_button(interaction, self.action, self.arg)


class BallotSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"mikasa:ballot:(?P<game>[0-9]+)"):
    game_id: str

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        select = cls(item)
        select.game_id = match["game"]
        return select

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_ballot_select(interaction, self.game_id, self.item.values)


class ConfirmVote(discord.ui.DynamicItem[discord.ui.Button], template=r"mikasa:confirm:(?P<game>[0-9]+)"):
    game_id: str

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        button = cls(item)
        button.game_id = match["game"]
        return button

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_confirm_vote(interaction, self.game_id)


class KickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"mikasa:queuekick"):
    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_kick(interaction, int(self.item.values[0]))


class QueueTeamSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"mikasa:queueteam:(?P<team>[ab])"):
    team: str

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        select = cls(item)
        select.team = match["team"]
        return select

    async def callback(self, interaction: discord.Interaction) -> None:
        await _set_team(interaction, self.team, [int(value) for value in self.item.values])


DYNAMIC_ITEMS = (GameButton, BallotSelect, ConfirmVote, KickSelect, QueueTeamSelect)


queue_group = app_commands.Group(
    name="queue",
    description="(Admins only) Manage custom game queues",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@queue_group.command(name="open", description="Open a queue: controls appear here, players see it in the chosen channel")
@app_commands.describe(channel="The channel where players see and join the queue")
async def queue_open(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can open a queue.")
        return
    permissions = channel.permissions_for(interaction.guild.me)
    if not (
        permissions.view_channel
        and permissions.send_messages
        and permissions.embed_links
        and permissions.add_reactions
        and permissions.read_message_history
    ):
        await _deny(
            interaction,
            f"❌ I can't run a queue in {channel.mention}. I need View Channel, Send Messages, "
            "Embed Links, Add Reactions and Read Message History there.",
        )
        return

    game = {
        "phase": "queue",
        "queue": [],
        "team_a": [],
        "team_b": [],
        "channel_id": channel.id,
        "panel_channel_id": interaction.channel_id,
    }
    embed, view = render_admin(game)
    await interaction.response.send_message(embed=embed, view=view)
    panel = await interaction.original_response()
    game["panel_id"] = panel.id

    embed, view = render_public(game)
    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException:
        log.exception("Couldn't post the queue in %s", channel)
        await panel.edit(content=f"❌ I couldn't post the queue in {channel.mention}.", embed=None, view=None)
        return

    store = interaction.client.store
    games = store.get(interaction.guild_id).get("games", {})
    games[str(message.id)] = game
    store.update(interaction.guild_id, games=games)
    try:
        await message.add_reaction(QUEUE_EMOJI)
    except discord.HTTPException:
        log.warning("Couldn't add the queue reaction in %s", channel)
        await interaction.followup.send(
            f"⚠️ I couldn't add the {QUEUE_EMOJI} reaction in {channel.mention}. Players can still add it themselves."
        )


@queue_group.command(name="start", description="Close this channel's newest open queue early and pick teams")
async def queue_start(interaction: discord.Interaction):
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can start a queue.")
        return

    store = interaction.client.store
    games = store.get(interaction.guild_id).get("games", {})
    open_here = [
        int(key)
        for key, game in games.items()
        if game["phase"] == "queue"
        and "queue" in game
        and interaction.channel_id in (game["channel_id"], game.get("panel_channel_id"))
    ]
    if not open_here:
        await _deny(interaction, "❌ There's no open queue for this channel. Run `/queue open` first.")
        return
    key = str(max(open_here))
    game = games[key]
    if len(game["queue"]) < MIN_PLAYERS_TO_START:
        await _deny(interaction, f"❌ The queue needs at least {MIN_PLAYERS_TO_START} players to start.")
        return

    game["phase"] = "teams"
    game["started_early"] = True
    store.update(interaction.guild_id, games=games)
    await interaction.response.send_message(f"✅ Started the queue early with {len(game['queue'])} players.")
    await _update_game_message(interaction.client, key, game)
    await announce_queue_started(interaction.client, game)


@queue_group.command(name="list", description="List the custom game queues in this server")
async def queue_list(interaction: discord.Interaction):
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can list queues.")
        return

    games = interaction.client.store.get(interaction.guild_id).get("games", {})
    labels = {"queue": "open", "teams": "picking teams"}
    lines = [
        f"• <#{game['channel_id']}>: {len(game['queue'])}/{QUEUE_SIZE}, {labels[game['phase']]} "
        f"([jump](https://discord.com/channels/{interaction.guild_id}/{game['channel_id']}/{key}))"
        for key, game in sorted(games.items(), key=lambda item: int(item[0]))
        if "queue" in game and game["phase"] in labels
    ]
    # Embeds can hold 4096 characters, which is about 40 queues at this length.
    embed = discord.Embed(
        title="🎮 Queues",
        description="\n".join(lines[-40:]) or "No queues right now. Run `/queue open` to make one.",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


VALORANT_RED = 0xFF4655
VALORANT_OFFWHITE = 0xECE8E1
RANK_MEDALS = ("🥇", "🥈", "🥉")
LEADERBOARD_FOOTER = f"VALORANT Custom Games • Top {LEADERBOARD_SIZE}"


def _rank_label(rank: int) -> str:
    return RANK_MEDALS[rank - 1] if rank <= len(RANK_MEDALS) else f"`#{rank:02d}`"


def _themed_board(title: str, lines: list[str], empty: str, color: int) -> discord.Embed:
    embed = discord.Embed(title=title, description="\n".join(lines) or empty, color=color)
    embed.set_footer(text=LEADERBOARD_FOOTER)
    return embed


def build_leaderboard(leaderboard: dict) -> discord.Embed:
    ranked = sorted(leaderboard.items(), key=lambda item: (-item[1]["wins"], item[1]["losses"]))
    lines = []
    for rank, (user_id, entry) in enumerate(ranked[:LEADERBOARD_SIZE], start=1):
        wins, losses = entry["wins"], entry["losses"]
        win_rate = round(wins * 100 / (wins + losses))
        lines.append(f"{_rank_label(rank)} <@{user_id}> • **{wins}W** - {losses}L • {win_rate}% WR")
    return _themed_board("🔺 VALORANT CUSTOMS // WINS", lines, "No games have been played yet.", VALORANT_RED)


def build_points_leaderboard(points: dict) -> discord.Embed:
    ranked = sorted(points.items(), key=lambda item: -item[1])
    lines = [
        f"{_rank_label(rank)} <@{user_id}> • **{amount:,} GP**"
        for rank, (user_id, amount) in enumerate(ranked[:LEADERBOARD_SIZE], start=1)
    ]
    return _themed_board("🔺 VALORANT CUSTOMS // GAME POINTS", lines, "Nobody has any Game Points yet.", VALORANT_OFFWHITE)


@app_commands.command(name="leaderboard", description="Show this server's custom game wins or Game Points")
@app_commands.guild_only()
async def leaderboard_command(interaction: discord.Interaction, board: Literal["wins", "game points"] = "wins"):
    settings = interaction.client.store.get(interaction.guild_id)
    if board == "wins":
        embed = build_leaderboard(settings.get("leaderboard", {}))
    else:
        embed = build_points_leaderboard(settings.get("points", {}))
    await interaction.response.send_message(embed=embed)


async def refresh_scoreboard(client: discord.Client, guild_id: int) -> None:
    """Edits the server's scoreboard message, posting a new one if it's gone."""
    async with _scoreboard_lock:
        settings = client.store.get(guild_id)
        channel_id = settings.get("scoreboard_channel_id")
        if not channel_id:
            return
        embeds = [
            build_leaderboard(settings.get("leaderboard", {})),
            build_points_leaderboard(settings.get("points", {})),
        ]
        channel = client.get_partial_messageable(channel_id)
        try:
            message_id = settings.get("scoreboard_message_id")
            if message_id:
                try:
                    await channel.get_partial_message(message_id).edit(embeds=embeds)
                    return
                except discord.NotFound:
                    pass  # someone deleted it, so post a replacement
            message = await channel.send(embeds=embeds)
            client.store.update(guild_id, scoreboard_message_id=message.id)
        except discord.HTTPException:
            log.exception("Couldn't update the scoreboard for server %s", guild_id)


async def _change_points(interaction: discord.Interaction, player: discord.Member, change) -> None:
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can change Game Points.")
        return
    store = interaction.client.store
    points = store.get(interaction.guild_id).get("points", {})
    total = min(MAX_POINTS, max(0, change(points.get(str(player.id), 0))))
    if total:
        points[str(player.id)] = total
    else:
        points.pop(str(player.id), None)
    store.update(interaction.guild_id, points=points)
    await interaction.response.send_message(
        f"✅ {player.mention} now has **{total:,} GP**.", allowed_mentions=discord.AllowedMentions.none()
    )
    await refresh_scoreboard(interaction.client, interaction.guild_id)


Amount = app_commands.Range[int, 1, MAX_POINTS]


@app_commands.command(name="addpoints", description="(Admins only) Add Game Points to a player")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def addpoints_command(interaction: discord.Interaction, player: discord.Member, amount: Amount):
    await _change_points(interaction, player, lambda current: current + amount)


@app_commands.command(name="removepoints", description="(Admins only) Remove Game Points from a player")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def removepoints_command(interaction: discord.Interaction, player: discord.Member, amount: Amount):
    await _change_points(interaction, player, lambda current: current - amount)


@app_commands.command(name="setpoints", description="(Admins only) Set a player's total Game Points")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setpoints_command(
    interaction: discord.Interaction, player: discord.Member, amount: app_commands.Range[int, 0, MAX_POINTS]
):
    await _change_points(interaction, player, lambda current: amount)


@app_commands.command(name="clearstats", description="(Admins only) Clear one player's wins, Game Points or both")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def clearstats_command(
    interaction: discord.Interaction, player: discord.User, board: Literal["wins", "game points", "both"]
):
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can clear a player's stats.")
        return

    store = interaction.client.store
    settings = store.get(interaction.guild_id)
    leaderboard = settings.get("leaderboard", {})
    points = settings.get("points", {})
    uid = str(player.id)

    cleared = []
    if board in ("wins", "both") and leaderboard.pop(uid, None) is not None:
        cleared.append("wins and losses")
    if board in ("game points", "both") and points.pop(uid, None) is not None:
        cleared.append("Game Points")

    no_pings = discord.AllowedMentions.none()
    if not cleared:
        await interaction.response.send_message(
            f"❌ {player.mention} has nothing to clear on that leaderboard.", allowed_mentions=no_pings
        )
        return
    store.update(interaction.guild_id, leaderboard=leaderboard, points=points)
    await interaction.response.send_message(
        f"✅ Cleared {player.mention}'s {' and '.join(cleared)}.", allowed_mentions=no_pings
    )
    await refresh_scoreboard(interaction.client, interaction.guild_id)


class ConfirmReset(discord.ui.View):
    def __init__(self, board: str):
        super().__init__(timeout=60)
        self.board = board

    @discord.ui.button(label="Yes, reset it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        key = "leaderboard" if self.board == "wins" else "points"
        interaction.client.store.update(interaction.guild_id, **{key: None})
        await interaction.response.edit_message(content=f"✅ The **{self.board}** leaderboard has been reset.", view=None)
        await refresh_scoreboard(interaction.client, interaction.guild_id)


@app_commands.command(name="resetleaderboard", description="(Admins only) Reset the wins or Game Points leaderboard")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def resetleaderboard_command(interaction: discord.Interaction, board: Literal["wins", "game points"]):
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can reset a leaderboard.")
        return
    await interaction.response.send_message(
        f"⚠️ This permanently clears the **{board}** leaderboard for this server. Are you sure?",
        view=ConfirmReset(board),
        ephemeral=True,
    )


COMMANDS = (
    queue_group,
    leaderboard_command,
    addpoints_command,
    removepoints_command,
    setpoints_command,
    resetleaderboard_command,
    clearstats_command,
)
