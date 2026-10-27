import discord
import requests
import re
import os
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import unescape
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

# How often (minutes) to check whether any guild's digest is due. The digest
# itself fires roughly every N days from whenever it was last posted (not
# at a fixed clock time) — simpler and more robust across restarts than
# trying to align to exact midnight/calendar boundaries.
DIGEST_CHECK_INTERVAL_MINUTES = int(os.getenv("DIGEST_CHECK_INTERVAL_MINUTES", "60"))
DIGEST_FREQUENCY_DAYS = {"daily": 1, "weekly": 7}

# How often (minutes) to re-sync automatic rank roles. Each sync costs 2 Riot
# API calls per registered player in every server that has rank roles turned
# on, so raise this if you have many registered players.
ROLE_SYNC_INTERVAL_MINUTES = int(os.getenv("ROLE_SYNC_INTERVAL_MINUTES", "30"))
RANK_ROLE_PREFIX = "LoL "

# How long (seconds) the item list from Data Dragon is cached before being
# re-fetched — items change with patches, but not often enough to need a
# fetch per command.
ITEM_CACHE_TTL_SECONDS = int(os.getenv("ITEM_CACHE_TTL_SECONDS", str(6 * 60 * 60)))

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

def approx_total_lp(entry) -> int:
    """Rough single-number ladder position (400 per tier, 100 per division)
    so two ranks can be compared as an LP gap. Approximate on purpose: real
    promotion costs vary, and Master+ is treated as one continuous LP scale."""
    if entry["tier"] in APEX_TIERS:
        base = TIER_ORDER.index("MASTER") * 400
    else:
        tier_index = TIER_ORDER.index(entry["tier"]) if entry["tier"] in TIER_ORDER else 0
        base = tier_index * 400 + DIVISION_ORDER.get(entry["division"], 0) * 100
    return base + entry["lp"]

