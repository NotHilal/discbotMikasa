import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

import customgame
from test_customgame import CustomGameCase


def payload(user_id, message_id=500, emoji=customgame.QUEUE_EMOJI, is_bot=False, guild_id=1):
    event = MagicMock(user_id=user_id, message_id=message_id, guild_id=guild_id)
    event.emoji.__str__.return_value = emoji
    event.member = MagicMock(bot=is_bot, display_name=f"Name{user_id}")
    return event


class QueueCase(CustomGameCase):
    def setUp(self):
        super().setUp()
        self.client.user.id = 999
        partial = self.client.get_partial_messageable.return_value.get_partial_message.return_value
        partial.edit = AsyncMock()
        partial.remove_reaction = AsyncMock()
        self.partial = partial

    def save_queue(self, queue=(), **fields):
        game = {"phase": "queue", "queue": list(queue), "team_a": [], "team_b": [], "channel_id": 77, **fields}
        self.store.update(self.GUILD_ID, games={str(self.MESSAGE_ID): game})

    async def react(self, *args, adding=True, **kwargs):
        await customgame.handle_queue_reaction(self.client, payload(*args, **kwargs), adding)


class ReactionTests(QueueCase):
    async def test_reacting_joins_and_updates_the_message(self):
        self.save_queue()
        await self.react(1)
        self.assertEqual(self.game()["queue"], [1])
        self.assertIn("1/10", self.partial.edit.await_args.kwargs["embed"].title)

    async def test_joining_twice_counts_once(self):
        self.save_queue([1])
        await self.react(1)
        self.assertEqual(self.game()["queue"], [1])

    async def test_removing_the_reaction_leaves(self):
        self.save_queue([1, 2])
        await self.react(1, adding=False)
        self.assertEqual(self.game()["queue"], [2])

    async def test_tenth_player_closes_the_queue(self):
        self.save_queue(range(1, 10))
        await self.react(10)
        self.assertEqual(self.game()["phase"], "teams")
        self.assertEqual(len(self.game()["queue"]), 10)

    async def test_late_reaction_is_removed(self):
        self.save_queue(range(1, 11), phase="teams")
        await self.react(11)
        self.assertNotIn(11, self.game()["queue"])
        self.assertEqual(self.partial.remove_reaction.await_args.args[1].id, 11)

    async def test_leaving_a_full_queue_reopens_it(self):
        self.save_queue(range(1, 11), phase="teams", team_a=[1, 2], team_b=[3])
        await self.react(1, adding=False)
        game = self.game()
        self.assertEqual(game["phase"], "queue")
        self.assertEqual(game["team_a"], [2])
        self.assertEqual(game["team_b"], [3])
        self.assertEqual(len(game["queue"]), 9)

    async def test_leaving_after_teams_are_confirmed_changes_nothing(self):
        self.save_queue(range(1, 11), phase="vote", team_a=[1], team_b=[2], maps=["A", "B"], votes={}, vote_ends_at=1)
        await self.react(1, adding=False)
        self.assertEqual(self.game()["phase"], "vote")
        self.assertIn(1, self.game()["queue"])

    async def test_ignores_the_bots_own_reaction_other_bots_and_other_emoji(self):
        self.save_queue()
        await self.react(999)
        await self.react(5, is_bot=True)
        await self.react(6, emoji="🔥")
        self.assertEqual(self.game()["queue"], [])

    async def test_ignores_reactions_on_other_messages_and_games_without_a_queue(self):
        self.save_queue()
        await self.react(1, message_id=12345)
        self.assertEqual(self.game()["queue"], [])
        game = {"phase": "playing", "map": "A", "team_a": [2], "team_b": [3], "channel_id": 77}
        self.store.update(self.GUILD_ID, games={str(self.MESSAGE_ID): game})
        await self.react(1)
        self.assertEqual(self.game(), game)


