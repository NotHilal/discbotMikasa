import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.components import _component_factory
from discord.ui.view import ViewStore

import bot
import customgame
import storage


def user(user_id, is_bot=False):
    return MagicMock(id=user_id, bot=is_bot)


class CustomGameCase(unittest.IsolatedAsyncioTestCase):
    MESSAGE_ID = 500
    GUILD_ID = 1

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "data.json"
        self.store = storage.Store(self.path)
        self.client = MagicMock(store=self.store)

    def interaction(self, user_id=100, admin=True):
        interaction = MagicMock(guild_id=self.GUILD_ID, client=self.client)
        interaction.message.id = self.MESSAGE_ID
        interaction.channel_id = 77
        interaction.followup.send = AsyncMock()
        interaction.user.id = user_id
        interaction.permissions.manage_guild = admin
        interaction.response.send_message = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        return interaction

    def save_game(self, **fields):
        game = {"phase": "teams", "team_a": [1, 2], "team_b": [3, 4], **fields}
        self.store.update(self.GUILD_ID, games={str(self.MESSAGE_ID): game})

    def game(self):
        return self.store.get(self.GUILD_ID).get("games", {}).get(str(self.MESSAGE_ID))

    def leaderboard(self):
        return self.store.get(self.GUILD_ID).get("leaderboard", {})

    def denial(self, interaction):
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        interaction.response.edit_message.assert_not_awaited()
        return interaction.response.send_message.await_args.args[0]


class HelperTests(unittest.TestCase):
    def test_parse_maps_cleans_up_the_list(self):
        self.assertEqual(customgame.parse_maps(" Bind, haven ,, BIND,Lotus"), (["Bind", "haven", "Lotus"], None))

    def test_parse_maps_rejects_bad_lists(self):
        self.assertIsNone(customgame.parse_maps("Bind")[0])
        self.assertIsNone(customgame.parse_maps(",".join(f"m{i}" for i in range(21)))[0])
        self.assertIsNone(customgame.parse_maps(f"Bind, {'x' * 41}")[0])

    def test_map_with_most_votes_wins(self):
        self.assertEqual(customgame.pick_map(["A", "B"], {"1": [1], "2": [1, 0], "3": [1]}), ("B", None))

    def test_tie_is_picked_from_the_tied_maps(self):
        for _ in range(20):
            name, note = customgame.pick_map(["A", "B", "C"], {"1": [0], "2": [2]})
            self.assertIn(name, ("A", "C"))
            self.assertIn("Tied", note)

    def test_no_votes_picks_at_random(self):
        name, note = customgame.pick_map(["A", "B"], {})
        self.assertIn(name, ("A", "B"))
        self.assertIn("No votes", note)

    def test_leaderboard_ranks_by_wins_then_fewest_losses(self):
        text = customgame.build_leaderboard(
            {
                "1": {"wins": 1, "losses": 5},
                "2": {"wins": 3, "losses": 1},
                "3": {"wins": 1, "losses": 0},
            }
        ).description
        self.assertEqual(
            text.split("\n"),
            ["1. <@2>: 3 wins, 1 loss", "2. <@3>: 1 win, 0 losses", "3. <@1>: 1 win, 5 losses"],
        )

    def test_empty_leaderboard(self):
        self.assertIn("No games", customgame.build_leaderboard({}).description)

    def test_leaderboard_shows_top_20(self):
        board = {str(i): {"wins": i, "losses": 0} for i in range(30)}
        self.assertEqual(len(customgame.build_leaderboard(board).description.split("\n")), 20)


class RenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_phase_builds_valid_components(self):
        maps = [f"Map {i}" for i in range(customgame.MAX_MAPS)]
        base = {"team_a": [1, 2, 3, 4, 5], "team_b": [6, 7, 8, 9, 10]}
        for game in (
            {"phase": "teams", **base},
            {"phase": "vote", "maps": maps, "votes": {"1": [0, 1]}, "vote_ends_at": 1000, **base},
            {"phase": "playing", "map": "Bind", **base},
            {"phase": "finished", "map": "Bind", "winner": "a", **base},
        ):
            embed, view = customgame.render(game)
            rows = view.to_components()
            self.assertLessEqual(len(rows), 5)
            self.assertTrue(embed.title)
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)
            for row in rows:
                for component in row["components"]:
                    self.assertTrue(component["custom_id"].startswith("mikasa:"))

    async def test_start_button_is_disabled_until_both_teams_have_players(self):
        def start_disabled(game):
            _, view = customgame.render(game)
            return next(i for i in view.children if i.custom_id == "mikasa:game:start").item.disabled

        self.assertTrue(start_disabled({"phase": "teams", "team_a": [1], "team_b": []}))
        self.assertFalse(start_disabled({"phase": "teams", "team_a": [1], "team_b": [2]}))


class StartTests(CustomGameCase):
    async def test_admin_starts_a_saved_game(self):
        interaction = self.interaction()
        interaction.response.send_message.return_value = MagicMock(message_id=self.MESSAGE_ID)
        await customgame.customgame_command.callback(interaction)
        self.assertEqual(self.game(), {"phase": "teams", "team_a": [], "team_b": [], "channel_id": 77})

    async def test_non_admin_cannot_start(self):
        interaction = self.interaction(admin=False)
        await customgame.customgame_command.callback(interaction)
        self.assertIsNone(self.game())

    def test_commands_have_the_right_access(self):
        self.assertTrue(customgame.customgame_command.default_permissions.manage_guild)
        self.assertIsNone(customgame.leaderboard_command.default_permissions)


class TeamTests(CustomGameCase):
    async def test_admin_sets_a_team(self):
        self.save_game(team_a=[], team_b=[])
        await customgame.handle_team_select(self.interaction(), "a", [user(1), user(2)])
        self.assertEqual(self.game()["team_a"], [1, 2])

    async def test_picking_someone_moves_them_from_the_other_team(self):
        self.save_game(team_a=[1], team_b=[2])
        await customgame.handle_team_select(self.interaction(), "b", [user(1), user(2)])
        self.assertEqual(self.game()["team_a"], [])
        self.assertEqual(self.game()["team_b"], [1, 2])

    async def test_bots_are_left_out(self):
        self.save_game(team_a=[], team_b=[])
        await customgame.handle_team_select(self.interaction(), "a", [user(1), user(2, is_bot=True)])
        self.assertEqual(self.game()["team_a"], [1])

    async def test_non_admin_cannot_set_teams(self):
        self.save_game()
        interaction = self.interaction(admin=False)
        await customgame.handle_team_select(interaction, "a", [user(9)])
        self.assertIn("admins", self.denial(interaction))
        self.assertEqual(self.game()["team_a"], [1, 2])

    async def test_teams_are_locked_once_the_vote_starts(self):
        self.save_game(phase="vote", maps=["A", "B"], votes={})
        interaction = self.interaction()
        await customgame.handle_team_select(interaction, "a", [user(9)])
        self.assertIn("can't be changed", self.denial(interaction))
        self.assertEqual(self.game()["team_a"], [1, 2])

    async def test_start_needs_both_teams(self):
        self.save_game(team_b=[])
        interaction = self.interaction()
        await customgame.handle_button(interaction, "start", None)
        self.assertIn("at least one", self.denial(interaction))
        self.assertEqual(self.game()["phase"], "teams")

    async def test_start_uses_the_servers_maps(self):
        self.save_game()
        self.store.update(self.GUILD_ID, maps=["Custom", "Other"])
        await customgame.handle_button(self.interaction(), "start", None)
        self.assertEqual(self.game()["phase"], "vote")
        self.assertEqual(self.game()["maps"], ["Custom", "Other"])
        self.assertAlmostEqual(self.game()["vote_ends_at"], time.time() + customgame.VOTE_SECONDS, delta=5)

    async def test_start_defaults_to_the_built_in_maps(self):
        self.save_game()
        await customgame.handle_button(self.interaction(), "start", None)
        self.assertEqual(self.game()["maps"], customgame.DEFAULT_MAPS)


