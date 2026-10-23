import asyncio
import dataclasses
import os
import re
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord.ext import commands

from core import db, embeds
from core.analysis import (
    Account, MmrInfo, SeasonInfo, MatchStat, RrChange, LobbySummary,
    parse_account, parse_mmr, parse_stored_matches, parse_mmr_history, parse_match_v4,
    aggregate, lobby_summary, reference_tier, rank_averages,
)
from core.analyzer import Analysis, PlayerNotFound
from core.discord_helpers import INVALID_RIOT_ID, MISSING_RIOT_ID, parse_riot_id, tracker_url
from core.errors import NOT_FOUND_MESSAGE
from core.henrik import RateLimited
from core.scoring import compute_suspicion, REPORT_THRESHOLD
from tests import load_fixture, PLAYER_PUUID, MATCH_FIXTURES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COGS = ["cogs.admin_utils", "cogs.maintenance", "cogs.reports", "cogs.smurfwatch", "cogs.watch"]
EXPECTED_COMMANDS = {
    "check", "compare", "help", "smurf-match", "historique", "link", "unlink",   # cogs/smurfwatch.py
    "report", "watchlist", "report-setup",                                       # cogs/reports.py
    "watch", "unwatch", "suivis",                                                # cogs/watch.py
    "botstats", "broadcast",                                                     # cogs/admin_utils.py
}
SMURF_PUUID = "5e1f0c3a-0000-4000-8000-00000000beef"


# ================= ANALYSES DE TEST =================
def fixture_analysis():
    # Vraies réponses enregistrées (anonymisées) passées par le vrai code d'analyse et de score
    account = parse_account(load_fixture("account"))
    mmr = parse_mmr(load_fixture("mmr_v3"))
    matches = parse_stored_matches(load_fixture("stored_matches"))
    history = parse_mmr_history(load_fixture("mmr_history"))
    lobbies = [m for m in (parse_match_v4(load_fixture(name)) for name in MATCH_FIXTURES) if m and m.player(account.puuid)]
    lobby = lobby_summary(lobbies, account.puuid)
    ref, source = reference_tier(mmr, lobby, matches)
    comparison = rank_averages(lobbies, account.puuid, ref, account.level)
    suspicion = compute_suspicion(account, mmr, matches, history, lobby, ref, source)
    return Analysis(account, mmr, matches, history, lobbies, lobby, aggregate(matches), ref, source, comparison,
                    suspicion, int(time.time()))


def smurf_analysis(name="Suspect", tag="EUW", puuid=SMURF_PUUID):
    # Profil synthétique de smurf évident (cf. calibration de core.scoring) : indice ≥ 61
    now = int(time.time())
    matches = []
    for i in range(16):
        won = i % 4 != 3
        matches.append(MatchStat(f"{i:08d}-aaaa-4bbb-8ccc-{i:012d}", now - i * 3600, "Ascent", "e11a6", "Jett", 14, 30,
                                 20, 10, 3, 5200, 22, 70, 8, 3200, 20, 13 if won else 7, 7 if won else 13))
    mmr = MmrInfo(14, 50, 1200, 10, 0, 14, "e11a6", 45, [SeasonInfo("e10a1", 12, 8, 12), SeasonInfo("e11a6", 33, 20, 14)])
    history = ([RrChange("h", 0, "e11a6", 14, 14, 28, False, False, False)] * 8
               + [RrChange("h", 0, "e11a6", 14, 14, -15, False, False, False)] * 4)
    lobby = LobbySummary(5, 1.6, 1.4, 14, [(matches[i].match_id, 1.4 + i / 10, 1.3) for i in range(5)])
    account = Account(puuid, name, tag, "eu", 30, None)
    ref, source = reference_tier(mmr, lobby, matches)
    suspicion = compute_suspicion(account, mmr, matches, history, lobby, ref, source)
    assert suspicion.index >= REPORT_THRESHOLD, suspicion
    return Analysis(account, mmr, matches, history, [], lobby, aggregate(matches), ref, source, None, suspicion, now)