class KickTests(QueueCase):
    async def test_admin_kicks_a_player_and_their_reaction_goes(self):
        self.save_queue([1, 2])
        interaction = self.interaction()
        interaction.message.remove_reaction = AsyncMock()
        await customgame.handle_kick(interaction, 1)
        self.assertEqual(self.game()["queue"], [2])
        interaction.message.remove_reaction.assert_awaited_once()

    async def test_kicked_player_cannot_rejoin_that_queue(self):
        self.save_queue([1, 2])
        interaction = self.interaction()
        interaction.message.remove_reaction = AsyncMock()
        await customgame.handle_kick(interaction, 1)
        await self.react(1)
        self.assertEqual(self.game()["queue"], [2])
        self.assertEqual(self.partial.remove_reaction.await_args.args[1].id, 1)

    async def test_other_players_can_still_join_after_a_kick(self):
        self.save_queue([1], kicked=[9])
        await self.react(2)
        self.assertEqual(self.game()["queue"], [1, 2])

    async def test_player_who_left_by_themselves_can_rejoin(self):
        self.save_queue([1])
        await self.react(1, adding=False)
        await self.react(1)
        self.assertEqual(self.game()["queue"], [1])

    async def test_kicking_from_a_full_queue_reopens_it(self):
        self.save_queue(range(1, 11), phase="teams")
        interaction = self.interaction()
        interaction.message.remove_reaction = AsyncMock()
        await customgame.handle_kick(interaction, 3)
        self.assertEqual(self.game()["phase"], "queue")

    async def test_non_admin_cannot_kick(self):
        self.save_queue([1])
        interaction = self.interaction(admin=False)
        await customgame.handle_kick(interaction, 1)
        self.assertIn("admins", self.denial(interaction))
        self.assertEqual(self.game()["queue"], [1])

    async def test_kicking_someone_not_queued_is_refused(self):
        self.save_queue([1])
        interaction = self.interaction()
        await customgame.handle_kick(interaction, 9)
        self.assertIn("isn't in the queue", self.denial(interaction))

    async def test_failed_reaction_removal_still_kicks(self):
        self.save_queue([1])
        interaction = self.interaction()
        interaction.message.remove_reaction = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403, reason="no"), "no")
        )
        await customgame.handle_kick(interaction, 1)
        self.assertEqual(self.game()["queue"], [])


class QueueTeamTests(QueueCase):
    def full(self, **fields):
        self.save_queue(range(1, 11), phase="teams", **fields)

    def selects(self, game):
        _, view = customgame.render(game)
        return {item.custom_id: item for item in view.children if hasattr(item.item, "options")}

    async def test_reacting_saves_the_players_name(self):
        self.save_queue()
        await self.react(1)
        self.assertEqual(self.game()["names"], {"1": "Name1"})

    async def test_team_dropdowns_only_offer_queued_players(self):
        self.save_queue([1, 2, 3], phase="teams", started_early=True, names={"1": "Ann", "2": "Bo"}, team_a=[1])
        selects = self.selects(self.game())
        team = selects["mikasa:queueteam:a"].item
        self.assertEqual([option.value for option in team.options], ["1", "2", "3"])
        self.assertEqual([option.label for option in team.options], ["Ann", "Bo", "Player 3"])
        self.assertEqual([option.default for option in team.options], [True, False, False])
        self.assertEqual(team.max_values, 3)
        self.assertEqual(team.min_values, 0)

    async def test_full_queue_limits_each_team_to_five(self):
        self.full()
        self.assertEqual(self.selects(self.game())["mikasa:queueteam:b"].item.max_values, 5)

    async def test_kick_dropdown_only_offers_queued_players(self):
        self.save_queue([1, 2])
        kick = self.selects(self.game())["mikasa:queuekick"].item
        self.assertEqual([option.value for option in kick.options], ["1", "2"])

    async def test_empty_queue_has_no_kick_dropdown(self):
        self.save_queue()
        self.assertEqual(self.selects(self.game()), {})

    async def test_custom_ids_match_their_handlers(self):
        self.full()
        selects = self.selects(self.game())
        for custom_id, item in selects.items():
            self.assertIn(type(item), customgame.DYNAMIC_ITEMS)
            self.assertIsNotNone(type(item).__discord_ui_compiled_template__.fullmatch(custom_id))

    async def test_picking_from_the_dropdown_sets_the_team(self):
        self.full()
        await customgame._set_team(self.interaction(), "a", [1, 2])
        self.assertEqual(self.game()["team_a"], [1, 2])

    async def test_picking_someone_moves_them_between_teams(self):
        self.full(team_a=[1])
        await customgame._set_team(self.interaction(), "b", [1])
        self.assertEqual(self.game()["team_a"], [])
        self.assertEqual(self.game()["team_b"], [1])

    async def test_only_queued_players_can_be_picked(self):
        self.full()
        interaction = self.interaction()
        await customgame._set_team(interaction, "a", [1, 50])
        self.assertIn("Only players in the queue", self.denial(interaction))
        self.assertEqual(self.game()["team_a"], [])

    async def test_confirm_needs_five_on_each_team(self):
        self.full(team_a=[1, 2, 3, 4, 5], team_b=[6, 7, 8, 9])
        interaction = self.interaction()
        await customgame.handle_button(interaction, "start", None)
        self.assertIn("exactly 5", self.denial(interaction))
        self.assertEqual(self.game()["phase"], "teams")

    async def test_confirm_starts_the_vote(self):
        self.full(team_a=[1, 2, 3, 4, 5], team_b=[6, 7, 8, 9, 10])
        await customgame.handle_button(self.interaction(), "start", None)
        self.assertEqual(self.game()["phase"], "vote")

    async def test_queue_phases_render_valid_components(self):
        base = {"channel_id": 77}
        for game in (
            {"phase": "queue", "queue": [], "team_a": [], "team_b": [], **base},
            {"phase": "teams", "queue": list(range(1, 11)), "team_a": [1], "team_b": [], **base},
        ):
            _, view = customgame.render(game)
            self.assertLessEqual(len(view.to_components()), 5)

    async def test_cancel_works_on_an_open_queue(self):
        self.save_queue([1])
        await customgame.handle_button(self.interaction(), "cancel", None)
        self.assertIsNone(self.game())


