"""Custom games: one message that moves through picking teams, voting on a map,
playing and reporting the result, plus a per-server leaderboard.

Games are saved by message ID and the buttons are dynamic items that look the
game up when clicked, so they keep working after the bot restarts.
"""

from __future__ import annotations

import logging
import random
import time

import discord
from discord import app_commands

log = logging.getLogger("mikasa.customgame")

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

TEAM_NAMES = {"a": "Team A", "b": "Team B"}

# Which phases each button is valid in, so stale or double clicks do nothing.
ACTION_PHASES = {
    "start": {"teams"},
    "cancel": {"teams", "vote", "playing"},
    "vote": {"vote"},
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
    game["map"], game["map_note"] = pick_map(game["maps"], game["votes"])
    game["phase"] = "playing"


def _team_text(game: dict, team: str) -> str:
    return "\n".join(f"<@{user_id}>" for user_id in game[f"team_{team}"]) or "No players yet"


def _base_embed(title: str, game: dict, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name=TEAM_NAMES["a"], value=_team_text(game, "a"))
    embed.add_field(name=TEAM_NAMES["b"], value=_team_text(game, "b"))
    return embed


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


def _team_select(game: dict, team: str, row: int) -> "TeamSelect":
    return TeamSelect(
        discord.ui.UserSelect(
            custom_id=f"mikasa:team:{team}",
            placeholder=f"Pick {TEAM_NAMES[team]}",
            min_values=0,
            max_values=MAX_TEAM_SIZE,
            default_values=[discord.Object(id=user_id) for user_id in game[f"team_{team}"]],
            row=row,
        )
    )


def render(game: dict) -> tuple[discord.Embed, discord.ui.View]:
    """The embed and components for the game's current phase."""
    view = discord.ui.View(timeout=None)
    phase = game["phase"]

    if phase == "teams":
        embed = _base_embed("🎮 Custom Game: pick the teams", game, discord.Color.blurple())
        view.add_item(_team_select(game, "a", 0))
        view.add_item(_team_select(game, "b", 1))
        ready = bool(game["team_a"] and game["team_b"])
        view.add_item(_button("start", "Start map vote", discord.ButtonStyle.success, disabled=not ready, row=2))
        view.add_item(_button("cancel", "Cancel", discord.ButtonStyle.secondary, row=2))

    elif phase == "vote":
        votes = game["votes"]
        allowed = votes_allowed(game)
        waiting = [user_id for user_id in players(game) if len(votes.get(str(user_id), [])) < allowed]
        finished = len(players(game)) - len(waiting)
        embed = _base_embed(f"🗺️ Vote for a map ({finished}/{len(players(game))} done)", game, discord.Color.gold())
        embed.description = (
            f"Pick up to {allowed} maps, and click a map again to remove your vote.\n"
            f"Voting ends <t:{int(game['vote_ends_at'])}:R>."
        )
        counts = count_votes(game["maps"], votes)
        embed.add_field(
            name="Votes",
            value="\n".join(f"{name}: **{count}**" for name, count in zip(game["maps"], counts)),
            inline=False,
        )
        embed.add_field(name="Waiting on", value=" ".join(f"<@{user_id}>" for user_id in waiting), inline=False)
        for index, name in enumerate(game["maps"]):
            view.add_item(_button("vote", name, discord.ButtonStyle.primary, arg=str(index), row=index // 5))
        view.add_item(_button("endvote", "End vote", discord.ButtonStyle.success, row=4))
        view.add_item(_button("cancel", "Cancel game", discord.ButtonStyle.secondary, row=4))

    elif phase == "playing":
        embed = _base_embed(f"⚔️ Map: {game['map']}", game, discord.Color.red())
        if game.get("map_note"):
            embed.description = game["map_note"]
        view.add_item(_button("won", "Team A won", discord.ButtonStyle.success, arg="a"))
        view.add_item(_button("won", "Team B won", discord.ButtonStyle.success, arg="b"))
        view.add_item(_button("cancel", "Cancel game", discord.ButtonStyle.secondary))

    else:  # finished
        title = f"🏆 {TEAM_NAMES[game['winner']]} won on {game['map']}!"
        embed = _base_embed(title, game, discord.Color.green())
        view.add_item(_button("undo", "Undo result", discord.ButtonStyle.secondary))

    return embed, view


async def _deny(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.send_message(message, ephemeral=True)


async def handle_team_select(interaction: discord.Interaction, team: str, selected: list) -> None:
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can set the teams.")
        return

    store = interaction.client.store
    games = store.get(interaction.guild_id).get("games", {})
    game = games.get(str(interaction.message.id))
    if game is None or game["phase"] != "teams":
        await _deny(interaction, "❌ The teams for this game can't be changed anymore.")
        return

    chosen = [user.id for user in selected if not user.bot]
    other = "b" if team == "a" else "a"
    # Picking someone already on the other team moves them, so nobody is on both.
    game[f"team_{other}"] = [user_id for user_id in game[f"team_{other}"] if user_id not in chosen]
    game[f"team_{team}"] = chosen
    store.update(interaction.guild_id, games=games)
    embed, view = render(game)
    await interaction.response.edit_message(embed=embed, view=view)


async def handle_button(interaction: discord.Interaction, action: str, arg: str | None) -> None:
    store = interaction.client.store
    settings = store.get(interaction.guild_id)
    games = settings.get("games", {})
    key = str(interaction.message.id)
    game = games.get(key)

    if game is None:
        await _deny(interaction, "❌ This game no longer exists.")
        return
    if action != "vote" and not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can do that.")
        return
    if game["phase"] not in ACTION_PHASES[action]:
        # An old click or a message that missed an update: just show the current step.
        embed, view = render(game)
        await interaction.response.edit_message(embed=embed, view=view)
        return

    leaderboard = settings.get("leaderboard", {})

    if action == "vote":
        if interaction.user.id not in players(game):
            await _deny(interaction, "❌ Only the players in this game can vote.")
            return
        index = int(arg)
        choices = game["votes"].get(str(interaction.user.id), [])
        if index in choices:
            choices.remove(index)
        elif len(choices) >= votes_allowed(game):
            await _deny(interaction, "❌ You've used all your votes. Click one of your maps to remove it first.")
            return
        else:
            choices.append(index)
        if choices:
            game["votes"][str(interaction.user.id)] = choices
        else:
            del game["votes"][str(interaction.user.id)]

        allowed = votes_allowed(game)
        if all(len(game["votes"].get(str(user_id), [])) >= allowed for user_id in players(game)):
            finish_vote(game)
        store.update(interaction.guild_id, games=games)
        embed, view = render(game)
        await interaction.response.edit_message(embed=embed, view=view)
        # The buttons look the same for everyone, so tell each player what they picked.
        names = ", ".join(game["maps"][i] for i in choices) or "none"
        await interaction.followup.send(f"Your votes: {names}", ephemeral=True)
        return
    elif action == "start":
        if not (game["team_a"] and game["team_b"]):
            await _deny(interaction, "❌ Both teams need at least one player.")
            return
        game["maps"] = settings.get("maps", DEFAULT_MAPS)
        game["votes"] = {}
        game["vote_ends_at"] = time.time() + VOTE_SECONDS
        game["phase"] = "vote"
    elif action == "endvote":
        finish_vote(game)
    elif action == "won":
        game["winner"] = arg
        game["phase"] = "finished"
        apply_result(leaderboard, game, 1)
    elif action == "undo":
        apply_result(leaderboard, game, -1)
        del game["winner"]
        game["phase"] = "playing"
    elif action == "cancel":
        del games[key]
        store.update(interaction.guild_id, games=games)
        embed = _base_embed("🚫 Custom game cancelled", game, discord.Color.dark_grey())
        await interaction.response.edit_message(embed=embed, view=None)
        return

    # Saved together, so a game and its leaderboard change can't get out of step.
    store.update(interaction.guild_id, games=games, leaderboard=leaderboard)
    embed, view = render(game)
    await interaction.response.edit_message(embed=embed, view=view)


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
            embed, view = render(game)
            message = client.get_partial_messageable(game["channel_id"]).get_partial_message(int(key))
            # The result is already saved, and clicking the old message refreshes it.
            try:
                await message.edit(embed=embed, view=view)
            except discord.HTTPException:
                log.exception("Couldn't update the custom game message %s in %s", key, guild)


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


class TeamSelect(discord.ui.DynamicItem[discord.ui.UserSelect], template=r"mikasa:team:(?P<team>[ab])"):
    team: str

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        select = cls(item)
        select.team = match["team"]
        return select

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_team_select(interaction, self.team, self.item.values)


@app_commands.command(name="customgame", description="(Admins only) Start a custom game")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def customgame_command(interaction: discord.Interaction):
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can start a custom game.")
        return

    game = {"phase": "teams", "team_a": [], "team_b": [], "channel_id": interaction.channel_id}
    embed, view = render(game)
    callback = await interaction.response.send_message(embed=embed, view=view)
    message_id = callback.message_id or (await interaction.original_response()).id

    store = interaction.client.store
    games = store.get(interaction.guild_id).get("games", {})
    games[str(message_id)] = game
    store.update(interaction.guild_id, games=games)


def build_leaderboard(leaderboard: dict) -> discord.Embed:
    ranked = sorted(leaderboard.items(), key=lambda item: (-item[1]["wins"], item[1]["losses"]))

    def plural(count: int, word: str) -> str:
        return f"{count} {word}" if count == 1 else f"{count} {word}{'es' if word == 'loss' else 's'}"

    lines = [
        f"{rank}. <@{user_id}>: {plural(entry['wins'], 'win')}, {plural(entry['losses'], 'loss')}"
        for rank, (user_id, entry) in enumerate(ranked[:LEADERBOARD_SIZE], start=1)
    ]
    return discord.Embed(
        title="🏆 Leaderboard",
        description="\n".join(lines) or "No games have been played yet.",
        color=discord.Color.gold(),
    )


@app_commands.command(name="leaderboard", description="Show who has the most custom game wins in this server")
@app_commands.guild_only()
async def leaderboard_command(interaction: discord.Interaction):
    leaderboard = interaction.client.store.get(interaction.guild_id).get("leaderboard", {})
    await interaction.response.send_message(embed=build_leaderboard(leaderboard))