class FakeAnalyzer:
    # Remplace core.analyzer.Analyzer : aucune requête, des analyses déjà construites
    def __init__(self):
        self.analyses = {}    # (nom, tag) casefold -> Analysis
        self.matches = {}     # (nom, tag) casefold -> (Account, LobbyMatch) ou None
        self.snapshots = {}   # puuid -> dict, None ou exception
        self.errors = {}      # méthode -> exception à lever
        self.calls = []
        self.analyzed = set()
        self.cache = {}       # puuid -> Analysis

    def add(self, analysis):
        self.analyses[(analysis.account.name.casefold(), analysis.account.tag.casefold())] = analysis
        return analysis

    def count(self, method):
        return sum(1 for c in self.calls if c[0] == method)

    def _lookup(self, method, name, tag):
        self.calls.append((method, name, tag))
        if method in self.errors:
            raise self.errors[method]
        key = (name.strip().casefold(), tag.strip().casefold())
        if key not in self.analyses:
            raise PlayerNotFound(name, tag)
        return key, self.analyses[key]

    async def find_account(self, name, tag):
        return self._lookup("find_account", name, tag)[1].account

    async def analyze(self, name, tag):
        key, analysis = self._lookup("analyze", name, tag)
        result = dataclasses.replace(analysis, from_cache=key in self.analyzed)
        self.analyzed.add(key)
        self.cache[analysis.account.puuid] = analysis
        return result

    def cached_by_puuid(self, puuid):
        analysis = self.cache.get(puuid)
        return dataclasses.replace(analysis, from_cache=True) if analysis else None

    async def last_match(self, name, tag):
        key, _ = self._lookup("last_match", name, tag)
        return self.matches.get(key)

    async def snapshot(self, region, puuid):
        self.calls.append(("snapshot", region, puuid))
        value = self.snapshots.get(puuid)
        if isinstance(value, Exception):
            raise value
        return value


# ================= FAUX OBJETS DISCORD =================
class FakeResponse:
    def __init__(self):
        self.done = False
        self.calls = []   # (type, kwargs)

    def is_done(self):
        return self.done

    def _record(self, kind, kwargs):
        if self.done:
            raise AssertionError(f"Interaction déjà répondue ({kind})")
        self.done = True
        self.calls.append((kind, kwargs))

    async def send_message(self, content=None, **kwargs):
        self._record("send_message", dict(kwargs, content=content))

    async def defer(self, **kwargs):
        self._record("defer", kwargs)

    async def edit_message(self, **kwargs):
        self._record("edit_message", kwargs)


class Obj(SimpleNamespace):
    # Comme SimpleNamespace, mais utilisable comme clé de dict (rôles et membres dans les permissions de salon)
    __eq__ = object.__eq__
    __hash__ = object.__hash__


def make_member(user_id=42, **permissions):
    return SimpleNamespace(id=user_id, mention=f"<@{user_id}>", name=f"membre{user_id}", display_name=f"Membre {user_id}",
                           guild_permissions=discord.Permissions(**permissions))


def make_guild(guild_id=100, manage_channels=True):
    guild = SimpleNamespace(id=guild_id, name=f"Serveur {guild_id}", text_channels=[])
    guild.default_role = Obj(id=guild_id, name="@everyone")
    guild.me = Obj(id=1, mention="<@1>", guild_permissions=discord.Permissions(manage_channels=manage_channels))
    guild.get_channel = lambda channel_id: next((c for c in guild.text_channels if c.id == channel_id), None)
    guild.create_text_channel = mock.AsyncMock()
    return guild


BOT_OK = dict(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)


def make_channel(guild, channel_id=500, name="reports-smurf", public=False, bot_permissions=None, attach=True):
    channel = mock.MagicMock(spec=discord.TextChannel)
    channel.id, channel.name, channel.mention, channel.guild = channel_id, name, f"<#{channel_id}>", guild
    everyone = discord.Permissions(view_channel=public)
    bot = discord.Permissions(**(BOT_OK if bot_permissions is None else bot_permissions))
    channel.permissions_for = mock.Mock(side_effect=lambda target: everyone if target is guild.default_role else bot)
    channel.send = mock.AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(id=9000 + channel_id + channel.send.await_count))
    if attach:
        guild.text_channels.append(channel)
    return channel


def make_interaction(bot, user=None, guild=None, permissions=None, app_permissions=None, channel_id=300, message=None):
    interaction = SimpleNamespace(client=bot, guild=guild, guild_id=guild.id if guild else None, channel_id=channel_id,
                                  message=message)
    interaction.user = user or make_member()
    interaction.permissions = permissions or discord.Permissions(view_channel=True, send_messages=True)
    interaction.app_permissions = app_permissions or discord.Permissions(**BOT_OK)
    interaction.response = FakeResponse()
    interaction.followup = SimpleNamespace(send=mock.AsyncMock())
    interaction.delete_original_response = mock.AsyncMock()
    interaction.command = SimpleNamespace(name="test", qualified_name="test")
    return interaction


def outputs(interaction):
    # Tout ce que le membre a reçu : réponses directes puis followups, sous forme de dicts
    sent = [dict(kwargs, via=kind) for kind, kwargs in interaction.response.calls if kind != "defer"]
    for call in interaction.followup.send.await_args_list:
        data = dict(call.kwargs, via="followup")
        if call.args:
            data["content"] = call.args[0]
        sent.append(data)
    return sent


def last_output(interaction):
    sent = outputs(interaction)
    if not sent:
        raise AssertionError("Aucune réponse envoyée")
    return sent[-1]