class QueueCommandTests(QueueCase):
    def reply(self, interaction):
        call = interaction.response.send_message.await_args
        return call.args[0] if call.args else call.kwargs["embed"].description

    def test_queue_is_a_group_for_server_managers(self):
        self.assertTrue(customgame.queue_group.guild_only)
        self.assertTrue(customgame.queue_group.default_permissions.manage_guild)
        self.assertEqual(sorted(c.name for c in customgame.queue_group.commands), ["list", "open", "start"])

    async def test_start_closes_the_queue_early(self):
        self.save_queue([1, 2])
        await customgame.queue_start.callback(self.interaction())
        self.assertEqual(self.game()["phase"], "teams")
        self.assertIn("started early", self.partial.edit.await_args.kwargs["embed"].description)

    async def test_start_needs_two_players(self):
        self.save_queue([1])
        interaction = self.interaction()
        await customgame.queue_start.callback(interaction)
        self.assertIn("at least 2", self.denial(interaction))
        self.assertEqual(self.game()["phase"], "queue")

    async def test_start_needs_an_open_queue_in_this_channel(self):
        self.save_queue([1, 2], channel_id=55)
        interaction = self.interaction()
        await customgame.queue_start.callback(interaction)
        self.assertIn("no open queue", self.denial(interaction))

    async def test_start_picks_the_newest_queue(self):
        older = {"phase": "queue", "queue": [1, 2], "team_a": [], "team_b": [], "channel_id": 77}
        newer = {**older, "queue": [3, 4]}
        self.store.update(self.GUILD_ID, games={"400": older, "500": newer})
        await customgame.queue_start.callback(self.interaction())
        games = self.store.get(self.GUILD_ID)["games"]
        self.assertEqual(games["400"]["phase"], "queue")
        self.assertEqual(games["500"]["phase"], "teams")

    async def test_started_queue_allows_small_teams(self):
        self.save_queue([1, 2], phase="teams", started_early=True, team_a=[1], team_b=[2])
        await customgame.handle_button(self.interaction(), "start", None)
        self.assertEqual(self.game()["phase"], "vote")

    async def test_leaving_a_started_queue_reopens_it_normally(self):
        self.save_queue([1, 2], phase="teams", started_early=True)
        await self.react(1, adding=False)
        self.assertEqual(self.game()["phase"], "queue")
        self.assertNotIn("started_early", self.game())

    async def test_only_admins_can_start_or_list(self):
        self.save_queue([1, 2])
        for command in (customgame.queue_start, customgame.queue_list):
            interaction = self.interaction(admin=False)
            await command.callback(interaction)
            self.assertIn("admins", self.denial(interaction))
        self.assertEqual(self.game()["phase"], "queue")

    async def test_list_shows_queues_with_links_and_skips_other_games(self):
        queue = {"phase": "queue", "queue": [1], "team_a": [], "team_b": [], "channel_id": 77}
        teams = {**queue, "phase": "teams", "queue": list(range(1, 11))}
        finished = {**queue, "phase": "finished", "winner": "a"}
        plain = {"phase": "teams", "team_a": [], "team_b": [], "channel_id": 77}
        self.store.update(self.GUILD_ID, games={"10": queue, "20": teams, "30": finished, "40": plain})
        interaction = self.interaction()
        await customgame.queue_list.callback(interaction)
        lines = self.reply(interaction).split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("1/10, open", lines[0])
        self.assertIn("https://discord.com/channels/1/77/10", lines[0])
        self.assertIn("10/10, picking teams", lines[1])

    async def test_list_when_empty(self):
        interaction = self.interaction()
        await customgame.queue_list.callback(interaction)
        self.assertIn("No queues", self.reply(interaction))


