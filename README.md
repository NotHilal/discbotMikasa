# Mikasa

A small Discord bot that:

- assigns an existing role to people when they join
- posts a welcome message
- announces when a Twitch stream goes live, with the Twitch and TikTok links
- runs VALORANT custom games with team picking, a map vote and a win leaderboard
- posts reaction role menus, where people react with an emoji to get a role

Only Twitch is checked for live status, about once a minute. The TikTok link is
just included in the message, so a TikTok-only stream won't trigger an announcement.

## Install

1. In the [Discord developer portal](https://discord.com/developers/applications), create an app and bot, and turn on **Server Members Intent** under Bot.
2. Register an app in the [Twitch developer console](https://dev.twitch.tv/console) to get a client ID and secret.
3. Copy `.env.example` to `.env` and fill it in.
4. Install and start the bot. On Linux, activate with `source venv/bin/activate`.

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

## Add it to a server

1. Invite the bot with the `bot` and `applications.commands` scopes and these permissions: View Channels, Send Messages, Manage Roles.
2. In **Server Settings > Roles**, drag the bot's role **above** the role it should assign.
3. Run the commands below. They need Manage Server, and everyone in the channel can see the replies, so run them in a staff channel to keep your settings private.

| Command | What it sets |
|---|---|
| `/setup joinrole <role>` | The role new members get |
| `/setup welcome <channel> [message]` | The welcome channel and message. Use `{member}` and `{server}` in the message. |
| `/setup livenotif <channel>` | Where live announcements go |
| `/setup pingrole <role>` | The role announcements ping |
| `/setup twitch <username>` | The Twitch channel to watch |
| `/setup tiktok <username>` | The TikTok account to link |
| `/setup testlive [ping]` | Posts a labelled test announcement. It only pings the ping role if you set `ping` to true. |
| `/setup maps <maps>` | The custom game maps, separated by commas |
| `/setup clear <setting>` | Turns off or removes one setting |
| `/setup show` | Shows this server's current settings |

Announcements need both `livenotif` and `twitch` to be set. The ping role only notifies people if the role is mentionable or the bot has Mention Everyone.

Settings, games in progress and leaderboards are saved in `data.json`. Back it up if you move the bot to another machine.

## Custom games

An admin (anyone with Manage Server) runs `/customgame`, and one message then moves through each step:

1. **Teams:** the admin picks up to 5 players per team from the dropdowns. Each team needs at least one player. Picking someone already on the other team moves them.
2. **Map vote:** players click **Vote**, pick up to 2 maps on a private ballot and click **Confirm vote**. Picks that aren't confirmed don't count, and a player can vote again to change their choice. The totals stay hidden until voting ends, which happens after 2 minutes, when everyone has confirmed, or when an admin clicks **End vote**. Ties are picked at random.
3. **Playing:** everyone starts the game in VALORANT, then an admin clicks the team that won.
4. **Result:** wins and losses are added to the leaderboard. An admin can click **Undo result** to fix a misclick.

Anyone can run `/leaderboard` to see the top 20, ranked by wins. Each server has its own leaderboard, and games keep working if the bot restarts.

### Queues

An admin runs `/queue`, and players react with ✅ to join. The queue closes at 10 players, and a new one is opened with `/queue` again. Admins then split the 10 players into two teams of 5 and click **Confirm teams**, which starts the map vote.

If a player removes their reaction or an admin kicks them with the dropdown before the teams are confirmed, the queue reopens for a replacement. Once the teams are confirmed, an admin has to cancel the game to change them.

### Team calls

When the map is announced, the bot creates **Team 1 Call** and **Team 2 Call** in the same category as the game, and only each team's players can join theirs. Both are deleted when an admin reports the winner or cancels the game. This needs the **Manage Channels** and **Connect** permissions, and the queue needs **Add Reactions**, **Read Message History** and **Manage Messages**.

### Game Points

`/leaderboard board:game points` ranks players by Game Points. Admins manage them with `/addpoints`, `/removepoints` and `/setpoints`, and `/resetleaderboard` clears either leaderboard after a confirmation.

## Role menus

Admins (anyone with Manage Server) build a menu in the channel it should be posted in, then post it. A channel can hold several menus, each with its own name, like `age`, `ranks` or `notifs`.

| Command | What it does |
|---|---|
| `/reactionrole <menu> <emoji> [role] [label] [description]` | Adds an option to a menu, and shows a private preview. Leave `role` empty and give a `label` for an option that gives no role, like "No thanks". |
| `/removereactionrole <menu> <emoji>` | Removes an option. Removing the last one deletes the menu. |
| `/displayroleselect <menu> [pick_one] [title] [text] [color]` | Posts the menu and adds its reactions. With `pick_one`, people can only hold one role from it, and picking another swaps it. Running it again reposts the menu and deletes the old message. |
| `/deleterolemenu <menu>` | Deletes a menu and its message. People keep the roles they already picked. |

Once a menu is posted, adding or removing options updates it in place. A menu holds up to 20 options, Discord's limit on reactions per message.

The bot needs Manage Roles, and its role must be above every role on a menu. In the menu's channel it needs View Channel, Send Messages, Embed Links, Add Reactions and Read Message History, plus Manage Messages for pick-one menus to remove old reactions. Roles with moderator or admin permissions, and roles above your own, can only be added by the server owner.

## Test

```
python -m unittest
```
