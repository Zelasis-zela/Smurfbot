import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord import app_commands
from discord.ext import commands

from core import db
from core.errors import on_app_command_error, error_message
from core.henrik import RateLimited, NotFound, ApiError
from core.analyzer import PlayerNotFound, AnalysisIncomplete

def make_error(cls, **attrs):
    # Construit l'exception sans dépendre de la signature de son __init__
    error = cls.__new__(cls)
    Exception.__init__(error, "erreur de test")
    for key, value in attrs.items():
        setattr(error, key, value)
    return error

def http_error(status=500):
    return discord.HTTPException(SimpleNamespace(status=status, reason="Erreur"), "échec simulé")

class FakeResponse:
    def __init__(self, done, fail=None):
        self.done, self.fail, self.sent = done, fail, []

    def is_done(self):
        return self.done

    async def send_message(self, content, ephemeral=False):
        if self.fail:
            raise self.fail
        self.sent.append((content, ephemeral))

class FakeFollowup:
    def __init__(self, fail=None):
        self.fail, self.sent = fail, []

    async def send(self, content, ephemeral=False):
        if self.fail:
            raise self.fail
        self.sent.append((content, ephemeral))

class FakeInteraction:
    def __init__(self, done=False, fail=None):
        self.response = FakeResponse(done, fail)
        self.followup = FakeFollowup(fail)
        self.command = SimpleNamespace(name="check", qualified_name="check")

    @property
    def sent(self):
        return self.response.sent + self.followup.sent

class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def handle(self, error, done=False):
        interaction = FakeInteraction(done=done)
        await on_app_command_error(interaction, error)
        self.assertEqual(len(interaction.sent), 1)
        content, ephemeral = interaction.sent[0]
        self.assertTrue(ephemeral)
        return content, interaction

    async def test_cooldown(self):
        error = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 15.0), 12.3)
        content, _ = await self.handle(error)
        self.assertEqual(content, "⏳ Doucement ! Réessaie dans **13 s**.")
        error = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 15.0), 0.2)
        self.assertEqual((await self.handle(error))[0], "⏳ Doucement ! Réessaie dans **1 s**.")

    async def test_no_private_message(self):
        content, _ = await self.handle(app_commands.NoPrivateMessage())
        self.assertEqual(content, "Cette commande s'utilise sur un serveur.")

    async def test_moderator_checks(self):
        for error in (app_commands.MissingPermissions(["manage_messages"]), app_commands.CheckFailure()):
            content, _ = await self.handle(error)
            self.assertEqual(content, "❌ Cette commande est réservée aux modérateurs.")

    async def test_bot_missing_permissions(self):
        content, _ = await self.handle(app_commands.BotMissingPermissions(["embed_links"]))
        self.assertEqual(content, "❌ Il me manque des permissions dans ce salon pour exécuter cette commande.")

    async def test_rate_limited(self):
        content, _ = await self.handle(RateLimited(42))
        self.assertEqual(content, "⏳ L'API Valorant est très sollicitée. Réessaie dans **42 s**.")
        content, _ = await self.handle(RateLimited(None))
        self.assertEqual(content, "⏳ L'API Valorant est très sollicitée. Réessaie dans une minute.")

    async def test_player_not_found(self):
        for error in (make_error(PlayerNotFound), NotFound(22, "Account not found")):
            content, _ = await self.handle(error)
            self.assertEqual(content, "❌ Joueur introuvable : vérifie le Riot ID (format `Nom#TAG`).")

    async def test_api_errors_log_a_warning(self):
        for error in (make_error(AnalysisIncomplete), ApiError(502, None, "Bad gateway")):
            with self.assertLogs("smurfbot", level="WARNING") as logs:
                content, _ = await self.handle(error)
            self.assertEqual(content, "❌ L'API Valorant n'a pas répondu correctement. Réessaie dans un instant.")
            self.assertEqual(logs.records[0].levelname, "WARNING")

    async def test_unknown_error_is_logged(self):
        with self.assertLogs("smurfbot", level="ERROR") as logs:
            content, _ = await self.handle(ValueError("bug"))
        self.assertEqual(content, "❌ Une erreur est survenue, réessaie dans un instant.")
        self.assertIsNotNone(logs.records[0].exc_info)

    async def test_command_invoke_error_is_unwrapped(self):
        wrapped = app_commands.CommandInvokeError(SimpleNamespace(name="check"), RateLimited(5))
        content, _ = await self.handle(wrapped)
        self.assertEqual(content, "⏳ L'API Valorant est très sollicitée. Réessaie dans **5 s**.")
        wrapped = app_commands.CommandInvokeError(SimpleNamespace(name="check"), make_error(PlayerNotFound))
        self.assertIn("Joueur introuvable", (await self.handle(wrapped))[0])

    async def test_followup_when_response_done(self):
        content, interaction = await self.handle(make_error(PlayerNotFound), done=True)
        self.assertEqual(interaction.response.sent, [])
        self.assertEqual(len(interaction.followup.sent), 1)
        content, interaction = await self.handle(app_commands.NoPrivateMessage(), done=False)
        self.assertEqual(interaction.followup.sent, [])
        self.assertEqual(len(interaction.response.sent), 1)

    async def test_never_raises(self):
        for done in (False, True):
            interaction = FakeInteraction(done=done, fail=http_error())
            with self.assertLogs("smurfbot", level="WARNING"):
                await on_app_command_error(interaction, make_error(PlayerNotFound))
            interaction = FakeInteraction(done=done, fail=RuntimeError("inattendu"))
            with self.assertLogs("smurfbot", level="ERROR"):
                await on_app_command_error(interaction, make_error(PlayerNotFound))
        # Interaction sans commande ni réponse exploitable
        broken = SimpleNamespace(command=None, response=None, followup=None)
        with self.assertLogs("smurfbot", level="ERROR"):
            await on_app_command_error(broken, ValueError("bug"))

    def test_error_message_is_pure_text(self):
        self.assertEqual(error_message(FakeInteraction(), app_commands.NoPrivateMessage()), "Cette commande s'utilise sur un serveur.")

