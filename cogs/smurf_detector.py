import discord
from discord.ext import commands, tasks
from discord import app_commands
import requests
from collections import Counter
from typing import Optional
import urllib.parse
import sqlite3
import os
import asyncio
import time
import datetime
import logging

log = logging.getLogger("smurfbot")

API_KEY = os.getenv("API_KEY")  # Chargée depuis le .env par app.py
if not API_KEY:
    raise RuntimeError("API_KEY manquante : ajoute-la dans le fichier .env")
DB_FILE = "smurfs_leaderboard.db"

# ================= RÉGLAGES =================
SMURF_COOLDOWN = 15.0     # secondes entre deux /smurf par utilisateur
MATCH_COOLDOWN = 30.0     # secondes entre deux /smurf-match par utilisateur
CACHE_TTL = 600           # durée de vie d'une analyse en cache (secondes)
ANALYZED_MATCHES = 10     # nombre de matchs classés récents pris en compte (10 max côté API)
FEW_RANKED_GAMES = 40     # en dessous, un compte Platine ou plus est jugé suspect
MAX_WATCH_PER_GUILD = 5   # joueurs surveillés au maximum par serveur
WATCH_INTERVAL_MIN = 30   # minutes entre deux vérifications des joueurs surveillés
WATCH_API_DELAY = 2.0     # secondes de pause entre deux joueurs surveillés (ménage l'API)
BACKUP_DIR = "backups"
BACKUP_KEEP = 7           # nombre de sauvegardes quotidiennes conservées

