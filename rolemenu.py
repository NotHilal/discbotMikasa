"""Reaction role menus: messages where people react with an emoji to get a role.

A channel can hold several named menus (age, ranks, notifs...). Admins build a
menu with /reactionrole, then post it with /displayroleselect. Menus are saved
in the store under "role_menus", keyed by "channel_id:name", so reactions keep
working after the bot restarts.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger("mikasa.rolemenu")

MAX_MENU_ROLES = 20  # Discord allows at most 20 different reactions on one message
DEFAULT_TEXT = "React with an emoji below to get its role.\nRemove your reaction any time to drop it."
DEFAULT_PICK_ONE_TEXT = "React with **one** emoji below to get its role.\nPicking a different one swaps it."

# Roles that would let whoever picks them moderate or take over the server. Only the
# server owner can put these on a role menu.
DANGEROUS_PERMISSIONS = discord.Permissions(
    administrator=True, manage_guild=True, manage_roles=True, manage_channels=True,
    manage_messages=True, manage_webhooks=True, ban_members=True, kick_members=True,
    moderate_members=True, mention_everyone=True,
)


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.permissions.manage_guild


def menu_name(raw: str) -> str:
    return " ".join(raw.lower().split())


def emoji_key(raw: str) -> str:
    return str(discord.PartialEmoji.from_str(raw.strip()))


def find_emoji(menu: dict, emoji: str) -> str | None:
    """The menu's own spelling of emoji, if it's on the menu. Reaction events don't
    always carry the U+FE0F variation selector a typed emoji has (❤️ vs ❤), so
    compare without it."""
    target = emoji.replace("️", "")
    return next((e for e in menu["roles"] if e.replace("️", "") == target), None)


def new_menu() -> dict:
    return {"message_id": None, "roles": {}, "labels": {}, "descriptions": {}}


def _menus(interaction: discord.Interaction) -> dict:
    return interaction.client.store.get(interaction.guild_id).get("role_menus", {})


def _save_menus(interaction: discord.Interaction, menus: dict) -> None:
    interaction.client.store.update(interaction.guild_id, role_menus=menus or None)


def build_embed(guild: discord.Guild, name: str, menu: dict) -> discord.Embed:
    lines = []
    for emoji, role_id in menu["roles"].items():
        if role_id is None:
            shown = f"**{menu['labels'].get(emoji, 'No role')}**"
        else:
            role = guild.get_role(role_id)
            shown = role.mention if role else "*deleted role*"
        line = f"{emoji}  ┃  {shown}"
        if menu["descriptions"].get(emoji):
            line += f"\n-# {menu['descriptions'][emoji]}"
        lines.append(line)

    text = menu.get("text") or (DEFAULT_PICK_ONE_TEXT if menu.get("pick_one") else DEFAULT_TEXT)
    embed = discord.Embed(
        title=menu.get("title") or f"✨ {name.title()}",
        description=f"{text}\n\n" + "\n\n".join(lines),
        color=menu.get("color") or discord.Color.blurple().value,
    )
    count = len(menu["roles"])
    pick = "pick one" if menu.get("pick_one") else "pick as many as you want"
    embed.set_footer(text=f"{guild.name} • {count} role{'s' if count != 1 else ''} • {pick}")
    return embed


def role_problem(role: discord.Role, interaction: discord.Interaction) -> str | None:
    guild, user = interaction.guild, interaction.user
    if role.is_default() or role.managed:
        return "❌ That role can't be handed out."
    if not guild.me.guild_permissions.manage_roles or role >= guild.me.top_role:
        return f"❌ I need **Manage Roles**, and my highest role has to be above {role.mention}."
    if user.id != guild.owner_id and role.permissions.value & DANGEROUS_PERMISSIONS.value:
        return f"❌ {role.mention} has moderator/admin permissions, so only the server owner can put it on a role menu."
    # Otherwise someone with Manage Server could hand everyone a role above their own.
    if user.id != guild.owner_id and not user.top_role > role:
        return f"❌ {role.mention} is above your highest role, so you can't put it on a menu."
    return None


async def _fetch_message(channel, menu: dict) -> discord.Message | None:
    if menu["message_id"] is None:
        return None
    try:
        return await channel.fetch_message(menu["message_id"])
    except discord.NotFound:
        return None


async def _deny(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.send_message(message, ephemeral=True)


async def _check(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction):
        await _deny(interaction, "❌ Only admins can manage role menus.")
        return False
    if not isinstance(interaction.channel, discord.TextChannel):
        await _deny(interaction, "❌ Run this in a server text channel.")
        return False
    return True


async def menu_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    prefix = f"{interaction.channel_id}:"
    current = menu_name(current)
    names = [key[len(prefix):] for key in _menus(interaction) if key.startswith(prefix)]
    return [app_commands.Choice(name=n, value=n) for n in names if current in n][:25]


@app_commands.command(name="reactionrole", description="(Admins only) Add a role to one of this channel's role menus")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.autocomplete(menu=menu_autocomplete)
@app_commands.describe(
    menu="Which menu in this channel, e.g. age, ranks, notifs (a new name starts a new menu)",
    emoji="The emoji people react with",
    role='The role they get (leave empty for a "no thanks" option that gives no role)',
    label="Text shown for an option with no role, e.g. No thanks",
    description="Optional short line shown under the role",
)
async def reactionrole_command(
    interaction: discord.Interaction,
    menu: app_commands.Range[str, 1, 32],
    emoji: str,
    role: discord.Role | None = None,
    label: app_commands.Range[str, 1, 80] | None = None,
    description: app_commands.Range[str, 1, 100] | None = None,
):
    if not await _check(interaction):
        return
    if role is None and label is None:
        await _deny(interaction, "❌ Pick a `role`, or give a `label` (like *No thanks*) for an option that gives no role.")
        return
    problem = role_problem(role, interaction) if role else None
    if problem:
        await _deny(interaction, problem)
        return

    name = menu_name(menu)
    key = f"{interaction.channel_id}:{name}"
    menus = _menus(interaction)
    menu = menus.setdefault(key, new_menu())
    emoji = find_emoji(menu, emoji_key(emoji)) or emoji_key(emoji)
    if emoji not in menu["roles"] and len(menu["roles"]) >= MAX_MENU_ROLES:
        await _deny(interaction, f"❌ A role menu can hold at most {MAX_MENU_ROLES} roles (Discord's reaction limit).")
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    menu["roles"][emoji] = role.id if role else None
    for field, value in (("labels", None if role else label), ("descriptions", description)):
        if value:
            menu[field][emoji] = value
        else:
            menu[field].pop(emoji, None)

    # Once the menu is posted, keep it in sync; before that, just build the list.
    message = await _fetch_message(interaction.channel, menu)
    if message is not None:
        try:
            await message.edit(embed=build_embed(interaction.guild, name, menu))
            await message.add_reaction(emoji)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Couldn't use that emoji ({exc.text or exc}). Custom emojis have to be from a server I'm in.",
                ephemeral=True,
            )
            return

    _save_menus(interaction, menus)
    gives = role.mention if role else f"**{label}** (no role)"
    if message is not None:
        await interaction.followup.send(f"✅ Added {emoji} → {gives} to the posted **{name}** menu.", ephemeral=True)
    else:
        await interaction.followup.send(
            f"✅ Added {emoji} → {gives} to **{name}**. Here's a preview. "
            f"Run `/displayroleselect menu:{name}` when you're done.",
            embed=build_embed(interaction.guild, name, menu),
            ephemeral=True,
        )


@app_commands.command(name="removereactionrole", description="(Admins only) Remove an emoji from one of this channel's role menus")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.autocomplete(menu=menu_autocomplete)
@app_commands.describe(menu="Which menu in this channel", emoji="The emoji to remove")
async def removereactionrole_command(interaction: discord.Interaction, menu: str, emoji: str):
    if not await _check(interaction):
        return
    name = menu_name(menu)
    key = f"{interaction.channel_id}:{name}"
    menus = _menus(interaction)
    menu = menus.get(key)
    emoji = find_emoji(menu, emoji_key(emoji)) if menu else None
    if emoji is None:
        await _deny(interaction, f"❌ That emoji isn't on a **{name}** menu here.")
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    for field in ("roles", "labels", "descriptions"):
        menu[field].pop(emoji, None)
    message = await _fetch_message(interaction.channel, menu)
    try:
        if not menu["roles"]:
            if message is not None:
                await message.delete()
            del menus[key]
            _save_menus(interaction, menus)
            await interaction.followup.send(f"🗑️ That was the last role, so the **{name}** menu is gone.", ephemeral=True)
            return
        if message is not None:
            await message.edit(embed=build_embed(interaction.guild, name, menu))
            await message.clear_reaction(emoji)
    except discord.HTTPException:
        pass  # the saved menu is what matters; a stale reaction is harmless
    _save_menus(interaction, menus)
    await interaction.followup.send(f"✅ Removed {emoji} from the **{name}** menu.", ephemeral=True)


@app_commands.command(name="displayroleselect", description="(Admins only) Post one of this channel's role menus")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.autocomplete(menu=menu_autocomplete)
@app_commands.describe(
    menu="Which menu to post",
    pick_one="Only let people have one role from this menu (good for age or rank)",
    title="Optional title (default: ✨ plus the menu's name)",
    text="Optional text shown above the roles",
    color="Optional embed color as a hex code, like #ff66cc",
)
async def displayroleselect_command(
    interaction: discord.Interaction,
    menu: str,
    pick_one: bool | None = None,
    title: app_commands.Range[str, 1, 256] | None = None,
    text: app_commands.Range[str, 1, 1000] | None = None,
    color: str | None = None,
):
    if not await _check(interaction):
        return
    name = menu_name(menu)
    menus = _menus(interaction)
    menu = menus.get(f"{interaction.channel_id}:{name}")
    if not menu:
        await _deny(interaction, f"❌ There's no **{name}** menu in this channel yet. Add roles with `/reactionrole menu:{name}` first.")
        return
    if color is not None:
        try:
            menu["color"] = discord.Color.from_str(color if color.startswith("#") else f"#{color}").value
        except ValueError:
            await _deny(interaction, "❌ That color should look like `#ff66cc`.")
            return
    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.add_reactions and perms.read_message_history):
        await _deny(
            interaction,
            "❌ I need **View Channel**, **Send Messages**, **Embed Links**, **Add Reactions** and "
            "**Read Message History** in this channel.",
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    for field, value in (("title", title), ("text", text), ("pick_one", pick_one)):
        if value is not None:
            menu[field] = value

    # Re-running this reposts the menu, so drop the old copy instead of leaving two.
    old_message = await _fetch_message(interaction.channel, menu)
    message = await interaction.channel.send(embed=build_embed(interaction.guild, name, menu))
    failed = []
    for emoji in menu["roles"]:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            failed.append(emoji)
    menu["message_id"] = message.id
    _save_menus(interaction, menus)
    if old_message is not None:
        try:
            await old_message.delete()
        except discord.HTTPException:
            pass

    note = ""
    if failed:
        note += (
            f"\n⚠️ I couldn't react with {' '.join(failed)}. Custom emojis have to be from a server I'm in, "
            "so remove those with `/removereactionrole`."
        )
    if menu.get("pick_one") and not perms.manage_messages:
        note += "\n⚠️ Without **Manage Messages** here I can still swap people's roles, but their old reaction will stay."
    await interaction.followup.send(f"✅ **{name}** menu posted: {message.jump_url}{note}", ephemeral=True)


@app_commands.command(name="deleterolemenu", description="(Admins only) Delete one of this channel's role menus")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.autocomplete(menu=menu_autocomplete)
@app_commands.describe(menu="Which menu to delete")
async def deleterolemenu_command(interaction: discord.Interaction, menu: str):
    if not await _check(interaction):
        return
    name = menu_name(menu)
    key = f"{interaction.channel_id}:{name}"
    menus = _menus(interaction)
    if key not in menus:
        await _deny(interaction, f"❌ There's no **{name}** menu in this channel.")
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    message = await _fetch_message(interaction.channel, menus.pop(key))
    if message is not None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    _save_menus(interaction, menus)
    # Members keep roles they already picked; this only removes the menu.
    await interaction.followup.send(
        f"🗑️ Deleted the **{name}** menu. Anyone who already picked a role from it keeps it.", ephemeral=True
    )


COMMANDS = (reactionrole_command, removereactionrole_command, displayroleselect_command, deleterolemenu_command)


async def handle_reaction(client: discord.Client, payload: discord.RawReactionActionEvent, adding: bool) -> None:
    if payload.guild_id is None or payload.user_id == client.user.id:
        return
    menus = client.store.get(payload.guild_id).get("role_menus", {})
    menu = next((m for m in menus.values() if m["message_id"] == payload.message_id), None)
    if menu is None:
        return
    emoji = find_emoji(menu, str(payload.emoji))
    guild = client.get_guild(payload.guild_id)
    if emoji is None or guild is None:
        return
    member = payload.member or guild.get_member(payload.user_id)
    if member is None or member.bot:
        return

    role_id = menu["roles"][emoji]
    if role_id is not None:
        role = guild.get_role(role_id)
        if role is None:
            return
        try:
            if adding:
                await member.add_roles(role, reason="Reaction role")
            else:
                await member.remove_roles(role, reason="Reaction role")
        except discord.HTTPException:
            log.exception("Reaction role %s failed for %s in %s", role.name, member, guild)
            return
    # A "no thanks" option gives nothing, but on a pick-one menu it still clears
    # whatever they picked before.
    if adding and menu.get("pick_one"):
        await _drop_other_picks(guild, payload, member, menu, emoji)


async def _drop_other_picks(
    guild: discord.Guild, payload: discord.RawReactionActionEvent, member: discord.Member, menu: dict, kept: str
) -> None:
    """On a pick-one menu, take away the member's other roles from it and their other reactions."""
    others = {e: r for e, r in menu["roles"].items() if e != kept}
    old_roles = [r for r in member.roles if r.id in others.values()]
    if old_roles:
        try:
            await member.remove_roles(*old_roles, reason="Reaction role (pick one)")
        except discord.HTTPException:
            log.exception("Pick-one role swap failed for %s in %s", member, guild)

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        return
    message = channel.get_partial_message(payload.message_id)
    for emoji, role_id in others.items():
        # Only bother for roles they actually had (and no-role options, which leave
        # no trace on the member), to avoid a request per emoji.
        if role_id is None or any(r.id == role_id for r in old_roles):
            try:
                await message.remove_reaction(emoji, member)
            except discord.HTTPException:
                pass  # needs Manage Messages; the role swap already happened either way
