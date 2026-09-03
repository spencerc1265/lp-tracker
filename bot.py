import discord
import requests
import re
import os
import json
import asyncio
import logging
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import tasks

# ==================== SETUP ====================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")

# Guild ID for instant command sync during development/testing.
# Commands synced to a specific guild show up immediately, instead of
# waiting up to an hour for Discord's global command propagation.
TEST_GUILD_ID = 1543320002692907170
TEST_GUILD = discord.Object(id=TEST_GUILD_ID)

# How often (minutes) to check registered players for new ranked games.
# Each check costs 2 Riot API calls per player, so raise this if you have
# a lot of registered players and start hitting rate limits (429s).
POLL_INTERVAL_MINUTES = int(os.getenv("LP_POLL_INTERVAL_MINUTES", "5"))

# How often (minutes) to check for a new patch notes article. Patches drop
# roughly biweekly, so this doesn't need to be frequent — kept longer than
# the LP poll interval to be a good citizen toward the community news feed.
PATCH_POLL_INTERVAL_MINUTES = int(os.getenv("PATCH_POLL_INTERVAL_MINUTES", "30"))

# Where players.json / config.json live. Point this at a mounted volume's
# path on hosting platforms with ephemeral filesystems (e.g. Railway),
# otherwise data is wiped on every redeploy/restart.
DATA_DIR = os.getenv("DATA_DIR", "data")

if not os.path.exists("logs"):
    os.makedirs("logs")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/discord_bot.log"),
        logging.StreamHandler()
    ]
)

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ==================== REGION MAPPING ====================
# Riot ID lookups (Account-V1) use "regional" routing.
# Summoner/League lookups use "platform" routing.
PLATFORM_TO_REGIONAL = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas", "oc1": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
    "kr": "asia", "jp1": "asia",
}

VALID_PLATFORMS = set(PLATFORM_TO_REGIONAL.keys())

TIER_ORDER = [
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
    "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"
]
DIVISION_ORDER = {"IV": 0, "III": 1, "II": 2, "I": 3}
APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}

# ==================== RIOT ASSETS (Community Dragon) ====================
RANK_EMBLEM_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/"
    "global/default/ranked-emblem/emblem-{tier}.png"
)
PROFILE_ICON_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/default/v1/profile-icons/{icon_id}.jpg"
)

def get_rank_emblem_url(tier: str) -> str:
    return RANK_EMBLEM_URL.format(tier=tier.lower())

def get_profile_icon_url(icon_id: int) -> str:
    return PROFILE_ICON_URL.format(icon_id=icon_id)

def _crop_emblem_sync(tier: str) -> tuple[BytesIO, str]:
    """Download a rank emblem and trim its transparent padding.
    Runs synchronously — call via asyncio.to_thread to avoid blocking the event loop."""
    resp = requests.get(get_rank_emblem_url(tier), timeout=10)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content)).convert("RGBA")

    alpha = img.split()[-1]
    bbox = alpha.getbbox()
    if bbox:
        img = img.crop(bbox)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf, f"emblem_{tier.lower()}.png"

async def get_cropped_emblem_file(tier: str) -> tuple[discord.File, str]:
    """Return (discord.File, attachment filename) for a cropped rank emblem."""
    buf, filename = await asyncio.to_thread(_crop_emblem_sync, tier)
    return discord.File(buf, filename=filename), filename

# ==================== STORAGE ====================
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

