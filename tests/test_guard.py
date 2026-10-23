import os
import time
import sqlite3
import tempfile
import unittest
from unittest import mock

from core import db, guard

HOUR = 3600
DAY = 24 * HOUR

def ago(seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - seconds))

class GuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "guard.db")
        for name, value in (("DB_FILE", self.path), ("BACKUP_DIR", os.path.join(tmp.name, "backups"))):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()

    def add(self, kind, seconds_ago=5, user="42", puuid="p1", count=1):
        conn = sqlite3.connect(self.path)
        try:
            conn.executemany("INSERT INTO activity (guild_id, user_id, puuid, kind, created_at) VALUES ('100', ?, ?, ?, ?)",
                             [(user, puuid, kind, ago(seconds_ago))] * count)
            conn.commit()
        finally:
            conn.close()

    def test_constants(self):
        self.assertEqual((guard.CHECK_WINDOW_HOURS, guard.CHECK_LIMIT), (24, 6))
        self.assertEqual((guard.REPORT_WINDOW_HOURS, guard.REPORT_LIMIT), (168, 3))
        self.assertEqual(guard.ALERT_COOLDOWN_HOURS, 24)

    def test_no_activity(self):
        self.assertIsNone(guard.harassment_reason(42, "p1"))

    def test_check_threshold(self):
        for _ in range(guard.CHECK_LIMIT - 1):
            db.log_activity(100, 42, "p1", "check")
        self.assertIsNone(guard.harassment_reason(42, "p1"))
        db.log_activity(100, 42, "p1", "check")
        self.assertEqual(guard.harassment_reason(42, "p1"), "6 analyses de ce joueur en 24 h")
        self.add("check", count=2)
        self.assertEqual(guard.harassment_reason("42", "p1"), "8 analyses de ce joueur en 24 h")
        # Autre joueur / autre membre : pas concernés
        self.assertIsNone(guard.harassment_reason(42, "p2"))
        self.assertIsNone(guard.harassment_reason(43, "p1"))

    def test_check_window_expires(self):
        self.add("check", seconds_ago=24 * HOUR + 60, count=10)
        self.add("check", seconds_ago=24 * HOUR - 60, count=5)
        self.assertIsNone(guard.harassment_reason(42, "p1"))
        self.add("check", seconds_ago=60)
        self.assertEqual(guard.harassment_reason(42, "p1"), "6 analyses de ce joueur en 24 h")

    def test_other_kinds_do_not_count_as_checks(self):
        self.add("alert", count=10)
        self.add("report", count=2)
        self.assertIsNone(guard.harassment_reason(42, "p1"))

    def test_report_attempts_threshold(self):
        db.log_activity(100, 42, "p1", "report")
        db.log_activity(200, 42, "p1", "report_refused")
        self.assertIsNone(guard.harassment_reason(42, "p1"))
        db.log_activity(100, 42, "p1", "report_refused")
        self.assertEqual(guard.harassment_reason(42, "p1"), "3 tentatives de signalement de ce joueur en 7 jours")

    def test_report_window_expires(self):
        self.add("report_refused", seconds_ago=7 * DAY + 60, count=5)
        self.add("report", seconds_ago=7 * DAY - 60, count=2)
        self.assertIsNone(guard.harassment_reason(42, "p1"))
        self.add("report_refused", seconds_ago=6 * DAY)
        self.assertEqual(guard.harassment_reason(42, "p1"), "3 tentatives de signalement de ce joueur en 7 jours")

    def test_check_reason_comes_first(self):
        self.add("check", count=6)
        self.add("report", count=3)
        self.assertEqual(guard.harassment_reason(42, "p1"), "6 analyses de ce joueur en 24 h")

    def test_should_alert_cooldown(self):
        self.assertTrue(guard.should_alert(42, "p1", guild_id=100))
        self.assertEqual(db.count_activity(42, "p1", ["alert"], 24), 1)
        # Déjà alerté dans les 24 h : pas de nouvelle alerte, rien n'est ajouté
        self.assertFalse(guard.should_alert(42, "p1"))
        self.assertFalse(guard.should_alert("42", "p1"))
        self.assertEqual(db.count_activity(42, "p1", ["alert"], 24), 1)
        # Autre joueur ou autre membre : alerte séparée
        self.assertTrue(guard.should_alert(42, "p2"))
        self.assertTrue(guard.should_alert(43, "p1"))
        # L'alerte ne compte ni comme analyse ni comme signalement
        self.assertIsNone(guard.harassment_reason(42, "p1"))

    def test_should_alert_after_cooldown(self):
        self.add("alert", seconds_ago=24 * HOUR + 60)
        self.assertTrue(guard.should_alert(42, "p1"))
        self.assertFalse(guard.should_alert(42, "p1"))

    def test_alert_recorded_per_guild(self):
        # R2-17 : l'alerte n'est enregistrée qu'une fois envoyée, et chaque serveur prévient ses propres modérateurs
        self.assertFalse(guard.alert_sent_recently(42, "p1", 100))
        guard.record_alert(42, "p1", 100)
        self.assertTrue(guard.alert_sent_recently(42, "p1", 100))
        self.assertFalse(guard.alert_sent_recently(42, "p1", 200))
        self.assertTrue(guard.alert_sent_recently(42, "p1"))          # tous serveurs confondus
        self.assertFalse(guard.alert_sent_recently(43, "p1", 100))
        self.assertIsNone(guard.harassment_reason(42, "p1"))          # une alerte n'est pas une consultation

if __name__ == "__main__":
    unittest.main()