def text_of(embed):
    data = embed.to_dict()
    parts = [data.get("title", ""), data.get("description", ""), data.get("footer", {}).get("text", "")]
    for field in data.get("fields", []):
        parts += [field["name"], field["value"]]
    return "\n".join(parts)


def field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), None)


def check_limits(test, embed):
    # Limites Discord : titre 256, description 4096, 25 champs (nom 256, valeur 1024), pied 2048, total 6000
    data = embed.to_dict()
    test.assertLessEqual(len(data.get("title", "")), 256)
    test.assertLessEqual(len(data.get("description", "")), 4096)
    test.assertLessEqual(len(data.get("fields", [])), 25)
    for f in data.get("fields", []):
        test.assertTrue(0 < len(f["name"]) <= 256, f["name"])
        test.assertTrue(0 < len(f["value"]) <= 1024, f["name"])
    test.assertLessEqual(len(data.get("footer", {}).get("text", "")), 2048)
    test.assertLessEqual(len(embed), 6000)


def custom_ids(view):
    return [getattr(item, "custom_id", None) for item in view.children]


class CogTestCase(unittest.IsolatedAsyncioTestCase):
    # Bot non connecté, cogs chargés comme dans app.py, base SQLite temporaire, analyseur factice
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2.0   # SQLite sur disque : pas d'alerte « took 0.1 s »
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("DB_FILE", os.path.join(tmp.name, "cogs.db")), ("BACKUP_DIR", os.path.join(tmp.name, "backups"))):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        self.bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.default(),
                                help_command=None, allowed_mentions=discord.AllowedMentions.none())
        self.never = asyncio.Event()
        self.bot.wait_until_ready = self.never.wait   # les boucles de fond restent en attente pendant les tests
        self.analyzer = FakeAnalyzer()
        self.bot.analyzer = self.analyzer
        for extension in COGS:
            await self.bot.load_extension(extension)
        self.fixture = self.analyzer.add(fixture_analysis())
        self.smurf = self.analyzer.add(smurf_analysis())
        self.guild = make_guild()

    async def asyncTearDown(self):
        for extension in list(self.bot.extensions):
            await self.bot.unload_extension(extension)

    def cog(self, name):
        return self.bot.get_cog(name)

    def module(self, name):
        return sys.modules[name]

    def interaction(self, **kwargs):
        kwargs.setdefault("guild", self.guild)
        return make_interaction(self.bot, **kwargs)

    def activity(self, user_id, puuid, kinds, hours=24 * 7):
        return db.count_activity(user_id, puuid, kinds, hours)


# ================= ENREGISTREMENT =================
class RegistrationTests(CogTestCase):
    def test_registered_commands(self):
        self.assertEqual({c.name for c in self.bot.tree.get_commands()}, EXPECTED_COMMANDS)

    def test_every_command_serializes_within_discord_limits(self):
        for command in self.bot.tree.get_commands():
            data = command.to_dict(self.bot.tree)
            self.assertRegex(data["name"], r"^[-_a-z0-9]{1,32}$")
            self.assertTrue(1 <= len(data["description"]) <= 100, data["name"])
            for option in data.get("options", []):
                self.assertRegex(option["name"], r"^[-_a-z0-9]{1,32}$")
                self.assertTrue(1 <= len(option["description"]) <= 100, (data["name"], option["name"]))
                for choice in option.get("choices", []):
                    self.assertLessEqual(len(choice["name"]), 100)

    def test_permissions_and_guild_only(self):
        tree = self.bot.tree
        data = {c.name: c.to_dict(tree) for c in tree.get_commands()}
        self.assertEqual(int(data["watchlist"]["default_member_permissions"]), discord.Permissions(manage_messages=True).value)
        self.assertEqual(int(data["report-setup"]["default_member_permissions"]), discord.Permissions(manage_guild=True).value)
        guild_only = {"check", "compare", "report", "watchlist", "report-setup", "smurf-match", "historique",
                      "watch", "unwatch", "suivis"}
        for name in guild_only:
            self.assertEqual(data[name].get("contexts"), [0], name)
        for name in ("help", "link", "unlink"):
            self.assertNotEqual(data[name].get("contexts"), [0], name)
        riot = next(o for o in data["check"]["options"] if o["name"] == "riot_id")
        self.assertEqual((riot["min_length"], riot["max_length"], riot["required"], riot["autocomplete"]), (3, 30, False, True))
        report = {o["name"]: o for o in data["report"]["options"]}
        self.assertTrue(report["riot_id"]["required"])
        self.assertEqual(report["observations"]["max_length"], 1000)

    def test_old_cog_is_gone(self):
        # app.py charge tous les cogs/*.py : la liste testée doit être exactement celle du dossier
        files = sorted(f[:-3] for f in os.listdir(os.path.join(ROOT, "cogs")) if f.endswith(".py") and not f.startswith("_"))
        self.assertEqual(files, [name.split(".")[1] for name in COGS])
        self.assertFalse(os.path.exists(os.path.join(ROOT, "cogs", "smurf_detector.py")))
        self.assertNotIn("smurf", {c.name for c in self.bot.tree.get_commands()})
        self.assertNotIn("leaderboard-smurfs", {c.name for c in self.bot.tree.get_commands()})

    async def test_dynamic_items_registered_then_removed(self):
        store = self.bot._connection._view_store._dynamic_items
        patterns = {p.pattern for p in store}
        self.assertIn(r"sw:matches:(?P<puuid>[A-Za-z0-9_-]{1,89})", patterns)
        self.assertIn(r"sw:review:(?P<id>[0-9]+):(?P<action>ok|no)", patterns)
        await self.bot.unload_extension("cogs.reports")
        await self.bot.unload_extension("cogs.smurfwatch")
        self.assertEqual(self.bot._connection._view_store._dynamic_items, {})


