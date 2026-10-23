import sqlite3
import os
import json
import datetime
import logging
from dataclasses import is_dataclass, asdict

log = logging.getLogger("smurfbot")

# Lu à chaque appel : les tests remplacent ces valeurs par des fichiers temporaires
DB_FILE = "smurfs_leaderboard.db"
BACKUP_DIR = "backups"
BACKUP_KEEP = 7           # nombre de sauvegardes quotidiennes conservées
SCHEMA_VERSION = 2        # PRAGMA user_version attendu après init_db()
MODEL_VERSION = 2         # version du calcul de l'indice (NULL = ancien calcul)
REPORT_STATUSES = ("pending", "validated", "rejected")

# ================= CONNEXION =================
def _casefold(s):
    return s.casefold() if isinstance(s, str) else s

def connect():
    conn = sqlite3.connect(DB_FILE)
    # lower() de SQLite ignore les accents : on compare les pseudos avec casefold() de Python
    conn.create_function("casefold", 1, _casefold, deterministic=True)
    return conn

def db_query(sql, params=()):
    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()

def _execute(sql, params=()):
    # Renvoie (lastrowid, rowcount) d'une requête d'écriture
    conn = connect()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.lastrowid, cursor.rowcount
    finally:
        conn.close()

def _id(value):
    return None if value is None else str(value)

def _to_json(items):
    # Accepte des dicts ou des dataclasses (Signal…)
    return json.dumps([asdict(i) if is_dataclass(i) else i for i in (items or [])], ensure_ascii=False)

def _from_json(text):
    if not text:
        return []
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []

def _window(hours):
    # Modificateur SQLite pour datetime('now', ?) : fenêtre glissante de `hours` heures
    return f"-{int(round(hours * 3600))} seconds"

def _unix(column):
    return f"CAST(strftime('%s', {column}) AS INTEGER)"

# ================= SCHÉMA =================
def _create_v1(cursor):
    # Tables historiques, à l'identique de l'ancien cog (bases déjà en production)
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
    # Quels joueurs ont été analysés sur quel serveur
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
    # Joueurs suivis (/watch)
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

def _add_columns(cursor, table, columns):
    existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns:
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