DATA_FILE = os.path.join(DATA_DIR, "players.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

def load_players():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_players(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ==================== HELPERS ====================
class LPLookupError(Exception):
    """Expected, user-facing lookup failure (bad Riot ID, wrong region, etc.)."""
    pass

def rank_sort_key(entry):
    tier_index = TIER_ORDER.index(entry["tier"]) if entry["tier"] in TIER_ORDER else -1
    division_index = DIVISION_ORDER.get(entry["division"], 0)
    return (tier_index, division_index, entry["lp"])

async def fetch_profile_icon_id(puuid: str, region: str) -> int | None:
    """Fetch just the summoner's profile icon ID. Returns None (never
    raises) on failure — this is decorative, never something to block on."""
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        summoner_url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        resp = requests.get(summoner_url, headers=headers)
        if resp.ok:
            return resp.json().get("profileIconId")
    except Exception:
        logging.warning("Could not fetch profile icon", exc_info=True)
    return None

async def fetch_puuid(name: str, region: str) -> str:
    """Resolve a Riot ID (GameName#Tag) to a puuid. Raises LPLookupError
    with a user-facing message on a bad region/format or unknown Riot ID."""
    region = region.lower()
    if region not in VALID_PLATFORMS:
        raise LPLookupError(f"Unknown region '{region}'. Valid options: {', '.join(sorted(VALID_PLATFORMS))}")

    if "#" not in name:
        raise LPLookupError("Riot ID must be in the form `GameName#Tag` (e.g. `Player#1234`).")

    game_name, tag_line = name.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    regional = PLATFORM_TO_REGIONAL[region]
    headers = {"X-Riot-Token": RIOT_API_KEY}

    account_url = (
        f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/"
        f"by-riot-id/{game_name}/{tag_line}"
    )
    account_resp = requests.get(account_url, headers=headers)
    if account_resp.status_code == 404:
        raise LPLookupError(f"Riot ID '{name}' not found.")
    account_resp.raise_for_status()
    return account_resp.json()["puuid"]

async def fetch_ranked_solo(name: str, region: str, fetch_icon: bool = False) -> dict:
    """Look up a Riot ID's Ranked Solo/Duo entry.
    Raises LPLookupError with a user-facing message on expected failures.
    If fetch_icon is True, also fetches the summoner's profile icon ID
    (costs one extra Riot API call — skip it for bulk lookups like the leaderboard)."""
    region = region.lower()
    headers = {"X-Riot-Token": RIOT_API_KEY}

    puuid = await fetch_puuid(name, region)

    # Optional: profile icon ID (Summoner-V4 still returns this field, even
    # though it no longer returns the encrypted "id" summoner ID).
    profile_icon_id = await fetch_profile_icon_id(puuid, region) if fetch_icon else None

    # PUUID -> Ranked entries (League-V4, platform routing)
    ranked_url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    ranked_resp = requests.get(ranked_url, headers=headers)
    if ranked_resp.status_code == 404:
        raise LPLookupError(f"No League of Legends account found for '{name}' on {region.upper()}.")
    ranked_resp.raise_for_status()
    ranked_data = ranked_resp.json()

    if not ranked_data:
        raise LPLookupError(f"{name} has no ranked games on {region.upper()}.")

    solo = next((e for e in ranked_data if e["queueType"] == "RANKED_SOLO_5x5"), None)
    if not solo:
        raise LPLookupError(f"{name} has no Ranked Solo/Duo games on {region.upper()}.")

    wins = solo["wins"]
    losses = solo["losses"]
    wr = (wins / (wins + losses)) * 100 if wins + losses > 0 else 0

    promo = None
    mini_series = solo.get("miniSeries")
    if mini_series:
        icon_map = {"W": "🟢", "L": "🔴", "N": "⚪"}
        promo = {
            "wins": mini_series.get("wins", 0),
            "losses": mini_series.get("losses", 0),
            "target": mini_series.get("target", 0),
            "progress_icons": "".join(icon_map.get(c, "⚪") for c in mini_series.get("progress", "")),
        }

    return {
        "tier": solo["tier"],
        "division": solo.get("rank", ""),
        "lp": solo["leaguePoints"],
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "profile_icon_id": profile_icon_id,
        "puuid": puuid,
        "promo": promo,
    }

# ==================== CHAMPION NAMES & ICONS ====================
_CHAMPION_DATA_CACHE = {}

def _load_champion_data_sync():
    """Fetch and cache championName (API key, e.g. 'MonkeyKing') -> {name, id, roles}."""
    if _CHAMPION_DATA_CACHE:
        return
    try:
        resp = requests.get(
            "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
            "global/default/v1/champion-summary.json",
            timeout=10
        )
        resp.raise_for_status()
        for champ in resp.json():
            alias = champ.get("alias")
            name = champ.get("name")
            champ_id = champ.get("id")
            if alias and name:
                _CHAMPION_DATA_CACHE[alias] = {"name": name, "id": champ_id, "roles": champ.get("roles", [])}
    except Exception:
        logging.warning("Could not load champion data list", exc_info=True)

async def get_champion_display_name(champion_key: str) -> str:
    await asyncio.to_thread(_load_champion_data_sync)
    entry = _CHAMPION_DATA_CACHE.get(champion_key)
    return entry["name"] if entry else champion_key

async def get_champion_icon_url(champion_key: str) -> str | None:
    await asyncio.to_thread(_load_champion_data_sync)
    entry = _CHAMPION_DATA_CACHE.get(champion_key)
    if not entry or entry.get("id") is None:
        return None
    return (
        "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
        f"global/default/v1/champion-icons/{entry['id']}.png"
    )

def get_champion_icon_url_by_id(champion_id: int) -> str:
    """Champion-icon URL from a numeric champion ID directly — no cache
    lookup needed, since the ID *is* the path segment."""
    return (
        "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
        f"global/default/v1/champion-icons/{champion_id}.png"
    )

async def get_champion_by_id(champion_id: int) -> dict | None:
    """Reverse lookup: numeric champion ID -> {name, id, roles}. Used for
    APIs (Mastery-V4, Spectator-V5) that return IDs instead of string keys."""
    await asyncio.to_thread(_load_champion_data_sync)
    for entry in _CHAMPION_DATA_CACHE.values():
        if entry.get("id") == champion_id:
            return entry
    return None

async def find_champion_by_name(query: str) -> tuple[str, dict] | None:
    """Look up a champion by display name or alias, case-insensitive.
    Returns (alias, entry) or None if no unambiguous match is found."""
    await asyncio.to_thread(_load_champion_data_sync)
    normalized = query.strip().lower()
    for alias, entry in _CHAMPION_DATA_CACHE.items():
        if entry["name"].lower() == normalized or alias.lower() == normalized:
            return alias, entry
    candidates = [(alias, entry) for alias, entry in _CHAMPION_DATA_CACHE.items() if normalized in entry["name"].lower()]
    if len(candidates) == 1:
        return candidates[0]
    return None

# ==================== MATCH HISTORY (Match-V5) ====================
async def fetch_last_ranked_match(puuid: str, region: str) -> dict | None:
    """Fetch champion/KDA details for the player's most recent Ranked Solo/Duo
    match. Returns None (never raises) if match data can't be retrieved —
    this is supplementary flavor for the auto-update announcement, not
    something that should ever block it."""
    regional = PLATFORM_TO_REGIONAL[region]
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        ids_url = (
            f"https://{regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/"
            f"{puuid}/ids?queue=420&start=0&count=1"
        )
        ids_resp = requests.get(ids_url, headers=headers)
        ids_resp.raise_for_status()
        match_ids = ids_resp.json()
        if not match_ids:
            return None
        match_id = match_ids[0]

        match_url = f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        match_resp = requests.get(match_url, headers=headers)
        match_resp.raise_for_status()
        match_data = match_resp.json()

        participant = next(
            (p for p in match_data["info"]["participants"] if p["puuid"] == puuid), None
        )
        if not participant:
            return None

        return {
            "match_id": match_id,
            "champion_key": participant["championName"],
            "champion": await get_champion_display_name(participant["championName"]),
            "kills": participant["kills"],
            "deaths": participant["deaths"],
            "assists": participant["assists"],
            "win": participant["win"],
            "duration_seconds": match_data["info"]["gameDuration"],
        }
    except Exception:
        logging.warning(f"Could not fetch match details for puuid {puuid}", exc_info=True)
        return None

ROLE_DISPLAY = {
    "TOP": "Top",
    "JUNGLE": "Jungle",
    "MIDDLE": "Mid",
    "BOTTOM": "Bot",
    "UTILITY": "Support",
}

# How many recent ranked games to analyze for /lp's "top champions" section.
# Each game costs 1 Riot API call, so this directly trades off /lp's cost
# and latency against how representative the stats are.
CHAMPION_STATS_GAME_COUNT = int(os.getenv("CHAMPION_STATS_GAME_COUNT", "10"))

async def _fetch_match_participant(match_id: str, regional: str, headers: dict, puuid: str):
    match_url = f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp = await asyncio.to_thread(requests.get, match_url, headers=headers)
    if not resp.ok:
        return None
    match_data = resp.json()
    return next((p for p in match_data["info"]["participants"] if p["puuid"] == puuid), None)

async def fetch_recent_champion_stats(puuid: str, region: str) -> dict | None:
    """Aggregate top champions and most-played role over the player's last
    CHAMPION_STATS_GAME_COUNT Ranked Solo/Duo games. Returns None (never
    raises) if this can't be computed — treated as optional flavor for /lp."""
    regional = PLATFORM_TO_REGIONAL[region]
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        ids_url = (
            f"https://{regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/"
            f"{puuid}/ids?queue=420&start=0&count={CHAMPION_STATS_GAME_COUNT}"
        )
        ids_resp = requests.get(ids_url, headers=headers)
        ids_resp.raise_for_status()
        match_ids = ids_resp.json()
        if not match_ids:
            return None

        participants = await asyncio.gather(
            *[_fetch_match_participant(mid, regional, headers, puuid) for mid in match_ids]
        )
        participants = [p for p in participants if p is not None]
        if not participants:
            return None

        champ_stats = {}  # champion_key -> [games, wins]
        role_counts = {}
        for p in participants:
            champ = p["championName"]
            games, wins = champ_stats.get(champ, (0, 0))
            champ_stats[champ] = (games + 1, wins + (1 if p["win"] else 0))

            role = p.get("teamPosition") or "UNKNOWN"
            role_counts[role] = role_counts.get(role, 0) + 1

        top_champs = sorted(champ_stats.items(), key=lambda kv: kv[1][0], reverse=True)[:3]
        top_champs_display = []
        for champ_key, (games, wins) in top_champs:
            display_name = await get_champion_display_name(champ_key)
            wr = (wins / games) * 100 if games else 0
            top_champs_display.append(f"{display_name} — {games}g, {wr:.0f}% WR")

        top_role = None
        if role_counts:
            top_role_key = max(role_counts.items(), key=lambda kv: kv[1])[0]
            top_role = ROLE_DISPLAY.get(top_role_key, top_role_key.title())

        return {
            "top_champions": top_champs_display,
            "top_role": top_role,
            "games_analyzed": len(participants),
        }
    except Exception:
        logging.warning(f"Could not fetch champion stats for puuid {puuid}", exc_info=True)
        return None

def get_rank_color(rank):
    color_dict = {
        "IRON": 0x808080,
        "BRONZE": 0xCD7F32,
        "SILVER": 0xC0C0C0,
        "GOLD": 0xFFD700,
        "PLATINUM": 0x00FFFF,
        "EMERALD": 0x50C878,
        "DIAMOND": 0xB19CD9,
        "MASTER": 0x800080,
        "GRANDMASTER": 0x800080,
        "CHALLENGER": 0x800080
    }
    return color_dict.get(rank, 0x808080)

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    logging.info(f"✅ BOT ONLINE! Logged in as {bot.user}")
    tree.copy_global_to(guild=TEST_GUILD)
    synced = await tree.sync(guild=TEST_GUILD)
    logging.info(f"Slash commands synced to test guild ({len(synced)} commands)!")
    if not poll_for_updates.is_running():
        poll_for_updates.start()
        logging.info(f"Started auto-update polling loop (every {POLL_INTERVAL_MINUTES} min).")
    if not poll_for_patch_notes.is_running():
        poll_for_patch_notes.start()
        logging.info(f"Started patch notes polling loop (every {PATCH_POLL_INTERVAL_MINUTES} min).")

# ==================== COMMANDS ====================
@tree.command(name="lp", description="Check your or someone's League LP / rank")
@app_commands.describe(
    name="Riot ID in the form GameName#Tag (e.g. 'Player#1234')",
    region="Platform region (default na1): na1, euw1, eun1, kr, jp1, br1, la1, la2, oc1, tr1, ru"
)
async def lp(interaction: discord.Interaction, name: str, region: str = "na1"):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    try:
        data = await fetch_ranked_solo(name, region, fetch_icon=True)
    except LPLookupError as e:
        await interaction.followup.send(f"❌ {e}")
        return
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        logging.error(f"Riot API HTTP error ({status}): {e}")
        await interaction.followup.send(f"❌ Riot API error ({status}). Check your API key or try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /lp")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    division = "" if data["tier"] in APEX_TIERS else f" {data['division']}"
    embed = discord.Embed(
        title=f"{data['tier'].title()}{division} • {data['lp']} LP",
        color=get_rank_color(data['tier'])
    )
    embed.set_author(
        name=f"{name} ({region.upper()})",
        icon_url=get_profile_icon_url(data["profile_icon_id"]) if data.get("profile_icon_id") else None
    )

    champ_stats = await fetch_recent_champion_stats(data["puuid"], region)

    embed.add_field(name="Record", value=f"{data['wins']}W - {data['losses']}L", inline=True)
    embed.add_field(name="Win Rate", value=f"{data['wr']:.1f}%", inline=True)
    embed.add_field(name="Main Role", value=champ_stats["top_role"] if champ_stats and champ_stats["top_role"] else "—", inline=True)

    if data.get("promo"):
        promo = data["promo"]
        embed.add_field(
            name=f"Promotion Series (first to {promo['target']})",
            value=f"{promo['progress_icons']} ({promo['wins']}W - {promo['losses']}L)",
            inline=False
        )

    if champ_stats and champ_stats["top_champions"]:
        embed.add_field(
            name="Top Champions",
            value="\n".join(champ_stats["top_champions"]),
            inline=False
        )
        embed.set_footer(text=f"Champion stats based on last {champ_stats['games_analyzed']} ranked games")

    try:
        emblem_file, emblem_filename = await get_cropped_emblem_file(data['tier'])
        embed.set_thumbnail(url=f"attachment://{emblem_filename}")
        await interaction.followup.send(embed=embed, file=emblem_file)
    except Exception:
        logging.exception("Could not crop emblem image, falling back to raw URL")
        embed.set_thumbnail(url=get_rank_emblem_url(data['tier']))
        await interaction.followup.send(embed=embed)

# ==================== EXTRA LOOKUPS ====================
QUEUE_NAMES = {
    400: "Normal (Draft)",
    420: "Ranked Solo/Duo",
    430: "Normal (Blind)",
    440: "Ranked Flex",
    450: "ARAM",
    700: "Clash",
    900: "ARURF",
    1700: "Arena",
}

@tree.command(name="mastery", description="Show a player's top champions by mastery")
@app_commands.describe(
    name="Riot ID in the form GameName#Tag (e.g. 'Player#1234')",
    region="Platform region (default na1): na1, euw1, eun1, kr, jp1, br1, la1, la2, oc1, tr1, ru"
)
async def mastery(interaction: discord.Interaction, name: str, region: str = "na1"):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    try:
        puuid = await fetch_puuid(name, region)
    except LPLookupError as e:
        await interaction.followup.send(f"❌ {e}")
        return
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return

    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        url = (
            f"https://{region.lower()}.api.riotgames.com/lol/champion-mastery/v4/"
            f"champion-masteries/by-puuid/{puuid}/top?count=5"
        )
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        top = resp.json()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /mastery")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    if not top:
        await interaction.followup.send(f"❌ No champion mastery data found for '{name}'.")
        return

    lines = []
    for entry in top:
        champ = await get_champion_by_id(entry["championId"])
        champ_name = champ["name"] if champ else f"Champion {entry['championId']}"
        lines.append(f"**{champ_name}** — Level {entry['championLevel']} • {entry['championPoints']:,} pts")

    embed = discord.Embed(
        title=f"{name} — Top Champions by Mastery",
        description="\n".join(lines),
        color=0x9B59B6
    )
    top_champ = await get_champion_by_id(top[0]["championId"])
    if top_champ:
        embed.set_thumbnail(url=get_champion_icon_url_by_id(top_champ["id"]))

    await interaction.followup.send(embed=embed)

@tree.command(name="livegame", description="Check if a player is currently in a game")
@app_commands.describe(
    name="Riot ID in the form GameName#Tag (e.g. 'Player#1234')",
    region="Platform region (default na1): na1, euw1, eun1, kr, jp1, br1, la1, la2, oc1, tr1, ru"
)
async def livegame(interaction: discord.Interaction, name: str, region: str = "na1"):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    try:
        puuid = await fetch_puuid(name, region)
    except LPLookupError as e:
        await interaction.followup.send(f"❌ {e}")
        return
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return

    headers = {"X-Riot-Token": RIOT_API_KEY}
    region = region.lower()
    try:
        url = f"https://{region}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
        resp = requests.get(url, headers=headers)
        if resp.status_code == 404:
            await interaction.followup.send(f"💤 **{name}** is not currently in a game.")
            return
        resp.raise_for_status()
        game_data = resp.json()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /livegame")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    participant = next((p for p in game_data.get("participants", []) if p.get("puuid") == puuid), None)
    if not participant:
        await interaction.followup.send(f"💤 **{name}** is not currently in a game.")
        return

    champ = await get_champion_by_id(participant["championId"])
    champ_name = champ["name"] if champ else f"Champion {participant['championId']}"
    queue_name = QUEUE_NAMES.get(game_data.get("gameQueueConfigId"), "Custom/Other")
    mins, secs = divmod(max(game_data.get("gameLength", 0), 0), 60)

    embed = discord.Embed(
        title=f"🔴 {name} is LIVE",
        description=f"Playing **{champ_name}** • {queue_name}\nGame time: {mins}:{secs:02d}",
        color=0xE74C3C
    )
    embed.set_thumbnail(url=get_champion_icon_url_by_id(participant["championId"]))
    await interaction.followup.send(embed=embed)

@tree.command(name="freerotation", description="Show this week's free champion rotation")
@app_commands.describe(region="Platform region (default na1)")
async def freerotation(interaction: discord.Interaction, region: str = "na1"):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    region = region.lower()
    if region not in VALID_PLATFORMS:
        await interaction.followup.send(f"❌ Unknown region '{region}'. Valid options: {', '.join(sorted(VALID_PLATFORMS))}")
        return

    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        url = f"https://{region}.api.riotgames.com/lol/platform/v3/champion-rotations"
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /freerotation")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    champ_ids = data.get("freeChampionIds", [])
    names = []
    for cid in champ_ids:
        champ = await get_champion_by_id(cid)
        names.append(champ["name"] if champ else f"Champion {cid}")
    names.sort()

    embed = discord.Embed(
        title="🔄 Free Champion Rotation",
        description="\n".join(f"• {n}" for n in names) if names else "No rotation data available.",
        color=0x1E90FF
    )
    await interaction.followup.send(embed=embed)

@tree.command(name="serverstatus", description="Check League of Legends server status")
@app_commands.describe(region="Platform region (default na1)")
async def serverstatus(interaction: discord.Interaction, region: str = "na1"):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    region = region.lower()
    if region not in VALID_PLATFORMS:
        await interaction.followup.send(f"❌ Unknown region '{region}'. Valid options: {', '.join(sorted(VALID_PLATFORMS))}")
        return

    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        url = f"https://{region}.api.riotgames.com/lol/status/v4/platform-data"
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /serverstatus")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    issues = data.get("incidents", []) + data.get("maintenances", [])
    if not issues:
        embed = discord.Embed(
            title=f"{region.upper()} Server Status",
            description="✅ All systems operational.",
            color=0x2ECC71
        )
    else:
        lines = []
        for issue in issues:
            titles = issue.get("titles", [])
            title = next((t["content"] for t in titles if t.get("locale") == "en_US"), titles[0]["content"] if titles else "Unknown issue")
            lines.append(f"⚠️ {title} ({issue.get('severity', 'info')})")
        embed = discord.Embed(
            title=f"{region.upper()} Server Status",
            description="\n".join(lines)[:4096],
            color=0xE74C3C
        )

    await interaction.followup.send(embed=embed)

@tree.command(name="champion", description="Show quick info about a champion")
@app_commands.describe(name="Champion name (e.g. 'Wukong', 'Miss Fortune')")
async def champion_info(interaction: discord.Interaction, name: str):
    await interaction.response.defer(thinking=True)

    match = await find_champion_by_name(name)
    if not match:
        await interaction.followup.send(f"❌ Couldn't find a champion matching '{name}'.")
        return

    alias, entry = match
    roles = ", ".join(r.title() for r in entry.get("roles", [])) or "—"
    embed = discord.Embed(
        title=entry["name"],
        description=f"**Common Roles:** {roles}",
        color=0x9B59B6
    )
    embed.set_thumbnail(url=get_champion_icon_url_by_id(entry["id"]))
    await interaction.followup.send(embed=embed)

# ==================== NEWS / PATCH NOTES ====================
# Riot doesn't publish an official patch-notes API. This uses a community
# service (rito-news-feeds / data.rito.news) that mirrors Riot's own
# official news page into a standard JSONFeed. It's unofficial and
# third-party — not Riot-supported — so it could change format or go
# down without warning, unlike the calls above. Costs 0 Riot API calls.
NEWS_FEED_URL = "https://data.rito.news/lol/en-us/news.jsonfeed"

def _fetch_news_items_sync(limit: int = 20) -> list:
    resp = requests.get(NEWS_FEED_URL, timeout=10)
    resp.raise_for_status()
    return resp.json().get("items", [])[:limit]

@tree.command(name="news", description="Show the latest League of Legends news")
async def news_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        items = await asyncio.to_thread(_fetch_news_items_sync, 5)
    except Exception:
        logging.exception("Unexpected error in /news")
        await interaction.followup.send("❌ Couldn't reach the news feed right now. Try again shortly.")
        return

    if not items:
        await interaction.followup.send("❌ No news articles found right now.")
        return

    lines = []
    for item in items:
        title = item.get("title", "Untitled")
        url = item.get("url", "")
        date = (item.get("date_published") or "")[:10]
        lines.append(f"**[{title}]({url})**" + (f" — {date}" if date else ""))

    embed = discord.Embed(
        title="📰 Latest League of Legends News",
        description="\n\n".join(lines)[:4096],
        color=0x1E90FF
    )
    embed.set_footer(text="Source: leagueoflegends.com (via unofficial community feed)")
    await interaction.followup.send(embed=embed)

@tree.command(name="patchnotes", description="Show the latest League of Legends patch notes")
async def patchnotes(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        items = await asyncio.to_thread(_fetch_news_items_sync, 20)
    except Exception:
        logging.exception("Unexpected error in /patchnotes")
        await interaction.followup.send("❌ Couldn't reach the patch notes feed right now. Try again shortly.")
        return

    patch_item = next((item for item in items if re.match(r"^patch", item.get("title", ""), re.IGNORECASE)), None)
    if not patch_item:
        await interaction.followup.send("❌ Couldn't find a recent patch notes article.")
        return

    embed = discord.Embed(
        title=patch_item.get("title", "Patch Notes"),
        url=patch_item.get("url"),
        description=(patch_item.get("summary") or "")[:500],
        color=0x1E90FF
    )
    image = patch_item.get("image") or patch_item.get("banner_image")
    if image:
        embed.set_image(url=image)
    date = (patch_item.get("date_published") or "")[:10]
    embed.set_footer(text=f"Published {date}" if date else "Source: leagueoflegends.com (via unofficial community feed)")
    await interaction.followup.send(embed=embed)

# ==================== REGISTRATION ====================
@tree.command(name="register", description="Link your Discord account to a Riot ID for the leaderboard")
@app_commands.describe(
    name="Riot ID in the form GameName#Tag (e.g. 'Player#1234')",
    region="Platform region (default na1): na1, euw1, eun1, kr, jp1, br1, la1, la2, oc1, tr1, ru"
)
async def register(interaction: discord.Interaction, name: str, region: str = "na1"):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    baseline = None
    try:
        baseline = await fetch_ranked_solo(name, region, fetch_icon=True)
    except LPLookupError as e:
        msg = str(e)
        # Allow registering unranked players — only block on a bad Riot ID/region.
        if "ranked games" not in msg and "Ranked Solo/Duo" not in msg:
            await interaction.followup.send(f"❌ {msg}")
            return
        baseline = None  # account/region is valid, they're just unranked
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /register")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    guild_id = str(interaction.guild_id)
    players = load_players()
    players.setdefault(guild_id, {})
    entry = {"name": name, "region": region.lower()}
    if baseline:
        entry.update({
            "last_tier": baseline["tier"],
            "last_division": baseline["division"],
            "last_lp": baseline["lp"],
            "last_wins": baseline["wins"],
            "last_losses": baseline["losses"],
        })
    players[guild_id][str(interaction.user.id)] = entry
    save_players(players)

    embed = discord.Embed(
        title="✅ Registered",
        description=f"Linked to {interaction.user.mention} — you'll now show up on `/leaderboard`.",
        color=get_rank_color(baseline["tier"]) if baseline else 0x2ECC71
    )
    embed.set_author(
        name=f"{name} ({region.upper()})",
        icon_url=get_profile_icon_url(baseline["profile_icon_id"]) if baseline and baseline.get("profile_icon_id") else None
    )
    if baseline:
        division = "" if baseline["tier"] in APEX_TIERS else f" {baseline['division']}"
        embed.add_field(name="Current Rank", value=f"{baseline['tier'].title()}{division} • {baseline['lp']} LP", inline=False)
    else:
        embed.add_field(name="Current Rank", value="Unranked", inline=False)

    await interaction.followup.send(embed=embed)

@tree.command(name="setchannel", description="Set the channel for automatic LP update announcements (admin only)")
@app_commands.describe(channel="The channel where win/loss LP updates should be posted")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    config = load_config()
    config.setdefault(guild_id, {})
    config[guild_id]["update_channel_id"] = channel.id
    save_config(config)
    await interaction.response.send_message(
        f"✅ LP update announcements will now be posted in {channel.mention} "
        f"(checked every {POLL_INTERVAL_MINUTES} min).",
        ephemeral=True
    )

@setchannel.error
async def setchannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to set the update channel.", ephemeral=True
        )
    else:
        logging.exception("Error in /setchannel")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)

@tree.command(name="setpatchchannel", description="Set the channel for automatic patch notes announcements (admin only)")
@app_commands.describe(channel="The channel where new patch notes should be posted")
@app_commands.checks.has_permissions(manage_guild=True)
async def setpatchchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    config = load_config()
    config.setdefault(guild_id, {})
    config[guild_id]["patch_channel_id"] = channel.id
    save_config(config)
    await interaction.response.send_message(
        f"✅ New patch notes will now be posted in {channel.mention} "
        f"(checked every {PATCH_POLL_INTERVAL_MINUTES} min). "
        f"Only patches published *after* this is set will be announced.",
        ephemeral=True
    )

@setpatchchannel.error
async def setpatchchannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to set the patch notes channel.", ephemeral=True
        )
    else:
        logging.exception("Error in /setpatchchannel")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)