# ================= BASE DE DONNÉES SQLITE =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS smurfs (
            puuid TEXT PRIMARY KEY,
            name TEXT,
            tag TEXT,
            score INTEGER,
            rank TEXT,
            level INTEGER,
            kd REAL,
            acs REAL,
            hs_percent REAL,
            last_analyzed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Une ligne par analyse, pour suivre l'évolution d'un joueur (/historique)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS smurf_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            puuid TEXT NOT NULL,
            score INTEGER,
            rank TEXT,
            level INTEGER,
            kd REAL,
            acs REAL,
            hs_percent REAL,
            analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_puuid ON smurf_history (puuid)')
    # Quels joueurs ont été analysés sur quel serveur (classement par serveur)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guild_players (
            guild_id TEXT NOT NULL,
            puuid TEXT NOT NULL,
            PRIMARY KEY (guild_id, puuid)
        )
    ''')
    # Compte Riot lié à un compte Discord (/link)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS linked_accounts (
            user_id TEXT PRIMARY KEY,
            name TEXT,
            tag TEXT
        )
    ''')
    # Joueurs surveillés (/watch)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            guild_id TEXT NOT NULL,
            puuid TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            name TEXT,
            tag TEXT,
            region TEXT,
            last_rank TEXT,
            last_elo INTEGER,
            PRIMARY KEY (guild_id, puuid)
        )
    ''')
    # Les joueurs déjà enregistrés reçoivent leur dernière analyse comme premier point d'historique
    cursor.execute('''
        INSERT INTO smurf_history (puuid, score, rank, level, kd, acs, hs_percent, analyzed_at)
        SELECT puuid, score, rank, level, kd, acs, hs_percent, last_analyzed FROM smurfs
        WHERE puuid NOT IN (SELECT puuid FROM smurf_history)
    ''')
    conn.commit()
    conn.close()

def db_query(sql, params=()):
    conn = sqlite3.connect(DB_FILE)
    # lower() de SQLite ignore les accents : on compare les pseudos avec casefold() de Python
    conn.create_function("casefold", 1, lambda s: s.casefold() if isinstance(s, str) else s)
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()

def save_or_update_smurf(puuid, name, tag, score, rank, level, game_stats):
    kd = game_stats["avg_kd"] if game_stats else 0.0
    acs = game_stats["avg_acs"] if game_stats else 0.0
    hs_pct = game_stats["avg_hs_percent"] if game_stats else 0.0

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO smurfs (puuid, name, tag, score, rank, level, kd, acs, hs_percent, last_analyzed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(puuid) DO UPDATE SET
            name=excluded.name,
            tag=excluded.tag,
            score=excluded.score,
            rank=excluded.rank,
            level=excluded.level,
            kd=excluded.kd,
            acs=excluded.acs,
            hs_percent=excluded.hs_percent,
            last_analyzed=CURRENT_TIMESTAMP
    ''', (puuid, name, tag, score, rank, level, kd, acs, hs_pct))
    cursor.execute('''
        INSERT INTO smurf_history (puuid, score, rank, level, kd, acs, hs_percent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (puuid, score, rank, level, kd, acs, hs_pct))
    conn.commit()
    conn.close()

def record_guild_player(guild_id, puuid):
    db_query("INSERT OR IGNORE INTO guild_players (guild_id, puuid) VALUES (?, ?)", (str(guild_id), puuid))

def get_top_smurfs(limit=10, guild_id=None):
    # guild_id=None : classement de tous les serveurs
    if guild_id is None:
        return db_query('''
            SELECT name, tag, score, rank, level, kd, acs, hs_percent
            FROM smurfs
            ORDER BY score DESC, kd DESC
            LIMIT ?
        ''', (limit,))
    return db_query('''
        SELECT name, tag, score, rank, level, kd, acs, hs_percent
        FROM smurfs
        WHERE puuid IN (SELECT puuid FROM guild_players WHERE guild_id = ?)
        ORDER BY score DESC, kd DESC
        LIMIT ?
    ''', (str(guild_id), limit))

def get_player_history(name, tag, limit=10):
    # Renvoie (les analyses les plus récentes, le nombre total d'analyses)
    rows = db_query('''
        SELECT s.name, s.tag, h.score, h.rank, h.kd, CAST(strftime('%s', h.analyzed_at) AS INTEGER)
        FROM smurf_history h JOIN smurfs s ON s.puuid = h.puuid
        WHERE casefold(s.name) = ? AND casefold(s.tag) = ?
        ORDER BY h.analyzed_at DESC, h.id DESC
    ''', (name.casefold(), tag.casefold()))
    return rows[:limit], len(rows)

def get_linked_account(user_id):
    rows = db_query("SELECT name, tag FROM linked_accounts WHERE user_id = ?", (str(user_id),))
    return rows[0] if rows else None

def set_linked_account(user_id, name, tag):
    db_query("INSERT OR REPLACE INTO linked_accounts (user_id, name, tag) VALUES (?, ?, ?)", (str(user_id), name, tag))

def delete_linked_account(user_id):
    linked = get_linked_account(user_id)
    db_query("DELETE FROM linked_accounts WHERE user_id = ?", (str(user_id),))
    return linked

WATCH_COLUMNS = ["guild_id", "puuid", "channel_id", "user_id", "name", "tag", "region", "last_rank", "last_elo"]

def get_watches(guild_id=None):
    # guild_id=None : toutes les surveillances, tous serveurs confondus
    sql = f"SELECT {', '.join(WATCH_COLUMNS)} FROM watchlist"
    if guild_id is None:
        rows = db_query(sql)
    else:
        rows = db_query(sql + " WHERE guild_id = ? ORDER BY name", (str(guild_id),))
    return [dict(zip(WATCH_COLUMNS, row)) for row in rows]

def add_watch(guild_id, channel_id, user_id, player, snapshot):
    db_query(
        f"INSERT INTO watchlist ({', '.join(WATCH_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(guild_id), player["puuid"], str(channel_id), str(user_id), player["name"], player["tag"],
         player["region"], snapshot["rank"], snapshot["elo"])
    )

def remove_watch(guild_id, puuid):
    db_query("DELETE FROM watchlist WHERE guild_id = ? AND puuid = ?", (str(guild_id), puuid))

def update_watch_state(guild_id, puuid, rank, elo):
    db_query("UPDATE watchlist SET last_rank = ?, last_elo = ? WHERE guild_id = ? AND puuid = ?", (rank, elo, str(guild_id), puuid))

def backup_database():
    # Copie cohérente de la base (même si le bot écrit dedans), une par jour, les plus anciennes sont supprimées
    os.makedirs(BACKUP_DIR, exist_ok=True)
    target = os.path.join(BACKUP_DIR, f"smurfs_leaderboard-{datetime.date.today().isoformat()}.db")
    source, dest = sqlite3.connect(DB_FILE), sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("smurfs_leaderboard-") and f.endswith(".db"))
    for old in backups[:-BACKUP_KEEP]:
        os.remove(os.path.join(BACKUP_DIR, old))
    return target

AGENT_EMOJIS = {
    "Jett": "<:jett:1527378099233362000>", "Reyna": "<:reyna:1527378937104171128>",
    "Raze": "<:raze:1527378379010211853>", "Phoenix": "<:phoenix:1527378438405750986>",
    "Yoru": "<:yoru:1527379215933112580>", "Neon": "<:neon:1527378640994832514>",
    "Iso": "<:iso:1527384160811876364>", "Sova": "<:sova:1527378232167370772>",
    "Breach": "<:breach:1527378328955388065>", "Skye": "<:skye:1527378704483750089>",
    "KAY/O": "<:kayo:1527379295822155966>", "Fade": "<:fade:1527379167900074164>",
    "Gekko": "<:gekko:1527378184679587931>", "Omen": "<:omen:1527379090691195070>",
    "Brimstone": "<:brimstone:1527378882985070762>", "Viper": "<:viper:1527378992766783548>",
    "Astra": "<:astra:1527378831390933162>", "Harbor": "<:harbor:1527379131166359835>",
    "Clove": "<:clove:1527384037986013294>", "Killjoy": "<:killjoy:1527379256060153916>",
    "Cypher": "<:cypher:1527378284789366906>", "Sage": "<:sage:1527378517921239240>",
    "Chamber": "<:chamber:1527378772012306562>", "Deadlock": "<:deadlock:1527379050816081991>",
    "Vyse": "<:vyse:1527384123335770142>", "Waylay": "<:waylay:1527384081891983420>",
}

def get_agent_emoji(agent_name):
    return AGENT_EMOJIS.get(agent_name, "🦸‍♂️")

def score_badge(score):
    return "🚨" if score >= 75 else ("⚠️" if score >= 45 else "🟢")

def get_headers():
    return {"Authorization": API_KEY, "User-Agent": "Valorant-Smurf-Bot-v3"}

# ================= LIMITES & CACHE =================
_analysis_cache = {}

class RateLimited(Exception):
    def __init__(self, retry_after=None):
        super().__init__("Limite de requêtes de l'API atteinte")
        self.retry_after = retry_after

def rate_limit_message(error):
    wait = f"dans **{error.retry_after} s**" if error.retry_after else "dans une minute"
    return f"⏳ Trop de requêtes envoyées à l'API Valorant. Réessaie {wait}."

def api_get(url, timeout=10):
    resp = requests.get(url, headers=get_headers(), timeout=timeout)
    if resp.status_code == 429:
        reset = resp.headers.get("x-ratelimit-reset") or resp.headers.get("retry-after") or ""
        raise RateLimited(int(reset) if reset.isdigit() else None)
    return resp

def cache_get(name, tag):
    entry = _analysis_cache.get((name.lower(), tag.lower()))
    if entry and time.monotonic() - entry[0] < CACHE_TTL:
        return entry[1]
    return None

def cache_set(name, tag, result):
    now = time.monotonic()
    for key in [k for k, (ts, _) in _analysis_cache.items() if now - ts >= CACHE_TTL]:
        del _analysis_cache[key]
    _analysis_cache[(name.lower(), tag.lower())] = (now, result)

# ================= PSEUDO / TAG =================
PSEUDO_HELP = "Pseudo Riot (ou Pseudo#TAG). Laisse vide pour utiliser ton compte lié avec /link"
TAG_HELP = "Tag Riot, sans le #"
MISSING_ID_MESSAGE = (
    "❌ Indique un pseudo et un tag (ou `Pseudo#TAG` dans le premier champ), "
    "ou lie ton compte une fois avec `/link` pour ne plus avoir à les saisir."
)

def parse_riot_id(pseudo, tag):
    # Accepte "Pseudo" + "TAG", ou "Pseudo#TAG" dans le premier champ
    pseudo, tag = (pseudo or "").strip(), (tag or "").strip().lstrip("#")
    if pseudo and not tag and "#" in pseudo:
        pseudo, _, tag = pseudo.rpartition("#")
        pseudo, tag = pseudo.strip(), tag.strip()
    return (pseudo, tag) if pseudo and tag else None

def resolve_riot_id(user_id, pseudo, tag):
    # Rien de saisi : on retombe sur le compte lié avec /link
    if not pseudo and not tag:
        return get_linked_account(user_id)
    return parse_riot_id(pseudo, tag)

class SmurfView(discord.ui.View):
    def __init__(self, tracker_url, history_text):
        super().__init__(timeout=300)
        self.history_text = history_text
        self.add_item(discord.ui.Button(label="📊 Profil Tracker.gg", url=tracker_url, style=discord.ButtonStyle.link))

    @discord.ui.button(label="📜 Voir les derniers matchs classés", style=discord.ButtonStyle.primary, custom_id="btn_history")
    async def show_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(self.history_text, ephemeral=True)

class RiotReportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        riot_url = "https://support-valorant.riotgames.com/hc/fr/requests/new"
        self.add_item(discord.ui.Button(label="🚨 Soumettre un ticket à Riot Games", url=riot_url, style=discord.ButtonStyle.link))

# ================= APPELS API =================
def fetch_player_stats(name, tag):
    url = f"https://api.henrikdev.xyz/valorant/v1/account/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}"
    resp = api_get(url)
    if resp.status_code == 200:
        return resp.json().get('data', {})
    return None

def fetch_last_match(name, tag, region):
    url = f"https://api.henrikdev.xyz/valorant/v3/matches/{region}/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}?filter=competitive&size=1"
    resp = api_get(url, timeout=15)
    if resp.status_code != 200: return None
    matches = [m for m in (resp.json().get('data') or []) if m]
    return matches[0] if matches else None

def quick_match_score(p, rounds):
    # Score rapide d'un joueur à partir des seules données du match (aucun appel API supplémentaire)
    stats = p.get("stats", {})
    kills, deaths = stats.get("kills", 0), stats.get("deaths", 0)
    hs = stats.get("headshots", 0)
    shots = hs + stats.get("bodyshots", 0) + stats.get("legshots", 0)
    game_stats = {
        "avg_kd": round(kills / max(1, deaths), 2),
        "avg_acs": round(stats.get("score", 0) / rounds, 1),
        "avg_hs_percent": round(hs / shots * 100, 1) if shots > 0 else 0,
        "frequent_duos": [],
    }
    level = p.get("level")
    rank = p.get("currenttier_patched") or "Inconnu"
    # Niveau inconnu : on ne le compte pas comme un niveau bas
    score, _ = analyze_smurf_score(level if isinstance(level, int) else 100, rank, game_stats)
    return {
        "puuid": p.get("puuid"), "name": f"{p.get('name')}#{p.get('tag')}", "team": str(p.get("team", "")).lower(),
        "agent": p.get("character", "Inconnu"), "rank": rank, "level": level if isinstance(level, int) else "?",
        "score": score, "kd": game_stats["avg_kd"], "acs": game_stats["avg_acs"], "hs_percent": game_stats["avg_hs_percent"],
    }

def fetch_mmr_v1(puuid, region):
    resp = api_get(f"https://api.henrikdev.xyz/valorant/v1/by-puuid/mmr/{region}/{puuid}")
    if resp.status_code == 200:
        return resp.json().get('data') or {}
    return None

def fetch_mmr_snapshot(puuid, region):
    # Un seul appel API : rang et elo du moment, pour la surveillance (/watch)
    data = fetch_mmr_v1(puuid, region)
    if data is None:
        return None
    return {"rank": data.get("currenttierpatched"), "elo": data.get("elo")}

def count_ranked_games(by_season):
    # Total des parties classées, toutes saisons confondues (None si l'API ne le fournit pas)
    if not isinstance(by_season, dict):
        return None
    total = sum((season.get("number_of_games") or 0) for season in by_season.values() if isinstance(season, dict))
    return total or None

def fetch_mmr_stats(puuid, region):
    data_v1 = fetch_mmr_v1(puuid, region) or {}
    rank_name = data_v1.get('currenttierpatched') or 'Inconnu'
    rank_icon = (data_v1.get('images') or {}).get('small', '')

    url_v2 = f"https://api.henrikdev.xyz/valorant/v2/by-puuid/mmr/{region}/{puuid}"
    resp_v2 = api_get(url_v2)
    peak_rank, ranked_games = "Inconnu", None
    if resp_v2.status_code == 200:
        data_v2 = resp_v2.json().get('data') or {}
        peak_rank = (data_v2.get('highest_rank') or {}).get('patched_tier') or 'Inconnu'
        ranked_games = count_ranked_games(data_v2.get('by_season'))

    return {
        "currenttierpatched": rank_name, "images": {"small": rank_icon},
        "highest_rank": {"patched_tier": peak_rank}, "ranked_games": ranked_games,
    }

def fetch_recent_matches_stats(name, tag, region, player_puuid):
    url = f"https://api.henrikdev.xyz/valorant/v3/matches/{region}/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}?filter=competitive&size={ANALYZED_MATCHES}"
    try:
        resp = api_get(url, timeout=20)
        if resp.status_code != 200: return None
        matches = resp.json().get('data', [])
        if not matches: return None

        total_kills, total_deaths, total_acs, total_headshots, total_shots, match_count, wins = 0, 0, 0, 0, 0, 0, 0
        agents_played, premades_list, history_details = [], [], []

        for match in matches:
            if not match: continue
            metadata = match.get("metadata", {})
            if not metadata or metadata.get("mode_id", "") != "competitive":
                continue

            map_name = metadata.get("map", "Inconnu")
            rounds = metadata.get("rounds_played") or 1
            all_players = match.get("players", {}).get("all_players", [])
            player_stats = next((p for p in all_players if p.get("puuid") == player_puuid), None)

            if player_stats:
                match_count += 1
                stats = player_stats.get("stats", {})
                kills, deaths, assists = stats.get("kills", 0), stats.get("deaths", 0), stats.get("assists", 0)
                agent = player_stats.get("character", "Inconnu")

                total_kills += kills
                total_deaths += max(1, deaths)
                total_acs += (stats.get("score", 0) / rounds)
                hs = stats.get("headshots", 0)
                total_headshots += hs
                total_shots += (hs + stats.get("bodyshots", 0) + stats.get("legshots", 0))

                team = str(player_stats.get("team", "")).lower()
                won = ((match.get("teams") or {}).get(team) or {}).get("has_won")
                if won: wins += 1
                result_icon = "✅" if won else ("❌" if won is False else "➖")

                agents_played.append(agent)
                history_details.append(f"{result_icon} 🗺️ **{map_name}** | {get_agent_emoji(agent)} {agent} | 🔫 KDA: {kills}/{deaths}/{assists}")

                party_id = player_stats.get("party_id")
                if party_id:
                    for p in all_players:
                        if p.get("party_id") == party_id and p.get("puuid") != player_puuid:
                            premades_list.append(f"{p.get('name')}#{p.get('tag')}")

            if match_count >= ANALYZED_MATCHES: break

        if match_count == 0: return None
        top_agents = [agent for agent, count in Counter(agents_played).most_common(2)]
        # Sur 10 matchs, deux parties ensemble ne suffisent plus à parler de duo récurrent
        min_duo_games = 3 if match_count > 5 else 2
        frequent_duos = [premade for premade, count in Counter(premades_list).items() if count >= min_duo_games]

        return {
            "avg_kd": round(total_kills / total_deaths, 2),
            "avg_acs": round(total_acs / match_count, 1),
            "avg_hs_percent": round((total_headshots / total_shots * 100), 1) if total_shots > 0 else 0,
            "win_rate": round(wins / match_count * 100),
            "match_count": match_count,
            "top_agents": top_agents,
            "frequent_duos": frequent_duos,
            "history_text": "\n".join(history_details)
        }
    except RateLimited:
        raise
    except Exception:
        log.exception("Erreur API matchs pour %s#%s", name, tag)
        return None

# ================= CALCUL DU SCORE =================
RANK_NAMES = ["iron", "bronze", "silver", "gold", "platinum", "diamond", "ascendant", "immortal"]

def rank_to_tier(rank):
    # "Iron 1" -> 0, "Gold 2" -> 10, "Radiant" -> 24 ; rang inconnu ou non classé -> None
    parts = (rank or "").lower().split()
    if not parts: return None
    if parts[0] == "radiant": return len(RANK_NAMES) * 3
    if parts[0] not in RANK_NAMES: return None
    division = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    return RANK_NAMES.index(parts[0]) * 3 + (division - 1)

def analyze_smurf_score(level, rank, game_stats, peak_rank=None, ranked_games=None):
    score, reasons = 0, []
    rank_lower = rank.lower()
    if level < 20: score += 25; reasons.append(f"Niveau très bas ({level})")
    elif level < 50: score += 10; reasons.append(f"Niveau relativement bas ({level})")
    if level < 40 and any(h in rank_lower for h in ["diamond", "ascendant", "immortal", "radiant"]):
        score += 35; reasons.append(f"Haut rang ({rank}) pour ce niveau")
    if game_stats:
        kd, acs, hs_pct = game_stats["avg_kd"], game_stats["avg_acs"], game_stats["avg_hs_percent"]
        duos = game_stats["frequent_duos"]
        if kd >= 1.5: score += 25; reasons.append(f"K/D moyen insolent : {kd}")
        elif kd >= 1.25: score += 15; reasons.append(f"Bon K/D moyen : {kd}")
        if acs >= 270: score += 20; reasons.append(f"ACS très élevé : {int(acs)}")
        elif acs >= 230: score += 10; reasons.append(f"ACS solide : {int(acs)}")
        is_low_mid = not any(h in rank_lower for h in ["diamond", "ascendant", "immortal", "radiant"])
        if hs_pct >= 32 and is_low_mid: score += 20; reasons.append(f"Précision suspecte : {hs_pct}% HS")
        if len(duos) > 0: score += 10; reasons.append(f"Joue souvent en premade (Boost potentiel)")
        win_rate, match_count = game_stats.get("win_rate"), game_stats.get("match_count", 0)
        if win_rate is not None and match_count >= 5 and win_rate >= 70:
            score += 10; reasons.append(f"Taux de victoire très élevé : {win_rate}% sur {match_count} matchs")
    current_tier, peak_tier = rank_to_tier(rank), rank_to_tier(peak_rank)
    # Peak au moins deux rangs complets au-dessus du rang actuel (ex. Diamant -> Or)
    if current_tier is not None and peak_tier is not None and peak_tier - current_tier >= 6:
        score += 15; reasons.append(f"Peak bien au-dessus du rang actuel ({peak_rank})")
    if ranked_games and ranked_games < FEW_RANKED_GAMES and current_tier is not None and current_tier >= rank_to_tier("Platinum 1"):
        score += 15; reasons.append(f"Seulement {ranked_games} parties classées pour ce rang")
    return min(score, 100), reasons

# ================= SURVEILLANCE =================
def describe_watch_change(watch, snapshot):
    # Renvoie le texte à annoncer, ou None si le joueur n'a pas rejoué depuis la dernière vérification
    old_elo, new_elo = watch["last_elo"], snapshot["elo"]
    if old_elo is None or new_elo is None or old_elo == new_elo:
        return None
    who = f"**{watch['name']}#{watch['tag']}**"
    old_rank, new_rank = watch["last_rank"], snapshot["rank"]
    if old_rank and new_rank and old_rank != new_rank:
        if new_elo > old_elo:
            return f"📈 {who} est monté de **{old_rank}** à **{new_rank}** !"
        return f"📉 {who} est descendu de **{old_rank}** à **{new_rank}**."
    return f"🎮 {who} a rejoué en classé : **{new_elo - old_elo:+d} RR** ({new_rank or 'rang inconnu'})."

# ================= CLASS DU COG =================
class SmurfDetectorCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        init_db()

    async def cog_load(self):
        self.watch_loop.start()
        self.backup_loop.start()

    def cog_unload(self):
        self.watch_loop.cancel()
        self.backup_loop.cancel()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Doucement ! Réessaie dans **{int(error.retry_after) + 1} s**."
        else:
            command_name = interaction.command.name if interaction.command else "?"
            log.error("Erreur dans la commande /%s", command_name, exc_info=error)
            msg = "❌ Une erreur est survenue, réessaie dans un instant."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ---------- Tâches de fond ----------
    @tasks.loop(minutes=WATCH_INTERVAL_MIN)
    async def watch_loop(self):
        # Une exception non rattrapée arrêterait la boucle définitivement
        try:
            await self.check_watchlist()
        except Exception:
            log.exception("Erreur pendant la vérification des joueurs surveillés")

    @watch_loop.before_loop
    async def before_watch_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def backup_loop(self):
        try:
            target = await asyncio.to_thread(backup_database)
            log.info("💾 Base sauvegardée dans %s", target)
        except Exception:
            log.exception("Échec de la sauvegarde de la base")

    async def check_watchlist(self):
        snapshots = {}
        for watch in get_watches():
            # Un joueur surveillé sur plusieurs serveurs ne coûte qu'un seul appel API
            key = (watch["puuid"], watch["region"])
            if key not in snapshots:
                try:
                    snapshots[key] = await asyncio.to_thread(fetch_mmr_snapshot, watch["puuid"], watch["region"])
                except RateLimited:
                    log.warning("Surveillance interrompue : limite de requêtes de l'API atteinte")
                    return
                except requests.RequestException as e:
                    log.warning("Surveillance : API injoignable pour %s#%s (%s)", watch["name"], watch["tag"], e)
                    snapshots[key] = None
                await asyncio.sleep(WATCH_API_DELAY)
            snapshot = snapshots[key]
            if not snapshot:
                continue
            text = describe_watch_change(watch, snapshot)
            update_watch_state(watch["guild_id"], watch["puuid"], snapshot["rank"] or watch["last_rank"], snapshot["elo"] if snapshot["elo"] is not None else watch["last_elo"])
            if text:
                await self.notify_watch(watch, text)

    async def notify_watch(self, watch, text):
        channel = self.bot.get_channel(int(watch["channel_id"]))
        if channel is None:
            log.warning("Surveillance : salon %s introuvable pour %s#%s", watch["channel_id"], watch["name"], watch["tag"])
            return
        embed = discord.Embed(description=text, color=discord.Color.blurple())
        embed.set_footer(text="Surveillance /watch — /unwatch pour arrêter")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("Surveillance : envoi impossible dans le salon %s (%s)", watch["channel_id"], e)

    # ---------- Analyse ----------
    @app_commands.command(name="smurf", description="Analyse complète d'un joueur Valorant")
    @app_commands.describe(pseudo=PSEUDO_HELP, tag=TAG_HELP)
    @app_commands.checks.cooldown(1, SMURF_COOLDOWN, key=lambda i: i.user.id)
    async def smurf(self, interaction: discord.Interaction, pseudo: Optional[str] = None, tag: Optional[str] = None):
        riot_id = resolve_riot_id(interaction.user.id, pseudo, tag)
        if not riot_id:
            return await interaction.response.send_message(MISSING_ID_MESSAGE, ephemeral=True)
        pseudo, tag = riot_id
        await interaction.response.defer()

        result = cache_get(pseudo, tag)
        from_cache = result is not None
        if not from_cache:
            # Les appels 'requests' sont bloquants : on les exécute dans un thread pour ne pas figer le bot
            try:
                player = await asyncio.to_thread(fetch_player_stats, pseudo, tag)
                if not player:
                    return await interaction.followup.send("❌ Impossible de trouver le joueur.")
                mmr, game_stats = await asyncio.gather(
                    asyncio.to_thread(fetch_mmr_stats, player["puuid"], player["region"]),
                    asyncio.to_thread(fetch_recent_matches_stats, pseudo, tag, player["region"], player["puuid"]),
                )
            except RateLimited as e:
                return await interaction.followup.send(rate_limit_message(e))
            except requests.RequestException as e:
                log.warning("Erreur API pendant /smurf %s#%s : %s", pseudo, tag, e)
                return await interaction.followup.send("❌ L'API Valorant ne répond pas, réessaie dans un instant.")

            rank_name = mmr.get("currenttierpatched", "Inconnu")
            peak_rank = mmr.get("highest_rank", {}).get("patched_tier", "Inconnu")
            ranked_games = mmr.get("ranked_games")
            score, reasons = analyze_smurf_score(player["account_level"], rank_name, game_stats, peak_rank, ranked_games)
            save_or_update_smurf(player["puuid"], player["name"], player["tag"], score, rank_name, player["account_level"], game_stats)

            result = {
                "player": player, "rank_name": rank_name,
                "rank_icon": mmr.get("images", {}).get("small", ""),
                "peak_rank": peak_rank, "ranked_games": ranked_games,
                "game_stats": game_stats, "score": score, "reasons": reasons,
            }
            cache_set(pseudo, tag, result)

        player, rank_name, rank_icon, peak_rank = result["player"], result["rank_name"], result["rank_icon"], result["peak_rank"]
        game_stats, score, reasons = result["game_stats"], result["score"], result["reasons"]
        if interaction.guild_id:
            record_guild_player(interaction.guild_id, player["puuid"])

        if score >= 75: color, status = discord.Color.red(), "🚨 ALERTE ROUGE : Smurf flagrant !"
        elif score >= 45: color, status = discord.Color.orange(), "⚠️ SUSPECT : Profil anormal."
        else: color, status = discord.Color.green(), "🟢 RAS : Joueur légitime."

        embed = discord.Embed(title=f"🔍 Rapport : {player['name']}#{player['tag']}", color=color)
        if rank_icon: embed.set_thumbnail(url=rank_icon)

        profile = f"**Niveau :** {player['account_level']}\n**Rang Actuel :** {rank_name}\n**Peak Elo :** {peak_rank}"
        if result["ranked_games"]: profile += f"\n**Parties classées :** {result['ranked_games']}"
        embed.add_field(name="Profil", value=profile, inline=True)
        if game_stats:
            matches_label = f"{game_stats['match_count']} match" + ("s" if game_stats['match_count'] > 1 else "")
            embed.add_field(name=f"Performances ({matches_label})", value=f"**K/D :** {game_stats['avg_kd']}\n**ACS :** {int(game_stats['avg_acs'])}\n**HS :** {game_stats['avg_hs_percent']}%\n**Victoires :** {game_stats['win_rate']}%", inline=True)
            agents_str = ", ".join([f"{get_agent_emoji(a)} {a}" for a in game_stats['top_agents']])
            embed.add_field(name="Agents favoris", value=agents_str or "Inconnu", inline=False)
            if game_stats['frequent_duos']:
                embed.add_field(name="Premades récurrentes", value="\n".join([f"👥 {d}" for d in game_stats['frequent_duos']]), inline=False)
        embed.add_field(name="Verdict", value=f"**Score : {score}/100**\n{status}", inline=False)
        if reasons: embed.add_field(name="Facteurs détectés", value="\n".join([f"• {r}" for r in reasons]), inline=False)
        if from_cache: embed.set_footer(text=f"Résultat en cache (analyse de moins de {CACHE_TTL // 60} min)")

        encoded_name = urllib.parse.quote(pseudo)
        encoded_tag = urllib.parse.quote(tag)
        tracker_url = f"https://tracker.gg/valorant/profile/riot/{encoded_name}%23{encoded_tag}/overview"

        view = SmurfView(tracker_url, game_stats['history_text'] if game_stats else "Aucun match classé récent.")
        await interaction.followup.send(embed=embed, view=view)

        if score >= 45:
            reasons_text = "\n".join([f"- {r}" for r in reasons]) if reasons else "- Statistiques anormales"
            title_text = "🚨 ALERTE SMURF : TICKET RECOMMANDÉ" if score >= 75 else "⚠️ PROFIL SUSPECT : SIGNALEMENT DÉCONSEILLÉ"
            color_alert = discord.Color.red() if score >= 75 else discord.Color.orange()

            report_embed = discord.Embed(
                title=title_text,
                description=(
                    f"Le joueur **{player['name']}#{player['tag']}** présente un score de smurf de **{score}/100**.\n\n"
                    f"📌 **Riot Games exige une soumission manuelle de ticket pour des raisons de sécurité (Captcha).**\n"
                    f"Pour soumettre le ticket rapidement, clique sur le bouton ci-dessous.\n\n"
                    f"**Motifs à copier/coller dans ton ticket :**\n```text\nSignalement de Smurf / Anti-Jeu : {player['name']}#{player['tag']}\n"
                    f"Score détecté : {score}/100\nAnomalies :\n{reasons_text}\n```"
                ),
                color=color_alert
            )
            report_embed.set_footer(text=f"Détection effectuée à la demande de {interaction.user.name}")
            # followup plutôt que channel.send : fonctionne même sans permission d'écrire dans le salon
            await interaction.followup.send(embed=report_embed, view=RiotReportView())

    @app_commands.command(name="smurf-match", description="Analyse les 10 joueurs du dernier match classé d'un joueur")
    @app_commands.describe(pseudo=PSEUDO_HELP, tag=TAG_HELP)
    @app_commands.checks.cooldown(1, MATCH_COOLDOWN, key=lambda i: i.user.id)
    async def smurf_match(self, interaction: discord.Interaction, pseudo: Optional[str] = None, tag: Optional[str] = None):
        riot_id = resolve_riot_id(interaction.user.id, pseudo, tag)
        if not riot_id:
            return await interaction.response.send_message(MISSING_ID_MESSAGE, ephemeral=True)
        pseudo, tag = riot_id
        await interaction.response.defer()
        try:
            player = await asyncio.to_thread(fetch_player_stats, pseudo, tag)
            if not player:
                return await interaction.followup.send("❌ Impossible de trouver le joueur.")
            match = await asyncio.to_thread(fetch_last_match, pseudo, tag, player["region"])
        except RateLimited as e:
            return await interaction.followup.send(rate_limit_message(e))
        except requests.RequestException as e:
            log.warning("Erreur API pendant /smurf-match %s#%s : %s", pseudo, tag, e)
            return await interaction.followup.send("❌ L'API Valorant ne répond pas, réessaie dans un instant.")
        if not match:
            return await interaction.followup.send("❌ Aucun match classé récent trouvé pour ce joueur.")

        metadata = match.get("metadata", {})
        rounds = metadata.get("rounds_played") or 1
        teams = match.get("teams") or {}
        all_players = match.get("players", {}).get("all_players", [])

        analyzed = [quick_match_score(p, rounds) for p in all_players]
        worst = max((a["score"] for a in analyzed), default=0)
        if worst >= 75: color = discord.Color.red()
        elif worst >= 45: color = discord.Color.orange()
        else: color = discord.Color.green()

        embed = discord.Embed(
            title=f"🔍 Analyse du match : {metadata.get('map', 'Inconnu')}",
            description=f"Dernier match classé de **{player['name']}#{player['tag']}**",
            color=color
        )
        for team_key, team_label in (("red", "🔴 Équipe Rouge"), ("blue", "🔵 Équipe Bleue")):
            members = sorted((a for a in analyzed if a["team"] == team_key), key=lambda a: a["score"], reverse=True)
            if not members: continue
            team_info = teams.get(team_key) or {}
            title = f"{team_label} — {team_info.get('rounds_won', '?')} manches" + (" 🏆" if team_info.get("has_won") else "")
            lines = []
            for a in members:
                me = " ⬅️" if a["puuid"] == player["puuid"] else ""
                lines.append(
                    f"{score_badge(a['score'])} **{a['score']}** · {get_agent_emoji(a['agent'])} **{a['name']}**{me}\n"
                    f"　{a['rank']} (Niv {a['level']}) · K/D `{a['kd']}` · ACS `{int(a['acs'])}` · HS `{a['hs_percent']}%`"
                )
            embed.add_field(name=title, value="\n".join(lines)[:1024], inline=False)

        embed.set_footer(text="Estimation basée sur ce seul match — utilise /smurf pour une analyse complète d'un joueur.")
        await interaction.followup.send(embed=embed)

    # ---------- Classement & historique ----------
    @app_commands.command(name="leaderboard-smurfs", description="Affiche le classement des 10 pires smurfs analysés par le bot")
    @app_commands.describe(portee="Classement de ce serveur (par défaut) ou de tous les serveurs")
    @app_commands.choices(portee=[
        app_commands.Choice(name="Ce serveur", value="serveur"),
        app_commands.Choice(name="Global", value="global"),
    ])
    async def leaderboard_smurfs(self, interaction: discord.Interaction, portee: Optional[app_commands.Choice[str]] = None):
        local = interaction.guild_id is not None and (portee is None or portee.value == "serveur")
        top_smurfs = get_top_smurfs(limit=10, guild_id=interaction.guild_id if local else None)
        if not top_smurfs:
            if local:
                msg = ("🥇 **Aucun joueur analysé sur ce serveur pour le moment.** Utilisez `/smurf` pour analyser des joueurs, "
                       "ou `/leaderboard-smurfs portee:Global` pour le classement de tous les serveurs.")
            else:
                msg = "🥇 **Aucun smurf enregistré pour le moment.** Utilisez `/smurf` pour analyser des joueurs !"
            return await interaction.response.send_message(msg, ephemeral=True)

        scope_text = "sur ce serveur" if local else "sur tous les serveurs"
        embed = discord.Embed(
            title="🏆 Hall of Shame : Top 10 des Pires Smurfs",
            description=f"Classement basé sur les scores de smurf les plus élevés analysés par le bot {scope_text}.",
            color=discord.Color.dark_red()
        )
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        for idx, (name, tag, score, rank, level, kd, acs, hs_pct) in enumerate(top_smurfs):
            medal = medals[idx] if idx < len(medals) else f"{idx+1}."
            field_value = (
                f"**Score : {score}/100** {score_badge(score)}\n"
                f"• **Rang / Niv :** {rank} (Lvl {level})\n"
                f"• **Stats :** K/D: `{kd}` | ACS: `{int(acs)}` | HS: `{hs_pct}%`"
            )
            embed.add_field(name=f"{medal} {name}#{tag}", value=field_value, inline=False)

        embed.set_footer(text="Système de détection de Smurfs - Valorant Bot")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="historique", description="Affiche l'évolution du score de smurf d'un joueur déjà analysé")
    @app_commands.describe(pseudo=PSEUDO_HELP, tag=TAG_HELP)
    async def historique(self, interaction: discord.Interaction, pseudo: Optional[str] = None, tag: Optional[str] = None):
        riot_id = resolve_riot_id(interaction.user.id, pseudo, tag)
        if not riot_id:
            return await interaction.response.send_message(MISSING_ID_MESSAGE, ephemeral=True)
        rows, total = get_player_history(*riot_id)
        if not rows:
            return await interaction.response.send_message(
                f"❌ **{riot_id[0]}#{riot_id[1]}** n'a jamais été analysé. Lance d'abord `/smurf` sur ce joueur.", ephemeral=True)

        lines = [
            f"<t:{ts}:d> <t:{ts}:t> — {score_badge(score)} **{score}/100** · {rank} · K/D `{kd}`"
            for _, _, score, rank, kd, ts in rows
        ]
        embed = discord.Embed(
            title=f"📈 Historique : {rows[0][0]}#{rows[0][1]}",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        if len(rows) >= 2:
            diff = rows[0][2] - rows[-1][2]
            if diff > 0: trend = f"📈 **+{diff} points**"
            elif diff < 0: trend = f"📉 **{diff} points**"
            else: trend = "➡️ **Score stable**"
            embed.add_field(name="Évolution", value=f"{trend} entre la plus ancienne et la plus récente des analyses affichées.", inline=False)
        embed.set_footer(text=f"{total} analyse(s) enregistrée(s) — les {len(rows)} plus récentes sont affichées")
        await interaction.response.send_message(embed=embed)

    # ---------- Compte lié ----------
    @app_commands.command(name="link", description="Lie ton compte Riot pour ne plus avoir à saisir ton pseudo et ton tag")
    @app_commands.describe(pseudo="Ton pseudo Riot (ou Pseudo#TAG)", tag=TAG_HELP)
    @app_commands.checks.cooldown(1, SMURF_COOLDOWN, key=lambda i: i.user.id)
    async def link(self, interaction: discord.Interaction, pseudo: str, tag: Optional[str] = None):
        riot_id = parse_riot_id(pseudo, tag)
        if not riot_id:
            return await interaction.response.send_message("❌ Indique ton pseudo et ton tag (ou `Pseudo#TAG` dans le premier champ).", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            player = await asyncio.to_thread(fetch_player_stats, *riot_id)
        except RateLimited as e:
            return await interaction.followup.send(rate_limit_message(e))
        except requests.RequestException as e:
            log.warning("Erreur API pendant /link %s#%s : %s", riot_id[0], riot_id[1], e)
            return await interaction.followup.send("❌ L'API Valorant ne répond pas, réessaie dans un instant.")
        if not player:
            return await interaction.followup.send("❌ Impossible de trouver ce compte Riot. Vérifie le pseudo et le tag.")

        set_linked_account(interaction.user.id, player["name"], player["tag"])
        await interaction.followup.send(
            f"🔗 Compte **{player['name']}#{player['tag']}** lié ! Tu peux maintenant utiliser `/smurf`, `/smurf-match` "
            f"et `/historique` sans rien saisir. Utilise `/unlink` pour le délier.")

    @app_commands.command(name="unlink", description="Supprime le lien entre ton compte Discord et ton compte Riot")
    async def unlink(self, interaction: discord.Interaction):
        linked = delete_linked_account(interaction.user.id)
        if linked:
            msg = f"✅ Le compte **{linked[0]}#{linked[1]}** n'est plus lié à ton compte Discord."
        else:
            msg = "ℹ️ Aucun compte Riot n'est lié à ton compte Discord."
        await interaction.response.send_message(msg, ephemeral=True)

    # ---------- Surveillance ----------
    @app_commands.command(name="watch", description="Surveille un joueur : annonce ses changements de rang et ses parties classées")
    @app_commands.describe(pseudo="Pseudo Riot du joueur à surveiller (ou Pseudo#TAG)", tag=TAG_HELP)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, SMURF_COOLDOWN, key=lambda i: i.user.id)
    async def watch(self, interaction: discord.Interaction, pseudo: str, tag: Optional[str] = None):
        riot_id = parse_riot_id(pseudo, tag)
        if not riot_id:
            return await interaction.response.send_message("❌ Indique le pseudo et le tag du joueur (ou `Pseudo#TAG` dans le premier champ).", ephemeral=True)
        watches = get_watches(interaction.guild_id)
        if len(watches) >= MAX_WATCH_PER_GUILD:
            return await interaction.response.send_message(
                f"❌ Ce serveur surveille déjà **{len(watches)}** joueurs (maximum {MAX_WATCH_PER_GUILD}). "
                f"Retires-en un avec `/unwatch` avant d'en ajouter un autre.", ephemeral=True)
        permissions = interaction.app_permissions
        if not (permissions.send_messages and permissions.embed_links):
            return await interaction.response.send_message(
                "❌ Je n'ai pas la permission d'écrire dans ce salon : lance `/watch` dans un salon où je peux envoyer des messages.", ephemeral=True)

        await interaction.response.defer()
        try:
            player = await asyncio.to_thread(fetch_player_stats, *riot_id)
            if not player:
                return await interaction.followup.send("❌ Impossible de trouver le joueur.")
            if any(w["puuid"] == player["puuid"] for w in watches):
                return await interaction.followup.send(f"ℹ️ **{player['name']}#{player['tag']}** est déjà surveillé sur ce serveur.")
            snapshot = await asyncio.to_thread(fetch_mmr_snapshot, player["puuid"], player["region"])
        except RateLimited as e:
            return await interaction.followup.send(rate_limit_message(e))
        except requests.RequestException as e:
            log.warning("Erreur API pendant /watch %s#%s : %s", riot_id[0], riot_id[1], e)
            return await interaction.followup.send("❌ L'API Valorant ne répond pas, réessaie dans un instant.")

        # Rang introuvable pour l'instant : le point de départ sera pris à la prochaine vérification
        snapshot = snapshot or {"rank": None, "elo": None}
        add_watch(interaction.guild_id, interaction.channel_id, interaction.user.id, player, snapshot)
        await interaction.followup.send(
            f"👁️ **{player['name']}#{player['tag']}** est maintenant surveillé (rang actuel : {snapshot['rank'] or 'inconnu'}).\n"
            f"Ses changements de rang et ses nouvelles parties classées seront annoncés dans ce salon, "
            f"avec une vérification toutes les {WATCH_INTERVAL_MIN} minutes.")

    @app_commands.command(name="unwatch", description="Arrête la surveillance d'un joueur sur ce serveur")
    @app_commands.describe(pseudo="Pseudo Riot du joueur surveillé (ou Pseudo#TAG)", tag=TAG_HELP)
    @app_commands.guild_only()
    async def unwatch(self, interaction: discord.Interaction, pseudo: str, tag: Optional[str] = None):
        riot_id = parse_riot_id(pseudo, tag)
        if not riot_id:
            return await interaction.response.send_message("❌ Indique le pseudo et le tag du joueur (ou `Pseudo#TAG` dans le premier champ).", ephemeral=True)
        wanted = (riot_id[0].casefold(), riot_id[1].casefold())
        watch = next((w for w in get_watches(interaction.guild_id) if (w["name"].casefold(), w["tag"].casefold()) == wanted), None)
        if not watch:
            return await interaction.response.send_message(
                f"❌ **{riot_id[0]}#{riot_id[1]}** n'est pas surveillé sur ce serveur. Utilise `/watchlist` pour voir la liste.", ephemeral=True)
        if str(interaction.user.id) != watch["user_id"] and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ Seul le membre qui a lancé cette surveillance, ou un membre pouvant gérer le serveur, peut l'arrêter.", ephemeral=True)
        remove_watch(interaction.guild_id, watch["puuid"])
        await interaction.response.send_message(f"✅ **{watch['name']}#{watch['tag']}** n'est plus surveillé.")

    @app_commands.command(name="watchlist", description="Affiche les joueurs surveillés sur ce serveur")
    @app_commands.guild_only()
    async def watchlist(self, interaction: discord.Interaction):
        watches = get_watches(interaction.guild_id)
        if not watches:
            return await interaction.response.send_message("👁️ Aucun joueur surveillé sur ce serveur. Utilise `/watch` pour en ajouter un.", ephemeral=True)
        lines = [
            f"• **{w['name']}#{w['tag']}** — {w['last_rank'] or 'rang inconnu'} · <#{w['channel_id']}> · ajouté par <@{w['user_id']}>"
            for w in watches
        ]
        embed = discord.Embed(title="👁️ Joueurs surveillés", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text=f"{len(watches)}/{MAX_WATCH_PER_GUILD} — vérification toutes les {WATCH_INTERVAL_MIN} minutes")
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(SmurfDetectorCog(bot))