def _migrate_v2(cursor):
    # Nouvelles mesures de l'analyse ; les lignes existantes gardent model_version NULL (ancien calcul)
    v2_columns = [("match_count", "INTEGER"), ("adr", "REAL"), ("win_rate", "REAL"), ("model_version", "INTEGER")]
    _add_columns(cursor, "smurfs", v2_columns + [("signals", "TEXT")])
    _add_columns(cursor, "smurf_history", v2_columns)
    # Dossiers /report (restent dans le salon des modérateurs)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            reporter_id TEXT NOT NULL,
            puuid TEXT NOT NULL,
            name TEXT,
            tag TEXT,
            score INTEGER,
            level TEXT,
            signals TEXT,
            match_ids TEXT,
            observations TEXT,
            channel_id TEXT,
            message_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by TEXT,
            reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports (reporter_id, puuid, created_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_reports_guild_status ON reports (guild_id, status)')
    # Journal des consultations / signalements, pour la protection anti-acharnement
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT,
            user_id TEXT NOT NULL,
            puuid TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_activity_lookup ON activity (user_id, puuid, kind, created_at)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id TEXT PRIMARY KEY,
            reports_channel_id TEXT
        )
    ''')

MIGRATIONS = [(2, _migrate_v2)]

def _backup_before_migration(conn, version):
    # Copie de la base telle qu'elle était avant la migration (jamais supprimée par la rotation)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    target = os.path.join(BACKUP_DIR, f"avant-migration-v{version}.db")
    if os.path.exists(target):
        return
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    log.info("💾 Copie de la base avant migration : %s", target)

def init_db():
    # isolation_level=None : les transactions sont gérées à la main (BEGIN / COMMIT / ROLLBACK)
    conn = sqlite3.connect(DB_FILE, isolation_level=None)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        had_data = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'smurfs'").fetchone() is not None
        if version < SCHEMA_VERSION and had_data:
            _backup_before_migration(conn, SCHEMA_VERSION)
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            _create_v1(cursor)
            # Relu dans la transaction : un autre processus a pu migrer entre-temps
            version = cursor.execute("PRAGMA user_version").fetchone()[0]
            for target, migrate in MIGRATIONS:
                if version < target:
                    migrate(cursor)
                    cursor.execute(f"PRAGMA user_version = {int(target)}")
                    log.info("🗃️ Base de données migrée en version %s", target)
            cursor.execute("COMMIT")
        except BaseException:
            # Tout ou rien : aucune ligne ni colonne à moitié migrée
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

# ================= ANALYSES =================
def save_analysis(puuid, name, tag, score, rank, level, kd, acs, hs_percent, adr, win_rate, match_count, signals):
    conn = connect()
    try:
        with conn:
            conn.execute('''
                INSERT INTO smurfs (puuid, name, tag, score, rank, level, kd, acs, hs_percent,
                                    match_count, adr, win_rate, model_version, signals, last_analyzed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(puuid) DO UPDATE SET
                    name=excluded.name,
                    tag=excluded.tag,
                    score=excluded.score,
                    rank=excluded.rank,
                    level=excluded.level,
                    kd=excluded.kd,
                    acs=excluded.acs,
                    hs_percent=excluded.hs_percent,
                    match_count=excluded.match_count,
                    adr=excluded.adr,
                    win_rate=excluded.win_rate,
                    model_version=excluded.model_version,
                    signals=excluded.signals,
                    last_analyzed=CURRENT_TIMESTAMP
            ''', (puuid, name, tag, score, rank, level, kd, acs, hs_percent,
                  match_count, adr, win_rate, MODEL_VERSION, _to_json(signals)))
            conn.execute('''
                INSERT INTO smurf_history (puuid, score, rank, level, kd, acs, hs_percent, match_count, adr, win_rate, model_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (puuid, score, rank, level, kd, acs, hs_percent, match_count, adr, win_rate, MODEL_VERSION))
    finally:
        conn.close()

def record_guild_player(guild_id, puuid):
    db_query("INSERT OR IGNORE INTO guild_players (guild_id, puuid) VALUES (?, ?)", (str(guild_id), puuid))

def get_player_history(name, tag, guild_id=None, limit=10):
    # Renvoie (les analyses les plus récentes, le nombre total d'analyses)
    # lignes : (name, tag, score, rank, kd, ts, match_count, model_version)
    sql = f'''
        SELECT s.name, s.tag, h.score, h.rank, h.kd, {_unix("h.analyzed_at")}, h.match_count, h.model_version
        FROM smurf_history h JOIN smurfs s ON s.puuid = h.puuid
        WHERE casefold(s.name) = ? AND casefold(s.tag) = ?
    '''
    params = [name.casefold(), tag.casefold()]
    if guild_id is not None:
        sql += " AND s.puuid IN (SELECT puuid FROM guild_players WHERE guild_id = ?)"
        params.append(str(guild_id))
    sql += " ORDER BY h.analyzed_at DESC, h.id DESC"
    rows = db_query(sql, params)
    return rows[:limit], len(rows)

def _like_pattern(text):
    escaped = text.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

def search_players(guild_id, text, limit=25):
    # Autocomplétion : seulement les joueurs déjà analysés sur ce serveur, les plus récents d'abord
    # (proposer ceux des autres serveurs révélerait qui a été analysé ailleurs)
    return db_query('''
        SELECT s.name, s.tag, s.rank, s.score
        FROM smurfs s JOIN guild_players g ON g.puuid = s.puuid AND g.guild_id = ?
        WHERE s.name IS NOT NULL AND s.tag IS NOT NULL
          AND casefold(s.name || '#' || s.tag) LIKE ? ESCAPE '\\'
        ORDER BY s.last_analyzed DESC, s.name
        LIMIT ?
    ''', (_id(guild_id), _like_pattern(text or ""), limit))

def find_player(name, tag, guild_id=None):
    # (puuid, name, tag) d'un joueur déjà analysé (sur ce serveur si guild_id), le plus récent ; None sinon
    sql = "SELECT s.puuid, s.name, s.tag FROM smurfs s WHERE casefold(s.name) = ? AND casefold(s.tag) = ?"
    params = [name.casefold(), tag.casefold()]
    if guild_id is not None:
        sql += " AND s.puuid IN (SELECT puuid FROM guild_players WHERE guild_id = ?)"
        params.append(str(guild_id))
    rows = db_query(sql + " ORDER BY s.last_analyzed DESC LIMIT 1", params)
    return rows[0] if rows else None

# ================= COMPTES LIÉS =================
def get_linked_account(user_id):
    rows = db_query("SELECT name, tag FROM linked_accounts WHERE user_id = ?", (str(user_id),))
    return rows[0] if rows else None

def set_linked_account(user_id, name, tag):
    db_query("INSERT OR REPLACE INTO linked_accounts (user_id, name, tag) VALUES (?, ?, ?)", (str(user_id), name, tag))

def delete_linked_account(user_id):
    linked = get_linked_account(user_id)
    db_query("DELETE FROM linked_accounts WHERE user_id = ?", (str(user_id),))
    return linked

# ================= SUIVI DE RANG (/watch) =================
WATCH_COLUMNS = ["guild_id", "puuid", "channel_id", "user_id", "name", "tag", "region", "last_rank", "last_elo"]

def get_watches(guild_id=None):
    # guild_id=None : tous les suivis, tous serveurs confondus
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

def purge_guild(guild_id):
    # Le bot a quitté le serveur : suivis et réglages supprimés (les dossiers restent pour la règle des 7 jours)
    conn = connect()
    try:
        with conn:
            removed = conn.execute("DELETE FROM watchlist WHERE guild_id = ?", (str(guild_id),)).rowcount
            removed += conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (str(guild_id),)).rowcount
        return removed
    finally:
        conn.close()