class VoteTests(CustomGameCase):
    def setUp(self):
        super().setUp()
        self.save_game(phase="vote", maps=["A", "B", "C"], votes={}, vote_ends_at=time.time() + 100, channel_id=77)

    async def test_player_votes_without_being_an_admin(self):
        interaction = self.interaction(user_id=1, admin=False)
        await customgame.handle_button(interaction, "vote", "1")
        self.assertEqual(self.game()["votes"], {"1": [1]})
        self.assertEqual(interaction.followup.send.await_args.args[0], "Your votes: B")
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_player_gets_two_votes(self):
        await customgame.handle_button(self.interaction(user_id=1), "vote", "0")
        await customgame.handle_button(self.interaction(user_id=1), "vote", "1")
        self.assertEqual(self.game()["votes"], {"1": [0, 1]})

    async def test_third_vote_is_refused(self):
        await customgame.handle_button(self.interaction(user_id=1), "vote", "0")
        await customgame.handle_button(self.interaction(user_id=1), "vote", "1")
        interaction = self.interaction(user_id=1)
        await customgame.handle_button(interaction, "vote", "2")
        self.assertIn("used all your votes", self.denial(interaction))
        self.assertEqual(self.game()["votes"], {"1": [0, 1]})

    async def test_clicking_a_map_again_removes_the_vote(self):
        await customgame.handle_button(self.interaction(user_id=1), "vote", "0")
        await customgame.handle_button(self.interaction(user_id=1), "vote", "0")
        self.assertEqual(self.game()["votes"], {})

    async def test_two_maps_allow_one_vote_each(self):
        self.save_game(phase="vote", maps=["A", "B"], votes={}, vote_ends_at=time.time() + 100)
        await customgame.handle_button(self.interaction(user_id=1), "vote", "0")
        interaction = self.interaction(user_id=1)
        await customgame.handle_button(interaction, "vote", "1")
        self.assertIn("used all your votes", self.denial(interaction))

    async def test_non_players_cannot_vote_even_if_admin(self):
        interaction = self.interaction(user_id=100, admin=True)
        await customgame.handle_button(interaction, "vote", "0")
        self.assertIn("Only the players", self.denial(interaction))
        self.assertEqual(self.game()["votes"], {})

    async def test_vote_ends_when_everyone_has_used_both_votes(self):
        for user_id in (1, 2, 3):
            await customgame.handle_button(self.interaction(user_id=user_id), "vote", "1")
            await customgame.handle_button(self.interaction(user_id=user_id), "vote", "0")
        await customgame.handle_button(self.interaction(user_id=4), "vote", "1")
        self.assertEqual(self.game()["phase"], "vote")
        await customgame.handle_button(self.interaction(user_id=4), "vote", "2")
        self.assertEqual(self.game()["phase"], "playing")
        self.assertEqual(self.game()["map"], "B")

    async def test_click_after_the_vote_closed_refreshes_the_message(self):
        self.save_game(phase="playing", map="A", maps=["A", "B", "C"], votes={})
        interaction = self.interaction(user_id=1, admin=False)
        await customgame.handle_button(interaction, "vote", "1")
        self.assertEqual(self.game()["votes"], {})
        self.assertIn("Map: A", interaction.response.edit_message.await_args.kwargs["embed"].title)

    async def test_admin_can_end_the_vote_early(self):
        await customgame.handle_button(self.interaction(user_id=1), "vote", "0")
        await customgame.handle_button(self.interaction(), "endvote", None)
        self.assertEqual(self.game()["phase"], "playing")
        self.assertEqual(self.game()["map"], "A")

    async def test_player_cannot_end_the_vote(self):
        interaction = self.interaction(user_id=1, admin=False)
        await customgame.handle_button(interaction, "endvote", None)
        self.assertIn("admins", self.denial(interaction))
        self.assertEqual(self.game()["phase"], "vote")