class ScoreboardTests(CustomGameCase):
    def setUp(self):
        super().setUp()
        self.channel = self.client.get_partial_messageable.return_value
        self.channel.send = AsyncMock(return_value=MagicMock(id=321))
        self.edit = AsyncMock()
        self.channel.get_partial_message.return_value.edit = self.edit

    def configure(self, **fields):
        self.store.update(self.GUILD_ID, scoreboard_channel_id=88, **fields)

    async def refresh(self):
        await customgame.refresh_scoreboard(self.client, self.GUILD_ID)

    async def test_does_nothing_without_a_channel(self):
        await self.refresh()
        self.channel.send.assert_not_awaited()

    async def test_first_update_posts_and_remembers_the_message(self):
        self.configure()
        await self.refresh()
        self.client.get_partial_messageable.assert_called_once_with(88)
        titles = [embed.title for embed in self.channel.send.await_args.kwargs["embeds"]]
        self.assertEqual(titles, ["🏆 Leaderboard", "🏆 Game Points Leaderboard"])
        self.assertEqual(self.store.get(self.GUILD_ID)["scoreboard_message_id"], 321)

    async def test_later_updates_edit_the_same_message(self):
        self.configure(scoreboard_message_id=321, points={"1": 500})
        await self.refresh()
        self.channel.send.assert_not_awaited()
        self.channel.get_partial_message.assert_called_once_with(321)
        self.assertIn("500 GP", self.edit.await_args.kwargs["embeds"][1].description)

    async def test_deleted_message_is_replaced(self):
        self.configure(scoreboard_message_id=321)
        self.edit.side_effect = discord.NotFound(MagicMock(status=404, reason="gone"), "gone")
        self.channel.send.return_value = MagicMock(id=654)
        await self.refresh()
        self.assertEqual(self.store.get(self.GUILD_ID)["scoreboard_message_id"], 654)

    async def test_failure_is_logged_and_does_not_raise(self):
        self.configure()
        self.channel.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="no"), "no")
        with self.assertLogs("mikasa.customgame", "ERROR"):
            await self.refresh()

    async def test_points_commands_update_it(self):
        self.configure(scoreboard_message_id=321)
        player = MagicMock(id=1, mention="<@1>")
        await customgame.addpoints_command.callback(self.interaction(), player, 75)
        self.assertIn("75 GP", self.edit.await_args.kwargs["embeds"][1].description)

    async def test_clearstats_updates_it(self):
        self.configure(scoreboard_message_id=321, points={"1": 75})
        await customgame.clearstats_command.callback(self.interaction(), MagicMock(id=1, mention="<@1>"), "both")
        self.assertIn("Nobody", self.edit.await_args.kwargs["embeds"][1].description)

    async def test_reset_updates_it(self):
        self.configure(scoreboard_message_id=321, points={"1": 75})
        interaction = self.interaction()
        await customgame.resetleaderboard_command.callback(interaction, "game points")
        view = interaction.response.send_message.await_args.kwargs["view"]
        await view.confirm.callback(self.interaction())
        self.assertIn("Nobody", self.edit.await_args.kwargs["embeds"][1].description)

    async def test_result_and_undo_update_it(self):
        self.configure(scoreboard_message_id=321)
        self.save_game(phase="playing", map="Bind", maps=["Bind"], votes={}, channel_id=77)
        await customgame.handle_button(self.interaction(), "won", "a")
        self.assertIn("1 win", self.edit.await_args.kwargs["embeds"][0].description)
        await customgame.handle_button(self.interaction(), "undo", None)
        self.assertIn("No games", self.edit.await_args.kwargs["embeds"][0].description)


