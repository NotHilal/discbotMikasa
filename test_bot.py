import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord

import bot
import storage
import twitch


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class TempStoreCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "data.json"
        self.store = storage.Store(self.path)


class StoreTests(TempStoreCase):
    def test_persists_and_keeps_servers_separate(self):
        self.store.update(1, twitch_username="one")
        self.store.update(2, twitch_username="two")
        reloaded = storage.Store(self.path)
        self.assertEqual(reloaded.get(1)["twitch_username"], "one")
        self.assertEqual(reloaded.get(2)["twitch_username"], "two")

    def test_none_removes_a_setting(self):
        self.store.update(1, twitch_username="one")
        self.store.update(1, twitch_username=None)
        self.assertEqual(self.store.get(1), {})

    def test_corrupt_file_stops_startup_and_is_left_alone(self):
        self.path.write_text('{"1": {"twitch', encoding="utf-8")
        with self.assertRaises(SystemExit):
            storage.Store(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"1": {"twitch')


class MessageTests(unittest.TestCase):
    def guild(self, role=None):
        return MagicMock(get_role=MagicMock(return_value=role))

    def test_announcement_has_ping_and_both_links(self):
        text = bot.build_announcement(
            {"twitch_username": "me", "tiktok_username": "tok", "ping_role_id": 5},
            self.guild(MagicMock(mention="<@&5>")),
        )
        self.assertTrue(text.startswith("<@&5>\n"))
        self.assertIn("https://twitch.tv/me", text)
        self.assertIn("https://www.tiktok.com/@tok/live", text)

    def test_announcement_without_tiktok_or_ping(self):
        text = bot.build_announcement({"twitch_username": "me"}, self.guild())
        self.assertTrue(text.startswith("🔴"))
        self.assertNotIn("TikTok", text)

    def test_deleted_ping_role_is_skipped(self):
        text = bot.build_announcement({"twitch_username": "me", "ping_role_id": 5}, self.guild(None))
        self.assertTrue(text.startswith("🔴"))

    def test_welcome_tolerates_stray_braces(self):
        member = MagicMock(mention="<@9>")
        member.guild.name = "Server"
        text = bot.build_welcome({"welcome_message": "{member} joined {server} {oops"}, member)
        self.assertEqual(text, "<@9> joined Server {oops")

    def test_usernames_are_normalized_and_validated(self):
        self.assertEqual(bot.normalize_username(" @MyName ", bot.TWITCH_USERNAME), "myname")
        self.assertEqual(bot.normalize_username("my.name", bot.TIKTOK_USERNAME), "my.name")
        self.assertIsNone(bot.normalize_username("my.name", bot.TWITCH_USERNAME))
        self.assertIsNone(bot.normalize_username("https://twitch.tv/me", bot.TWITCH_USERNAME))


class ShouldAnnounceTests(unittest.TestCase):
    def test_first_stream(self):
        self.assertTrue(twitch.should_announce("a", {}, 1_000_000))

    def test_same_stream_once(self):
        self.assertFalse(twitch.should_announce("a", {"stream_id": "a", "last_seen": 0}, 1_000_000))

    def test_quick_reconnect_is_not_a_new_stream(self):
        self.assertFalse(twitch.should_announce("b", {"stream_id": "a", "last_seen": 1000}, 1060))

    def test_later_stream_is_announced(self):
        settings = {"stream_id": "a", "last_seen": 1000}
        self.assertTrue(twitch.should_announce("b", settings, 1000 + twitch.RECONNECT_GRACE_SECONDS))


class TwitchClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *stream_responses):
        session = MagicMock()
        session.post = MagicMock(side_effect=lambda *a, **k: FakeResponse(body={"access_token": "t"}))
        session.get = MagicMock(side_effect=list(stream_responses))
        return twitch.TwitchClient(session, "id", "secret"), session

    async def test_maps_live_users_to_stream_ids(self):
        client, session = self.client(FakeResponse(body={"data": [{"user_login": "Me", "id": "42"}]}))
        self.assertEqual(await client.get_live_streams({"me", "other"}), {"me": "42"})
        params = session.get.call_args.kwargs["params"]
        self.assertIn(("user_login", "me"), params)
        self.assertIn(("user_login", "other"), params)

    async def test_large_lists_are_split_into_batches(self):
        client, session = self.client(FakeResponse(body={"data": []}), FakeResponse(body={"data": []}))
        await client.get_live_streams({f"user{i}" for i in range(101)})
        self.assertEqual(session.get.call_count, 2)

    async def test_expired_token_is_refreshed(self):
        client, session = self.client(
            FakeResponse(status=401), FakeResponse(body={"data": [{"user_login": "me", "id": "42"}]})
        )
        self.assertEqual(await client.get_live_streams({"me"}), {"me": "42"})
        self.assertEqual(session.post.call_count, 2)

    async def test_repeated_401_raises(self):
        client, _ = self.client(FakeResponse(status=401), FakeResponse(status=401))
        with self.assertRaises(RuntimeError):
            await client.get_live_streams({"me"})


class BotCase(TempStoreCase):
    def setUp(self):
        super().setUp()
        self.bot = bot.MikasaBot(self.store, "id", "secret")
        self.channel = MagicMock(send=AsyncMock())
        self.bot._get_channel = AsyncMock(return_value=self.channel)


class JoinTests(BotCase):
    def member(self, is_bot=False):
        member = MagicMock(bot=is_bot, mention="<@9>", add_roles=AsyncMock())
        member.guild.id = 1
        member.guild.name = "Server"
        return member

    async def test_assigns_role_and_welcomes(self):
        self.store.update(1, join_role_id=2, welcome_channel_id=3)
        member = self.member()
        await self.bot.on_member_join(member)
        member.guild.get_role.assert_called_once_with(2)
        member.add_roles.assert_awaited_once()
        self.assertIn("<@9>", self.channel.send.await_args.args[0])

    async def test_role_failure_still_welcomes(self):
        self.store.update(1, join_role_id=2, welcome_channel_id=3)
        member = self.member()
        member.add_roles.side_effect = discord.Forbidden(MagicMock(status=403, reason="no"), "no")
        with self.assertLogs("mikasa", "ERROR"):
            await self.bot.on_member_join(member)
        self.channel.send.assert_awaited_once()

    async def test_unconfigured_server_does_nothing(self):
        member = self.member()
        await self.bot.on_member_join(member)
        member.add_roles.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_ignores_bots(self):
        self.store.update(1, join_role_id=2, welcome_channel_id=3)
        member = self.member(is_bot=True)
        await self.bot.on_member_join(member)
        member.add_roles.assert_not_awaited()
        self.channel.send.assert_not_awaited()


class PollTests(BotCase):
    def setUp(self):
        super().setUp()
        self.guilds = [MagicMock(id=1), MagicMock(id=2)]
        type(self.bot).guilds = property(lambda _: self.guilds)
        self.addCleanup(lambda: delattr(type(self.bot), "guilds"))
        self.bot.twitch = MagicMock(get_live_streams=AsyncMock(return_value={"me": "42"}))
        self.store.update(1, twitch_username="me", announce_channel_id=3)

    async def test_announces_each_stream_once(self):
        await self.bot.poll_twitch()
        await self.bot.poll_twitch()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.get(1)["stream_id"], "42")

    async def test_servers_are_announced_independently(self):
        self.store.update(2, twitch_username="me", announce_channel_id=4)
        await self.bot.poll_twitch()
        self.assertEqual(self.channel.send.await_count, 2)
        self.bot.twitch.get_live_streams.assert_awaited_once_with({"me"})

    async def test_one_failing_server_does_not_block_another(self):
        self.store.update(2, twitch_username="me", announce_channel_id=4)
        self.channel.send.side_effect = [RuntimeError("no access"), MagicMock()]
        with self.assertLogs("mikasa", "ERROR"):
            await self.bot.poll_twitch()
        self.assertNotIn("stream_id", self.store.get(1))
        self.assertEqual(self.store.get(2)["stream_id"], "42")

    async def test_failed_post_retries_next_check(self):
        self.channel.send.side_effect = [RuntimeError("discord down"), MagicMock()]
        with self.assertLogs("mikasa", "ERROR"):
            await self.bot.poll_twitch()
        await self.bot.poll_twitch()
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual(self.store.get(1)["stream_id"], "42")

    async def test_offline_does_nothing(self):
        self.bot.twitch.get_live_streams.return_value = {}
        await self.bot.poll_twitch()
        self.channel.send.assert_not_awaited()

    async def test_incomplete_setup_skips_twitch(self):
        self.store.update(1, announce_channel_id=None)
        await self.bot.poll_twitch()
        self.bot.twitch.get_live_streams.assert_not_awaited()