class ResultTests(CustomGameCase):
    def setUp(self):
        super().setUp()
        self.save_game(phase="playing", map="Bind", maps=["Bind"], votes={})

    async def test_result_updates_the_leaderboard(self):
        await customgame.handle_button(self.interaction(), "won", "a")
        self.assertEqual(self.game()["phase"], "finished")
        board = self.leaderboard()
        self.assertEqual(board["1"], {"wins": 1, "losses": 0})
        self.assertEqual(board["3"], {"wins": 0, "losses": 1})

    async def test_double_click_only_counts_once(self):
        await customgame.handle_button(self.interaction(), "won", "a")
        await customgame.handle_button(self.interaction(), "won", "b")
        self.assertEqual(self.game()["winner"], "a")
        self.assertEqual(self.leaderboard()["1"], {"wins": 1, "losses": 0})

    async def test_only_admins_report_results(self):
        interaction = self.interaction(user_id=1, admin=False)
        await customgame.handle_button(interaction, "won", "a")
        self.assertIn("admins", self.denial(interaction))
        self.assertEqual(self.leaderboard(), {})

    async def test_undo_restores_the_leaderboard_and_allows_a_correction(self):
        self.store.update(self.GUILD_ID, leaderboard={"1": {"wins": 4, "losses": 2}})
        await customgame.handle_button(self.interaction(), "won", "b")
        await customgame.handle_button(self.interaction(), "undo", None)
        self.assertEqual(self.game()["phase"], "playing")
        self.assertEqual(self.leaderboard(), {"1": {"wins": 4, "losses": 2}})
        await customgame.handle_button(self.interaction(), "won", "a")
        self.assertEqual(self.leaderboard()["1"], {"wins": 5, "losses": 2})
        self.assertEqual(self.leaderboard()["3"], {"wins": 0, "losses": 1})

    async def test_cancel_removes_the_game_without_scoring(self):
        interaction = self.interaction()
        await customgame.handle_button(interaction, "cancel", None)
        self.assertIsNone(self.game())
        self.assertEqual(self.leaderboard(), {})
        self.assertIsNone(interaction.response.edit_message.await_args.kwargs["view"])

    async def test_clicks_on_a_cancelled_game_are_refused(self):
        await customgame.handle_button(self.interaction(), "cancel", None)
        interaction = self.interaction()
        await customgame.handle_button(interaction, "won", "a")
        self.assertIn("no longer exists", self.denial(interaction))

    async def test_game_and_result_survive_reloading_from_disk(self):
        await customgame.handle_button(self.interaction(), "won", "a")
        reloaded = storage.Store(self.path)
        settings = reloaded.get(self.GUILD_ID)
        self.assertEqual(settings["games"][str(self.MESSAGE_ID)]["winner"], "a")
        self.assertEqual(settings["leaderboard"]["1"]["wins"], 1)


class VoteTimerTests(CustomGameCase):
    def setUp(self):
        super().setUp()
        self.edit = AsyncMock()
        self.client.guilds = [MagicMock(id=self.GUILD_ID)]
        self.client.get_partial_messageable.return_value.get_partial_message.return_value.edit = self.edit

    def vote_game(self, ends_at):
        self.save_game(phase="vote", maps=["A", "B", "C"], votes={"1": [2]}, vote_ends_at=ends_at, channel_id=77)

    async def test_expired_vote_picks_the_map_and_updates_the_message(self):
        self.vote_game(ends_at=1000)
        await customgame.expire_votes(self.client, 1000)
        self.assertEqual(self.game()["phase"], "playing")
        self.assertEqual(self.game()["map"], "C")
        self.client.get_partial_messageable.assert_called_once_with(77)
        self.client.get_partial_messageable.return_value.get_partial_message.assert_called_once_with(self.MESSAGE_ID)
        self.assertIn("Map: C", self.edit.await_args.kwargs["embed"].title)

    async def test_vote_with_time_left_is_untouched(self):
        self.vote_game(ends_at=1001)
        await customgame.expire_votes(self.client, 1000)
        self.assertEqual(self.game()["phase"], "vote")
        self.edit.assert_not_awaited()

    async def test_other_phases_are_untouched(self):
        self.save_game(phase="playing", map="A", vote_ends_at=1, channel_id=77)
        await customgame.expire_votes(self.client, 1000)
        self.edit.assert_not_awaited()

    async def test_failed_message_update_keeps_the_result(self):
        self.vote_game(ends_at=1000)
        self.edit.side_effect = discord.HTTPException(MagicMock(status=500, reason="x"), "x")
        with self.assertLogs("mikasa.customgame", "ERROR"):
            await customgame.expire_votes(self.client, 1000)
        self.assertEqual(self.game()["map"], "C")

    async def test_vote_that_ended_while_offline_closes_after_a_reload(self):
        self.vote_game(ends_at=1000)
        self.store = storage.Store(self.path)
        self.client.store = self.store
        await customgame.expire_votes(self.client, 5000)
        self.assertEqual(self.game()["phase"], "playing")