# ================= /check =================
class CheckTests(CogTestCase):
    async def run_check(self, riot_id="TestSmurf#0001", prive=False, **kwargs):
        interaction = self.interaction(**kwargs)
        cog = self.cog("SmurfWatchCog")
        await cog.check.callback(cog, interaction, riot_id=riot_id, prive=prive)
        return interaction

    async def test_public_check(self):
        interaction = await self.run_check()
        self.assertEqual(interaction.response.calls[0], ("defer", {"ephemeral": False}))
        out = last_output(interaction)
        embed = out["embed"]
        self.assertEqual(embed.title, "🔍 Analyse de TestSmurf#0001")
        self.assertEqual([f.name for f in embed.fields][:4],
                         ["Profil", "Stats clés (20 dernières parties)", "Indice de suspicion", "Signaux détectés"])
        profile = field(embed, "Profil")
        for label in ("Rang actuel", "Pic", "Niveau du compte"):
            self.assertIn(label, profile)
        self.assertIn("Platine 1", profile)
        stats = field(embed, "Stats clés (20 dernières parties)")
        for label in ("K/D", "HS %", "ACS", "ADR", "Winrate"):
            self.assertIn(f"**{label} :**", stats)
        self.assertTrue(field(embed, "Indice de suspicion").startswith(f"🟢 **{self.fixture.suspicion.index}/100 — Faible**"))
        self.assertEqual(embed.footer.text, "Analyse indicative, ne constitue pas une preuve.")
        self.assertIn("revient d'une pause", embed.description)
        self.assertNotIn("/report", embed.description)
        check_limits(self, embed)
        view = out["view"]
        self.assertEqual(custom_ids(view)[0], f"sw:matches:{PLAYER_PUUID}")
        self.assertEqual(view.children[1].url, tracker_url("TestSmurf", "0001"))
        self.assertIsNone(view.timeout)
        # Analyse enregistrée (nouveau calcul), joueur rattaché au serveur, consultation journalisée
        rows, total = db.get_player_history("TestSmurf", "0001", guild_id=self.guild.id)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0][2], self.fixture.suspicion.index)
        self.assertEqual(rows[0][3], "Platine 1")
        self.assertEqual(rows[0][7], db.MODEL_VERSION)
        self.assertEqual(self.activity(42, PLAYER_PUUID, "check"), 1)

    async def test_private_check(self):
        interaction = await self.run_check(prive=True)
        self.assertEqual(interaction.response.calls[0], ("defer", {"ephemeral": True}))
        self.assertTrue(last_output(interaction)["ephemeral"])

    async def test_cached_result_is_not_saved_twice(self):
        await self.run_check()
        interaction = await self.run_check()
        self.assertIn("Résultat en cache", last_output(interaction)["embed"].description)
        self.assertEqual(db.get_player_history("TestSmurf", "0001")[1], 1)
        self.assertEqual(self.activity(42, PLAYER_PUUID, "check"), 2)

    async def test_database_failure_does_not_hide_the_analysis(self):
        with mock.patch.object(db, "save_analysis", side_effect=RuntimeError("base verrouillée")), \
                self.assertLogs("smurfbot", "ERROR"):
            interaction = await self.run_check()
        self.assertEqual(last_output(interaction)["embed"].title, "🔍 Analyse de TestSmurf#0001")

    async def test_high_index_mentions_report(self):
        interaction = await self.run_check("Suspect#EUW")
        embed = last_output(interaction)["embed"]
        self.assertIn("`/report` prépare un dossier", embed.description)
        self.assertEqual(embed.colour.value, embeds.level_row(self.smurf.suspicion.index)[5])
        self.assertIn("(+", field(embed, "Signaux détectés"))

    async def test_linked_account_and_errors(self):
        interaction = await self.run_check(riot_id=None)
        self.assertEqual(interaction.response.calls, [("send_message", {"ephemeral": True, "content": MISSING_RIOT_ID})])
        interaction = await self.run_check(riot_id="SansTag")
        self.assertEqual(last_output(interaction)["content"], INVALID_RIOT_ID)
        db.set_linked_account(42, "TestSmurf", "0001")
        interaction = await self.run_check(riot_id=None)
        self.assertEqual(last_output(interaction)["embed"].title, "🔍 Analyse de TestSmurf#0001")
        # /link n'est pas vérifié : le compte lié est compté comme n'importe quel autre (R2-01)
        self.assertEqual(self.activity(42, PLAYER_PUUID, "check"), 1)

    async def test_linking_the_target_does_not_bypass_the_guard(self):
        # R2-01 : lier le Riot ID d'un autre joueur ne doit pas désactiver la protection anti-acharnement
        db.set_linked_account(42, "Suspect", "EUW")
        channel = make_channel(self.guild)
        for _ in range(6):
            interaction = await self.run_check(riot_id=None)
            self.assertIn("embed", last_output(interaction))
        interaction = await self.run_check(riot_id=None)
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        self.assertIn("pour éviter tout acharnement", out["content"].lower())
        self.assertIn("les modérateurs ont été prévenus", out["content"])
        alert = channel.send.await_args.kwargs["embed"]
        self.assertIn("compte lié par ce membre avec `/link`", alert.description)
        self.assertEqual(self.analyzer.count("analyze"), 6)

    async def test_alert_not_recorded_when_sending_fails(self):
        # R2-17 : une alerte qui n'a pas pu partir n'est ni enregistrée ni annoncée comme envoyée
        for _ in range(6):
            db.log_activity(self.guild.id, 42, PLAYER_PUUID, "check")
        channel = make_channel(self.guild)
        channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Erreur"), "interdit")
        with self.assertLogs("smurfbot", "WARNING"):
            interaction = await self.run_check()
        self.assertIn("Réessaie dans quelques heures", last_output(interaction)["content"])
        self.assertEqual(self.activity(42, PLAYER_PUUID, "alert", 24), 0)
        channel.send.side_effect = None
        interaction = await self.run_check()
        self.assertIn("les modérateurs ont été prévenus", last_output(interaction)["content"])
        self.assertEqual(channel.send.await_count, 2)   # nouvel essai, cette fois envoyé
        self.assertEqual(self.activity(42, PLAYER_PUUID, "alert", 24), 1)

    async def test_errors_stay_private_after_public_defer(self):
        interaction = await self.run_check("Inconnu#000")
        interaction.delete_original_response.assert_awaited_once()
        out = last_output(interaction)
        self.assertEqual((out["content"], out["ephemeral"]), (NOT_FOUND_MESSAGE, True))
        self.analyzer.errors["analyze"] = RateLimited(42)
        interaction = await self.run_check()
        self.assertEqual(last_output(interaction)["content"], "⏳ L'API Valorant est très sollicitée. Réessaie dans **42 s**.")

    async def test_harassment_guard(self):
        for _ in range(6):
            db.log_activity(self.guild.id, 42, PLAYER_PUUID, "check")
        # Pas de salon des modérateurs : refus poli sans alerte
        interaction = await self.run_check()
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        self.assertIn("pour éviter tout acharnement", out["content"].lower())
        self.assertIn("Réessaie dans quelques heures", out["content"])
        self.assertEqual(self.analyzer.count("analyze"), 0)
        # Salon privé : alerte une seule fois, et le refus dit que les modérateurs sont prévenus
        channel = make_channel(self.guild)
        for _ in range(2):
            interaction = await self.run_check()
            self.assertIn("les modérateurs ont été prévenus", last_output(interaction)["content"])
        channel.send.assert_awaited_once()
        alert = channel.send.await_args.kwargs["embed"]
        self.assertEqual(alert.title, "🚨 Protection anti-acharnement")
        self.assertIn("<@42>", alert.description)
        self.assertIn("TestSmurf#0001", alert.description)
        check_limits(self, alert)
        self.assertEqual(self.activity(42, PLAYER_PUUID, "check"), 6)   # refus non journalisés comme consultation
        self.assertEqual(self.analyzer.count("analyze"), 0)
        # Un autre membre n'est pas concerné
        interaction = await self.run_check(user=make_member(43))
        self.assertIn("embed", last_output(interaction))


