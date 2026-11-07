import functools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord

import rolemenu
import storage


@functools.total_ordering
class FakeRole:
    def __init__(self, role_id, position, permissions=None, managed=False, default=False):
        self.id = role_id
        self.position = position
        self.permissions = permissions or discord.Permissions.none()
        self.managed = managed
        self.mention = f"<@&{role_id}>"
        self.name = f"role{role_id}"
        self._default = default

    def is_default(self):
        return self._default

    def __eq__(self, other):
        return self.position == other.position

    def __lt__(self, other):
        return self.position < other.position

    __hash__ = object.__hash__


OWNER_ID = 1
ADMIN_ID = 2
CHANNEL_ID = 77
GUILD_ID = 5


class RoleMenuCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "data.json"
        self.store = storage.Store(self.path)
        self.client = MagicMock(store=self.store)
        self.client.user.id = 999

        self.bot_role = FakeRole(50, position=10)
        self.admin_role = FakeRole(40, position=8)
        self.gamer = FakeRole(11, position=3)
        self.artist = FakeRole(12, position=2)
        self.roles = {r.id: r for r in (self.bot_role, self.admin_role, self.gamer, self.artist)}

        self.guild = MagicMock(id=GUILD_ID, owner_id=OWNER_ID)
        self.guild.name = "Test Server"
        self.guild.me.top_role = self.bot_role
        self.guild.me.guild_permissions.manage_roles = True
        self.guild.get_role = lambda role_id: self.roles.get(role_id)
        self.client.get_guild = lambda guild_id: self.guild

        self.posted = []
        self.channel = MagicMock(spec=discord.TextChannel, id=CHANNEL_ID)
        self.channel.permissions_for.return_value = discord.Permissions.all()
        self.channel.send = AsyncMock(side_effect=self._send)
        self.channel.fetch_message = AsyncMock(side_effect=self._fetch)
        self.partial = MagicMock()
        self.partial.remove_reaction = AsyncMock()
        self.channel.get_partial_message.return_value = self.partial
        self.guild.get_channel = lambda channel_id: self.channel

    async def _send(self, embed=None):
        message = MagicMock(id=1000 + len(self.posted), embed=embed, jump_url="https://discord.com/x")
        message.add_reaction = AsyncMock()
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        message.clear_reaction = AsyncMock()
        self.posted.append(message)
        return message

    async def _fetch(self, message_id):
        for message in self.posted:
            if message.id == message_id and not message.delete.await_count:
                return message
        raise discord.NotFound(MagicMock(status=404), "Unknown Message")

    def interaction(self, user_id=ADMIN_ID, admin=True):
        interaction = MagicMock(guild_id=GUILD_ID, channel_id=CHANNEL_ID, client=self.client)
        interaction.guild = self.guild
        interaction.channel = self.channel
        interaction.user.id = user_id
        interaction.user.top_role = self.admin_role
        interaction.permissions.manage_guild = admin
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    def menus(self):
        return self.store.get(GUILD_ID).get("role_menus", {})

    def menu(self, name):
        return self.menus().get(f"{CHANNEL_ID}:{name}")

    async def add(self, menu, emoji, role=None, **kwargs):
        interaction = self.interaction(**kwargs.pop("as_", {}))
        await rolemenu.reactionrole_command.callback(interaction, menu, emoji, role, **kwargs)
        return interaction

    async def display(self, menu, **kwargs):
        interaction = self.interaction()
        await rolemenu.displayroleselect_command.callback(interaction, menu, **kwargs)
        return interaction

    def member(self, *roles):
        member = MagicMock(id=300, bot=False, roles=list(roles))
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        return member

    async def react(self, member, emoji, adding=True, message_id=None):
        payload = MagicMock(guild_id=GUILD_ID, channel_id=CHANNEL_ID, user_id=member.id, member=member)
        payload.message_id = message_id or self.posted[-1].id
        payload.emoji = emoji
        await rolemenu.handle_reaction(self.client, payload, adding)


