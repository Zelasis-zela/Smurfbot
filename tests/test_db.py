import os
import json
import time
import shutil
import sqlite3
import hashlib
import tempfile
import unittest
from dataclasses import dataclass
from unittest import mock

from core import db

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DB = os.path.join(PROJECT_DIR, "smurfs_leaderboard.db")
V1_TABLES = ["smurfs", "smurf_history", "guild_players", "linked_accounts", "watchlist"]
V2_TABLES = ["reports", "activity", "guild_settings"]

# Schéma v1 exact de l'ancien cogs/smurf_detector.py (init_db), tel qu'il existe en production
V1_SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS smurfs (
            puuid TEXT PRIMARY KEY, name TEXT, tag TEXT, score INTEGER, rank TEXT, level INTEGER,
            kd REAL, acs REAL, hs_percent REAL, last_analyzed TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS smurf_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, puuid TEXT NOT NULL, score INTEGER, rank TEXT, level INTEGER,
            kd REAL, acs REAL, hs_percent REAL, analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    'CREATE INDEX IF NOT EXISTS idx_history_puuid ON smurf_history (puuid)',
    '''CREATE TABLE IF NOT EXISTS guild_players (
            guild_id TEXT NOT NULL, puuid TEXT NOT NULL, PRIMARY KEY (guild_id, puuid))''',
    '''CREATE TABLE IF NOT EXISTS linked_accounts (user_id TEXT PRIMARY KEY, name TEXT, tag TEXT)''',
    '''CREATE TABLE IF NOT EXISTS watchlist (
            guild_id TEXT NOT NULL, puuid TEXT NOT NULL, channel_id TEXT NOT NULL, user_id TEXT NOT NULL,
            name TEXT, tag TEXT, region TEXT, last_rank TEXT, last_elo INTEGER, PRIMARY KEY (guild_id, puuid))''',
]

def ago(seconds):
    # Horodatage SQLite (UTC, format CURRENT_TIMESTAMP) d'il y a `seconds` secondes
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - seconds))

HOUR = 3600
DAY = 24 * HOUR

class TempDbCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        self.path = os.path.join(self.tmpdir, "test.db")
        self.backup_dir = os.path.join(self.tmpdir, "backups")
        for name, value in (("DB_FILE", self.path), ("BACKUP_DIR", self.backup_dir)):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sql(self, query, params=(), path=None):
        conn = sqlite3.connect(path or self.path)
        try:
            rows = conn.execute(query, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def columns(self, table, path=None):
        return [row[1] for row in self.sql(f"PRAGMA table_info({table})", path=path)]

    def tables(self, path=None):
        return {row[0] for row in self.sql("SELECT name FROM sqlite_master WHERE type = 'table'", path=path)}

    def user_version(self, path=None):
        return self.sql("PRAGMA user_version", path=path)[0][0]

    def schema_dump(self, path=None):
        return self.sql("SELECT type, name, sql FROM sqlite_master ORDER BY type, name", path=path)

    def counts(self, tables, path=None):
        return {t: self.sql(f"SELECT COUNT(*) FROM {t}", path=path)[0][0] for t in tables}

    def build_v1(self, path=None):
        conn = sqlite3.connect(path or self.path)
        try:
            for statement in V1_SCHEMA:
                conn.execute(statement)
            conn.executemany("INSERT INTO smurfs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                ("p1", "Élodie", "EUW", 72, "Gold 2", 25, 1.9, 260.5, 31.2, "2026-10-01 10:00:00"),
                ("p2", "Bob", "1234", 15, "Silver 1", 210, 1.0, 190.0, 18.0, "2026-10-02 11:00:00"),
                ("p3", "Sans Historique", "X1", 40, "Platinum 1", 80, 1.3, 220.0, 25.0, "2026-10-03 12:00:00"),
            ])
            conn.executemany("INSERT INTO smurf_history (puuid, score, rank, level, kd, acs, hs_percent, analyzed_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                ("p1", 60, "Gold 1", 24, 1.7, 240.0, 29.0, "2026-09-30 09:00:00"),
                ("p1", 72, "Gold 2", 25, 1.9, 260.5, 31.2, "2026-10-01 10:00:00"),
                ("p2", 15, "Silver 1", 210, 1.0, 190.0, 18.0, "2026-10-02 11:00:00"),
            ])
            conn.executemany("INSERT INTO guild_players VALUES (?, ?)", [("100", "p1"), ("100", "p2"), ("200", "p3")])
            conn.execute("INSERT INTO linked_accounts VALUES ('42', 'Bob', '1234')")
            conn.execute("INSERT INTO watchlist VALUES ('100', 'p1', '555', '42', 'Élodie', 'EUW', 'eu', 'Gold 2', 1234)")
            conn.commit()
        finally:
            conn.close()