class MatchesButtonTests(CogTestCase):
    async def test_button(self):
        cls = type(self.cog("SmurfWatchCog")).__module__
        MatchesButton = sys.modules[cls].MatchesButton
        match = MatchesButton.__discord_ui_compiled_template__.fullmatch(f"sw:matches:{PLAYER_PUUID}")
        button = await MatchesButton.from_custom_id(None, None, match)
        self.assertEqual(button.puuid, PLAYER_PUUID)
        interaction = self.interaction()
        await button.callback(interaction)
        self.assertEqual(last_output(interaction)["content"], "Analyse expirée : relance /check.")
        await self.analyzer.analyze("TestSmurf", "0001")
        interaction = self.interaction()
        await button.callback(interaction)
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        self.assertIn("Dernières parties classées", out["embed"].title)
        self.assertEqual(out["embed"].description.count("\n") + 1, 20)
        check_limits(self, out["embed"])


# ================= /compare =================
class CompareTests(CogTestCase):
    async def run_compare(self, riot_id):
        interaction = self.interaction()
        cog = self.cog("SmurfWatchCog")
        await cog.compare.callback(cog, interaction, riot_id=riot_id)
        return interaction

    async def test_compare(self):
        interaction = await self.run_compare("TestSmurf#0001")
        embed = last_output(interaction)["embed"]
        self.assertEqual(embed.title, "📊 TestSmurf#0001 face à la moyenne de son rang")
        self.assertIn(f"ses {self.fixture.comparison.matches} dernières parties", embed.description)
        self.assertIn("```text", embed.description)
        for label in ("Joueur", "Moyenne", "Écart", "K/D", "HS %", "ACS", "ADR", "Niveau"):
            self.assertIn(label, embed.description)
        self.assertRegex(field(embed, "Lecture"), r"Au-dessus de la moyenne sur (\d stats? sur 4\.|aucune stat \(sur 4\)\.)")
        self.assertEqual(embed.footer.text, embeds.FOOTER)
        check_limits(self, embed)
        self.assertEqual(self.activity(42, PLAYER_PUUID, "check"), 1)   # /compare compte comme une consultation

    async def test_no_comparison(self):
        smurfwatch = self.module("cogs.smurfwatch")
        interaction = await self.run_compare("Suspect#EUW")   # analyse sans détail de lobby
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        self.assertEqual(out["content"], smurfwatch.NO_COMPARISON_DETAILS)
        interaction.delete_original_response.assert_awaited_once()
        # R1-F1 : rien d'affiché, donc rien de compté (réessayer ne mène pas au blocage anti-acharnement)
        self.assertEqual(self.activity(42, SMURF_PUUID, "check"), 0)
        # Détail limité par le budget d'API : l'analyse n'est gardée qu'une minute, « réessaie dans une minute » est vrai
        self.analyzer.add(dataclasses.replace(self.smurf, partial=True))
        self.assertEqual(last_output(await self.run_compare("Suspect#EUW"))["content"], smurfwatch.NO_COMPARISON)
        # Aucune partie classée récente : réessayer ne changerait rien
        empty = dataclasses.replace(self.smurf, matches=[], agg=aggregate([]))
        self.analyzer.add(empty)
        self.assertEqual(last_output(await self.run_compare("Suspect#EUW"))["content"], smurfwatch.NO_MATCHES_TO_COMPARE)
        self.assertEqual(self.activity(42, SMURF_PUUID, "check"), 0)