class CommandTests(BotCase):
    def interaction(self):
        interaction = MagicMock(guild_id=1, client=self.bot)
        interaction.response.send_message = AsyncMock()
        interaction.user.id = 10
        interaction.guild.owner_id = 99
        interaction.user.top_role.__gt__.return_value = True
        interaction.guild.me.guild_permissions.manage_roles = True
        return interaction

    def role(self, assignable=True, administrator=False):
        role = MagicMock(id=7, mention="<@&7>")
        role.is_assignable.return_value = assignable
        role.permissions.administrator = administrator
        return role

    def replied(self, interaction):
        return interaction.response.send_message.await_args.args[0]

    async def test_setup_is_restricted_to_server_managers(self):
        self.assertTrue(bot.setup_group.guild_only)
        self.assertTrue(bot.setup_group.default_permissions.manage_guild)

    async def test_joinrole_saves_a_valid_role(self):
        interaction = self.interaction()
        await bot.setup_joinrole.callback(interaction, self.role())
        self.assertEqual(self.store.get(1)["join_role_id"], 7)
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_joinrole_rejects_unusable_roles(self):
        for role, expected in (
            (self.role(assignable=False), "Drag my role"),
            (self.role(administrator=True), "Administrator"),
        ):
            interaction = self.interaction()
            await bot.setup_joinrole.callback(interaction, role)
            self.assertIn(expected, self.replied(interaction))
        self.assertEqual(self.store.get(1), {})

    async def test_joinrole_rejects_role_above_the_user(self):
        interaction = self.interaction()
        interaction.user.top_role.__gt__.return_value = False
        await bot.setup_joinrole.callback(interaction, self.role())
        self.assertIn("above your highest role", self.replied(interaction))
        self.assertEqual(self.store.get(1), {})

    async def test_joinrole_lets_the_owner_set_any_lower_role(self):
        interaction = self.interaction()
        interaction.user.id = 99
        interaction.user.top_role.__gt__.return_value = False
        await bot.setup_joinrole.callback(interaction, self.role())
        self.assertEqual(self.store.get(1)["join_role_id"], 7)

    async def test_welcome_keeps_existing_message_when_omitted(self):
        self.store.update(1, welcome_message="hi")
        channel = MagicMock(id=3)
        await bot.setup_welcome.callback(self.interaction(), channel)
        self.assertEqual(self.store.get(1), {"welcome_message": "hi", "welcome_channel_id": 3})

    async def test_channel_without_access_is_rejected(self):
        channel = MagicMock(id=3, mention="<#3>")
        channel.permissions_for.return_value.send_messages = False
        interaction = self.interaction()
        await bot.setup_livenotif.callback(interaction, channel)
        self.assertIn("can't post", self.replied(interaction))
        self.assertEqual(self.store.get(1), {})

    async def test_pingrole_warns_when_it_would_not_notify(self):
        role = self.role()
        role.mentionable = False
        interaction = self.interaction()
        interaction.guild.me.guild_permissions.mention_everyone = False
        await bot.setup_pingrole.callback(interaction, role)
        self.assertEqual(self.store.get(1)["ping_role_id"], 7)
        self.assertIn("won't notify", self.replied(interaction))

    async def test_changing_twitch_forgets_the_last_stream(self):
        self.store.update(1, twitch_username="old", stream_id="42", last_seen=1000)
        await bot.setup_twitch.callback(self.interaction(), "@New")
        self.assertEqual(self.store.get(1), {"twitch_username": "new"})

    async def test_same_twitch_name_keeps_the_last_stream(self):
        self.store.update(1, twitch_username="me", stream_id="42", last_seen=1000)
        await bot.setup_twitch.callback(self.interaction(), "ME")
        self.assertEqual(self.store.get(1)["stream_id"], "42")

    async def test_invalid_usernames_are_rejected(self):
        await bot.setup_twitch.callback(self.interaction(), "https://twitch.tv/me")
        await bot.setup_tiktok.callback(self.interaction(), "not valid!")
        self.assertEqual(self.store.get(1), {})

    async def test_clear_removes_only_that_setting(self):
        self.store.update(1, twitch_username="me", stream_id="42", last_seen=1000, tiktok_username="tok")
        await bot.setup_clear.callback(self.interaction(), "twitch")
        self.assertEqual(self.store.get(1), {"tiktok_username": "tok"})

    async def test_show_lists_settings_and_defaults(self):
        self.store.update(1, twitch_username="me", ping_role_id=7)
        interaction = self.interaction()
        await bot.setup_show.callback(interaction)
        text = self.replied(interaction)
        self.assertIn("**Twitch:** me", text)
        self.assertIn("**Ping role:** <@&7>", text)
        self.assertIn("**TikTok:** not set", text)


if __name__ == "__main__":
    unittest.main()
