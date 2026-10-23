import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from core import db
from core.henrik import RateLimited, ApiError
from tests import PLAYER_PUUID
from tests.test_cogs import CogTestCase, SMURF_PUUID, make_member, last_output, check_limits

PLATINUM_1 = {"tier": 15, "rank": "Platine 1", "elo": 1231, "games_needed": 0}


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="Erreur"), "échec simulé")


def add_watch(guild_id, puuid, name, rank, elo, channel_id=300, user_id=42):
    db.add_watch(guild_id, channel_id, user_id, {"puuid": puuid, "name": name, "tag": "EUW", "region": "eu"},
                 {"rank": rank, "elo": elo})


class WatchCommandTests(CogTestCase):
    async def run_watch(self, riot_id="TestSmurf#0001", **kwargs):
        interaction = self.interaction(**kwargs)
        cog = self.cog("WatchCog")
        await cog.watch.callback(cog, interaction, riot_id=riot_id)
        return interaction

    async def test_watch_starts_from_current_rank(self):
        self.analyzer.snapshots[PLAYER_PUUID] = PLATINUM_1
        interaction = await self.run_watch(channel_id=321)
        self.assertEqual(interaction.response.calls[0], ("defer", {}))
        out = last_output(interaction)
        self.assertNotIn("ephemeral", out)
        self.assertIn("Le rang de **TestSmurf#0001** est maintenant suivi (rang actuel : Platine 1)", out["content"])
        self.assertIn("toutes les 30 minutes", out["content"])
        [watch] = db.get_watches(self.guild.id)
        self.assertEqual((watch["puuid"], watch["channel_id"], watch["user_id"], watch["region"]),
                         (PLAYER_PUUID, "321", "42", "eu"))
        self.assertEqual((watch["last_rank"], watch["last_elo"]), ("Platine 1", 1231))
        self.assertIn(("snapshot", "eu", PLAYER_PUUID), self.analyzer.calls)

    async def test_placements_are_not_a_starting_point(self):
        self.analyzer.snapshots[PLAYER_PUUID] = {"tier": 0, "rank": "Non classé", "elo": 50, "games_needed": 3}
        out = last_output(await self.run_watch())
        self.assertIn("rang actuel : en placements", out["content"])
        [watch] = db.get_watches(self.guild.id)
        self.assertEqual((watch["last_rank"], watch["last_elo"]), (None, None))

    async def test_limits_and_permissions(self):
        for i in range(5):
            add_watch(self.guild.id, f"p{i}", f"Joueur{i}", "Or 1", 1000)
        out = last_output(await self.run_watch())
        self.assertIn("maximum 5", out["content"])
        self.assertTrue(out["ephemeral"])
        self.assertEqual(self.analyzer.calls, [])
        db.remove_watch(self.guild.id, "p0")
        out = last_output(await self.run_watch(permissions=discord.Permissions(view_channel=True)))
        self.assertIn("Tu ne peux pas écrire dans ce salon", out["content"])
        out = last_output(await self.run_watch(app_permissions=discord.Permissions(view_channel=True, send_messages=True)))
        self.assertIn("Je n'ai pas la permission d'écrire", out["content"])
        out = last_output(await self.run_watch("SansTag"))
        self.assertIn("Riot ID invalide", out["content"])
        self.assertEqual(len(db.get_watches(self.guild.id)), 4)

    async def test_duplicate_and_errors_are_private(self):
        add_watch(self.guild.id, PLAYER_PUUID, "TestSmurf", "Platine 1", 1231)
        interaction = await self.run_watch()
        out = last_output(interaction)
        self.assertEqual((out["content"], out["ephemeral"]), ("ℹ️ **TestSmurf#0001** est déjà suivi sur ce serveur.", True))
        interaction.delete_original_response.assert_awaited_once()
        out = last_output(await self.run_watch("Personne#000"))
        self.assertIn("Joueur introuvable", out["content"])
        self.assertTrue(out["ephemeral"])

    async def test_unwatch(self):
        cog = self.cog("WatchCog")
        add_watch(self.guild.id, SMURF_PUUID, "Suspect", "Or 3", 1190, user_id=42)
        interaction = self.interaction()
        await cog.unwatch.callback(cog, interaction, riot_id="Inconnu#EUW")
        self.assertIn("n'est pas suivi sur ce serveur", last_output(interaction)["content"])
        interaction = self.interaction(user=make_member(43))
        await cog.unwatch.callback(cog, interaction, riot_id="suspect#euw")
        self.assertIn("Seul le membre qui a lancé ce suivi", last_output(interaction)["content"])
        self.assertEqual(len(db.get_watches(self.guild.id)), 1)
        interaction = self.interaction(user=make_member(44, manage_guild=True))
        await cog.unwatch.callback(cog, interaction, riot_id="suspect#euw")
        self.assertEqual(last_output(interaction)["content"], "✅ Le rang de **Suspect#EUW** n'est plus suivi.")
        self.assertEqual(db.get_watches(self.guild.id), [])

    def test_loop_pace_leaves_room_for_commands(self):
        # R1-F3 / R2-16 : au plus ~15 requêtes/min sur les 30 de la clé, et un budget gardé pour /check
        watch_module = self.module("cogs.watch")
        self.assertGreaterEqual(watch_module.WATCH_API_DELAY, 4.0)
        self.assertGreaterEqual(watch_module.WATCH_MIN_BUDGET, 9)

    async def test_suivis(self):
        cog = self.cog("WatchCog")
        interaction = self.interaction()
        await cog.suivis.callback(cog, interaction)
        self.assertIn("Aucun joueur suivi", last_output(interaction)["content"])
        add_watch(self.guild.id, SMURF_PUUID, "Suspect", "Gold 3", 1190)   # ancien nom anglais
        add_watch(self.guild.id, PLAYER_PUUID, "TestSmurf", None, None)
        interaction = self.interaction()
        await cog.suivis.callback(cog, interaction)
        embed = last_output(interaction)["embed"]
        self.assertEqual(embed.title, "👁️ Joueurs suivis")
        self.assertIn("**Suspect#EUW** — Or 3 · <#300> · ajouté par <@42>", embed.description)
        self.assertIn("**TestSmurf#EUW** — rang inconnu", embed.description)
        self.assertEqual(embed.footer.text, "2/5 — vérification toutes les 30 minutes")
        check_limits(self, embed)