class RestartTests(CustomGameCase):
    """Runs clicks through discord.py's real dispatcher with a fresh view store, as
    after a restart: nothing is in memory except the registered dynamic item types."""

    async def click(self, game, custom_id, component_type, item_type, data=None, **interaction_args):
        client = bot.MikasaBot(self.store, "id", "secret")
        store = ViewStore(client._connection)
        store.add_dynamic_items(customgame.GameButton, customgame.TeamSelect)

        _, view = customgame.render(game)
        interaction = self.interaction(**interaction_args)
        interaction.client = client
        interaction._state = client._connection
        interaction.data = {"custom_id": custom_id, "component_type": component_type, **(data or {})}
        interaction.message.flags.components_v2 = False
        interaction.message.components = [
            _component_factory(row, client._connection) for row in view.to_components()
        ]

        match = item_type.__discord_ui_compiled_template__.fullmatch(custom_id)
        self.assertIsNotNone(match)
        await store.schedule_dynamic_item_call(component_type, item_type, interaction, custom_id, match)
        return interaction

    async def test_vote_button_works_after_restart(self):
        self.save_game(phase="vote", maps=["A", "B", "C"], votes={}, vote_ends_at=time.time() + 100)
        await self.click(self.game(), "mikasa:game:vote:1", 2, customgame.GameButton, user_id=1, admin=False)
        self.assertEqual(self.game()["votes"], {"1": [1]})

    async def test_result_button_works_after_restart(self):
        self.save_game(phase="playing", map="Bind")
        await self.click(self.game(), "mikasa:game:won:b", 2, customgame.GameButton)
        self.assertEqual(self.game()["winner"], "b")
        self.assertEqual(self.leaderboard()["3"]["wins"], 1)

    async def test_team_dropdown_works_after_restart(self):
        self.save_game(team_a=[], team_b=[])
        with patch.object(
            discord.app_commands.Namespace,
            "_get_resolved_items",
            return_value={(0, 1): user(1), (0, 2): user(2)},
        ):
            await self.click(
                self.game(),
                "mikasa:team:a",
                5,
                customgame.TeamSelect,
                data={"values": ["1", "2"], "resolved": {}},
            )
        self.assertEqual(self.game()["team_a"], [1, 2])


class SetupMapsTests(CustomGameCase):
    async def test_setup_maps_saves_and_clear_restores_defaults(self):
        self.client = MagicMock(store=self.store)
        await bot.setup_maps.callback(self.interaction(), "Bind, Haven")
        self.assertEqual(self.store.get(self.GUILD_ID)["maps"], ["Bind", "Haven"])
        await bot.setup_clear.callback(self.interaction(), "maps")
        self.assertNotIn("maps", self.store.get(self.GUILD_ID))

    async def test_setup_maps_rejects_bad_input(self):
        interaction = self.interaction()
        await bot.setup_maps.callback(interaction, "Bind")
        self.assertIn("between 2", interaction.response.send_message.await_args.args[0])
        self.assertNotIn("maps", self.store.get(self.GUILD_ID))


if __name__ == "__main__":
    unittest.main()