# ================= DOSSIERS (/report) =================
REPORT_COLUMNS = ["id", "guild_id", "reporter_id", "puuid", "name", "tag", "score", "level", "signals", "match_ids",
                  "observations", "channel_id", "message_id", "status", "reviewed_by", "reviewed_at", "created_at"]
_REPORT_SELECT = ", ".join(_unix(c) if c in ("reviewed_at", "created_at") else c for c in REPORT_COLUMNS)

def _report_dict(row):
    report = dict(zip(REPORT_COLUMNS, row))
    report["signals"] = _from_json(report["signals"])
    report["match_ids"] = _from_json(report["match_ids"])
    return report

def create_report(guild_id, reporter_id, puuid, name, tag, score, level, signals, match_ids, observations):
    report_id, _ = _execute('''
        INSERT INTO reports (guild_id, reporter_id, puuid, name, tag, score, level, signals, match_ids, observations)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (str(guild_id), str(reporter_id), puuid, name, tag, score, level, _to_json(signals),
          json.dumps(list(match_ids or []), ensure_ascii=False), observations or None))
    return report_id

def set_report_message(report_id, channel_id, message_id):
    db_query("UPDATE reports SET channel_id = ?, message_id = ? WHERE id = ?", (_id(channel_id), _id(message_id), report_id))

def delete_unpublished_report(report_id):
    # Dossier jamais publié (envoi impossible) : retiré pour ne pas bloquer le membre pendant 7 jours
    _, changed = _execute("DELETE FROM reports WHERE id = ? AND message_id IS NULL", (report_id,))
    return changed == 1

def count_reports(guild_id, puuid):
    # Dossiers déjà ouverts sur ce compte dans ce serveur (pour repérer les signalements en groupe)
    return db_query("SELECT COUNT(*) FROM reports WHERE guild_id = ? AND puuid = ?", (str(guild_id), puuid))[0][0]

def get_report(report_id):
    rows = db_query(f"SELECT {_REPORT_SELECT} FROM reports WHERE id = ?", (report_id,))
    return _report_dict(rows[0]) if rows else None

def review_report(report_id, status, reviewer_id):
    # Un dossier ne peut être traité qu'une fois (deux modérateurs qui cliquent en même temps : un seul gagne)
    if status not in ("validated", "rejected"):
        raise ValueError(f"Statut de dossier invalide : {status!r}")
    _, changed = _execute('''
        UPDATE reports SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending'
    ''', (status, str(reviewer_id), report_id))
    return changed == 1

def last_report_by(reporter_id, puuid, days=7):
    # Dernier dossier de ce membre sur ce compte, tous serveurs confondus, dans la fenêtre
    rows = db_query(f'''
        SELECT {_REPORT_SELECT} FROM reports
        WHERE reporter_id = ? AND puuid = ? AND created_at > datetime('now', ?)
        ORDER BY created_at DESC, id DESC LIMIT 1
    ''', (str(reporter_id), puuid, _window(days * 24)))
    return _report_dict(rows[0]) if rows else None

REPORTED_KEYS = ["puuid", "name", "tag", "reports", "reporters", "max_score", "last_at", "last_id",
                 "pending", "validated", "rejected"]

def list_reported(guild_id, status=None):
    # Un élément par compte signalé sur ce serveur ; ceux qui attendent une validation d'abord
    if status is not None and status not in REPORT_STATUSES:
        raise ValueError(f"Statut de dossier invalide : {status!r}")
    sql = f'''
        WITH g AS (
            SELECT puuid,
                   COUNT(*) AS reports,
                   COUNT(DISTINCT reporter_id) AS reporters,
                   MAX(score) AS max_score,
                   MAX(created_at) AS last_created,
                   MAX(id) AS last_id,
                   SUM(status = 'pending') AS pending,
                   SUM(status = 'validated') AS validated,
                   SUM(status = 'rejected') AS rejected
            FROM reports WHERE guild_id = ? GROUP BY puuid
        )
        SELECT g.puuid, r.name, r.tag, g.reports, g.reporters, g.max_score, {_unix("g.last_created")}, g.last_id,
               g.pending, g.validated, g.rejected
        FROM g JOIN reports r ON r.id = g.last_id
    '''
    if status is not None:
        sql += f" WHERE g.{status} > 0"
    sql += " ORDER BY (g.pending > 0) DESC, g.last_created DESC, g.last_id DESC"
    return [dict(zip(REPORTED_KEYS, row)) for row in db_query(sql, (str(guild_id),))]

# ================= ANTI-ACHARNEMENT =================
ACTIVITY_KINDS = ("check", "report", "report_refused", "alert")

def log_activity(guild_id, user_id, puuid, kind):
    db_query("INSERT INTO activity (guild_id, user_id, puuid, kind) VALUES (?, ?, ?, ?)",
             (_id(guild_id), str(user_id), puuid, kind))

def count_activity(user_id, puuid, kinds, hours, guild_id=None):
    # guild_id=None : tous serveurs confondus
    kinds = [kinds] if isinstance(kinds, str) else list(kinds)
    if not kinds:
        return 0
    sql = f'''
        SELECT COUNT(*) FROM activity
        WHERE user_id = ? AND puuid = ? AND kind IN ({", ".join("?" * len(kinds))})
          AND created_at > datetime('now', ?)
    '''
    params = [str(user_id), puuid, *kinds, _window(hours)]
    if guild_id is not None:
        sql += " AND guild_id = ?"
        params.append(str(guild_id))
    return db_query(sql, params)[0][0]

def prune_activity(days=30):
    # Le journal ne sert qu'aux fenêtres anti-acharnement (7 jours au plus) : inutile de garder plus
    _, removed = _execute("DELETE FROM activity WHERE created_at < datetime('now', ?)", (_window(days * 24),))
    return removed

# ================= RÉGLAGES DU SERVEUR =================
def get_reports_channel_id(guild_id):
    rows = db_query("SELECT reports_channel_id FROM guild_settings WHERE guild_id = ?", (str(guild_id),))
    if not rows or rows[0][0] is None:
        return None
    try:
        return int(rows[0][0])
    except ValueError:
        return None

def set_reports_channel_id(guild_id, channel_id):
    db_query('''
        INSERT INTO guild_settings (guild_id, reports_channel_id) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET reports_channel_id = excluded.reports_channel_id
    ''', (str(guild_id), _id(channel_id)))

# ================= SAUVEGARDE =================
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
