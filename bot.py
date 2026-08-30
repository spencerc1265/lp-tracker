import discord
import requests
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

async def fetch_ranked_solo(name: str, region: str, fetch_icon: bool = False) -> dict:
    """Look up a Riot ID's Ranked Solo/Duo entry.
    Raises LPLookupError with a user-facing message on expected failures.
    If fetch_icon is True, also fetches the summoner's profile icon ID
    (costs one extra Riot API call — skip it for bulk lookups like the leaderboard)."""
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

    # 1. Riot ID -> PUUID (Account-V1, regional routing)
    account_url = (
        f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/"
        f"by-riot-id/{game_name}/{tag_line}"
    )
    account_resp = requests.get(account_url, headers=headers)
    if account_resp.status_code == 404:
        raise LPLookupError(f"Riot ID '{name}' not found.")
    account_resp.raise_for_status()
    puuid = account_resp.json()["puuid"]

    # Optional: profile icon ID (Summoner-V4 still returns this field, even
    # though it no longer returns the encrypted "id" summoner ID).
    profile_icon_id = None
    if fetch_icon:
        try:
            summoner_url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
            summoner_resp = requests.get(summoner_url, headers=headers)
            if summoner_resp.ok:
                profile_icon_id = summoner_resp.json().get("profileIconId")
        except Exception:
            logging.warning("Could not fetch profile icon", exc_info=True)

    # 2. PUUID -> Ranked entries (League-V4, platform routing)
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

    return {
        "tier": solo["tier"],
        "division": solo.get("rank", ""),
        "lp": solo["leaguePoints"],
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "profile_icon_id": profile_icon_id,
        "puuid": puuid,
    }

# ==================== CHAMPION NAMES ====================
_CHAMPION_NAME_CACHE = {}

def _load_champion_names_sync():
    """Fetch and cache championName (API key, e.g. 'MonkeyKing') -> display name (e.g. 'Wukong')."""
    if _CHAMPION_NAME_CACHE:
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
            if alias and name:
                _CHAMPION_NAME_CACHE[alias] = name
    except Exception:
        logging.warning("Could not load champion name list", exc_info=True)

async def get_champion_display_name(champion_key: str) -> str:
    await asyncio.to_thread(_load_champion_names_sync)
    return _CHAMPION_NAME_CACHE.get(champion_key, champion_key)

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

def get_rank_emoji(rank):
    emoji_dict = {
        "IRON": "🛡️ Iron",
        "BRONZE": "🥉 Bronze",
        "SILVER": "🥈 Silver",
        "GOLD": "🥇 Gold",
        "PLATINUM": "💎 Platinum",
        "EMERALD": "🌟 Emerald",
        "DIAMOND": "⭐ Diamond",
        "MASTER": "👑 Master",
        "GRANDMASTER": "👑 Grandmaster",
        "CHALLENGER": "🔥 Challenger"
    }
    return emoji_dict.get(rank, "❓ Unranked")

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
    embed.add_field(name="Wins", value=f"{data['wins']} - {data['losses']}", inline=True)
    embed.add_field(name="Win Rate", value=f"{data['wr']:.1f}%", inline=True)

    try:
        emblem_file, emblem_filename = await get_cropped_emblem_file(data['tier'])
        embed.set_image(url=f"attachment://{emblem_filename}")
        await interaction.followup.send(embed=embed, file=emblem_file)
    except Exception:
        logging.exception("Could not crop emblem image, falling back to raw URL")
        embed.set_image(url=get_rank_emblem_url(data['tier']))
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
        baseline = await fetch_ranked_solo(name, region)
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

    await interaction.followup.send(f"✅ Registered **{name}** ({region.upper()}) for {interaction.user.mention}.")

@tree.command(name="setchannel", description="Set the channel for automatic LP update announcements (admin only)")
@app_commands.describe(channel="The channel where win/loss LP updates should be posted")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    config = load_config()
    config[guild_id] = {"update_channel_id": channel.id}
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

    lines = []
    for i, (user_id, name, data) in enumerate(results, start=1):
        member = interaction.guild.get_member(int(user_id))
        display = member.mention if member else name
        division = "" if data["tier"] in APEX_TIERS else f" {data['division']}"
        lines.append(f"**{i}.** {display} — {get_rank_emoji(data['tier'])}{division} • {data['lp']} LP")

    embed = discord.Embed(
        title=f"🏆 {interaction.guild.name} Ranked Leaderboard",
        description="\n".join(lines)[:4096],
        color=0xFFD700
    )
    embed.set_thumbnail(url=get_rank_emblem_url(results[0][2]["tier"]))
    if failures:
        embed.set_footer(text=f"Could not fetch: {', '.join(failures)}"[:2048])

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
                    result = "🟢 Won" if win_diff > 0 else "🔴 Lost"

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
                    lines = [
                        f"**{info['name']}** {result} a ranked game — now "
                        f"{data['tier'].title()}{division} • {data['lp']} LP{lp_note}"
                    ]

                    match_details = await fetch_last_ranked_match(data["puuid"], info["region"])
                    if match_details:
                        mins, secs = divmod(match_details["duration_seconds"], 60)
                        lines.append(
                            f"{match_details['champion']} • "
                            f"{match_details['kills']}/{match_details['deaths']}/{match_details['assists']} KDA "
                            f"• {mins}:{secs:02d}"
                        )

                    embed = discord.Embed(
                        description="\n".join(lines),
                        color=get_rank_color(data["tier"])
                    )
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
