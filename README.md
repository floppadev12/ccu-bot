# Roblox CCU Discord Bot

Tracks Roblox concurrent users and shows the current top 3 games in Discord voice channels reset with `/stat`. The bot status shows total CCU, and a daily report is sent at 06:00 Europe/Bratislava.

## Railway environment variables

Required:

- `DISCORD_TOKEN` - Discord bot token

Optional:

- `TIMEZONE` - defaults to `Europe/Bratislava`
- `POLL_SECONDS` - defaults to `120`
- `REPORT_HOUR` - defaults to `6`
- `REPORT_MINUTE` - defaults to `0`
- `DATABASE_PATH` - defaults to `ccu_bot.sqlite3`

## Discord permissions

Invite the bot with these permissions:

- Manage Channels
- View Channels
- Send Messages
- Use Slash Commands

## Commands

- `/track_add game:<roblox url, place id, or universe id>`
- `/track_remove game:<name, url, place id, or universe id>`
- `/track_list`
- `/rename game:<tracked game> name:<short display name>`
- `/stat` - delete the old stat channels and create 3 fresh top-CCU channels
- `/ccu`
- `/track_refresh`
- `/config_set_report_channel channel:<text channel>`
- `/report_now`

All server members can use the commands.

## Daily report

At 06:00 local time, the bot reports the previous completed day. The change percentage compares that day's peak CCU against the average daily peak from the previous 7 completed days.