class MigrationTests(TempDbCase):
    def test_fresh_database(self):
        db.init_db()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        self.assertTrue(set(V1_TABLES + V2_TABLES) <= self.tables())
        for col in ("match_count", "adr", "win_rate", "model_version", "signals"):
            self.assertIn(col, self.columns("smurfs"))
        for col in ("match_count", "adr", "win_rate", "model_version"):
            self.assertIn(col, self.columns("smurf_history"))
        self.assertNotIn("signals", self.columns("smurf_history"))
        indexes = {row[0] for row in self.sql("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"idx_history_puuid", "idx_reports_reporter", "idx_reports_guild_status", "idx_activity_lookup"} <= indexes)
        # Base neuve : pas de copie « avant migration »
        self.assertFalse(os.path.exists(os.path.join(self.backup_dir, "avant-migration-v2.db")))

    def test_v1_migration_keeps_every_row(self):
        self.build_v1()
        before = self.counts(V1_TABLES)
        old_smurfs = self.sql("SELECT puuid, name, tag, score, rank, level, kd, acs, hs_percent, last_analyzed FROM smurfs ORDER BY puuid")
        old_history = self.sql("SELECT id, puuid, score, rank, level, kd, acs, hs_percent, analyzed_at FROM smurf_history ORDER BY id")

        db.init_db()

        self.assertEqual(self.user_version(), 2)
        after = self.counts(V1_TABLES)
        # p3 n'avait pas d'historique : l'ancien remplissage lui en crée un, rien d'autre ne change
        self.assertEqual(after, {**before, "smurf_history": before["smurf_history"] + 1})
        self.assertEqual(self.sql("SELECT puuid, name, tag, score, rank, level, kd, acs, hs_percent, last_analyzed FROM smurfs ORDER BY puuid"), old_smurfs)
        self.assertEqual(self.sql("SELECT id, puuid, score, rank, level, kd, acs, hs_percent, analyzed_at FROM smurf_history WHERE id <= 3 ORDER BY id"), old_history)
        self.assertEqual(self.sql("SELECT puuid, score, analyzed_at FROM smurf_history WHERE id = 4"), [("p3", 40, "2026-10-03 12:00:00")])
        # Anciennes lignes : nouvelles colonnes à NULL (= ancien calcul)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM smurfs WHERE model_version IS NULL AND match_count IS NULL AND signals IS NULL")[0][0], 3)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM smurf_history WHERE model_version IS NULL")[0][0], 4)
        self.assertTrue(set(V2_TABLES) <= self.tables())
        self.assertEqual(self.counts(V2_TABLES), {t: 0 for t in V2_TABLES})

        # Copie de sécurité prise avant la migration, encore en v1
        backup = os.path.join(self.backup_dir, "avant-migration-v2.db")
        self.assertTrue(os.path.exists(backup))
        self.assertEqual(self.user_version(backup), 0)
        self.assertEqual(self.counts(V1_TABLES, backup), before)
        self.assertNotIn("model_version", self.columns("smurfs", backup))

    def test_second_init_is_a_no_op(self):
        self.build_v1()
        db.init_db()
        schema, counts = self.schema_dump(), self.counts(V1_TABLES + V2_TABLES)
        backup = os.path.join(self.backup_dir, "avant-migration-v2.db")
        backup_mtime = os.path.getmtime(backup)
        db.init_db()
        db.init_db()
        self.assertEqual(self.user_version(), 2)
        self.assertEqual(self.schema_dump(), schema)
        self.assertEqual(self.counts(V1_TABLES + V2_TABLES), counts)
        self.assertEqual(os.path.getmtime(backup), backup_mtime)

    def test_failed_migration_rolls_back(self):
        self.build_v1()
        before = self.counts(V1_TABLES)

        def broken(cursor):
            db._migrate_v2(cursor)
            raise RuntimeError("panne simulée")

        with mock.patch.object(db, "MIGRATIONS", [(2, broken)]):
            with self.assertRaises(RuntimeError):
                db.init_db()
        # Rien n'est resté à moitié fait : ni colonne, ni table, ni version, ni historique rempli
        self.assertEqual(self.user_version(), 0)
        self.assertNotIn("model_version", self.columns("smurfs"))
        self.assertFalse(set(V2_TABLES) & self.tables())
        self.assertEqual(self.counts(V1_TABLES), before)
        # La migration normale passe ensuite
        db.init_db()
        self.assertEqual(self.user_version(), 2)
        self.assertIn("model_version", self.columns("smurfs"))

    def test_existing_columns_are_not_added_twice(self):
        self.build_v1()
        self.sql("ALTER TABLE smurfs ADD COLUMN adr REAL")
        self.sql("UPDATE smurfs SET adr = 150.5 WHERE puuid = 'p1'")
        db.init_db()
        self.assertEqual(self.columns("smurfs").count("adr"), 1)
        self.assertEqual(self.sql("SELECT adr FROM smurfs WHERE puuid = 'p1'")[0][0], 150.5)
        self.assertEqual(self.user_version(), 2)

    def test_reports_status_defaults_to_pending(self):
        db.init_db()
        self.sql("INSERT INTO reports (guild_id, reporter_id, puuid) VALUES ('1', '2', 'p')")
        self.assertEqual(self.sql("SELECT status, created_at IS NOT NULL FROM reports")[0], ("pending", 1))

def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

@unittest.skipUnless(os.path.exists(REAL_DB), "base de production absente")
class RealDatabaseCopyTests(TempDbCase):
    # Migration jouée sur une COPIE de smurfs_leaderboard.db ; l'original n'est jamais ouvert en écriture
    def test_migration_on_copy_of_real_database(self):
        original_hash = file_sha256(REAL_DB)
        shutil.copy(REAL_DB, self.path)
        before = self.counts(V1_TABLES)
        missing_history = self.sql("SELECT COUNT(*) FROM smurfs WHERE puuid NOT IN (SELECT puuid FROM smurf_history)")[0][0]
        old_smurfs = self.sql("SELECT * FROM smurfs ORDER BY puuid")
        version_before = self.user_version()

        db.init_db()

        self.assertEqual(self.user_version(), 2)
        after = self.counts(V1_TABLES)
        self.assertEqual(after, {**before, "smurf_history": before["smurf_history"] + missing_history})
        # Les colonnes d'origine sont inchangées, les nouvelles sont à NULL
        self.assertEqual(self.sql(f"SELECT {', '.join(c for c in self.columns('smurfs') if c not in ('match_count', 'adr', 'win_rate', 'model_version', 'signals'))} FROM smurfs ORDER BY puuid"), old_smurfs)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM smurfs WHERE model_version IS NOT NULL")[0][0], 0)
        if version_before < 2 and before["smurfs"]:
            self.assertTrue(os.path.exists(os.path.join(self.backup_dir, "avant-migration-v2.db")))

        # Relancer ne change rien
        schema = self.schema_dump()
        db.init_db()
        self.assertEqual(self.schema_dump(), schema)
        self.assertEqual(self.counts(V1_TABLES), after)

        # Les requêtes fonctionnent sur les données réelles
        name, tag = self.sql("SELECT name, tag FROM smurfs WHERE name IS NOT NULL AND tag IS NOT NULL LIMIT 1")[0]
        rows, total = db.get_player_history(name.casefold(), tag.casefold())
        self.assertGreaterEqual(total, 1)
        self.assertIsInstance(rows[0][5], int)
        guild_player = self.sql("SELECT s.name, g.guild_id FROM smurfs s JOIN guild_players g ON g.puuid = s.puuid "
                                "WHERE s.name IS NOT NULL AND s.tag IS NOT NULL LIMIT 1")
        if guild_player:   # l'autocomplétion ne propose que les joueurs analysés sur ce serveur
            self.assertTrue(db.search_players(guild_player[0][1], guild_player[0][0][:3]))
        # L'original n'a pas bougé
        self.assertEqual(file_sha256(REAL_DB), original_hash)

class AnalysisTests(TempDbCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def save(self, puuid="p1", name="Élodie", tag="EUW", score=70, **kw):
        values = dict(rank="Or 2", level=25, kd=1.9, acs=260.0, hs_percent=31.0, adr=170.0, win_rate=75.0,
                      match_count=20, signals=[{"key": "kd", "label": "K/D", "points": 10, "max_points": 16}])
        values.update(kw)
        db.save_analysis(puuid, name, tag, score, values["rank"], values["level"], values["kd"], values["acs"],
                         values["hs_percent"], values["adr"], values["win_rate"], values["match_count"], values["signals"])

    def test_save_analysis_upserts_and_appends_history(self):
        self.save(score=70)
        self.save(score=80, rank="Or 3", name="Élodie2")
        rows = self.sql("SELECT name, score, rank, match_count, adr, win_rate, model_version, signals FROM smurfs")
        self.assertEqual(len(rows), 1)
        name, score, rank, match_count, adr, win_rate, model_version, signals = rows[0]
        self.assertEqual((name, score, rank, match_count, adr, win_rate, model_version), ("Élodie2", 80, "Or 3", 20, 170.0, 75.0, 2))
        self.assertEqual(json.loads(signals)[0]["key"], "kd")
        history = self.sql("SELECT score, model_version, match_count FROM smurf_history ORDER BY id")
        self.assertEqual(history, [(70, 2, 20), (80, 2, 20)])

    def test_save_analysis_accepts_dataclass_signals(self):
        @dataclass
        class Signal:
            key: str
            label: str
            points: int
            max_points: int
        self.save(signals=[Signal("headshot", "HS % de 34,1 %", 12, 18)])
        self.assertEqual(json.loads(self.sql("SELECT signals FROM smurfs")[0][0]),
                         [{"key": "headshot", "label": "HS % de 34,1 %", "points": 12, "max_points": 18}])
        self.save(signals=None)
        self.assertEqual(json.loads(self.sql("SELECT signals FROM smurfs")[0][0]), [])

    def test_record_guild_player_is_idempotent(self):
        db.record_guild_player(100, "p1")
        db.record_guild_player("100", "p1")
        self.assertEqual(self.sql("SELECT guild_id, puuid FROM guild_players"), [("100", "p1")])

    def test_player_history(self):
        self.save(score=50)
        self.save(puuid="p2", name="Autre", tag="EUW", score=10)
        # Une ancienne ligne (avant la refonte) et des dates explicites pour l'ordre
        self.sql("INSERT INTO smurf_history (puuid, score, rank, kd, analyzed_at) VALUES ('p1', 30, 'Gold 1', 1.2, ?)", (ago(10 * DAY),))
        self.sql("UPDATE smurf_history SET analyzed_at = ? WHERE puuid = 'p1' AND model_version = 2", (ago(DAY),))
        self.save(score=65)   # la plus récente
        db.record_guild_player(100, "p1")

        rows, total = db.get_player_history("ÉLODIE", "euw")
        self.assertEqual(total, 3)
        self.assertEqual([r[2] for r in rows], [65, 50, 30])
        name, tag, score, rank, kd, ts, match_count, model_version = rows[0]
        self.assertEqual((name, tag, rank, match_count, model_version), ("Élodie", "EUW", "Or 2", 20, 2))
        self.assertIsInstance(ts, int)
        self.assertLess(abs(ts - time.time()), 120)
        self.assertEqual(rows[2][6:], (None, None))     # ancien calcul : match_count / model_version NULL

        rows, total = db.get_player_history("élodie", "EUW", limit=2)
        self.assertEqual((len(rows), total), (2, 3))
        # Filtre serveur : seulement les joueurs analysés sur ce serveur
        self.assertEqual(db.get_player_history("Élodie", "EUW", guild_id=100)[1], 3)
        self.assertEqual(db.get_player_history("Élodie", "EUW", guild_id=200), ([], 0))
        self.assertEqual(db.get_player_history("Autre", "EUW", guild_id=100), ([], 0))
        self.assertEqual(db.get_player_history("Inconnu", "X"), ([], 0))

    def test_search_players(self):
        self.save(puuid="p1", name="Alpha", tag="EUW", score=10)
        self.save(puuid="p2", name="Alphonse", tag="FR1", score=20)
        self.save(puuid="p3", name="Élan", tag="AL1", score=30)
        self.save(puuid="p4", name="50%_off", tag="X", score=40)
        self.sql("UPDATE smurfs SET last_analyzed = ? WHERE puuid = 'p1'", (ago(3 * DAY),))
        self.sql("UPDATE smurfs SET last_analyzed = ? WHERE puuid = 'p2'", (ago(2 * DAY),))
        self.sql("UPDATE smurfs SET last_analyzed = ? WHERE puuid = 'p3'", (ago(1 * DAY),))
        for puuid in ("p1", "p2", "p3", "p4"):
            db.record_guild_player(100, puuid)
        db.record_guild_player(200, "p1")

        # Seulement les joueurs analysés sur ce serveur, les plus récents d'abord ; « Nom#TAG » sans casse ni accents
        self.assertEqual([r[0] for r in db.search_players(100, "AL")], ["Élan", "Alphonse", "Alpha"])
        self.assertEqual([r[0] for r in db.search_players(200, "al")], ["Alpha"])
        self.assertEqual(db.search_players(300, "al"), [])     # rien des autres serveurs
        self.assertEqual(db.search_players(None, "al"), [])
        self.assertEqual(db.search_players(100, "élan#")[0], ("Élan", "AL1", "Or 2", 30))
        self.assertEqual([r[0] for r in db.search_players(100, "#fr")], ["Alphonse"])
        # Les jokers LIKE sont pris à la lettre
        self.assertEqual([r[0] for r in db.search_players(100, "%")], ["50%_off"])
        self.assertEqual([r[0] for r in db.search_players(100, "_off")], ["50%_off"])
        self.assertEqual(db.search_players(100, "a_p"), [])
        self.assertEqual(len(db.search_players(100, "")), 4)
        self.assertEqual(len(db.search_players(100, "", limit=2)), 2)
        self.assertEqual(db.search_players(100, "zzz"), [])

class LinkAndWatchTests(TempDbCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def test_linked_accounts(self):
        self.assertIsNone(db.get_linked_account(42))
        db.set_linked_account(42, "Bob", "1234")
        self.assertEqual(db.get_linked_account("42"), ("Bob", "1234"))
        db.set_linked_account(42, "Bobby", "9999")
        self.assertEqual(db.get_linked_account(42), ("Bobby", "9999"))
        self.assertEqual(db.delete_linked_account(42), ("Bobby", "9999"))
        self.assertIsNone(db.delete_linked_account(42))

    def test_watches(self):
        player = {"puuid": "p1", "name": "Zed", "tag": "EUW", "region": "eu"}
        db.add_watch(100, 555, 42, player, {"rank": "Or 2", "elo": 1200})
        db.add_watch(200, 556, 43, {**player, "name": "Zed"}, {"rank": None, "elo": None})
        watches = db.get_watches(100)
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0], {"guild_id": "100", "puuid": "p1", "channel_id": "555", "user_id": "42", "name": "Zed",
                                      "tag": "EUW", "region": "eu", "last_rank": "Or 2", "last_elo": 1200})
        self.assertEqual(len(db.get_watches()), 2)
        db.update_watch_state(100, "p1", "Or 3", 1290)
        self.assertEqual((db.get_watches(100)[0]["last_rank"], db.get_watches(100)[0]["last_elo"]), ("Or 3", 1290))
        db.remove_watch(100, "p1")
        self.assertEqual(db.get_watches(100), [])
        self.assertEqual(len(db.get_watches(200)), 1)
        self.assertEqual(db.WATCH_COLUMNS, ["guild_id", "puuid", "channel_id", "user_id", "name", "tag", "region", "last_rank", "last_elo"])

    def test_purge_guild(self):
        player = {"puuid": "p1", "name": "Zed", "tag": "EUW", "region": "eu"}
        db.add_watch(100, 555, 42, player, {"rank": "Or 2", "elo": 1200})
        db.add_watch(200, 556, 42, player, {"rank": "Or 2", "elo": 1200})
        db.set_reports_channel_id(100, 777)
        db.set_reports_channel_id(200, 888)
        report_id = db.create_report(100, 42, "p1", "Zed", "EUW", 70, "eleve", [], [], None)
        self.assertEqual(db.purge_guild(100), 2)
        self.assertEqual(db.get_watches(100), [])
        self.assertIsNone(db.get_reports_channel_id(100))
        # Les autres serveurs et les dossiers (règle des 7 jours) restent
        self.assertEqual(len(db.get_watches(200)), 1)
        self.assertEqual(db.get_reports_channel_id(200), 888)
        self.assertIsNotNone(db.get_report(report_id))
        self.assertEqual(db.purge_guild(100), 0)

    def test_reports_channel(self):
        self.assertIsNone(db.get_reports_channel_id(100))
        db.set_reports_channel_id(100, 123456789012345678)
        self.assertEqual(db.get_reports_channel_id("100"), 123456789012345678)
        db.set_reports_channel_id(100, 987)
        self.assertEqual(db.get_reports_channel_id(100), 987)
        db.set_reports_channel_id(100, None)
        self.assertIsNone(db.get_reports_channel_id(100))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM guild_settings")[0][0], 1)

class ReportTests(TempDbCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def report(self, guild=100, reporter=42, puuid="p1", name="Zed", tag="EUW", score=70, created=None, status=None):
        report_id = db.create_report(guild, reporter, puuid, name, tag, score, "eleve",
                                     [{"key": "kd", "label": "K/D de 2,10", "points": 12, "max_points": 16}],
                                     ["m1", "m2"], "A joué très haut niveau")
        if created is not None:
            self.sql("UPDATE reports SET created_at = ? WHERE id = ?", (ago(created), report_id))
        if status is not None:
            self.sql("UPDATE reports SET status = ? WHERE id = ?", (status, report_id))
        return report_id

    def test_create_and_get_report(self):
        report_id = self.report()
        self.assertIsInstance(report_id, int)
        report = db.get_report(report_id)
        self.assertEqual(set(report), set(db.REPORT_COLUMNS))
        self.assertEqual((report["guild_id"], report["reporter_id"], report["puuid"], report["name"], report["tag"]),
                         ("100", "42", "p1", "Zed", "EUW"))
        self.assertEqual((report["score"], report["level"], report["status"]), (70, "eleve", "pending"))
        self.assertEqual(report["signals"][0]["points"], 12)
        self.assertEqual(report["match_ids"], ["m1", "m2"])
        self.assertEqual(report["observations"], "A joué très haut niveau")
        self.assertIsInstance(report["created_at"], int)
        self.assertLess(abs(report["created_at"] - time.time()), 120)
        self.assertIsNone(report["reviewed_at"])
        self.assertIsNone(report["reviewed_by"])
        self.assertIsNone(report["channel_id"])
        db.set_report_message(report_id, 555, 999888777666)
        report = db.get_report(report_id)
        self.assertEqual((report["channel_id"], report["message_id"]), ("555", "999888777666"))
        self.assertIsNone(db.get_report(report_id + 1))
        # Observations vides → NULL
        empty = db.create_report(100, 42, "p2", "A", "B", 61, "eleve", [], [], "")
        self.assertIsNone(db.get_report(empty)["observations"])
        self.assertEqual(db.get_report(empty)["signals"], [])

    def test_review_report_only_once(self):
        report_id = self.report()
        self.assertTrue(db.review_report(report_id, "validated", 7))
        report = db.get_report(report_id)
        self.assertEqual((report["status"], report["reviewed_by"]), ("validated", "7"))
        self.assertIsInstance(report["reviewed_at"], int)
        self.assertFalse(db.review_report(report_id, "rejected", 8))
        self.assertFalse(db.review_report(report_id, "validated", 8))
        report = db.get_report(report_id)
        self.assertEqual((report["status"], report["reviewed_by"]), ("validated", "7"))
        self.assertFalse(db.review_report(report_id + 50, "rejected", 8))
        other = self.report(puuid="p2")
        self.assertTrue(db.review_report(other, "rejected", 9))
        self.assertEqual(db.get_report(other)["status"], "rejected")
        with self.assertRaises(ValueError):
            db.review_report(self.report(puuid="p3"), "pending", 9)

    def test_last_report_by_window_and_guilds(self):
        self.assertIsNone(db.last_report_by(42, "p1"))
        old = self.report(guild=100, created=7 * DAY + 60)        # juste hors de la fenêtre de 7 jours
        self.assertIsNone(db.last_report_by(42, "p1"))
        inside = self.report(guild=200, created=7 * DAY - 60)     # autre serveur, juste dans la fenêtre
        found = db.last_report_by("42", "p1")
        self.assertEqual(found["id"], inside)
        self.assertEqual(found["guild_id"], "200")
        self.assertIsInstance(found["created_at"], int)
        recent = self.report(guild=100, created=HOUR)
        self.assertEqual(db.last_report_by(42, "p1")["id"], recent)
        # Autre membre / autre compte : rien
        self.assertIsNone(db.last_report_by(43, "p1"))
        self.assertIsNone(db.last_report_by(42, "p2"))
        # Fenêtre configurable
        self.assertIsNone(db.last_report_by(42, "p1", days=0.01))
        self.assertEqual(db.last_report_by(42, "p1", days=30)["id"], recent)
        self.assertNotEqual(old, inside)

    def test_list_reported_aggregation(self):
        # p1 : 3 dossiers par 2 membres, renommé entre-temps ; p2 : 1 validé ; p3 : 1 rejeté ; p4 : autre serveur
        self.report(reporter=42, puuid="p1", name="Ancien", score=70, created=5 * DAY, status="validated")
        self.report(reporter=43, puuid="p1", name="Ancien", score=85, created=4 * DAY, status="rejected")
        p1_last = self.report(reporter=42, puuid="p1", name="Nouveau", tag="NEW", score=65, created=3 * DAY)
        p2 = self.report(reporter=44, puuid="p2", name="Deux", score=90, created=HOUR, status="validated")
        p3 = self.report(reporter=45, puuid="p3", name="Trois", score=62, created=2 * HOUR, status="rejected")
        self.report(guild=200, reporter=42, puuid="p4", name="Ailleurs", created=60)

        listed = db.list_reported(100)
        self.assertEqual([r["puuid"] for r in listed], ["p1", "p2", "p3"])   # en attente d'abord, puis le plus récent
        p1 = listed[0]
        self.assertEqual(set(p1), set(db.REPORTED_KEYS))
        self.assertEqual((p1["name"], p1["tag"]), ("Nouveau", "NEW"))
        self.assertEqual((p1["reports"], p1["reporters"], p1["max_score"], p1["last_id"]), (3, 2, 85, p1_last))
        self.assertEqual((p1["pending"], p1["validated"], p1["rejected"]), (1, 1, 1))
        self.assertIsInstance(p1["last_at"], int)
        self.assertLess(abs(p1["last_at"] - (time.time() - 3 * DAY)), 120)
        self.assertEqual((listed[1]["last_id"], listed[1]["validated"], listed[1]["pending"]), (p2, 1, 0))
        self.assertEqual((listed[2]["last_id"], listed[2]["rejected"]), (p3, 1))

        # Filtre : comptes ayant au moins un dossier de ce statut (compteurs complets conservés)
        self.assertEqual([r["puuid"] for r in db.list_reported(100, "pending")], ["p1"])
        self.assertEqual([r["puuid"] for r in db.list_reported(100, "validated")], ["p1", "p2"])
        self.assertEqual([r["puuid"] for r in db.list_reported(100, "rejected")], ["p1", "p3"])
        self.assertEqual(db.list_reported(100, "validated")[0]["reports"], 3)
        self.assertEqual([r["puuid"] for r in db.list_reported("200")], ["p4"])
        self.assertEqual(db.list_reported(300), [])
        with self.assertRaises(ValueError):
            db.list_reported(100, "tous")

    def test_list_reported_orders_pending_by_recency(self):
        a = self.report(puuid="a", created=2 * DAY)
        b = self.report(puuid="b", created=DAY)
        c = self.report(puuid="c", created=HOUR, status="validated")
        self.assertEqual([r["last_id"] for r in db.list_reported(100)], [b, a, c])

class ActivityTests(TempDbCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def add(self, kind, seconds_ago, user="42", puuid="p1", guild="100"):
        self.sql("INSERT INTO activity (guild_id, user_id, puuid, kind, created_at) VALUES (?, ?, ?, ?, ?)",
                 (guild, user, puuid, kind, ago(seconds_ago)))

    def test_log_and_count_activity(self):
        db.log_activity(100, 42, "p1", "check")
        db.log_activity(None, 42, "p1", "check")
        db.log_activity(100, 42, "p1", "report")
        self.assertEqual(db.count_activity(42, "p1", ["check"], 24), 2)
        self.assertEqual(db.count_activity("42", "p1", "check", 24), 2)
        self.assertEqual(db.count_activity(42, "p1", ("check", "report"), 24), 3)
        self.assertEqual(db.count_activity(42, "p1", [], 24), 0)
        self.assertEqual(db.count_activity(43, "p1", ["check"], 24), 0)
        self.assertEqual(db.count_activity(42, "p2", ["check"], 24), 0)
        self.assertEqual(self.sql("SELECT guild_id FROM activity WHERE guild_id IS NULL"), [(None,)])

    def test_count_activity_24h_window(self):
        self.add("check", 24 * HOUR - 60)   # dans la fenêtre
        self.add("check", 24 * HOUR + 60)   # hors fenêtre
        self.add("check", 5)
        self.assertEqual(db.count_activity(42, "p1", ["check"], 24), 2)
        self.assertEqual(db.count_activity(42, "p1", ["check"], 1), 1)
        self.assertEqual(db.count_activity(42, "p1", ["check"], 48), 3)

    def test_count_activity_7_day_window(self):
        self.add("report", 7 * DAY - 60)
        self.add("report_refused", 7 * DAY + 60)
        self.add("report_refused", DAY)
        self.assertEqual(db.count_activity(42, "p1", ["report", "report_refused"], 24 * 7), 2)
        self.assertEqual(db.count_activity(42, "p1", ["report_refused"], 24 * 7), 1)

    def test_count_activity_by_guild(self):
        self.add("alert", 60, guild="100")
        self.add("alert", 60, guild="200")
        self.assertEqual(db.count_activity(42, "p1", "alert", 24), 2)
        self.assertEqual(db.count_activity(42, "p1", "alert", 24, guild_id=100), 1)
        self.assertEqual(db.count_activity(42, "p1", "alert", 24, guild_id="300"), 0)

    def test_prune_activity(self):
        # R2-15 : le journal « qui a consulté qui » n'est pas gardé indéfiniment
        self.add("check", 31 * DAY)
        self.add("alert", 40 * DAY)
        self.add("report", 29 * DAY)
        self.add("check", 60)
        self.assertEqual(db.prune_activity(30), 2)
        self.assertEqual(sorted(r[0] for r in self.sql("SELECT kind FROM activity")), ["check", "report"])
        self.assertEqual(db.prune_activity(30), 0)

class ReportHelpersTests(TempDbCase):
    report = ReportTests.report

    def setUp(self):
        super().setUp()
        db.init_db()

    def test_delete_unpublished_report(self):
        unpublished = self.report()
        published = self.report(puuid="p2")
        db.set_report_message(published, 1, 2)
        self.assertTrue(db.delete_unpublished_report(unpublished))
        self.assertIsNone(db.get_report(unpublished))
        self.assertFalse(db.delete_unpublished_report(published))   # un dossier publié n'est jamais supprimé
        self.assertIsNotNone(db.get_report(published))

    def test_count_reports(self):
        self.report(reporter=42)
        self.report(reporter=43)
        self.report(guild=200, reporter=44)
        self.assertEqual(db.count_reports(100, "p1"), 2)
        self.assertEqual(db.count_reports("200", "p1"), 1)
        self.assertEqual(db.count_reports(100, "p2"), 0)

class FindPlayerTests(TempDbCase):
    def test_find_player(self):
        db.init_db()
        db.save_analysis("p1", "Élodie", "EUW", 50, "Or 2", 25, 1.5, 230, 25, 150, 55, 20, [])
        db.record_guild_player(100, "p1")
        self.assertEqual(db.find_player("ÉLODIE", "euw"), ("p1", "Élodie", "EUW"))
        self.assertEqual(db.find_player("élodie", "EUW", guild_id=100), ("p1", "Élodie", "EUW"))
        self.assertIsNone(db.find_player("Élodie", "EUW", guild_id=200))
        self.assertIsNone(db.find_player("Inconnu", "X"))

class BackupTests(TempDbCase):
    def test_backup_database_and_rotation(self):
        db.init_db()
        db.set_linked_account(42, "Bob", "1234")
        os.makedirs(self.backup_dir, exist_ok=True)
        for day in range(1, 10):
            open(os.path.join(self.backup_dir, f"smurfs_leaderboard-2020-01-0{day}.db"), "w").close()
        open(os.path.join(self.backup_dir, "avant-migration-v2.db"), "w").close()
        target = db.backup_database()
        self.assertTrue(os.path.exists(target))
        self.assertEqual(self.sql("SELECT name FROM linked_accounts", path=target), [("Bob",)])
        kept = sorted(f for f in os.listdir(self.backup_dir) if f.startswith("smurfs_leaderboard-"))
        self.assertEqual(len(kept), db.BACKUP_KEEP)
        self.assertIn(os.path.basename(target), kept)
        self.assertNotIn("smurfs_leaderboard-2020-01-01.db", kept)
        # La copie d'avant migration n'est jamais supprimée par la rotation
        self.assertTrue(os.path.exists(os.path.join(self.backup_dir, "avant-migration-v2.db")))

if __name__ == "__main__":
    unittest.main()