# ================= /help =================
class HelpTests(CogTestCase):
    async def help_text(self):
        interaction = self.interaction()
        cog = self.cog("SmurfWatchCog")
        await cog.help_command.callback(cog, interaction)
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        check_limits(self, out["embed"])
        return text_of(out["embed"])

    async def test_help_lists_exactly_the_registered_commands(self):
        text = await self.help_text()
        mentioned = set(re.findall(r"`/([a-z-]+)`", text))
        registered = {c.name for c in self.bot.tree.get_commands()}
        # /help elle-même et /broadcast (propriétaire du bot) ne sont pas listées ; /botstats est ouverte à tous
        self.assertEqual(mentioned, registered - {"help", "broadcast"})
        for level in ("🟢", "🟡", "🟠", "🔴"):
            self.assertIn(level, text)
        self.assertIn("ne constitue jamais une preuve", text)
        self.assertIn("7 jours", text)

    async def test_help_follows_loaded_cogs(self):
        await self.bot.unload_extension("cogs.watch")
        mentioned = set(re.findall(r"`/([a-z-]+)`", await self.help_text()))
        self.assertFalse(mentioned & {"watch", "unwatch", "suivis"})
        self.assertIn("check", mentioned)


# ================= /smurf-match =================
class SmurfMatchTests(CogTestCase):
    async def run_match(self, riot_id="TestSmurf#0001"):
        interaction = self.interaction()
        cog = self.cog("SmurfWatchCog")
        await cog.smurf_match.callback(cog, interaction, riot_id=riot_id)
        return interaction

    async def test_last_match(self):
        match = parse_match_v4(load_fixture("match_v4_single"))
        self.analyzer.matches[("testsmurf", "0001")] = (self.fixture.account, match)
        interaction = await self.run_match()
        embed = last_output(interaction)["embed"]
        self.assertEqual(embed.title, "🔍 Dernière partie classée de TestSmurf#0001")
        names = [f.name for f in embed.fields]
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].startswith("🔴 Équipe Rouge — "))
        self.assertTrue(names[1].startswith("🔵 Équipe Bleue — "))
        self.assertEqual(sum(1 for n in names if n.endswith("🏆")), 1 if match.winner else 0)
        body = "\n".join(f.value for f in embed.fields)
        self.assertEqual(body.count("\n") + 1, len(match.players))
        self.assertEqual(body.count("⬅️"), 1)
        self.assertIn("**TestSmurf#0001** ⬅️", body)
        self.assertIn("plafond 60", embed.footer.text)
        self.assertIn("ne constitue pas une preuve", embed.footer.text)
        check_limits(self, embed)

    async def test_no_ranked_match(self):
        interaction = await self.run_match()
        out = last_output(interaction)
        self.assertEqual((out["content"], out["ephemeral"]), ("❌ Aucune partie classée récente pour ce joueur.", True))
        self.assertEqual(self.activity(42, PLAYER_PUUID, "check"), 0)

    async def test_guarded_and_counted(self):
        # R1-F5 / R2-02 : /smurf-match ne contourne pas la protection anti-acharnement
        match = parse_match_v4(load_fixture("match_v4_single"))
        self.analyzer.matches[("testsmurf", "0001")] = (self.fixture.account, match)
        await self.run_match()
        self.assertEqual(self.activity(42, PLAYER_PUUID, "check"), 1)
        for _ in range(5):
            db.log_activity(self.guild.id, 42, PLAYER_PUUID, "check")
        interaction = await self.run_match()
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        self.assertIn("pour éviter tout acharnement", out["content"].lower())
        interaction.delete_original_response.assert_awaited_once()
        self.assertEqual(self.analyzer.count("last_match"), 1)