class WatchLoopTests(CogTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog_ = self.cog("WatchCog")
        patcher = mock.patch.object(self.module("cogs.watch"), "WATCH_API_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.channels = {}
        self.bot.get_channel = lambda channel_id: self.channels.get(channel_id)
        self.bot.fetch_channel = mock.AsyncMock(side_effect=http_error(discord.NotFound, 404))

    def channel(self, channel_id):
        channel = SimpleNamespace(id=channel_id, send=mock.AsyncMock())
        self.channels[channel_id] = channel
        return channel

    def notices(self, channel):
        return [call.kwargs["embed"].description for call in channel.send.await_args_list]

    def state(self, guild_id, puuid):
        watch = next(w for w in db.get_watches(guild_id) if w["puuid"] == puuid)
        return watch["last_rank"], watch["last_elo"]

    async def test_change_detection(self):
        a, b = self.channel(1), self.channel(2)
        add_watch(100, "rank-up", "Monte", "Gold 3", 1190, channel_id=1)        # ancien nom anglais en base
        add_watch(200, "rank-up", "Monte", "Or 3", 1190, channel_id=2)          # même joueur, autre serveur
        add_watch(100, "rr", "Rejoue", "Platine 1", 1214, channel_id=1)
        add_watch(100, "same", "Pareil", "Platine 2", 1250, channel_id=1)
        add_watch(100, "down", "Descend", "Diamant 1", 1800, channel_id=1)
        add_watch(100, "placements", "Placements", "Or 1", 1000, channel_id=1)
        add_watch(100, "new", "Nouveau", None, None, channel_id=1)
        self.analyzer.snapshots.update({
            "rank-up": PLATINUM_1,
            "rr": {"tier": 15, "rank": "Platine 1", "elo": 1231, "games_needed": 0},
            "same": {"tier": 16, "rank": "Platine 2", "elo": 1250, "games_needed": 0},
            "down": {"tier": 17, "rank": "Platine 3", "elo": 1780, "games_needed": 0},
            "placements": {"tier": 0, "rank": "Non classé", "elo": None, "games_needed": 2},
            "new": PLATINUM_1,
        })
        await self.cog_.check_watches()
        self.assertEqual(self.analyzer.count("snapshot"), 6)   # « rank-up » suivi sur 2 serveurs : un seul appel
        self.assertCountEqual(self.notices(a), [
            "📉 **Descend#EUW** est descendu de **Diamant 1** à **Platine 3**.",
            "📈 **Monte#EUW** est monté de **Or 3** à **Platine 1**.",
            "🎮 **Rejoue#EUW** a rejoué en classé : **+17 RR** (Platine 1).",
        ])
        self.assertEqual(self.notices(b), ["📈 **Monte#EUW** est monté de **Or 3** à **Platine 1**."])
        self.assertEqual(a.send.await_args.kwargs["embed"].footer.text, "Suivi de rang /watch — /unwatch pour arrêter")
        self.assertEqual(self.state(100, "rank-up"), ("Platine 1", 1231))
        self.assertEqual(self.state(100, "placements"), ("Or 1", 1000))   # placements ignorés, état conservé
        self.assertEqual(self.state(100, "new"), ("Platine 1", 1231))     # premier point de départ, sans annonce
        # Deuxième passage sans changement : rien n'est annoncé
        await self.cog_.check_watches()
        self.assertEqual(len(self.notices(a)), 3)

    async def test_rate_limit_stops_the_round(self):
        a = self.channel(1)
        add_watch(100, "x", "Joueur", "Or 3", 1190, channel_id=1)
        add_watch(100, "y", "Autre", "Or 3", 1190, channel_id=1)
        self.analyzer.snapshots.update({"x": RateLimited(30), "y": PLATINUM_1})
        with self.assertLogs("smurfbot", "WARNING"):
            await self.cog_.check_watches()
        self.assertEqual(self.analyzer.count("snapshot"), 1)
        a.send.assert_not_awaited()
        self.assertEqual(self.state(100, "y"), ("Or 3", 1190))

    async def test_api_error_skips_player(self):
        a = self.channel(1)
        add_watch(100, "x", "Joueur", "Or 3", 1190, channel_id=1)
        add_watch(100, "y", "Autre", "Or 3", 1190, channel_id=1)
        self.analyzer.snapshots.update({"x": ApiError(502), "y": PLATINUM_1})
        with self.assertLogs("smurfbot", "WARNING"):
            await self.cog_.check_watches()
        self.assertEqual(len(self.notices(a)), 1)
        self.assertEqual(self.state(100, "x"), ("Or 3", 1190))

    async def test_unreachable_channels_remove_the_watch(self):
        forbidden = self.channel(1)
        forbidden.send.side_effect = http_error(discord.Forbidden, 403)
        flaky = self.channel(3)
        flaky.send.side_effect = http_error(discord.HTTPException, 500)
        add_watch(100, "a", "Interdit", "Or 3", 1190, channel_id=1)
        add_watch(200, "b", "Disparu", "Or 3", 1190, channel_id=2)     # salon supprimé : fetch_channel -> 404
        add_watch(300, "c", "Panne", "Or 3", 1190, channel_id=3)       # erreur passagère : suivi conservé
        for puuid in "abc":
            self.analyzer.snapshots[puuid] = PLATINUM_1
        with self.assertLogs("smurfbot", "INFO") as logs:
            await self.cog_.check_watches()
        self.assertEqual(db.get_watches(100), [])
        self.assertEqual(db.get_watches(200), [])
        self.assertEqual(len(db.get_watches(300)), 1)
        self.bot.fetch_channel.assert_awaited_once_with(2)
        self.assertTrue(any("retiré" in line for line in logs.output))

    async def test_waits_for_api_budget(self):
        # R1-F3 / R2-16 : le suivi laisse la priorité aux commandes des membres
        watch_module = self.module("cogs.watch")
        a = self.channel(1)
        add_watch(100, "x", "Joueur", "Or 3", 1190, channel_id=1)
        self.analyzer.snapshots["x"] = PLATINUM_1
        budgets = [3, 5, 20]   # deux lectures trop basses (attente), puis assez d'appels disponibles
        seen = []

        def budget():
            seen.append(budgets[0])
            return budgets.pop(0) if len(budgets) > 1 else budgets[0]

        self.bot.henrik = SimpleNamespace(budget=budget)
        with mock.patch.object(watch_module, "WATCH_BUDGET_WAIT", 0):
            await self.cog_.check_watches()
        self.assertEqual(seen, [3, 5, 20])
        self.assertEqual(self.analyzer.count("snapshot"), 1)
        self.assertEqual(len(self.notices(a)), 1)

    async def test_round_postponed_when_api_stays_saturated(self):
        watch_module = self.module("cogs.watch")
        add_watch(100, "x", "Joueur", "Or 3", 1190, channel_id=1)
        self.analyzer.snapshots["x"] = PLATINUM_1
        self.bot.henrik = SimpleNamespace(budget=lambda: 0)
        with mock.patch.object(watch_module, "WATCH_BUDGET_WAIT", 0), self.assertLogs("smurfbot", "INFO") as logs:
            await self.cog_.check_watches()
        self.assertEqual(self.analyzer.count("snapshot"), 0)
        self.assertEqual(self.state(100, "x"), ("Or 3", 1190))
        self.assertTrue(any("reporté" in line for line in logs.output))

    async def test_loop_body_never_raises(self):
        with mock.patch.object(self.cog_, "check_watches", side_effect=RuntimeError("panne")), \
                self.assertLogs("smurfbot", "ERROR"):
            await self.cog_.watch_loop.coro(self.cog_)
        self.assertTrue(self.cog_.watch_loop.is_running())
        self.assertEqual(self.cog_.watch_loop.minutes, 30)


if __name__ == "__main__":
    unittest.main()
