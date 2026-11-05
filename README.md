# Mikasa

A small Discord bot that:

- assigns an existing role to people when they join
- posts a welcome message
- announces when a Twitch stream goes live, with the Twitch and TikTok links

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
3. Run the commands below. They need Manage Server, and only the person running them sees the replies.

| Command | What it sets |
|---|---|
| `/setup joinrole <role>` | The role new members get |
| `/setup welcome <channel> [message]` | The welcome channel and message. Use `{member}` and `{server}` in the message. |
| `/setup livenotif <channel>` | Where live announcements go |
| `/setup pingrole <role>` | The role announcements ping |
| `/setup twitch <username>` | The Twitch channel to watch |
| `/setup tiktok <username>` | The TikTok account to link |
| `/setup clear <setting>` | Turns off or removes one setting |
| `/setup show` | Shows this server's current settings |

Announcements need both `livenotif` and `twitch` to be set. The ping role only notifies people if the role is mentionable or the bot has Mention Everyone.

Settings are saved in `data.json`. Back it up if you move the bot to another machine.

## Test

```
python -m unittest
```