# ================= /historique =================
class HistoriqueTests(CogTestCase):
    async def run_history(self, riot_id, guild=None):
        interaction = self.interaction(guild=guild or self.guild)
        cog = self.cog("SmurfWatchCog")
        await cog.historique.callback(cog, interaction, riot_id=riot_id)
        return last_output(interaction)

    async def test_history_with_old_model_rows(self):
        a = self.fixture.account
        db.db_query("INSERT INTO smurfs (puuid, name, tag, score, rank, level, kd, acs, hs_percent, last_analyzed) "
                    "VALUES (?, ?, ?, 80, 'Gold 2', 331, 1.5, 250, 30, '2026-01-01 10:00:00')", (a.puuid, a.name, a.tag))
        db.db_query("INSERT INTO smurf_history (puuid, score, rank, level, kd, acs, hs_percent, analyzed_at) "
                    "VALUES (?, 80, 'Gold 2', 331, 1.5, 250, 30, '2026-01-01 10:00:00')", (a.puuid,))
        db.record_guild_player(self.guild.id, a.puuid)
        for score in (10, 25):
            db.save_analysis(a.puuid, a.name, a.tag, score, "Platine 1", 331, 1.0, 221, 20.3, 144, 60, 20, [])
        out = await self.run_history("testsmurf#0001")
        embed = out["embed"]
        reminder, *lines = embed.description.split("\n")
        self.assertEqual(reminder, embeds.REMINDER)
        self.assertEqual(len(lines), 3)
        self.assertIn("**25/100**", lines[0])
        self.assertIn("20 parties", lines[0])
        self.assertTrue(lines[2].endswith("(ancien calcul)"))
        self.assertIn("Or 2", lines[2])          # ancien nom anglais converti
        self.assertIn("⚪", lines[2])
        self.assertNotIn("ancien calcul", lines[0])
        self.assertIn("+15 points", field(embed, "Évolution"))   # l'ancienne ligne (80) est exclue de la tendance
        self.assertIsNotNone(field(embed, "Ancien calcul"))
        check_limits(self, embed)

    async def test_guild_filter(self):
        a = self.fixture.account
        db.save_analysis(a.puuid, a.name, a.tag, 10, "Platine 1", 331, 1.0, 221, 20.3, 144, 60, 20, [])
        db.record_guild_player(999, a.puuid)
        out = await self.run_history("TestSmurf#0001")
        # R2-12 : les anciennes analyses non rattachées à un serveur existent peut-être : on ne dit pas « jamais »
        self.assertEqual(out["content"], "❌ Aucune analyse de **TestSmurf#0001** n'est enregistrée pour ce serveur. "
                                         "Lance d'abord `/check`.")
        self.assertTrue(out["ephemeral"])
        out = await self.run_history("TestSmurf#0001", guild=make_guild(999))
        self.assertIn("embed", out)

    async def test_guarded_and_counted(self):
        # R1-F5 : afficher l'historique d'un joueur est une consultation, soumise à la même protection
        a = self.fixture.account
        db.save_analysis(a.puuid, a.name, a.tag, 10, "Platine 1", 331, 1.0, 221, 20.3, 144, 60, 20, [])
        db.record_guild_player(self.guild.id, a.puuid)
        self.assertIn("embed", await self.run_history("TestSmurf#0001"))
        self.assertEqual(self.activity(42, a.puuid, "check"), 1)
        for _ in range(5):
            db.log_activity(self.guild.id, 42, a.puuid, "check")
        out = await self.run_history("TestSmurf#0001")
        self.assertTrue(out["ephemeral"])
        self.assertIn("je n'affiche pas son historique", out["content"])
        self.assertEqual(self.activity(42, a.puuid, "check"), 6)
        command = self.cog("SmurfWatchCog").historique
        self.assertTrue(command.checks)   # anti-spam : délai entre deux /historique