class AppSyncTests(unittest.IsolatedAsyncioTestCase):
    # app.py s'importe sans .env ni DISCORD_TOKEN : les vérifications sont dans main()
    async def asyncSetUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("SYNC_COMMANDS", "DISCORD_TOKEN", "API_KEY"):
            os.environ.pop(key, None)
        import app
        self.app = app
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        self.hash_file = os.path.join(tmp.name, ".commands-synced")
        for target, name, value in ((app, "SYNC_HASH_FILE", self.hash_file),
                                    (db, "DB_FILE", os.path.join(tmp.name, "app.db")),
                                    (db, "BACKUP_DIR", os.path.join(tmp.name, "backups"))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bot = app.SmurfBot("cle-de-test")
        self.add_command("ping")
        self.sync = mock.AsyncMock(return_value=[object()])
        self.bot.tree.sync = self.sync

    def add_command(self, name):
        @self.bot.tree.command(name=name, description="Commande de test")
        async def _command(interaction: discord.Interaction):
            pass

    def test_commands_hash(self):
        h = self.app.commands_hash([{"name": "a", "options": [], "type": 1}])
        self.assertEqual(h, self.app.commands_hash([{"type": 1, "options": [], "name": "a"}]))
        self.assertNotEqual(h, self.app.commands_hash([{"name": "b", "options": [], "type": 1}]))
        self.assertRegex(h, r"^[0-9a-f]{64}$")

    def test_needs_sync(self):
        app = self.app
        self.assertIsNone(app.read_synced_hash())
        self.assertTrue(app.needs_sync("abc"))
        app.write_synced_hash("abc")
        self.assertEqual(app.read_synced_hash(), "abc")
        self.assertFalse(app.needs_sync("abc"))
        self.assertTrue(app.needs_sync("abd"))
        self.assertTrue(app.needs_sync("abc", force=True))
        other = os.path.join(self.tmpdir, "autre")
        self.assertTrue(app.needs_sync("abc", path=other))

    async def test_sync_only_when_commands_change(self):
        await self.bot.sync_commands()
        self.assertEqual(self.sync.await_count, 1)
        digest = self.app.read_synced_hash()
        self.assertIsNotNone(digest)
        await self.bot.sync_commands()
        self.assertEqual(self.sync.await_count, 1)
        self.add_command("pong")
        await self.bot.sync_commands()
        self.assertEqual(self.sync.await_count, 2)
        self.assertNotEqual(self.app.read_synced_hash(), digest)

    async def test_sync_forced_by_env(self):
        await self.bot.sync_commands()
        os.environ["SYNC_COMMANDS"] = "1"
        await self.bot.sync_commands()
        self.assertEqual(self.sync.await_count, 2)

    async def test_no_sync_when_a_cog_failed(self):
        self.bot.failed_cogs.append("cogs.casse")
        with self.assertLogs("smurfbot", level="ERROR"):
            await self.bot.sync_commands()
        self.sync.assert_not_awaited()
        self.assertIsNone(self.app.read_synced_hash())

    async def test_sync_failure_is_logged_and_hash_not_written(self):
        self.sync.side_effect = http_error(429)
        with self.assertLogs("smurfbot", level="ERROR"):
            await self.bot.sync_commands()
        self.assertIsNone(self.app.read_synced_hash())

    async def test_setup_hook(self):
        from core.errors import on_app_command_error as handler
        from core.henrik import HenrikClient
        from core.analyzer import Analyzer
        cogs = os.path.join(self.tmpdir, "cogs")
        os.makedirs(cogs)
        for filename in ("b_ok.py", "a_casse.py", "_prive.py", "notes.txt"):
            open(os.path.join(cogs, filename), "w").close()
        loaded = []

        async def fake_load(name):
            loaded.append(name)
            if name.endswith("a_casse"):
                raise RuntimeError("cog cassé")

        self.bot.load_extension = fake_load
        with mock.patch.object(self.app, "COGS_DIR", cogs), self.assertLogs("smurfbot", level="ERROR"):
            await self.bot.setup_hook()
        try:
            self.assertIsInstance(self.bot.henrik, HenrikClient)
            self.assertIsInstance(self.bot.analyzer, Analyzer)
            self.assertIs(self.bot.tree.on_error, handler)
            self.assertEqual(loaded, [f"{cogs}.a_casse", f"{cogs}.b_ok"])   # triés, fichiers « _ » ignorés
            self.assertEqual(self.bot.failed_cogs, [f"{cogs}.a_casse"])
            self.sync.assert_not_awaited()                                    # un cog en erreur : pas de synchro
            self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        finally:
            await self.bot.henrik.close()

    def user_version(self):
        import sqlite3
        conn = sqlite3.connect(db.DB_FILE)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    async def test_close_stops_cogs_before_the_api_client(self):
        # R1-F2 : les cogs (et leurs boucles) s'arrêtent avant la fermeture du client HenrikDev
        order = []
        self.bot.henrik = SimpleNamespace(close=mock.AsyncMock(side_effect=lambda: order.append("henrik")))

        async def parent_close(bot):
            order.append("cogs")

        with mock.patch.object(commands.Bot, "close", parent_close):
            await self.bot.close()
        self.assertEqual(order, ["cogs", "henrik"])

        async def failing_close(bot):
            order.append("cogs")
            raise RuntimeError("fermeture interrompue")

        order.clear()
        with mock.patch.object(commands.Bot, "close", failing_close), self.assertRaises(RuntimeError):
            await self.bot.close()
        self.assertEqual(order, ["cogs", "henrik"])   # le client est fermé même si l'arrêt échoue

    def test_main_requires_token_and_key(self):
        with mock.patch.object(self.app, "setup_logging"), mock.patch.object(self.app, "load_env"), \
                mock.patch.object(self.app, "run") as run:
            with self.assertRaises(SystemExit) as ctx:
                self.app.main()
            self.assertIn("DISCORD_TOKEN", str(ctx.exception.code))
            os.environ["DISCORD_TOKEN"] = "jeton-de-test"
            with self.assertRaises(SystemExit) as ctx:
                self.app.main()
            self.assertIn("API_KEY", str(ctx.exception.code))
            run.assert_not_called()

class MaintenanceCogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cogs.maintenance import MaintenanceCog, BACKUP_TIME
        import datetime
        self.assertEqual(BACKUP_TIME, datetime.time(4, 0, tzinfo=datetime.timezone.utc))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backup_dir = os.path.join(tmp.name, "backups")
        for name, value in (("DB_FILE", os.path.join(tmp.name, "m.db")), ("BACKUP_DIR", self.backup_dir)):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        self.cog = MaintenanceCog(SimpleNamespace(wait_until_ready=mock.AsyncMock()))

    async def test_guild_remove_purges_watches_and_settings(self):
        db.add_watch(100, 1, 2, {"puuid": "p", "name": "N", "tag": "T", "region": "eu"}, {"rank": None, "elo": None})
        db.set_reports_channel_id(100, 5)
        with self.assertLogs("smurfbot", level="INFO"):
            await self.cog.on_guild_remove(SimpleNamespace(id=100, name="Serveur"))
        self.assertEqual(db.get_watches(100), [])
        self.assertIsNone(db.get_reports_channel_id(100))
        with mock.patch.object(db, "purge_guild", side_effect=RuntimeError("panne")), \
                self.assertLogs("smurfbot", level="ERROR"):
            await self.cog.on_guild_remove(SimpleNamespace(id=100, name="Serveur"))

    async def test_command_completion_is_logged(self):
        interaction = SimpleNamespace(guild_id=123)
        with self.assertLogs("smurfbot", level="INFO") as logs:
            await self.cog.on_app_command_completion(interaction, SimpleNamespace(name="check", qualified_name="check"))
        self.assertIn("/check", logs.output[0])
        self.assertIn("123", logs.output[0])

    async def test_daily_loop_prunes_old_activity(self):
        # R2-15 : le journal anti-acharnement est purgé chaque jour (30 jours gardés)
        import sqlite3
        conn = sqlite3.connect(db.DB_FILE)
        try:
            conn.execute("INSERT INTO activity (guild_id, user_id, puuid, kind, created_at) "
                         "VALUES ('1', '42', 'p', 'check', datetime('now', '-40 days'))")
            conn.commit()
        finally:
            conn.close()
        db.log_activity(1, 42, "p", "check")
        with self.assertLogs("smurfbot", level="INFO") as logs:
            await self.cog.backup_loop.coro(self.cog)
        self.assertEqual(db.count_activity(42, "p", "check", 24 * 365), 1)
        self.assertTrue(any("journal anti-acharnement" in line for line in logs.output))
        with mock.patch.object(db, "prune_activity", side_effect=RuntimeError("panne")), \
                self.assertLogs("smurfbot", level="ERROR"):
            await self.cog.backup_loop.coro(self.cog)

    async def test_backup_loop_body_is_error_safe(self):
        with self.assertLogs("smurfbot", level="INFO"):
            await self.cog.backup_loop.coro(self.cog)
        self.assertEqual(len(os.listdir(self.backup_dir)), 1)
        with mock.patch.object(db, "backup_database", side_effect=OSError("disque plein")), \
                self.assertLogs("smurfbot", level="ERROR"):
            await self.cog.backup_loop.coro(self.cog)
        await self.cog.before_backup_loop()
        self.cog.bot.wait_until_ready.assert_awaited()

if __name__ == "__main__":
    unittest.main()