class CallTests(QueueCase):
    def setUp(self):
        super().setUp()
        self.created = []

        async def create(name, **kwargs):
            channel = MagicMock(id=100 + len(self.created), delete=AsyncMock())
            channel.name = name
            channel.kwargs = kwargs
            self.created.append(channel)
            return channel

        self.guild = MagicMock(create_voice_channel=AsyncMock(side_effect=create))
        self.client.get_guild.return_value = self.guild
        self.client.get_channel.side_effect = lambda channel_id: next(
            (channel for channel in self.created if channel.id == channel_id), None
        )

    def vote_game(self):
        self.save_game(
            phase="vote", team_a=[1], team_b=[2], maps=["A", "B"], votes={}, picks={}, vote_ends_at=1, channel_id=77
        )

    async def test_calls_are_created_when_the_map_is_chosen(self):
        self.vote_game()
        await customgame.handle_button(self.interaction(), "endvote", None)
        self.assertEqual([channel.name for channel in self.created], ["Team 1 Call", "Team 2 Call"])
        self.assertEqual(self.game()["call_ids"], [100, 101])

    async def test_each_call_only_lets_its_own_team_connect(self):
        self.vote_game()
        await customgame.handle_button(self.interaction(), "endvote", None)
        first = self.created[0].kwargs["overwrites"]
        self.assertIs(first[self.guild.default_role].connect, False)
        allowed = {target.id for target, overwrite in first.items() if overwrite.connect}
        self.assertEqual(allowed, {1})

    async def test_calls_open_when_the_timer_ends_or_everyone_votes(self):
        self.vote_game()
        self.client.guilds = [MagicMock(id=self.GUILD_ID)]
        await customgame.expire_votes(self.client, 1000)
        self.assertEqual(len(self.created), 2)

    async def test_result_deletes_the_calls(self):
        self.vote_game()
        await customgame.handle_button(self.interaction(), "endvote", None)
        await customgame.handle_button(self.interaction(), "won", "a")
        for channel in self.created:
            channel.delete.assert_awaited_once()
        self.assertNotIn("call_ids", self.game())

    async def test_cancel_deletes_the_calls(self):
        self.vote_game()
        await customgame.handle_button(self.interaction(), "endvote", None)
        await customgame.handle_button(self.interaction(), "cancel", None)
        for channel in self.created:
            channel.delete.assert_awaited_once()

    async def test_undo_opens_new_calls(self):
        self.vote_game()
        await customgame.handle_button(self.interaction(), "endvote", None)
        await customgame.handle_button(self.interaction(), "won", "a")
        await customgame.handle_button(self.interaction(), "undo", None)
        self.assertEqual(len(self.created), 4)
        self.assertEqual(self.game()["call_ids"], [102, 103])

    async def test_failure_keeps_the_game_going_and_cleans_up(self):
        self.vote_game()
        error = discord.Forbidden(MagicMock(status=403, reason="no"), "no")
        original = self.guild.create_voice_channel.side_effect

        async def fail_second(name, **kwargs):
            if self.created:
                raise error
            return await original(name, **kwargs)

        self.guild.create_voice_channel.side_effect = fail_second
        with self.assertLogs("mikasa.customgame", "ERROR"):
            await customgame.handle_button(self.interaction(), "endvote", None)
        self.assertEqual(self.game()["phase"], "playing")
        self.assertTrue(self.game()["calls_failed"])
        self.created[0].delete.assert_awaited_once()

    async def test_already_deleted_call_is_ignored(self):
        self.vote_game()
        await customgame.handle_button(self.interaction(), "endvote", None)
        self.created.clear()
        await customgame.handle_button(self.interaction(), "won", "a")
        self.assertEqual(self.game()["phase"], "finished")