class BuildTests(RoleMenuCase):
    async def test_adding_roles_builds_a_menu_without_posting(self):
        interaction = await self.add("Valo  Ranks", "🎮", self.gamer, description="Plays a lot")
        await self.add("valo ranks", "🎨", self.artist)
        menu = self.menu("valo ranks")
        self.assertEqual(menu["roles"], {"🎮": 11, "🎨": 12})
        self.assertEqual(menu["descriptions"], {"🎮": "Plays a lot"})
        self.assertEqual(self.posted, [])
        self.assertIsNotNone(interaction.followup.send.await_args.kwargs["embed"])

    async def test_menus_in_one_channel_are_separate(self):
        await self.add("age", "🎮", self.gamer)
        await self.add("ranks", "🎨", self.artist)
        self.assertEqual(set(self.menus()), {f"{CHANNEL_ID}:age", f"{CHANNEL_ID}:ranks"})

    async def test_no_role_option_needs_a_label(self):
        interaction = await self.add("mc", "❌")
        self.assertIsNone(self.menu("mc"))
        self.assertIn("label", interaction.response.send_message.await_args.args[0])
        await self.add("mc", "❌", label="No")
        self.assertEqual(self.menu("mc")["roles"], {"❌": None})
        self.assertEqual(self.menu("mc")["labels"], {"❌": "No"})

    async def test_refuses_roles_it_should_not_hand_out(self):
        mod = FakeRole(60, position=4, permissions=discord.Permissions(ban_members=True))
        too_high = FakeRole(61, position=9)
        for role in (mod, too_high, FakeRole(62, position=0, default=True), FakeRole(63, position=1, managed=True)):
            self.roles[role.id] = role
            await self.add("x", "🎮", role)
        self.assertIsNone(self.menu("x"))

    async def test_owner_can_add_moderator_roles(self):
        mod = FakeRole(60, position=4, permissions=discord.Permissions(ban_members=True))
        await self.add("x", "🎮", mod, as_={"user_id": OWNER_ID})
        self.assertEqual(self.menu("x")["roles"], {"🎮": 60})

    async def test_only_admins_can_use_the_commands(self):
        interaction = await self.add("x", "🎮", self.gamer, as_={"admin": False})
        self.assertIsNone(self.menu("x"))
        self.assertIn("Only admins", interaction.response.send_message.await_args.args[0])
        for command in rolemenu.COMMANDS:
            self.assertTrue(command.default_permissions.manage_guild)

    async def test_caps_at_twenty_roles(self):
        emojis = ["😀", "😁", "😂", "😃", "😄", "😅", "😆", "😇", "😈", "😉",
                  "😊", "😋", "😌", "😍", "😎", "😏", "😐", "😑", "😒", "😓", "😔"]
        for emoji in emojis:
            await self.add("big", emoji, self.gamer)
        self.assertEqual(len(self.menu("big")["roles"]), 20)


