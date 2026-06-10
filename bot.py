import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "ccu_bot.sqlite3")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Bratislava")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "6"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))

ROBLOX_GAMES_URL = "https://games.roblox.com/v1/games"
ROBLOX_PLACE_UNIVERSE_URL = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"

DEFAULT_GAME_LINKS = [
    "https://www.roblox.com/games/122084827117805/Would-You-Rather-Lisa-or-Lena-Outfit-Tower",
    "https://www.roblox.com/games/135307462721056/Would-You-Rather-Baby-Outfit-Tower",
    "https://www.roblox.com/games/120968518804229/Would-You-Rather-Lucky-Block-Outfit-Tower",
]


tz = ZoneInfo(TIMEZONE_NAME)


@dataclass
class Game:
    universe_id: int
    place_id: Optional[int]
    name: str
    channel_id: Optional[int]
    current_ccu: int


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS games (
                universe_id INTEGER PRIMARY KEY,
                place_id INTEGER,
                name TEXT NOT NULL,
                channel_id INTEGER,
                current_ccu INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                universe_id INTEGER NOT NULL,
                sampled_at TEXT NOT NULL,
                ccu INTEGER NOT NULL,
                FOREIGN KEY (universe_id) REFERENCES games(universe_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_samples_game_time
                ON samples(universe_id, sampled_at);
            """
        )
        self.conn.commit()

    def get_config(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_config(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def upsert_game(self, universe_id: int, place_id: Optional[int], name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO games (universe_id, place_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(universe_id) DO UPDATE SET
                place_id = COALESCE(excluded.place_id, games.place_id),
                name = excluded.name,
                updated_at = excluded.updated_at
            """,
            (universe_id, place_id, name, now, now),
        )
        self.conn.commit()

    def update_game_channel(self, universe_id: int, channel_id: int) -> None:
        self.conn.execute(
            "UPDATE games SET channel_id = ?, updated_at = ? WHERE universe_id = ?",
            (channel_id, datetime.now(timezone.utc).isoformat(), universe_id),
        )
        self.conn.commit()

    def update_ccu(self, universe_id: int, ccu: int) -> None:
        sampled_at = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE games SET current_ccu = ?, updated_at = ? WHERE universe_id = ?",
            (ccu, sampled_at, universe_id),
        )
        self.conn.execute(
            "INSERT INTO samples (universe_id, sampled_at, ccu) VALUES (?, ?, ?)",
            (universe_id, sampled_at, ccu),
        )
        self.conn.commit()

    def remove_game(self, universe_id: int) -> None:
        self.conn.execute("DELETE FROM games WHERE universe_id = ?", (universe_id,))
        self.conn.commit()

    def games(self) -> list[Game]:
        rows = self.conn.execute(
            """
            SELECT universe_id, place_id, name, channel_id, current_ccu
            FROM games
            ORDER BY name COLLATE NOCASE
            """
        ).fetchall()
        return [
            Game(
                universe_id=row["universe_id"],
                place_id=row["place_id"],
                name=row["name"],
                channel_id=row["channel_id"],
                current_ccu=row["current_ccu"],
            )
            for row in rows
        ]

    def find_game(self, query: str) -> Optional[Game]:
        digits = extract_first_number(query)
        games = self.games()
        if digits:
            for game in games:
                if game.universe_id == digits or game.place_id == digits:
                    return game

        lowered = query.lower()
        for game in games:
            if lowered in game.name.lower():
                return game
        return None

    def peak_for_day(self, universe_id: int, local_day: date) -> Optional[int]:
        start = datetime.combine(local_day, time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
        end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
        row = self.conn.execute(
            """
            SELECT MAX(ccu) AS peak
            FROM samples
            WHERE universe_id = ? AND sampled_at >= ? AND sampled_at < ?
            """,
            (universe_id, start, end),
        ).fetchone()
        return row["peak"] if row and row["peak"] is not None else None

    def average_peak(self, universe_id: int, start_day: date, days: int) -> Optional[float]:
        peaks = []
        for offset in range(days):
            peak = self.peak_for_day(universe_id, start_day + timedelta(days=offset))
            if peak is not None:
                peaks.append(peak)
        return sum(peaks) / len(peaks) if peaks else None


def extract_first_number(value: str) -> Optional[int]:
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def short_channel_name(name: str, ccu: int) -> str:
    clean = re.sub(r"\s+", " ", name).strip()
    max_name_len = max(8, 90 - len(f": {ccu:,} CCU"))
    if len(clean) > max_name_len:
        clean = clean[: max_name_len - 1].rstrip() + "..."
    return f"{clean}: {ccu:,} CCU"


def percent_change(value: int, baseline: Optional[float]) -> str:
    if baseline is None:
        return "new data"
    if baseline == 0:
        return "+100.0%" if value > 0 else "0.0%"
    change = ((value - baseline) / baseline) * 100
    return f"{change:+.1f}%"


class CCUBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(DATABASE_PATH)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.last_report_day: Optional[str] = self.db.get_config("last_report_day")

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        await self.tree.sync()
        self.poll_ccu.start()
        self.daily_report.start()

    async def close(self) -> None:
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        await self.seed_default_games()
        print(f"Logged in as {self.user} ({self.user.id if self.user else 'unknown'})")

    async def seed_default_games(self) -> None:
        if self.db.games():
            return
        for link in DEFAULT_GAME_LINKS:
            try:
                info = await self.resolve_game(link)
                self.db.upsert_game(info["universe_id"], info.get("place_id"), info["name"])
            except Exception as exc:
                print(f"Failed to add default game {link}: {exc}")

    async def resolve_game(self, value: str) -> dict:
        number = extract_first_number(value)
        if number is None:
            raise ValueError("Could not find a Roblox place ID or universe ID.")

        is_url = "roblox.com/games/" in value.lower()
        if is_url:
            universe_id = await self.place_to_universe(number)
            place_id = number
        else:
            universe_id = number
            place_id = None

        game = await self.fetch_game(universe_id)
        if not game and not is_url:
            universe_id = await self.place_to_universe(number)
            place_id = number
            game = await self.fetch_game(universe_id)

        if not game:
            raise ValueError("Roblox did not return a game for that ID.")

        return {
            "universe_id": int(game["id"]),
            "place_id": place_id or int(game.get("rootPlaceId", 0)) or None,
            "name": game["name"],
            "playing": int(game.get("playing", 0)),
        }

    async def place_to_universe(self, place_id: int) -> int:
        assert self.http_session is not None
        async with self.http_session.get(ROBLOX_PLACE_UNIVERSE_URL.format(place_id=place_id)) as response:
            if response.status != 200:
                raise ValueError(f"Could not convert place ID {place_id} to a universe ID.")
            data = await response.json()
        universe_id = data.get("universeId")
        if not universe_id:
            raise ValueError(f"No universe ID found for place ID {place_id}.")
        return int(universe_id)

    async def fetch_game(self, universe_id: int) -> Optional[dict]:
        assert self.http_session is not None
        async with self.http_session.get(ROBLOX_GAMES_URL, params={"universeIds": str(universe_id)}) as response:
            if response.status != 200:
                return None
            data = await response.json()
        games = data.get("data") or []
        return games[0] if games else None

    async def fetch_games(self, universe_ids: list[int]) -> dict[int, dict]:
        assert self.http_session is not None
        if not universe_ids:
            return {}

        results: dict[int, dict] = {}
        for start in range(0, len(universe_ids), 50):
            chunk = universe_ids[start : start + 50]
            async with self.http_session.get(
                ROBLOX_GAMES_URL,
                params={"universeIds": ",".join(str(item) for item in chunk)},
            ) as response:
                if response.status != 200:
                    print(f"Roblox API returned HTTP {response.status}")
                    continue
                data = await response.json()
            for item in data.get("data", []):
                results[int(item["id"])] = item
        return results

    async def ensure_voice_channel(self, guild: discord.Guild, game: Game) -> Optional[discord.VoiceChannel]:
        if game.channel_id:
            channel = guild.get_channel(game.channel_id)
            if isinstance(channel, discord.VoiceChannel):
                return channel

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True),
        }
        try:
            channel = await guild.create_voice_channel(
                name=short_channel_name(game.name, game.current_ccu),
                overwrites=overwrites,
                reason="Creating Roblox CCU tracker channel",
            )
        except discord.Forbidden:
            print(f"Missing permission to create voice channel in {guild.name}")
            return None

        self.db.update_game_channel(game.universe_id, channel.id)
        return channel

    async def update_voice_channels(self) -> None:
        games = self.db.games()
        if not games:
            return

        for guild in self.guilds:
            for game in games:
                channel = await self.ensure_voice_channel(guild, game)
                if not channel:
                    continue
                new_name = short_channel_name(game.name, game.current_ccu)
                if channel.name == new_name:
                    continue
                try:
                    await channel.edit(name=new_name, reason="Updating Roblox CCU")
                except discord.Forbidden:
                    print(f"Missing permission to rename {channel.name} in {guild.name}")
                except discord.HTTPException as exc:
                    print(f"Could not rename {channel.name}: {exc}")

    async def update_presence(self) -> None:
        total = sum(game.current_ccu for game in self.db.games())
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"Total CCU: {total:,}"))

    def build_report(self, report_day: date) -> str:
        previous_window_start = report_day - timedelta(days=7)
        lines = [
            f"**Daily CCU Report - {report_day.isoformat()}**",
            "",
            "Compared with the average daily peak from the previous 7 completed days.",
            "",
        ]

        total_peak = 0
        total_baseline = 0.0
        total_baseline_games = 0

        for game in self.db.games():
            peak = self.db.peak_for_day(game.universe_id, report_day)
            if peak is None:
                peak = 0
            baseline = self.db.average_peak(game.universe_id, previous_window_start, 7)
            change = percent_change(peak, baseline)
            baseline_text = f"{baseline:.1f}" if baseline is not None else "0.0"
            lines.append(f"**{game.name}**")
            lines.append(f"Peak CCU: `{peak:,}` | 7-day avg peak: `{baseline_text}` | Change: `{change}`")
            lines.append("")

            total_peak += peak
            if baseline is not None:
                total_baseline += baseline
                total_baseline_games += 1

        total_baseline_value = total_baseline if total_baseline_games else None
        total_baseline_text = f"{total_baseline_value:.1f}" if total_baseline_value is not None else "0.0"
        lines.append("**Total**")
        lines.append(
            f"Peak CCU: `{total_peak:,}` | 7-day avg peak: `{total_baseline_text}` | "
            f"Change: `{percent_change(total_peak, total_baseline_value)}`"
        )
        return "\n".join(lines)

    async def send_daily_report(self, report_day: date) -> bool:
        channel_id = self.db.get_config("report_channel_id")
        if not channel_id:
            return False

        channel = self.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return False

        await channel.send(self.build_report(report_day))
        return True

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll_ccu(self) -> None:
        games = self.db.games()
        if not games:
            return

        roblox_games = await self.fetch_games([game.universe_id for game in games])
        for game in games:
            roblox_game = roblox_games.get(game.universe_id)
            if not roblox_game:
                continue
            self.db.upsert_game(game.universe_id, game.place_id, roblox_game["name"])
            self.db.update_ccu(game.universe_id, int(roblox_game.get("playing", 0)))

        await self.update_voice_channels()
        await self.update_presence()

    @poll_ccu.before_loop
    async def before_poll_ccu(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def daily_report(self) -> None:
        now = datetime.now(tz)
        scheduled = time(REPORT_HOUR, REPORT_MINUTE, tzinfo=tz)
        if now.timetz() < scheduled:
            return

        report_day = now.date() - timedelta(days=1)
        report_day_key = report_day.isoformat()
        if self.last_report_day == report_day_key:
            return

        if await self.send_daily_report(report_day):
            self.db.set_config("last_report_day", report_day_key)
            self.last_report_day = report_day_key

    @daily_report.before_loop
    async def before_daily_report(self) -> None:
        await self.wait_until_ready()


bot = CCUBot()


@bot.tree.command(name="track_add", description="Add a Roblox game URL, place ID, or universe ID to CCU tracking.")
@app_commands.describe(game="Roblox game URL, place ID, or universe ID")
async def track_add(interaction: discord.Interaction, game: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        info = await bot.resolve_game(game)
    except Exception as exc:
        await interaction.followup.send(f"Could not add that game: {exc}", ephemeral=True)
        return

    bot.db.upsert_game(info["universe_id"], info.get("place_id"), info["name"])
    bot.db.update_ccu(info["universe_id"], info["playing"])
    await bot.update_voice_channels()
    await bot.update_presence()
    await interaction.followup.send(f"Tracking **{info['name']}** with `{info['playing']:,}` current CCU.", ephemeral=True)


@bot.tree.command(name="track_remove", description="Remove a Roblox game from CCU tracking.")
@app_commands.describe(game="Game name, place ID, universe ID, or Roblox URL")
async def track_remove(interaction: discord.Interaction, game: str) -> None:
    tracked = bot.db.find_game(game)
    if not tracked:
        await interaction.response.send_message("I could not find that tracked game.", ephemeral=True)
        return

    channel_id = tracked.channel_id
    bot.db.remove_game(tracked.universe_id)
    if channel_id:
        channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
        if isinstance(channel, discord.VoiceChannel):
            try:
                await channel.delete(reason="Removing Roblox CCU tracker channel")
            except discord.HTTPException:
                pass

    await bot.update_presence()
    await interaction.response.send_message(f"Removed **{tracked.name}** from tracking.", ephemeral=True)


@bot.tree.command(name="track_list", description="List all tracked Roblox games.")
async def track_list(interaction: discord.Interaction) -> None:
    games = bot.db.games()
    if not games:
        await interaction.response.send_message("No games are being tracked yet.", ephemeral=True)
        return

    lines = [
        f"**{game.name}** - `{game.current_ccu:,}` CCU - universe `{game.universe_id}`"
        for game in games
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="track_refresh", description="Refresh CCU counts and voice channel names now.")
async def track_refresh(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await bot.poll_ccu()
    await interaction.followup.send("Refreshed CCU counts and voice channels.", ephemeral=True)


@bot.tree.command(name="config_set_report_channel", description="Set the text channel for daily CCU reports.")
@app_commands.describe(channel="Text channel where daily CCU reports should be sent")
async def config_set_report_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    bot.db.set_config("report_channel_id", str(channel.id))
    await interaction.response.send_message(f"Daily reports will be sent in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="report_now", description="Send a CCU report for yesterday immediately.")
async def report_now(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    report_day = datetime.now(tz).date() - timedelta(days=1)
    message = bot.build_report(report_day)

    channel_id = bot.db.get_config("report_channel_id")
    target = bot.get_channel(int(channel_id)) if channel_id else interaction.channel
    if isinstance(target, discord.abc.Messageable):
        await target.send(message)
        await interaction.followup.send("Report sent.", ephemeral=True)
    else:
        await interaction.followup.send("No valid report channel is configured.", ephemeral=True)


if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

bot.run(DISCORD_TOKEN)