@tree.command(name="unregister", description="Remove yourself from this server's leaderboard")
async def unregister(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    players = load_players()
    if guild_id not in players or str(interaction.user.id) not in players[guild_id]:
        await interaction.response.send_message("You're not registered here.", ephemeral=True)
        return
    del players[guild_id][str(interaction.user.id)]
    save_players(players)
    await interaction.response.send_message("✅ You've been removed from the leaderboard.", ephemeral=True)

# ==================== LEADERBOARD ====================
@tree.command(name="leaderboard", description="Show the ranked LP leaderboard for registered server members")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    guild_id = str(interaction.guild_id)
    players = load_players().get(guild_id, {})

    if not players:
        await interaction.followup.send("No one has registered yet — use `/register` first!")
        return

    results = []
    failures = []

    for user_id, info in players.items():
        try:
            data = await fetch_ranked_solo(info["name"], info["region"])
            results.append((user_id, info["name"], data))
        except LPLookupError as e:
            failures.append(f"{info['name']}: {e}")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            failures.append(f"{info['name']}: Riot API error ({status})")
        except Exception:
            logging.exception(f"Unexpected error fetching leaderboard entry for {info['name']}")
            failures.append(f"{info['name']}: unexpected error")
        await asyncio.sleep(0.1)  # stay gentle on the rate limit across many lookups

    if not results:
        await interaction.followup.send("Couldn't fetch data for any registered players right now. Try again shortly.")
        return

    results.sort(key=lambda r: rank_sort_key(r[2]), reverse=True)

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (user_id, name, data) in enumerate(results, start=1):
        member = interaction.guild.get_member(int(user_id))
        display = member.mention if member else name
        division = "" if data["tier"] in APEX_TIERS else f" {data['division']}"
        rank_label = medals.get(i, f"`{i}.`")
        lines.append(f"{rank_label} {display} — {data['tier'].title()}{division} • {data['lp']} LP")

    footer_text = f"{len(results)} player{'s' if len(results) != 1 else ''} ranked"
    if failures:
        footer_text += f" • Couldn't fetch: {', '.join(failures)}"

    embed = discord.Embed(
        title=f"🏆 {interaction.guild.name} Ranked Leaderboard",
        description="\n".join(lines)[:4096],
        color=get_rank_color(results[0][2]["tier"])
    )
    embed.set_footer(text=footer_text[:2048])

    try:
        emblem_file, emblem_filename = await get_cropped_emblem_file(results[0][2]["tier"])
        embed.set_thumbnail(url=f"attachment://{emblem_filename}")
        await interaction.followup.send(embed=embed, file=emblem_file)
    except Exception:
        logging.exception("Could not crop leaderboard emblem, falling back to raw URL")
        embed.set_thumbnail(url=get_rank_emblem_url(results[0][2]["tier"]))
        await interaction.followup.send(embed=embed)

# ==================== AUTOMATIC UPDATES ====================
async def do_poll_updates():
    """Check every registered player for new ranked games and post any
    win/loss + LP change to that guild's configured update channel."""
    players = load_players()
    config = load_config()
    any_changed = False

    for guild_id, guild_players in players.items():
        channel_cfg = config.get(guild_id)
        if not channel_cfg:
            continue  # no update channel set for this guild yet

        channel = bot.get_channel(channel_cfg["update_channel_id"])
        if channel is None:
            continue  # bot can't see that channel (deleted? no access?)

        for user_id, info in guild_players.items():
            try:
                data = await fetch_ranked_solo(info["name"], info["region"])
            except LPLookupError:
                await asyncio.sleep(0.3)
                continue
            except Exception:
                logging.exception(f"Polling error for {info['name']}")
                await asyncio.sleep(0.3)
                continue

            prev_wins = info.get("last_wins")
            prev_losses = info.get("last_losses")

            if prev_wins is not None:
                win_diff = data["wins"] - prev_wins
                loss_diff = data["losses"] - prev_losses

                if win_diff > 0 or loss_diff > 0:
                    won = win_diff > 0
                    title = "🟢 Victory" if won else "🔴 Defeat"

                    # Only show an LP delta if tier/division didn't change —
                    # otherwise the raw LP numbers aren't comparable (e.g. a
                    # promotion resets LP), so we skip the (possibly confusing) diff.
                    same_rank = (
                        info.get("last_tier") == data["tier"]
                        and info.get("last_division") == data["division"]
                    )
                    lp_note = ""
                    if same_rank:
                        lp_diff = data["lp"] - info.get("last_lp", data["lp"])
                        lp_note = f" ({'+' if lp_diff >= 0 else ''}{lp_diff} LP)"

                    division = "" if data["tier"] in APEX_TIERS else f" {data['division']}"
                    lines = [f"{data['tier'].title()}{division} • {data['lp']} LP{lp_note}"]

                    champion_icon_url = None
                    match_details = await fetch_last_ranked_match(data["puuid"], info["region"])
                    if match_details:
                        champion_icon_url = await get_champion_icon_url(match_details["champion_key"])
                        mins, secs = divmod(match_details["duration_seconds"], 60)
                        lines.append(
                            f"{match_details['champion']} • "
                            f"{match_details['kills']}/{match_details['deaths']}/{match_details['assists']} KDA "
                            f"• {mins}:{secs:02d}"
                        )

                    embed = discord.Embed(
                        title=title,
                        description="\n".join(lines),
                        color=get_rank_color(data["tier"])
                    )
                    profile_icon_id = await fetch_profile_icon_id(data["puuid"], info["region"])
                    embed.set_author(
                        name=f"{info['name']} ({info['region'].upper()})",
                        icon_url=get_profile_icon_url(profile_icon_id) if profile_icon_id else None
                    )
                    if champion_icon_url:
                        embed.set_thumbnail(url=champion_icon_url)

                    try:
                        await channel.send(embed=embed)
                    except discord.Forbidden:
                        logging.warning(f"No permission to post updates in channel {channel.id}")

            info["last_tier"] = data["tier"]
            info["last_division"] = data["division"]
            info["last_lp"] = data["lp"]
            info["last_wins"] = data["wins"]
            info["last_losses"] = data["losses"]
            any_changed = True
            await asyncio.sleep(0.3)  # stay gentle on the rate limit across many lookups

    if any_changed:
        save_players(players)

@tasks.loop(minutes=POLL_INTERVAL_MINUTES)
async def poll_for_updates():
    await do_poll_updates()

@poll_for_updates.before_loop
async def before_poll_for_updates():
    await bot.wait_until_ready()

# ==================== PATCH NOTES POLLING ====================
async def do_poll_patch_notes():
    """Check the news feed for a new patch notes article and announce it
    to every guild that's configured a patch channel. State is tracked
    globally (under config["_meta"]) since the patch content is the same
    for everyone — one feed check covers all servers."""
    config = load_config()

    try:
        items = await asyncio.to_thread(_fetch_news_items_sync, 20)
    except Exception:
        logging.exception("Could not fetch news feed during patch poll")
        return

    patch_item = next((item for item in items if re.match(r"^patch", item.get("title", ""), re.IGNORECASE)), None)
    if not patch_item:
        return

    patch_url = patch_item.get("url")
    meta = config.get("_meta", {})
    if meta.get("last_patch_url") == patch_url:
        return  # already announced (or this is still the known-latest patch)

    is_first_check = "last_patch_url" not in meta
    config["_meta"] = {"last_patch_url": patch_url, "last_patch_title": patch_item.get("title")}
    save_config(config)

    if is_first_check:
        # Don't blast the currently-latest patch to every newly-configured
        # channel — just record it as the baseline, same as LP registration.
        return

    embed = discord.Embed(
        title=patch_item.get("title", "Patch Notes"),
        url=patch_url,
        description=(patch_item.get("summary") or "")[:500],
        color=0x1E90FF
    )
    image = patch_item.get("image") or patch_item.get("banner_image")
    if image:
        embed.set_image(url=image)
    date = (patch_item.get("date_published") or "")[:10]
    embed.set_footer(text=f"Published {date}" if date else "Source: leagueoflegends.com (via unofficial community feed)")

    for guild_id, guild_cfg in config.items():
        if guild_id == "_meta":
            continue
        channel_id = guild_cfg.get("patch_channel_id")
        if not channel_id:
            continue
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logging.warning(f"No permission to post patch notes in channel {channel_id}")

@tasks.loop(minutes=PATCH_POLL_INTERVAL_MINUTES)
async def poll_for_patch_notes():
    await do_poll_patch_notes()

@poll_for_patch_notes.before_loop
async def before_poll_for_patch_notes():
    await bot.wait_until_ready()

@tree.command(name="forcepatchcheck", description="Manually trigger a patch notes check right now (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def forcepatchcheck(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Checking for new patch notes now...", ephemeral=True)
    await do_poll_patch_notes()

@forcepatchcheck.error
async def forcepatchcheck_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to force a patch check.", ephemeral=True
        )
    else:
        logging.exception("Error in /forcepatchcheck")

@tree.command(name="forceupdate", description="Manually trigger an LP update check right now (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def forceupdate(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Running an update check now...", ephemeral=True)
    await do_poll_updates()

@forceupdate.error
async def forceupdate_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to force an update check.", ephemeral=True
        )
    else:
        logging.exception("Error in /forceupdate")

# ==================== START ====================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("No DISCORD_TOKEN found in environment!")
    logging.info("Starting bot...")
    bot.run(TOKEN)