class PostTests(RoleMenuCase):
    async def test_display_posts_the_menu_with_every_reaction(self):
        await self.add("age", "🔞", self.gamer)
        await self.add("age", "❌", label="No")
        await self.display("age", pick_one=True, title="How old?", color="ff66cc")
        message = self.posted[-1]
        self.assertEqual([c.args[0] for c in message.add_reaction.await_args_list], ["🔞", "❌"])
        embed = self.channel.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "How old?")
        self.assertEqual(embed.color.value, 0xFF66CC)
        self.assertIn("**No**", embed.description)
        self.assertIn("pick one", embed.footer.text)
        self.assertEqual(self.menu("age")["message_id"], message.id)

    async def test_displaying_again_replaces_the_old_message(self):
        await self.add("age", "🔞", self.gamer)
        await self.display("age")
        first = self.posted[-1]
        await self.display("age", title="New")
        first.delete.assert_awaited_once()
        self.assertEqual(self.menu("age")["message_id"], self.posted[-1].id)

    async def test_display_needs_a_menu_and_channel_permissions(self):
        interaction = await self.display("nothing")
        self.assertIn("no **nothing** menu", interaction.response.send_message.await_args.args[0])
        await self.add("age", "🔞", self.gamer)
        self.channel.permissions_for.return_value = discord.Permissions(view_channel=True)
        interaction = await self.display("age")
        self.assertIn("Embed Links", interaction.response.send_message.await_args.args[0])
        self.assertEqual(self.posted, [])

    async def test_adding_to_a_posted_menu_updates_it(self):
        await self.add("age", "🔞", self.gamer)
        await self.display("age")
        await self.add("age", "🎨", self.artist)
        message = self.posted[-1]
        message.edit.assert_awaited()
        self.assertEqual(message.add_reaction.await_args.args[0], "🎨")

    async def test_removing_the_last_role_deletes_the_menu(self):
        await self.add("age", "🔞", self.gamer)
        await self.display("age")
        await rolemenu.removereactionrole_command.callback(self.interaction(), "age", "🔞")
        self.posted[-1].delete.assert_awaited_once()
        self.assertIsNone(self.menu("age"))

    async def test_delete_menu_removes_message_and_data(self):
        await self.add("age", "🔞", self.gamer)
        await self.add("ranks", "🎨", self.artist)
        await self.display("age")
        await rolemenu.deleterolemenu_command.callback(self.interaction(), "age")
        self.posted[-1].delete.assert_awaited_once()
        self.assertIsNone(self.menu("age"))
        self.assertIsNotNone(self.menu("ranks"))


class ReactionTests(RoleMenuCase):
    async def asyncSetUp(self):
        await self.add("ranks", "🎮", self.gamer)
        await self.add("ranks", "🎨", self.artist)
        await self.add("ranks", "❤️", label="No thanks")

    async def test_reacting_gives_and_unreacting_takes_the_role(self):
        await self.display("ranks")
        member = self.member()
        await self.react(member, "🎮")
        member.add_roles.assert_awaited_once_with(self.gamer, reason="Reaction role")
        await self.react(member, "🎮", adding=False)
        member.remove_roles.assert_awaited_once_with(self.gamer, reason="Reaction role")

    async def test_many_roles_can_be_held_when_not_pick_one(self):
        await self.display("ranks")
        member = self.member(self.artist)
        await self.react(member, "🎮")
        member.remove_roles.assert_not_awaited()
        self.partial.remove_reaction.assert_not_awaited()

    async def test_pick_one_swaps_the_role_and_reaction(self):
        await self.display("ranks", pick_one=True)
        member = self.member(self.artist)
        await self.react(member, "🎮")
        member.add_roles.assert_awaited_once_with(self.gamer, reason="Reaction role")
        member.remove_roles.assert_awaited_once_with(self.artist, reason="Reaction role (pick one)")
        removed = {c.args[0] for c in self.partial.remove_reaction.await_args_list}
        self.assertEqual(removed, {"🎨", "❤️"})

    async def test_no_role_option_clears_picks_on_a_pick_one_menu(self):
        await self.display("ranks", pick_one=True)
        member = self.member(self.gamer)
        await self.react(member, "❤")  # without the variation selector, as Discord may send it
        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_awaited_once_with(self.gamer, reason="Reaction role (pick one)")

    async def test_ignores_other_messages_emojis_and_bots(self):
        await self.display("ranks")
        member = self.member()
        await self.react(member, "🎮", message_id=123)
        await self.react(member, "🍕")
        bot_member = self.member()
        bot_member.bot = True
        await self.react(bot_member, "🎮")
        member.add_roles.assert_not_awaited()
        bot_member.add_roles.assert_not_awaited()

    async def test_menus_work_after_reloading_from_disk(self):
        await self.display("ranks")
        self.client.store = storage.Store(self.path)
        member = self.member()
        await self.react(member, "🎨")
        member.add_roles.assert_awaited_once_with(self.artist, reason="Reaction role")


if __name__ == "__main__":
    unittest.main()