def resolve_lol_target(interaction: discord.Interaction, name, member, region, default_to_caller: bool = True):
    """Same lookup rules as /lp: an explicit Riot ID wins, otherwise use a
    registered member's stored account (the caller, if default_to_caller).
    Returns (name, region, error_message) — error_message is None on success."""
    if name is not None:
        return name, (region or "na1"), None
    if member is None and not default_to_caller:
        return None, None, "Please provide a second player (a registered member or a Riot ID)."
    target = member or interaction.user
    entry = load_players().get(str(interaction.guild_id), {}).get(str(target.id))
    if not entry:
        who = "You aren't" if target.id == interaction.user.id else f"{target.mention} isn't"
        return None, None, f"{who} registered. Use `/register` first, or provide a Riot ID directly."
    return entry["name"], (region or entry["region"]), None

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
async def fetch_last_ranked_match(puuid: str, region: str, ranked_only: bool = True) -> dict | None:
    """Fetch champion/KDA/stat details for the player's most recent match
    (Ranked Solo/Duo only by default; pass ranked_only=False to include any
    queue, e.g. for /lastgame). Returns None (never raises) if match data
    can't be retrieved — this is supplementary flavor for announcements, not
    something that should ever block them."""
    regional = PLATFORM_TO_REGIONAL[region]
    headers = {"X-Riot-Token": RIOT_API_KEY}
    queue_filter = "queue=420&" if ranked_only else ""
    try:
        ids_url = (
            f"https://{regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/"
            f"{puuid}/ids?{queue_filter}start=0&count=1"
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

        info = match_data["info"]
        team_kills = sum(
            p.get("kills", 0) for p in info["participants"] if p.get("teamId") == participant.get("teamId")
        )
        kills, assists = participant["kills"], participant["assists"]
        kill_participation = ((kills + assists) / team_kills * 100) if team_kills else 0.0

        # gameEndTimestamp can be missing on older matches; fall back to
        # start time + duration so "played X ago" still works.
        end_ms = info.get("gameEndTimestamp") or (info.get("gameCreation", 0) + info["gameDuration"] * 1000)

        return {
            "match_id": match_id,
            "champion_key": participant["championName"],
            "champion": await get_champion_display_name(participant["championName"]),
            "kills": kills,
            "deaths": participant["deaths"],
            "assists": assists,
            "win": participant["win"],
            "duration_seconds": info["gameDuration"],
            "queue_id": info.get("queueId"),
            "cs": participant.get("totalMinionsKilled", 0) + participant.get("neutralMinionsKilled", 0),
            "damage": participant.get("totalDamageDealtToChampions", 0),
            "vision_score": participant.get("visionScore", 0),
            "gold": participant.get("goldEarned", 0),
            "kill_participation": kill_participation,
            "role": ROLE_DISPLAY.get(participant.get("teamPosition") or "", None),
            "remake": bool(participant.get("gameEndedInEarlySurrender", False)),
            "ended_at": int(end_ms // 1000) if end_ms else None,
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

async def _fetch_match_json(match_id: str, regional: str, headers: dict) -> dict | None:
    match_url = f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp = await asyncio.to_thread(requests.get, match_url, headers=headers)
    return resp.json() if resp.ok else None

async def fetch_ranked_match_jsons(puuid: str, region: str, count: int) -> list[dict]:
    """Full Match-V5 payloads for the player's last `count` Ranked Solo/Duo
    games, newest first. Costs count + 1 Riot API calls. Raises
    requests.HTTPError if the match-id lookup itself fails; individual
    matches that fail to load are simply skipped."""
    regional = PLATFORM_TO_REGIONAL[region]
    headers = {"X-Riot-Token": RIOT_API_KEY}
    ids_url = (
        f"https://{regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/"
        f"{puuid}/ids?queue=420&start=0&count={count}"
    )
    ids_resp = await asyncio.to_thread(requests.get, ids_url, headers=headers)
    ids_resp.raise_for_status()
    match_ids = ids_resp.json()
    if not match_ids:
        return []
    matches = await asyncio.gather(*[_fetch_match_json(mid, regional, headers) for mid in match_ids])
    return [m for m in matches if m]  # gather preserves order, so still newest first

def find_participant(match_json: dict, puuid: str) -> dict | None:
    return next((p for p in match_json["info"]["participants"] if p["puuid"] == puuid), None)

def is_remake(match_json: dict, participant: dict) -> bool:
    return bool(participant.get("gameEndedInEarlySurrender")) and match_json["info"]["gameDuration"] < 300

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

    # Copy the in-memory global command list (populated by the @tree.command
    # decorators above) into the test guild, and sync it there FIRST — this
    # must happen before clearing global commands below, since that copy
    # step reads from the same in-memory global list we're about to wipe.
    tree.copy_global_to(guild=TEST_GUILD)
    synced = await tree.sync(guild=TEST_GUILD)
    logging.info(f"Slash commands synced to test guild ({len(synced)} commands)!")

    # One-time cleanup: an earlier version of this bot used a global
    # tree.sync() before switching to guild-specific sync above. Global
    # command registrations persist on Discord's side independently of
    # this code, so without this they'd keep showing up duplicated
    # alongside the guild-scoped commands. Safe to leave this in
    # permanently since we're guild-scoped going forward.
    tree.clear_commands(guild=None)
    await tree.sync(guild=None)
    if not poll_for_updates.is_running():
        poll_for_updates.start()
        logging.info(f"Started auto-update polling loop (every {POLL_INTERVAL_MINUTES} min).")
    if not poll_for_patch_notes.is_running():
        poll_for_patch_notes.start()
        logging.info(f"Started patch notes polling loop (every {PATCH_POLL_INTERVAL_MINUTES} min).")
    if not check_digests.is_running():
        check_digests.start()
        logging.info(f"Started LP digest check loop (every {DIGEST_CHECK_INTERVAL_MINUTES} min).")
    if not sync_rank_roles_loop.is_running():
        sync_rank_roles_loop.start()
        logging.info(f"Started rank role sync loop (every {ROLE_SYNC_INTERVAL_MINUTES} min).")

    # Warm the item cache in the background so /item autocomplete has data
    # right away instead of waiting for the first /item call.
    warm_task = asyncio.create_task(ensure_item_data())
    _background_tasks.add(warm_task)
    warm_task.add_done_callback(_background_tasks.discard)

    await refresh_all_capabilities_messages()

# ==================== COMMANDS ====================
@tree.command(name="lp", description="Check your or someone's League LP / rank")
@app_commands.describe(
    name="Riot ID in the form GameName#Tag — leave blank to use your (or member's) registered account",
    member="A registered Discord member to check instead of yourself (ignored if 'name' is given)",
    region="Platform region — leave blank to use the registered region, or a default of na1"
)
async def lp(interaction: discord.Interaction, name: str = None, member: discord.Member = None, region: str = None):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    if name is None:
        target = member or interaction.user
        guild_id = str(interaction.guild_id)
        entry = load_players().get(guild_id, {}).get(str(target.id))
        if not entry:
            who = "You aren't" if target.id == interaction.user.id else f"{target.mention} isn't"
            await interaction.followup.send(f"❌ {who} registered. Use `/register` first, or provide a Riot ID directly.")
            return
        name = entry["name"]
        region = region or entry["region"]
    else:
        region = region or "na1"

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

# ==================== MATCH LOOKUPS ====================
@tree.command(name="matchhistory", description="Show a player's recent Ranked Solo/Duo games")
@app_commands.describe(
    name="Riot ID (GameName#Tag) — leave blank to use your (or member's) registered account",
    member="A registered Discord member to check instead of yourself (ignored if 'name' is given)",
    region="Platform region — leave blank to use the registered region, or a default of na1",
    games="How many games to show (1-10, default 5)"
)
async def matchhistory(
    interaction: discord.Interaction,
    name: str = None,
    member: discord.Member = None,
    region: str = None,
    games: app_commands.Range[int, 1, 10] = 5,
):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    name, region, error = resolve_lol_target(interaction, name, member, region)
    if error:
        await interaction.followup.send(f"❌ {error}")
        return

    try:
        puuid = await fetch_puuid(name, region)
        matches = await fetch_ranked_match_jsons(puuid, region.lower(), games)
    except LPLookupError as e:
        await interaction.followup.send(f"❌ {e}")
        return
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /matchhistory")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    if not matches:
        await interaction.followup.send(f"❌ No recent Ranked Solo/Duo games found for **{name}**.")
        return

    lines = []
    wins = losses = 0
    for match in matches:
        p = find_participant(match, puuid)
        if not p:
            continue
        info = match["info"]
        champ = await get_champion_display_name(p["championName"])
        mins, secs = divmod(info["gameDuration"], 60)

        if is_remake(match, p):
            icon = "⚪"
        elif p["win"]:
            icon = "🟢"
            wins += 1
        else:
            icon = "🔴"
            losses += 1

        when = f" • <t:{info['gameEndTimestamp'] // 1000}:R>" if info.get("gameEndTimestamp") else ""
        lines.append(f"{icon} **{champ}** — {p['kills']}/{p['deaths']}/{p['assists']} • {mins}:{secs:02d}{when}")

    embed = discord.Embed(
        title=f"{name} — Recent Ranked Games",
        description="\n".join(lines)[:4096],
        color=0x5865F2
    )
    embed.set_footer(text=f"Last {len(lines)} games: {wins}W {losses}L ({region.upper()})")
    await interaction.followup.send(embed=embed)

@tree.command(name="compare", description="Compare two players' ranked stats side by side")
@app_commands.describe(
    other_member="The registered Discord member to compare against",
    other_name="Or a Riot ID (GameName#Tag) to compare against instead",
    member="First player, if not you (a registered Discord member)",
    name="Or a Riot ID for the first player instead",
    region="Region for any Riot IDs you type in (registered players use their own; default na1)"
)
async def compare(
    interaction: discord.Interaction,
    other_member: discord.Member = None,
    other_name: str = None,
    member: discord.Member = None,
    name: str = None,
    region: str = None,
):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    name1, region1, error = resolve_lol_target(interaction, name, member, region)
    if error:
        await interaction.followup.send(f"❌ {error}")
        return
    name2, region2, error = resolve_lol_target(interaction, other_name, other_member, region, default_to_caller=False)
    if error:
        await interaction.followup.send(f"❌ {error}")
        return

    if (name1.lower(), region1.lower()) == (name2.lower(), region2.lower()):
        await interaction.followup.send("❌ Pick two different players to compare.")
        return

    results = await asyncio.gather(
        fetch_ranked_solo(name1, region1),
        fetch_ranked_solo(name2, region2),
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, LPLookupError):
            await interaction.followup.send(f"❌ {result}")
            return
        if isinstance(result, requests.exceptions.HTTPError):
            status = result.response.status_code if result.response is not None else "?"
            await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
            return
        if isinstance(result, Exception):
            logging.error(f"Unexpected error in /compare: {result}", exc_info=result)
            await interaction.followup.send(f"❌ Error: {str(result)}")
            return

    data1, data2 = results
    gap = approx_total_lp(data1) - approx_total_lp(data2)
    if gap == 0:
        headline, leader = "Dead even.", data1
    else:
        winner_name, leader = (name1, data1) if gap > 0 else (name2, data2)
        headline = f"**{winner_name}** is ahead by about **{abs(gap)} LP**."

    embed = discord.Embed(title=f"⚔️ {name1} vs {name2}", description=headline, color=get_rank_color(leader["tier"]))
    for label, d in ((name1, data1), (name2, data2)):
        division = "" if d["tier"] in APEX_TIERS else f" {d['division']}"
        embed.add_field(
            name=label,
            value=f"{d['tier'].title()}{division} • {d['lp']} LP\n{d['wins']}W - {d['losses']}L\n{d['wr']:.1f}% win rate",
            inline=True
        )
    embed.set_footer(text="LP gap is approximate across tiers and divisions")
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

# ==================== LAST GAME ====================
def resolve_registered_lol_player(interaction: discord.Interaction, name: str | None, member: discord.Member | None, region: str | None):
    """Shared 'who did they mean?' logic for commands that take either an
    explicit Riot ID or fall back to a registered Discord member (the caller
    by default). Returns (name, region, error_message); error_message is
    None on success."""
    if name is not None:
        return name, (region or "na1"), None

    target = member or interaction.user
    entry = load_players().get(str(interaction.guild_id), {}).get(str(target.id))
    if not entry:
        who = "You aren't" if target.id == interaction.user.id else f"{target.mention} isn't"
        return None, None, f"❌ {who} registered. Use `/register` first, or provide a Riot ID directly."
    return entry["name"], (region or entry["region"]), None

@tree.command(name="lastgame", description="Show a detailed breakdown of a player's most recent game")
@app_commands.describe(
    name="Riot ID in the form GameName#Tag — leave blank to use your (or member's) registered account",
    member="A registered Discord member to check instead of yourself (ignored if 'name' is given)",
    region="Platform region — leave blank to use the registered region, or a default of na1",
    ranked="Only look at Ranked Solo/Duo games (default: any queue)"
)
async def lastgame(interaction: discord.Interaction, name: str = None, member: discord.Member = None, region: str = None, ranked: bool = False):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    name, region, error = resolve_registered_lol_player(interaction, name, member, region)
    if error:
        await interaction.followup.send(error)
        return
    region = region.lower()

    try:
        puuid = await fetch_puuid(name, region)
    except LPLookupError as e:
        await interaction.followup.send(f"❌ {e}")
        return
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /lastgame")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    match = await fetch_last_ranked_match(puuid, region, ranked_only=ranked)
    if not match:
        scope = "ranked Solo/Duo " if ranked else ""
        await interaction.followup.send(
            f"❌ Couldn't find a recent {scope}game for **{name}** "
            f"(or Riot's match data isn't responding — try again shortly)."
        )
        return

    kills, deaths, assists = match["kills"], match["deaths"], match["assists"]
    duration = match["duration_seconds"]
    mins, secs = divmod(duration, 60)

    if match["remake"]:
        title_prefix, color = "⚪ Remake", 0x95A5A6
    elif match["win"]:
        title_prefix, color = "🟢 Victory", 0x2ECC71
    else:
        title_prefix, color = "🔴 Defeat", 0xE74C3C

    if deaths == 0 and (kills + assists) > 0:
        kda_ratio = "Perfect"
    else:
        kda_ratio = f"{(kills + assists) / max(deaths, 1):.2f}"
    cs_per_min = (match["cs"] / (duration / 60)) if duration > 0 else 0.0

    details = [QUEUE_NAMES.get(match["queue_id"], "Other mode")]
    if match["role"]:
        details.append(match["role"])
    details.append(f"{mins}:{secs:02d}")
    if match["ended_at"]:
        details.append(f"<t:{match['ended_at']}:R>")

    embed = discord.Embed(
        title=f"{title_prefix} — {match['champion']}",
        description=" • ".join(details),
        color=color
    )
    embed.set_author(name=f"{name} ({region.upper()})")
    embed.add_field(name="KDA", value=f"{kills}/{deaths}/{assists}\n{kda_ratio} ratio", inline=True)
    embed.add_field(name="CS", value=f"{match['cs']}\n{cs_per_min:.1f}/min", inline=True)
    embed.add_field(name="Kill Part.", value=f"{match['kill_participation']:.0f}%", inline=True)
    embed.add_field(name="Damage", value=f"{match['damage']:,}", inline=True)
    embed.add_field(name="Vision", value=str(match["vision_score"]), inline=True)
    embed.add_field(name="Gold", value=f"{match['gold']:,}", inline=True)

    icon_url = await get_champion_icon_url(match["champion_key"])
    if icon_url:
        embed.set_thumbnail(url=icon_url)

    await interaction.followup.send(embed=embed)

# ==================== ITEM LOOKUP (Data Dragon) ====================
# Item data comes from Riot's Data Dragon (the official static-data CDN).
# Not a Riot API call, so it costs nothing against your rate limit. The
# schema used here (name / plaintext / description / gold / from / into /
# maps) matches Data Dragon's documented item format, but this has not been
# run against the live host from the build environment — if /item ever
# misbehaves, the log lines in _load_item_data_sync say what came back.
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_ITEM_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"
DDRAGON_ITEM_ICON_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/img/item/{item_id}.png"
SUMMONERS_RIFT_MAP_ID = "11"

_ITEM_CACHE = {"loaded_at": 0.0, "version": None, "items": {}}
_background_tasks = set()

def _clean_item_text(raw: str) -> str:
    """Data Dragon descriptions are HTML-ish (<br>, <stats>, <attention>...).
    Turn <br> into newlines, strip the rest, unescape entities, tidy spacing."""
    text = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()

def _load_item_data_sync(force: bool = False) -> None:
    now = datetime.now(timezone.utc).timestamp()
    if not force and _ITEM_CACHE["items"] and now - _ITEM_CACHE["loaded_at"] < ITEM_CACHE_TTL_SECONDS:
        return
    try:
        versions_resp = requests.get(DDRAGON_VERSIONS_URL, timeout=10)
        versions_resp.raise_for_status()
        version = versions_resp.json()[0]

        item_resp = requests.get(DDRAGON_ITEM_URL.format(version=version), timeout=15)
        item_resp.raise_for_status()
        payload = item_resp.json()

        # Normally the items live under "data", keyed by item ID; tolerate a
        # bare ID->item mapping too rather than assuming.
        raw_items = payload.get("data", payload)
        items = {
            str(item_id): entry
            for item_id, entry in raw_items.items()
            if isinstance(entry, dict) and entry.get("name")
        }
        if not items:
            logging.warning(f"Item data fetched but no items parsed. Top-level keys: {list(payload.keys())[:10]}")
            return

        _ITEM_CACHE.update(loaded_at=now, version=version, items=items)
        logging.info(f"Loaded {len(items)} items from Data Dragon (patch {version}).")
    except Exception:
        logging.warning("Could not load item data from Data Dragon", exc_info=True)

async def ensure_item_data() -> bool:
    await asyncio.to_thread(_load_item_data_sync)
    return bool(_ITEM_CACHE["items"])

def _item_is_primary(entry: dict) -> bool:
    """Prefer items you can actually buy on Summoner's Rift over the many
    mode-specific copies that share names."""
    gold = entry.get("gold") or {}
    maps = entry.get("maps") or {}
    return (
        bool(gold.get("purchasable", True))
        and bool(maps.get(SUMMONERS_RIFT_MAP_ID, True))
        and entry.get("inStore", True) is not False
    )

def find_items_by_name(query: str) -> list:
    q = query.strip().lower()
    if not q:
        return []
    items = _ITEM_CACHE["items"]
    exact = [(i, e) for i, e in items.items() if e["name"].lower() == q]
    pool = exact or [(i, e) for i, e in items.items() if q in e["name"].lower()]
    pool.sort(key=lambda ie: (not _item_is_primary(ie[1]), len(ie[1]["name"]), ie[1]["name"]))
    return pool

def _item_name(item_id) -> str:
    return _ITEM_CACHE["items"].get(str(item_id), {}).get("name", f"Item {item_id}")

def _format_item_names(item_ids: list, with_counts: bool = False, limit: int | None = None) -> str:
    order, counts = [], {}
    for item_id in item_ids:
        n = _item_name(item_id)
        if n not in counts:
            counts[n] = 0
            order.append(n)
        counts[n] += 1
    labels = [f"{n} ×{counts[n]}" if with_counts and counts[n] > 1 else n for n in order]
    if limit is not None and len(labels) > limit:
        extra = len(labels) - limit
        labels = labels[:limit] + [f"…and {extra} more"]
    return ", ".join(labels)

@tree.command(name="item", description="Look up a League of Legends item: cost, stats and build path")
@app_commands.describe(name="Item name (e.g. 'Infinity Edge')")
async def item_info(interaction: discord.Interaction, name: str):
    await interaction.response.defer(thinking=True)

    if not await ensure_item_data():
        await interaction.followup.send("❌ Couldn't load item data right now. Try again shortly.")
        return

    matches = find_items_by_name(name)
    if not matches:
        await interaction.followup.send(f"❌ No item found matching '{name}'.")
        return

    item_id, entry = matches[0]
    items = _ITEM_CACHE["items"]
    version = _ITEM_CACHE["version"]
    gold = entry.get("gold") or {}

    tagline = (entry.get("plaintext") or "").strip()
    body = _clean_item_text(entry.get("description", ""))
    description = "\n\n".join(part for part in (f"*{tagline}*" if tagline else "", body) if part)
    if len(description) > 1800:
        description = description[:1800].rsplit(" ", 1)[0] + "…"

    embed = discord.Embed(
        title=entry["name"],
        description=description or "No description available.",
        color=0xC89B3C
    )
    embed.set_thumbnail(url=DDRAGON_ITEM_ICON_URL.format(version=version, item_id=item_id))

    components = entry.get("from") or []
    if gold.get("total") is not None:
        cost = f"{gold['total']:,}g"
        if components and gold.get("base"):
            cost += f" ({gold['base']:,}g recipe)"
        embed.add_field(name="Cost", value=cost, inline=True)
    if gold.get("sell") is not None:
        embed.add_field(name="Sells for", value=f"{gold['sell']:,}g", inline=True)

    if components:
        embed.add_field(name="Builds from", value=_format_item_names(components, with_counts=True)[:1024], inline=False)

    into = entry.get("into") or []
    primary_into = [i for i in into if _item_is_primary(items.get(str(i), {}))] or into
    if primary_into:
        embed.add_field(name="Builds into", value=_format_item_names(primary_into, limit=8)[:1024], inline=False)

    footer = [f"Patch {version}"]
    if gold.get("purchasable") is False:
        footer.append("Not purchasable")
    other_names = []
    for _, e in matches[1:]:
        if e["name"] != entry["name"] and e["name"] not in other_names:
            other_names.append(e["name"])
    if other_names and entry["name"].lower() != name.strip().lower():
        footer.append("Other matches: " + ", ".join(other_names[:4]))
    embed.set_footer(text=" • ".join(footer)[:2048])

    await interaction.followup.send(embed=embed)

@item_info.autocomplete("name")
async def item_name_autocomplete(interaction: discord.Interaction, current: str):
    items = _ITEM_CACHE["items"]
    if not items:
        return []  # cache still warming; the command itself will load it
    q = current.strip().lower()
    seen, choices = set(), []
    for _, e in sorted(items.items(), key=lambda ie: ie[1]["name"]):
        n = e["name"]
        if n in seen or not _item_is_primary(e) or (q and q not in n.lower()):
            continue
        seen.add(n)
        choices.append(app_commands.Choice(name=n[:100], value=n[:100]))
        if len(choices) >= 25:
            break
    return choices

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

    # Riot changed this response's field names at some point without
    # updating public docs — it used to be "freeChampionIds", now it's the
    # shorter "sr" (Summoner's Rift). Falling back to the old name too in
    # case this reverts or varies by region/key.
    champ_ids = data.get("sr") or data.get("freeChampionIds") or []
    if not champ_ids:
        logging.warning(f"/freerotation got no free champion list — raw response: {data}")
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

def _fetch_patch_intro_blurb_sync(article_url: str, max_chars: int = 400) -> str | None:
    """Fetch Riot's actual article page directly (the feed itself carries
    no body text — confirmed empty on every item) and pull the intro
    blockquote out of its static HTML. Unlike the highlights graphic, this
    text is genuinely server-rendered, not JS-injected — confirmed by
    seeing it directly in a plain page fetch. Returns None (never raises)
    if the page can't be fetched or the expected markup isn't found."""
    try:
        resp = requests.get(article_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        match = re.search(r"<blockquote[^>]*>(.*?)</blockquote>", resp.text, re.IGNORECASE | re.DOTALL)
        if not match:
            logging.info(f"No <blockquote> found in article HTML for {article_url}")
            return None
        text = re.sub(r"<[^>]+>", " ", match.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + "…"
        return text
    except Exception:
        logging.warning(f"Could not fetch/parse article page for intro blurb: {article_url}", exc_info=True)
        return None

async def get_patch_intro_blurb(item: dict, max_chars: int = 400) -> str:
    """Get a real intro blurb for the patch article. The feed's own
    content_text/content_html are empty for every item (confirmed via
    logs), so this fetches Riot's actual page directly instead. Falls
    back to the feed's short one-line 'summary' if that fetch/parse fails."""
    url = item.get("url")
    if url:
        blurb = await asyncio.to_thread(_fetch_patch_intro_blurb_sync, url, max_chars)
        if blurb:
            return blurb
    return item.get("summary") or ""

def get_patch_highlight_image(item: dict) -> str | None:
    """Attempt to find the article's 'patch highlights' nerfs/buffs/new-skins
    summary graphic among its inline content images. The first inline image
    turned out to be the wrong one (article splash art, not the highlights
    graphic) — logging every candidate URL here so the right one can be
    identified with certainty instead of guessed at again."""
    html = item.get("content_html") or ""
    matches = re.findall(r'<img[^>]+src="([^"]+)"', html, re.IGNORECASE)
    logging.info(f"Patch image candidates for '{item.get('title')}': {matches}")
    if matches:
        return matches[0]
    logging.warning(f"No inline image found in content_html for '{item.get('title')}' — falling back to feed image field.")
    return item.get("image") or item.get("banner_image")

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

    patch_item = next((item for item in items if re.search(r"patch\s+[\d.]+.*notes", item.get("title", ""), re.IGNORECASE)), None)
    if not patch_item:
        await interaction.followup.send("❌ Couldn't find a recent patch notes article.")
        return

    blurb = await get_patch_intro_blurb(patch_item)
    logging.info(f"Patch blurb extracted ({len(blurb)} chars): {blurb[:120]!r}")

    embed = discord.Embed(
        title=patch_item.get("title", "Patch Notes"),
        url=patch_item.get("url"),
        description=blurb,
        color=0x1E90FF
    )
    image = get_patch_highlight_image(patch_item)
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

    # Give them the matching rank role right away (does nothing unless rank
    # roles have been turned on for this server with /setrankroles).
    if baseline and isinstance(interaction.user, discord.Member):
        await apply_rank_role(interaction.guild, interaction.user, baseline["tier"])
    # Match their nickname to their League name (no-op unless /setnicknames is on).
    if isinstance(interaction.user, discord.Member):
        await apply_nickname(interaction.guild, interaction.user, name)

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

# ==================== LP DIGEST ====================
async def build_and_post_digest(guild_id: str, guild: discord.Guild, channel: discord.TextChannel):
    """Fetch every registered player's current rank, diff it against their
    stored baseline, post a summary, then reset the baseline for the next
    period. Costs one fetch per registered player — same pattern as
    /leaderboard, just run on a schedule instead of on demand."""
    players = load_players()
    guild_players = players.get(guild_id, {})
    if not guild_players:
        return

    results = []
    for user_id, info in guild_players.items():
        try:
            data = await fetch_ranked_solo(info["name"], info["region"])
        except Exception:
            logging.warning(f"Digest: could not fetch {info['name']}", exc_info=True)
            await asyncio.sleep(0.3)
            continue

        start_wins = info.get("digest_start_wins")
        start_losses = info.get("digest_start_losses")

        if start_wins is not None:
            games_won = data["wins"] - start_wins
            games_lost = data["losses"] - start_losses
            same_rank = (
                info.get("digest_start_tier") == data["tier"]
                and info.get("digest_start_division") == data["division"]
            )
            lp_change = (data["lp"] - info.get("digest_start_lp", data["lp"])) if same_rank else None
            results.append({
                "user_id": user_id, "name": info["name"], "data": data,
                "games_won": games_won, "games_lost": games_lost,
                "lp_change": lp_change, "rank_changed": not same_rank,
            })

        # Reset baseline for the next period regardless (covers both a
        # reported player and one being baselined for the first time).
        info["digest_start_tier"] = data["tier"]
        info["digest_start_division"] = data["division"]
        info["digest_start_lp"] = data["lp"]
        info["digest_start_wins"] = data["wins"]
        info["digest_start_losses"] = data["losses"]
        await asyncio.sleep(0.3)

    save_players(players)

    active = [r for r in results if (r["games_won"] + r["games_lost"]) > 0]
    if not active:
        embed = discord.Embed(title="📊 LP Digest", description="No ranked games played this period.", color=0x95A5A6)
        await channel.send(embed=embed)
        return

    active.sort(key=lambda r: (r["lp_change"] if r["lp_change"] is not None else 0), reverse=True)

    lines = []
    for r in active:
        member = guild.get_member(int(r["user_id"]))
        display = member.mention if member else r["name"]
        division = "" if r["data"]["tier"] in APEX_TIERS else f" {r['data']['division']}"
        rank_str = f"{r['data']['tier'].title()}{division} • {r['data']['lp']} LP"
        if r["rank_changed"]:
            change_str = "📈 rank changed"
        elif r["lp_change"] is not None:
            change_str = f"{'+' if r['lp_change'] >= 0 else ''}{r['lp_change']} LP"
        else:
            change_str = ""
        lines.append(f"**{display}** — {r['games_won']}W {r['games_lost']}L ({change_str}) — now {rank_str}")

    embed = discord.Embed(
        title="📊 LP Digest",
        description="\n".join(lines)[:4096],
        color=0x5865F2
    )
    embed.set_footer(text=f"{len(active)} player(s) played this period")
    await channel.send(embed=embed)

async def do_check_digests():
    config = load_config()
    now = datetime.now(timezone.utc)

    for guild_id, guild_cfg in list(config.items()):
        if guild_id == "_meta":
            continue
        channel_id = guild_cfg.get("digest_channel_id")
        frequency = guild_cfg.get("digest_frequency")
        last_posted_str = guild_cfg.get("digest_last_posted")
        if not channel_id or not frequency or not last_posted_str:
            continue

        try:
            last_posted = datetime.fromisoformat(last_posted_str)
        except ValueError:
            continue

        days = DIGEST_FREQUENCY_DAYS.get(frequency, 1)
        if now - last_posted < timedelta(days=days):
            continue

        channel = bot.get_channel(channel_id)
        guild = bot.get_guild(int(guild_id))
        if channel is None or guild is None:
            continue

        try:
            await build_and_post_digest(guild_id, guild, channel)
        except Exception:
            logging.exception(f"Error building digest for guild {guild_id}")
            continue

        config = load_config()
        config.setdefault(guild_id, {})
        config[guild_id]["digest_last_posted"] = now.isoformat()
        save_config(config)

@tasks.loop(minutes=DIGEST_CHECK_INTERVAL_MINUTES)
async def check_digests():
    await do_check_digests()

@check_digests.before_loop
async def before_check_digests():
    await bot.wait_until_ready()

@tree.command(name="setdigestchannel", description="Set the channel + frequency for automatic LP digest summaries (admin only)")
@app_commands.describe(channel="Channel for the digest", frequency="How often to post it")
@app_commands.choices(frequency=[
    app_commands.Choice(name="Daily", value="daily"),
    app_commands.Choice(name="Weekly", value="weekly"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def setdigestchannel(interaction: discord.Interaction, channel: discord.TextChannel, frequency: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id)
    config = load_config()
    config.setdefault(guild_id, {})
    config[guild_id]["digest_channel_id"] = channel.id
    config[guild_id]["digest_frequency"] = frequency.value
    config[guild_id]["digest_last_posted"] = datetime.now(timezone.utc).isoformat()
    save_config(config)

    # Snapshot a baseline for everyone currently registered, so the first
    # digest measures from right now rather than from account creation.
    players = load_players()
    guild_players = players.get(guild_id, {})
    updated = False
    for user_id, info in guild_players.items():
        try:
            data = await fetch_ranked_solo(info["name"], info["region"])
            info["digest_start_tier"] = data["tier"]
            info["digest_start_division"] = data["division"]
            info["digest_start_lp"] = data["lp"]
            info["digest_start_wins"] = data["wins"]
            info["digest_start_losses"] = data["losses"]
            updated = True
        except Exception:
            logging.warning(f"Could not snapshot baseline for {info['name']} during digest setup", exc_info=True)
        await asyncio.sleep(0.3)
    if updated:
        save_players(players)

    days = DIGEST_FREQUENCY_DAYS[frequency.value]
    await interaction.followup.send(
        f"✅ A {frequency.value} LP digest will now be posted in {channel.mention} "
        f"(roughly every {days} day{'s' if days != 1 else ''} from now — not pinned to a fixed clock time).",
        ephemeral=True
    )

@setdigestchannel.error
async def setdigestchannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to set the digest channel.", ephemeral=True
        )
    else:
        logging.exception("Error in /setdigestchannel")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)

@tree.command(name="forcedigest", description="Manually post the LP digest right now (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def forcedigest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id)
    config = load_config()
    channel_id = config.get(guild_id, {}).get("digest_channel_id")
    if not channel_id:
        await interaction.followup.send("❌ No digest channel set — use `/setdigestchannel` first.", ephemeral=True)
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        await interaction.followup.send("❌ Can't access the configured digest channel.", ephemeral=True)
        return

    await build_and_post_digest(guild_id, interaction.guild, channel)

    config = load_config()
    config.setdefault(guild_id, {})
    config[guild_id]["digest_last_posted"] = datetime.now(timezone.utc).isoformat()
    save_config(config)

    await interaction.followup.send("✅ Digest posted.", ephemeral=True)

@forcedigest.error
async def forcedigest_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to force a digest.", ephemeral=True
        )
    else:
        logging.error(f"Error in /forcedigest: {error}", exc_info=error)
        try:
            await interaction.followup.send("❌ Something went wrong — check the bot logs for details.", ephemeral=True)
        except discord.HTTPException:
            pass

# ==================== RANK ROLES ====================
# Gives each registered player a Discord role matching their current
# Solo/Duo tier. Per-guild config: "rank_roles_enabled" (bool) and
# "rank_roles" ({TIER: role_id}). Members that aren't in the cache are
# fetched from Discord directly, so this doesn't need the privileged
# "members" intent.

async def get_guild_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None  # no longer in the server
    except discord.HTTPException:
        logging.warning(f"Could not fetch member {user_id} in guild {guild.id}", exc_info=True)
        return None

def _rank_role_config(guild: discord.Guild) -> dict | None:
    cfg = load_config().get(str(guild.id), {})
    if not cfg.get("rank_roles_enabled") or not cfg.get("rank_roles"):
        return None
    return cfg["rank_roles"]

async def apply_rank_role(guild: discord.Guild, member: discord.Member, tier: str) -> bool:
    """Give `member` the role for `tier` and take away any other rank role.
    Never raises. Returns True if they end up with the right role, False if
    rank roles are off, the role is gone, or Discord refused."""
    role_map = _rank_role_config(guild)
    if not role_map or tier not in role_map:
        return False

    target = guild.get_role(role_map[tier])
    if target is None:
        logging.warning(f"Rank role for {tier} no longer exists in guild {guild.id} — run /setrankroles again.")
        return False

    rank_role_ids = set(role_map.values())
    stale = [r for r in member.roles if r.id in rank_role_ids and r.id != target.id]
    try:
        if stale:
            await member.remove_roles(*stale, reason="LP Tracker: rank changed")
        if target not in member.roles:
            await member.add_roles(target, reason="LP Tracker: rank role")
        return True
    except discord.Forbidden:
        logging.warning(f"Not allowed to manage rank roles in guild {guild.id} (is my role above the rank roles?)")
        return False
    except discord.HTTPException:
        logging.warning(f"Discord error while setting rank role for member {member.id}", exc_info=True)
        return False

async def remove_rank_roles(guild: discord.Guild, member: discord.Member) -> None:
    role_map = _rank_role_config(guild)
    if not role_map:
        return
    rank_role_ids = set(role_map.values())
    held = [r for r in member.roles if r.id in rank_role_ids]
    if not held:
        return
    try:
        await member.remove_roles(*held, reason="LP Tracker: unregistered")
    except discord.HTTPException:
        logging.warning(f"Could not remove rank roles from member {member.id}", exc_info=True)

async def sync_rank_roles_for_guild(guild: discord.Guild) -> dict:
    """Look up every registered player in this server and apply their rank
    role. Costs 2 Riot API calls per registered player. Unranked players
    and people who left the server are skipped, not stripped."""
    counts = {"synced": 0, "skipped": 0, "failed": 0}
    players = load_players().get(str(guild.id), {})

    for user_id, info in players.items():
        member = await get_guild_member(guild, int(user_id))
        if member is None:
            counts["skipped"] += 1
            continue
        try:
            data = await fetch_ranked_solo(info["name"], info["region"])
            ok = await apply_rank_role(guild, member, data["tier"])
            counts["synced" if ok else "failed"] += 1
        except LPLookupError:
            counts["skipped"] += 1
        except Exception:
            logging.warning(f"Rank role sync: could not process {info['name']}", exc_info=True)
            counts["failed"] += 1
        await asyncio.sleep(0.3)

    return counts

@tasks.loop(minutes=ROLE_SYNC_INTERVAL_MINUTES)
async def sync_rank_roles_loop():
    config = load_config()
    for guild_id, guild_cfg in list(config.items()):
        if guild_id == "_meta" or not guild_cfg.get("rank_roles_enabled"):
            continue
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            continue
        try:
            counts = await sync_rank_roles_for_guild(guild)
            logging.info(f"Rank role sync for guild {guild_id}: {counts}")
        except Exception:
            logging.exception(f"Rank role sync failed for guild {guild_id}")

@sync_rank_roles_loop.before_loop
async def before_sync_rank_roles_loop():
    await bot.wait_until_ready()

@tree.command(name="setrankroles", description="Turn automatic rank roles on or off for registered players (admin only)")
@app_commands.describe(enabled="On (default) creates the tier roles and starts assigning them; off stops, leaving roles in place")
@app_commands.checks.has_permissions(manage_guild=True, manage_roles=True)
async def setrankroles(interaction: discord.Interaction, enabled: bool = True):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    guild_id = str(guild.id)
    config = load_config()
    config.setdefault(guild_id, {})

    if not enabled:
        config[guild_id]["rank_roles_enabled"] = False
        save_config(config)
        await interaction.followup.send(
            "✅ Automatic rank roles are off. The existing roles are left in place — "
            "delete them in Server Settings if you don't want them, or run this again to turn it back on.",
            ephemeral=True
        )
        return

    if not guild.me.guild_permissions.manage_roles:
        await interaction.followup.send("❌ I need the 'Manage Roles' permission to create and assign rank roles.", ephemeral=True)
        return

    role_map = {}
    created, reused, too_high = [], [], []
    try:
        for tier in TIER_ORDER:
            role_name = f"{RANK_ROLE_PREFIX}{tier.title()}"
            role = discord.utils.get(guild.roles, name=role_name)
            if role is None:
                role = await guild.create_role(
                    name=role_name,
                    colour=discord.Colour(get_rank_color(tier)),
                    reason="LP Tracker rank roles"
                )
                created.append(role_name)
            else:
                reused.append(role_name)
                if role >= guild.me.top_role:
                    too_high.append(role_name)
            role_map[tier] = role.id
    except discord.Forbidden:
        await interaction.followup.send("❌ Discord refused to create the roles — check my permissions and try again.", ephemeral=True)
        return
    except discord.HTTPException:
        logging.exception("Error creating rank roles")
        await interaction.followup.send("❌ Something went wrong creating the roles — try again shortly.", ephemeral=True)
        return

    config = load_config()
    config.setdefault(guild_id, {})
    config[guild_id]["rank_roles"] = role_map
    config[guild_id]["rank_roles_enabled"] = True
    save_config(config)

    counts = await sync_rank_roles_for_guild(guild)

    lines = [
        f"✅ Rank roles are on. Created {len(created)} new role(s), reused {len(reused)} that already existed.",
        f"First sync: {counts['synced']} updated, {counts['skipped']} skipped (unranked or left the server), {counts['failed']} failed.",
        f"Roles re-sync every {ROLE_SYNC_INTERVAL_MINUTES} min, and right away when someone registers.",
    ]
    if too_high:
        lines.append(f"⚠️ My role sits below these existing roles, so I can't assign them: {', '.join(too_high)}.")
    if counts["failed"]:
        lines.append("Failures usually mean my role is below the rank roles — drag it above them in Server Settings → Roles.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@setrankroles.error
async def setrankroles_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' and 'Manage Roles' permissions to set up rank roles.", ephemeral=True
        )
    else:
        logging.error(f"Error in /setrankroles: {error}", exc_info=error)

@tree.command(name="forcerolesync", description="Re-sync everyone's rank role right now (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def forcerolesync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if _rank_role_config(interaction.guild) is None:
        await interaction.followup.send("❌ Rank roles aren't turned on here — use `/setrankroles` first.", ephemeral=True)
        return

    counts = await sync_rank_roles_for_guild(interaction.guild)
    await interaction.followup.send(
        f"✅ Synced: {counts['synced']} updated, {counts['skipped']} skipped, {counts['failed']} failed.",
        ephemeral=True
    )

@forcerolesync.error
async def forcerolesync_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to force a role sync.", ephemeral=True
        )
    else:
        logging.error(f"Error in /forcerolesync: {error}", exc_info=error)
        try:
            await interaction.followup.send("❌ Something went wrong — check the bot logs for details.", ephemeral=True)
        except discord.HTTPException:
            pass

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

# ==================== CAPABILITIES (LIVING DOCUMENT) ====================
# Manually maintained grouping/order — command descriptions themselves are
# pulled live from the tree below, so wording never goes stale, but a brand
# new command won't appear here until it's added to one of these lists.
CAPABILITIES_CATEGORIES = {
    "🎮 League of Legends": ["lp", "lastgame", "matchhistory", "compare", "register", "unregister", "leaderboard", "mastery", "livegame", "freerotation", "serverstatus", "champion", "item"],
    "📰 News": ["news", "patchnotes"],
}

async def build_capabilities_embed() -> discord.Embed:
    # Read from the guild-scoped command list, not the global one — the
    # global list gets intentionally emptied in on_ready (see the comment
    # there about clearing stale Discord-side global command registrations),
    # so it's empty by the time this runs. The guild-scoped copy isn't
    # affected by that and reflects what's actually live in the server.
    all_commands = {cmd.name: cmd for cmd in tree.get_commands(guild=TEST_GUILD)}

    embed = discord.Embed(
        title="🤖 LP Tracker — Current Capabilities",
        description="Live command list — this message edits itself, it's not a static snapshot.",
        color=0x5865F2
    )
    for category, names in CAPABILITIES_CATEGORIES.items():
        lines = [f"`/{name}` — {all_commands[name].description}" for name in names if name in all_commands]
        if lines:
            embed.add_field(name=category, value="\n".join(lines), inline=False)

    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text="Last refreshed")
    return embed

async def refresh_capabilities_message(guild_id: str) -> str:
    """Edit the stored capabilities message in place. Returns a short
    human-readable result, used both by /updatecapabilities and the
    automatic on-startup refresh."""
    config = load_config()
    guild_cfg = config.get(guild_id, {})
    channel_id = guild_cfg.get("capabilities_channel_id")
    message_id = guild_cfg.get("capabilities_message_id")
    if not channel_id or not message_id:
        return "No capabilities message set up yet — use `/setcapabilities` first."

    channel = bot.get_channel(channel_id)
    if channel is None:
        return "Can't access the configured channel anymore."

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return "The capabilities message was deleted — run `/setcapabilities` again to recreate it."
    except discord.Forbidden:
        return "I don't have permission to read messages in that channel."

    embed = await build_capabilities_embed()
    try:
        await message.edit(embed=embed)
    except discord.Forbidden:
        return "I don't have permission to edit that message."

    return "Capabilities message refreshed."

async def refresh_all_capabilities_messages():
    """Called on every bot startup so the living document self-heals after
    a redeploy adds/changes commands, without anyone needing to remember to run /updatecapabilities."""
    config = load_config()
    for guild_id in list(config.keys()):
        if guild_id == "_meta" or not config[guild_id].get("capabilities_message_id"):
            continue
        result = await refresh_capabilities_message(guild_id)
        logging.info(f"Capabilities auto-refresh for guild {guild_id}: {result}")

@tree.command(name="setcapabilities", description="Post a self-updating 'current capabilities' message in a channel (admin only)")
@app_commands.describe(channel="The channel to post the living capabilities message in")
@app_commands.checks.has_permissions(manage_guild=True)
async def setcapabilities(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)

    embed = await build_capabilities_embed()
    try:
        message = await channel.send(embed=embed)
    except discord.Forbidden:
        await interaction.followup.send(f"❌ I don't have permission to send messages in {channel.mention}.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    config = load_config()
    config.setdefault(guild_id, {})
    config[guild_id]["capabilities_channel_id"] = channel.id
    config[guild_id]["capabilities_message_id"] = message.id
    save_config(config)

    await interaction.followup.send(
        f"✅ Posted in {channel.mention}. It refreshes automatically on every bot restart, "
        f"or run `/updatecapabilities` any time to refresh it on demand.",
        ephemeral=True
    )

@setcapabilities.error
async def setcapabilities_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to set up the capabilities message.", ephemeral=True
        )
    else:
        logging.exception("Error in /setcapabilities")

@tree.command(name="updatecapabilities", description="Refresh the living capabilities message now (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def updatecapabilities(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await refresh_capabilities_message(str(interaction.guild_id))
    await interaction.followup.send(f"{'✅' if result == 'Capabilities message refreshed.' else '❌'} {result}", ephemeral=True)

@updatecapabilities.error
async def updatecapabilities_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to refresh the capabilities message.", ephemeral=True
        )
    else:
        logging.exception("Error in /updatecapabilities")

@tree.command(name="unregister", description="Remove yourself from this server's leaderboard")
async def unregister(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    players = load_players()
    if guild_id not in players or str(interaction.user.id) not in players[guild_id]:
        await interaction.response.send_message("You're not registered here.", ephemeral=True)
        return
    removed_name = players[guild_id][str(interaction.user.id)].get("name")
    del players[guild_id][str(interaction.user.id)]
    save_players(players)
    await interaction.response.send_message("✅ You've been removed from the leaderboard.", ephemeral=True)

    # Strip any rank role afterwards (the reply above has to go out within
    # Discord's 3-second window, so this comes second).
    if isinstance(interaction.user, discord.Member):
        await remove_rank_roles(interaction.guild, interaction.user)
        await reset_nickname(interaction.guild, interaction.user, info_name=removed_name)

# ==================== AUTO NICKNAMES ====================
# Sets a registered member's server nickname to their League game name
# (the part before the #). Per-guild config: "nickname_sync_enabled".
# Uses the name stored at /register, so it costs no Riot API calls.

def league_nickname(riot_id: str) -> str:
    return riot_id.split("#", 1)[0].strip()[:32]

def _nickname_sync_enabled(guild: discord.Guild) -> bool:
    return bool(load_config().get(str(guild.id), {}).get("nickname_sync_enabled"))

async def apply_nickname(guild: discord.Guild, member: discord.Member, riot_id: str) -> str:
    """Returns 'set', 'unchanged', 'off' or 'failed'. Never raises."""
    if not _nickname_sync_enabled(guild):
        return "off"
    nick = league_nickname(riot_id)
    if not nick:
        return "failed"
    if member.nick == nick:
        return "unchanged"
    try:
        await member.edit(nick=nick, reason="LP Tracker: match League name")
        return "set"
    except discord.Forbidden:
        # Server owner, or someone whose top role is above mine.
        return "failed"
    except discord.HTTPException:
        logging.warning(f"Could not set nickname for member {member.id}", exc_info=True)
        return "failed"

async def reset_nickname(guild: discord.Guild, member: discord.Member, info_name: str | None) -> None:
    """On unregister, clear the nickname only if it's the one we set."""
    if not info_name or not _nickname_sync_enabled(guild):
        return
    if member.nick != league_nickname(info_name):
        return
    try:
        await member.edit(nick=None, reason="LP Tracker: unregistered")
    except discord.HTTPException:
        logging.warning(f"Could not reset nickname for member {member.id}", exc_info=True)

@tree.command(name="setnicknames", description="Auto-set registered players' nicknames to their League name (admin only)")
@app_commands.describe(enabled="On (default) renames registered players now and when they register; off stops")
@app_commands.checks.has_permissions(manage_guild=True, manage_nicknames=True)
async def setnicknames(interaction: discord.Interaction, enabled: bool = True):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    guild_id = str(guild.id)

    config = load_config()
    config.setdefault(guild_id, {})
    config[guild_id]["nickname_sync_enabled"] = enabled
    save_config(config)

    if not enabled:
        await interaction.followup.send("✅ Auto nicknames are off. Existing nicknames are left as they are.", ephemeral=True)
        return

    if not guild.me.guild_permissions.manage_nicknames:
        await interaction.followup.send("❌ I need the 'Manage Nicknames' permission first.", ephemeral=True)
        return

    counts = {"set": 0, "unchanged": 0, "failed": 0, "gone": 0}
    for user_id, info in load_players().get(guild_id, {}).items():
        member = await get_guild_member(guild, int(user_id))
        if member is None:
            counts["gone"] += 1
            continue
        result = await apply_nickname(guild, member, info["name"])
        counts[result] += 1
        await asyncio.sleep(0.3)

    lines = [
        f"✅ Auto nicknames are on. {counts['set']} renamed, {counts['unchanged']} already matched.",
        "New registrations are renamed automatically.",
    ]
    if counts["failed"]:
        lines.append(
            f"⚠️ Couldn't rename {counts['failed']} member(s) — Discord never lets bots rename the server owner, "
            "and my role must sit above the member's top role."
        )
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@setnicknames.error
async def setnicknames_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' and 'Manage Nicknames' permissions.", ephemeral=True
        )
    else:
        logging.error(f"Error in /setnicknames: {error}", exc_info=error)

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

    patch_item = next((item for item in items if re.search(r"patch\s+[\d.]+.*notes", item.get("title", ""), re.IGNORECASE)), None)
    if not patch_item:
        logging.info(f"Patch poll: no article titled 'Patch...' found among {len(items)} news items.")
        return

    patch_url = patch_item.get("url")
    meta = config.get("_meta", {})
    logging.info(f"Patch poll: latest article is '{patch_item.get('title')}' ({patch_url}). Last announced: {meta.get('last_patch_url')}")

    if meta.get("last_patch_url") == patch_url:
        logging.info("Patch poll: no change since last check.")
        return  # already announced (or this is still the known-latest patch)

    is_first_check = "last_patch_url" not in meta
    config["_meta"] = {"last_patch_url": patch_url, "last_patch_title": patch_item.get("title")}
    save_config(config)

    if is_first_check:
        # Don't blast the currently-latest patch to every newly-configured
        # channel — just record it as the baseline, same as LP registration.
        logging.info("Patch poll: first check ever, recorded baseline, not announcing.")
        return

    patch_channels = [
        (guild_id, guild_cfg.get("patch_channel_id"))
        for guild_id, guild_cfg in config.items()
        if guild_id != "_meta" and guild_cfg.get("patch_channel_id")
    ]
    logging.info(f"Patch poll: NEW patch detected, announcing to {len(patch_channels)} configured channel(s).")

    blurb = await get_patch_intro_blurb(patch_item)
    logging.info(f"Patch blurb extracted ({len(blurb)} chars): {blurb[:120]!r}")

    embed = discord.Embed(
        title=patch_item.get("title", "Patch Notes"),
        url=patch_url,
        description=blurb,
        color=0x1E90FF
    )
    image = get_patch_highlight_image(patch_item)
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
            logging.warning(f"Patch poll: bot.get_channel({channel_id}) returned None — bot may not have access to this channel.")
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
        logging.error(f"Error in /forcepatchcheck: {error}", exc_info=error)
        try:
            await interaction.followup.send("❌ Something went wrong — check the bot logs for details.", ephemeral=True)
        except discord.HTTPException:
            pass

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
        logging.error(f"Error in /forceupdate: {error}", exc_info=error)
        try:
            await interaction.followup.send("❌ Something went wrong — check the bot logs for details.", ephemeral=True)
        except discord.HTTPException:
            pass

# ==================== START ====================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("No DISCORD_TOKEN found in environment!")
    logging.info("Starting bot...")
    bot.run(TOKEN)
