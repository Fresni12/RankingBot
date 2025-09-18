import os
import json
import asyncio
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import unicodedata

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# =============================
# Config — EUW-only, v4 by PUUID
# =============================
DATA_FILE = "data.json"
TZ = ZoneInfo("Europe/London")
DEFAULT_QUEUE = "RANKED_SOLO_5x5"  # or "RANKED_FLEX_SR"

# Weekly post (optional): set ANNOUNCE_CHANNEL_ID in .env to enable
WEEKLY_POST_HOUR = 18  # 18:00 local
WEEKLY_POST_MIN = 0

TIER_ORDER = {
    "IRON": 0, "BRONZE": 1, "SILVER": 2, "GOLD": 3, "PLATINUM": 4,
    "EMERALD": 5, "DIAMOND": 6, "MASTER": 7, "GRANDMASTER": 8, "CHALLENGER": 9
}
DIV_ORDER = {"IV": 0, "III": 1, "II": 2, "I": 3}

def sanitize_user_key(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip()

def split_riot_id(s: str) -> Tuple[str, Optional[str]]:
    s = sanitize_user_key(s)
    if "#" in s:
        game, tag = s.split("#", 1)
        return game.strip(), tag.strip()
    return s, None

# -----------------------------
# Storage
# -----------------------------
def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"players": {}}  # players[display_name] = { riot_id, puuid }
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