# ================= /link /unlink =================
class LinkTests(CogTestCase):
    async def test_link_and_unlink(self):
        cog = self.cog("SmurfWatchCog")
        interaction = self.interaction(guild=None)
        await cog.link.callback(cog, interaction, riot_id="testsmurf #0001")
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        self.assertIn("**TestSmurf#0001** lié", out["content"])
        self.assertEqual(db.get_linked_account(42), ("TestSmurf", "0001"))   # nom renvoyé par l'API

        interaction = self.interaction(guild=None)
        await cog.link.callback(cog, interaction, riot_id="Personne#000")
        self.assertEqual(last_output(interaction)["content"], NOT_FOUND_MESSAGE)
        self.assertEqual(db.get_linked_account(42), ("TestSmurf", "0001"))

        interaction = self.interaction(guild=None)
        await cog.unlink.callback(cog, interaction)
        self.assertIn("n'est plus lié", last_output(interaction)["content"])
        self.assertIsNone(db.get_linked_account(42))
        interaction = self.interaction(guild=None)
        await cog.unlink.callback(cog, interaction)
        self.assertIn("Aucun compte Riot", last_output(interaction)["content"])


# ================= Riot ID et autocomplétion =================
class RiotIdTests(CogTestCase):
    def test_parse_riot_id(self):
        self.assertEqual(parse_riot_id(" Nom Avec Espace # EUW "), ("Nom Avec Espace", "EUW"))
        self.assertEqual(parse_riot_id("a#b#TAG"), ("a#b", "TAG"))
        for bad in ("", "SansTag", "#TAG", "Nom#", "x" * 17 + "#TAG", "Nom#TAGTOO"):
            self.assertIsNone(parse_riot_id(bad), bad)
        self.assertEqual(parse_riot_id("ẞüñíçødé_Ωmega16#ÆØÅ12"), ("ẞüñíçødé_Ωmega16", "ÆØÅ12"))

    async def test_autocomplete(self):
        from core.discord_helpers import riot_id_autocomplete, watch_autocomplete
        a = self.fixture.account
        db.save_analysis(a.puuid, a.name, a.tag, 10, "Gold 2", 331, 1.0, 221, 20.3, 144, 60, 20, [])
        db.record_guild_player(self.guild.id, a.puuid)
        db.save_analysis("ailleurs", "Ailleurs", "EUW", 10, "Or 1", 100, 1.0, 221, 20.3, 144, 60, 20, [])
        db.record_guild_player(999, "ailleurs")   # analysé sur un autre serveur seulement : jamais proposé ici (R2-15)
        db.set_linked_account(42, "Suspect", "EUW")
        interaction = self.interaction()
        choices = await riot_id_autocomplete(interaction, "")
        self.assertEqual([c.value for c in choices], ["Suspect#EUW", "TestSmurf#0001"])
        self.assertTrue(choices[0].name.startswith("⭐"))
        self.assertIn("Or 2", choices[1].name)
        self.assertNotIn("/100", choices[1].name)   # aucun indice affiché dans l'autocomplétion
        self.assertEqual([c.value for c in await riot_id_autocomplete(interaction, "testsm")], ["TestSmurf#0001"])
        db.add_watch(self.guild.id, 1, 2, {"puuid": "p", "name": "Suivi", "tag": "T1", "region": "eu"}, {"rank": None, "elo": None})
        self.assertEqual([c.value for c in await watch_autocomplete(interaction, "sui")], ["Suivi#T1"])
        with mock.patch.object(db, "search_players", side_effect=RuntimeError("panne")), self.assertLogs("smurfbot", "ERROR"):
            self.assertEqual(await riot_id_autocomplete(interaction, ""), [])


if __name__ == "__main__":
    unittest.main()