class PointsTests(CustomGameCase):
    def points(self):
        return self.store.get(self.GUILD_ID).get("points", {})

    def player(self, user_id=1):
        return MagicMock(id=user_id, mention=f"<@{user_id}>")

    async def test_add_remove_and_set(self):
        await customgame.addpoints_command.callback(self.interaction(), self.player(), 50)
        await customgame.addpoints_command.callback(self.interaction(), self.player(), 25)
        self.assertEqual(self.points(), {"1": 75})
        await customgame.removepoints_command.callback(self.interaction(), self.player(), 5)
        self.assertEqual(self.points(), {"1": 70})
        await customgame.setpoints_command.callback(self.interaction(), self.player(), 300)
        self.assertEqual(self.points(), {"1": 300})

    async def test_points_never_go_below_zero(self):
        await customgame.addpoints_command.callback(self.interaction(), self.player(), 10)
        await customgame.removepoints_command.callback(self.interaction(), self.player(), 50)
        self.assertEqual(self.points(), {})

    async def test_replies_never_ping(self):
        interaction = self.interaction()
        await customgame.addpoints_command.callback(interaction, self.player(), 10)
        self.assertFalse(interaction.response.send_message.await_args.kwargs["allowed_mentions"].users)

    async def test_only_admins_change_points(self):
        for command in (customgame.addpoints_command, customgame.removepoints_command, customgame.setpoints_command):
            interaction = self.interaction(admin=False)
            await command.callback(interaction, self.player(), 10)
            self.assertIn("admins", self.denial(interaction))
            self.assertTrue(command.default_permissions.manage_guild)
        self.assertEqual(self.points(), {})

    async def test_leaderboards_show_the_right_data(self):
        self.store.update(self.GUILD_ID, points={"1": 10, "2": 1500}, leaderboard={"3": {"wins": 1, "losses": 0}})
        interaction = self.interaction()
        await customgame.leaderboard_command.callback(interaction, "game points")
        text = interaction.response.send_message.await_args.kwargs["embed"].description
        self.assertEqual(text.split("\n"), ["1. <@2>: 1,500 GP", "2. <@1>: 10 GP"])
        interaction = self.interaction()
        await customgame.leaderboard_command.callback(interaction)
        self.assertIn("<@3>", interaction.response.send_message.await_args.kwargs["embed"].description)

    async def test_reset_only_clears_the_chosen_board_after_confirming(self):
        self.store.update(self.GUILD_ID, points={"1": 10}, leaderboard={"3": {"wins": 1, "losses": 0}})
        interaction = self.interaction()
        await customgame.resetleaderboard_command.callback(interaction, "game points")
        self.assertEqual(self.points(), {"1": 10})
        view = interaction.response.send_message.await_args.kwargs["view"]
        click = self.interaction()
        await view.confirm.callback(click)
        self.assertEqual(self.points(), {})
        self.assertIn("3", self.store.get(self.GUILD_ID)["leaderboard"])

    def seed_stats(self):
        self.store.update(
            self.GUILD_ID,
            points={"1": 10, "2": 20},
            leaderboard={"1": {"wins": 3, "losses": 1}, "2": {"wins": 2, "losses": 2}},
        )

    def board(self):
        return self.store.get(self.GUILD_ID).get("leaderboard", {})

    async def test_clearstats_wins_only_touches_that_player_and_board(self):
        self.seed_stats()
        await customgame.clearstats_command.callback(self.interaction(), self.player(1), "wins")
        self.assertNotIn("1", self.board())
        self.assertIn("2", self.board())
        self.assertEqual(self.points(), {"1": 10, "2": 20})

    async def test_clearstats_game_points_only(self):
        self.seed_stats()
        await customgame.clearstats_command.callback(self.interaction(), self.player(1), "game points")
        self.assertEqual(self.points(), {"2": 20})
        self.assertIn("1", self.board())

    async def test_clearstats_both(self):
        self.seed_stats()
        interaction = self.interaction()
        await customgame.clearstats_command.callback(interaction, self.player(1), "both")
        self.assertEqual(self.points(), {"2": 20})
        self.assertNotIn("1", self.board())
        call = interaction.response.send_message.await_args
        self.assertIn("wins and losses and Game Points", call.args[0])
        self.assertFalse(call.kwargs["allowed_mentions"].users)

    async def test_clearstats_with_nothing_to_clear_says_so(self):
        self.seed_stats()
        interaction = self.interaction()
        await customgame.clearstats_command.callback(interaction, self.player(9), "both")
        self.assertIn("nothing to clear", interaction.response.send_message.await_args.args[0])
        self.assertEqual(self.points(), {"1": 10, "2": 20})

    async def test_only_admins_clear_stats(self):
        self.seed_stats()
        interaction = self.interaction(admin=False)
        await customgame.clearstats_command.callback(interaction, self.player(1), "both")
        self.assertIn("admins", self.denial(interaction))
        self.assertEqual(self.points(), {"1": 10, "2": 20})
        self.assertTrue(customgame.clearstats_command.default_permissions.manage_guild)

    async def test_only_admins_reset(self):
        interaction = self.interaction(admin=False)
        await customgame.resetleaderboard_command.callback(interaction, "wins")
        self.assertIn("admins", self.denial(interaction))


if __name__ == "__main__":
    unittest.main()