# -----------------------------
# Riot API (ONLY league v4 by-puuid)
# -----------------------------
class RiotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={
                "X-Riot-Token": self.api_key,
                "Accept": "application/json",
                "User-Agent": "RankingBot/1.0 (discord; contact: example@example.com)"
            }
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    @staticmethod
    def platform_base_euw() -> str:
        return "https://euw1.api.riotgames.com"

    @staticmethod
    def account_base_europe() -> str:
        return "https://europe.api.riotgames.com"

    async def _get(self, url: str, params: Dict[str, str] = None, tries: int = 2) -> Any:
        assert self.session is not None
        for i in range(tries):
            try:
                async with self.session.get(url, params=params or {}) as resp:
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        wait = float(retry_after) if retry_after else max(1.5 * (i + 1), 2.0)
                        print(f"[rate-limit] 429 on {url} — sleeping {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue

                    if resp.status in (401, 403, 400, 404):
                        text = await resp.text()
                        print(f"[http {resp.status}] {url} | {text[:200]}")
                        return None

                    resp.raise_for_status()
                    return await resp.json()

            except (aiohttp.ClientConnectorError, aiohttp.ClientConnectorDNSError) as e:
                print(f"[net] connector error for {url}: {e!r}")
                return None
            except Exception as e:
                print(f"[net] generic error for {url}: {e!r}")
                return None
        return None

    # --- The ONLY LoL v4 we use:
    async def league_entries_by_puuid_euw(self, encrypted_puuid: str) -> List[Dict[str, Any]]:
        url = f"{self.platform_base_euw()}/lol/league/v4/entries/by-puuid/{encrypted_puuid}"
        data = await self._get(url)
        return data or []

    # --- Account-V1 (not LoL v4) to resolve RiotID -> PUUID
    async def account_by_riot_id_eu(self, game_name: str, tag_line: str) -> Optional[Dict[str, Any]]:
        game_q = quote(game_name, safe="")
        tag_q = quote(tag_line, safe="")
        url = f"{self.account_base_europe()}/riot/account/v1/accounts/by-riot-id/{game_q}/{tag_q}"
        return await self._get(url)

# -----------------------------
# Ranking utils
# -----------------------------
def pick_queue_entry(entries: List[Dict[str, Any]], queue: str) -> Optional[Dict[str, Any]]:
    return next((e for e in entries if e.get("queueType") == queue), None)

def score_for_sort(entry: Optional[Dict[str, Any]]) -> int:
    if not entry:
        return -1
    tier = entry.get("tier", "IRON")
    lp = entry.get("leaguePoints", 0)
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return TIER_ORDER[tier] * 4000 + lp
    div = entry.get("rank", "IV")
    return TIER_ORDER[tier] * 400 + DIV_ORDER.get(div, 0) * 100 + lp

def compact_rank(entry: Optional[Dict[str, Any]]) -> str:
    if not entry:
        return "*Unranked*"
    tier = entry["tier"]
    lp = entry.get("leaguePoints", 0)
    w = entry.get("wins", 0); l = entry.get("losses", 0)
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return f"{tier.title()} {lp} LP (W {w}/L {l})"
    return f"{tier.title()} {entry.get('rank','IV')} — {lp} LP (W {w}/L {l})"

def build_embed(rows: List[Tuple[str, Optional[Dict[str, Any]]]], queue_label: str) -> discord.Embed:
    lines = []
    for i, (name, entry) in enumerate(rows, start=1):
        lines.append(f"**{i}. {name}** — {compact_rank(entry)}")
    if not lines:
        lines = ["No players tracked. Use `!setplayer <display_name> <Game#Tag>` (EUW)."]
    embed = discord.Embed(
        title=f"LoL — {queue_label} Ranking (EUW)",
        description="\n".join(lines),
        timestamp=datetime.now(tz=TZ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Riot API • EUW only • League v4 by PUUID")
    return embed

# -----------------------------
# Snapshot (PUUID-only path)
# -----------------------------
async def fetch_snapshot(queue: str, players: Dict[str, Dict[str, str]], api_key: str) -> Dict[str, Optional[Dict[str, Any]]]:
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    async with RiotClient(api_key) as rc:
        for name, cfg in players.items():
            puuid = cfg.get("puuid")
            if not puuid:
                out[name] = None
                continue
            try:
                entries = await rc.league_entries_by_puuid_euw(puuid)
                out[name] = pick_queue_entry(entries, queue)
            except Exception as e:
                print(f"[error] fetch for {name} failed: {e!r}")
                out[name] = None
            await asyncio.sleep(0.2)
    return out

def sort_snapshot(snap: Dict[str, Optional[Dict[str, Any]]]) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
    return sorted(snap.items(), key=lambda kv: score_for_sort(kv[1]), reverse=True)

def build_embed_from_snapshot(snap: Dict[str, Optional[Dict[str, Any]]], queue_label: str) -> discord.Embed:
    rows = sort_snapshot(snap)
    return build_embed(rows, queue_label)

# -----------------------------
# Bot
# -----------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = (os.getenv("RIOT_API_KEY") or "").strip().strip('"').strip("'")
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))

print("RIOT key preview:", (RIOT_API_KEY[:10] if RIOT_API_KEY else ""), "len=", len(RIOT_API_KEY))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

data = load_data()  # { "players": { display_name: { "riot_id": "Game#Tag", "puuid": "..." } } }

# -----------------------------
# Helpers
# -----------------------------
async def resolve_puuid_from_riot_id(riot_id: str, api_key: str) -> Optional[str]:
    game, tag = split_riot_id(riot_id)
    if not tag:
        return None
    async with RiotClient(api_key) as rc:
        acct = await rc.account_by_riot_id_eu(game, tag)
        if acct and "puuid" in acct:
            return acct["puuid"]
        return None

def require_api(ctx) -> bool:
    if not RIOT_API_KEY:
        asyncio.create_task(ctx.send("RIOT_API_KEY missing in .env"))
        return False
    return True

# -----------------------------
# Commands
# -----------------------------
@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")
    # Start weekly poster if channel configured
    if ANNOUNCE_CHANNEL_ID > 0:
        asyncio.create_task(weekly_poster_task())

@bot.command(help="Add/update a tracked EUW player by Riot ID. Usage: !setplayer <display_name> <Game#Tag>")
async def setplayer(ctx, display_name: str, *, riot_id: str):
    if not require_api(ctx):
        return
    riot_id = sanitize_user_key(riot_id)
    puuid = await resolve_puuid_from_riot_id(riot_id, RIOT_API_KEY)
    if not puuid:
        return await ctx.send("Could not resolve **PUUID** from that Riot ID. Use the format `Game#Tag` and ensure it exists.")
    data.setdefault("players", {})
    data["players"][display_name] = {"riot_id": riot_id, "puuid": puuid}
    save_data(data)
    await ctx.send(f"✅ Set **{display_name}** → **{riot_id}** (PUUID cached)")

@bot.command(help="Remove a tracked player. Usage: !removeplayer <display_name>")
async def removeplayer(ctx, display_name: str):
    if display_name in data.get("players", {}):
        del data["players"][display_name]
        save_data(data)
        await ctx.send(f"🗑️ Removed **{display_name}**")
    else:
        await ctx.send("⚠️ That display name is not tracked.")

@bot.command(help="List tracked players.")
async def players_cmd(ctx):
    players = data.get("players", {})
    if not players:
        return await ctx.send("No players tracked yet. Use `!setplayer <display_name> <Game#Tag>`.")
    lines = [f"• **{name}** → **{cfg.get('riot_id','?')}** (PUUID: `{cfg.get('puuid','?')[:12]}…`)" for name, cfg in players.items()]
    await ctx.send("\n".join(lines))

@bot.command(help="Show EUW ranking. Usage: !rank [solo|flex]  (default: solo)")
async def rank(ctx, mode: str = "solo"):
    if not require_api(ctx):
        return
    queue = DEFAULT_QUEUE if mode.lower() == "solo" else "RANKED_FLEX_SR"
    label = "Solo/Duo" if queue == "RANKED_SOLO_5x5" else "Flex"
    snap = await fetch_snapshot(queue, data.get("players", {}), RIOT_API_KEY)
    embed = build_embed_from_snapshot(snap, label)
    await ctx.send(embed=embed)

@bot.command(help="Quick rank for a Riot ID (no tracking). Usage: !rankid <Game#Tag> [solo|flex]")
async def rankid(ctx, riot_id: str, mode: str = "solo"):
    if not require_api(ctx):
        return
    puuid = await resolve_puuid_from_riot_id(riot_id, RIOT_API_KEY)
    if not puuid:
        return await ctx.send("Couldn’t resolve that Riot ID (Game#Tag).")
    async with RiotClient(RIOT_API_KEY) as rc:
        entries = await rc.league_entries_by_puuid_euw(puuid)
    picked = pick_queue_entry(entries, "RANKED_SOLO_5x5" if mode.lower()=="solo" else "RANKED_FLEX_SR")
    await ctx.send(f"{riot_id}: {compact_rank(picked)}")

@bot.command(help="Debug: show cached PUUID and raw entries. Usage: !debugrank <display_name> [solo|flex]")
async def debugrank(ctx, display_name: str, mode: str = "solo"):
    if not require_api(ctx):
        return
    cfg = data.get("players", {}).get(display_name)
    if not cfg:
        return await ctx.send("Unknown display_name.")
    puuid = cfg.get("puuid")
    riot_id = cfg.get("riot_id", "?")
    if not puuid:
        return await ctx.send("No PUUID cached for that player. Re-set the player with a Riot ID.")
    await ctx.send(f"PUUID: `{puuid}` for **{display_name}** ({riot_id})")
    async with RiotClient(RIOT_API_KEY) as rc:
        entries = await rc.league_entries_by_puuid_euw(puuid)
    await ctx.send(f"entries: `{entries}`")
    picked = pick_queue_entry(entries, DEFAULT_QUEUE if mode.lower()=="solo" else "RANKED_FLEX_SR")
    await ctx.send(f"picked ({mode}): `{picked}`")

@bot.command(help="Post the ranking now to the announce channel (if set). Usage: !postnow [solo|flex]")
async def postnow(ctx, mode: str = "solo"):
    if not require_api(ctx):
        return
    if ANNOUNCE_CHANNEL_ID <= 0:
        return await ctx.send("ANNOUNCE_CHANNEL_ID not set in .env, cannot post automatically.")
    queue = DEFAULT_QUEUE if mode.lower() == "solo" else "RANKED_FLEX_SR"
    label = "Solo/Duo" if queue == "RANKED_SOLO_5x5" else "Flex"
    snap = await fetch_snapshot(queue, data.get("players", {}), RIOT_API_KEY)
    embed = build_embed_from_snapshot(snap, label)
    ch = bot.get_channel(ANNOUNCE_CHANNEL_ID)
    if not ch:
        return await ctx.send("Announce channel not found (check permissions / ID).")
    await ch.send(embed=embed)
    await ctx.send("Posted.")

# -----------------------------
# Weekly poster (Sunday 18:00 local)
# -----------------------------
def next_sunday_1800(now: datetime) -> datetime:
    # weekday(): Mon=0 ... Sun=6
    days_ahead = (6 - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(hour=WEEKLY_POST_HOUR, minute=WEEKLY_POST_MIN, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=7)
    return target

async def weekly_poster_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.now(tz=TZ)
            target = next_sunday_1800(now)
            sleep_s = (target - now).total_seconds()
            print(f"[weekly] Next post at {target.isoformat()} (sleep {sleep_s:.0f}s)")
            await asyncio.sleep(sleep_s)

            if ANNOUNCE_CHANNEL_ID > 0 and RIOT_API_KEY:
                queue = DEFAULT_QUEUE
                label = "Solo/Duo" if queue == "RANKED_SOLO_5x5" else "Flex"
                snap = await fetch_snapshot(queue, data.get("players", {}), RIOT_API_KEY)
                embed = build_embed_from_snapshot(snap, label)
                ch = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                if ch:
                    await ch.send(embed=embed)
                else:
                    print("[weekly] Announce channel not found.")
            # Sleep 60s to avoid tight loop if clock skew
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[weekly] error: {e!r}")
            await asyncio.sleep(60)

# -----------------------------
# Ping
# -----------------------------
@bot.command(help="Pings the bot")
async def ping(ctx):
    await ctx.send("Pong!")

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("No encontré el token. ¿Está en .env?")
    if not RIOT_API_KEY:
        raise RuntimeError("RIOT_API_KEY missing in .env")
    try:
        import sys
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore
    except Exception:
        pass
    bot.run(TOKEN)
