import dataclasses
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from core import db, embeds
from core.discord_helpers import NO_REPORTS_CHANNEL, PUBLIC_REPORTS_CHANNEL, SUPPORT_URL, MOD_ONLY
from tests import PLAYER_PUUID
from tests.test_cogs import (
    CogTestCase, SMURF_PUUID, make_channel, make_guild, make_member, last_output, outputs, field, text_of,
    check_limits, custom_ids, smurf_analysis,
)


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="Erreur"), "échec simulé")


class ReportTestCase(CogTestCase):
    async def run_report(self, riot_id="Suspect#EUW", observations=None, user=None, guild=None):
        interaction = self.interaction(user=user or make_member(42), guild=guild or self.guild)
        cog = self.cog("ReportsCog")
        await cog.report.callback(cog, interaction, riot_id=riot_id, observations=observations)
        return interaction

    def report_count(self):
        return db.db_query("SELECT COUNT(*) FROM reports")[0][0]

    def review_button(self, report_id, action):
        return self.module("cogs.reports").ReviewButton(report_id, action)


# ================= /report =================
class ReportChannelTests(ReportTestCase):
    async def test_no_reports_channel(self):
        interaction = await self.run_report()
        self.assertEqual(interaction.response.calls, [("send_message", {"ephemeral": True, "content": NO_REPORTS_CHANNEL})])
        self.assertEqual(self.analyzer.calls, [])
        self.assertEqual(self.report_count(), 0)

    async def test_public_channel_refused(self):
        channel = make_channel(self.guild, public=True)
        interaction = await self.run_report()
        self.assertEqual(last_output(interaction)["content"], PUBLIC_REPORTS_CHANNEL)
        channel.send.assert_not_awaited()
        self.assertEqual(self.report_count(), 0)

    async def test_bot_cannot_post(self):
        make_channel(self.guild, bot_permissions=dict(view_channel=True, send_messages=True))
        out = last_output(await self.run_report())
        self.assertIn("**Intégrer des liens**", out["content"])
        self.assertTrue(out["ephemeral"])

    async def test_configured_channel_wins_over_name(self):
        make_channel(self.guild, channel_id=500)
        configured = make_channel(self.guild, channel_id=600, name="dossiers-modo")
        db.set_reports_channel_id(self.guild.id, 600)
        await self.run_report()
        configured.send.assert_awaited_once()
        self.assertEqual(db.get_report(1)["channel_id"], "600")

    async def test_invalid_riot_id(self):
        make_channel(self.guild)
        out = last_output(await self.run_report("PasDeTag"))
        self.assertIn("Riot ID invalide", out["content"])


class ReportFlowTests(ReportTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.channel = make_channel(self.guild)

    async def test_low_index_refused_with_explanation(self):
        interaction = await self.run_report("TestSmurf#0001")
        self.assertEqual(interaction.response.calls[0], ("defer", {"ephemeral": True}))
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        embed = out["embed"]
        self.assertEqual(embed.title, "Signalement non retenu")
        self.assertIn(f"**{self.fixture.suspicion.index}/100 (Faible)**", embed.description)
        self.assertIn("qu'à partir de 61/100", embed.description)
        self.assertIn("revient d'une pause", embed.description)
        self.assertEqual(field(embed, "Signaux détectés"), "Aucun signal détecté.")
        check_limits(self, embed)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.report_count(), 0)
        self.assertEqual(self.activity(42, PLAYER_PUUID, "report_refused"), 1)
        self.assertEqual(db.get_player_history("TestSmurf", "0001")[1], 1)   # analyse neuve enregistrée

    async def test_accepted_report(self):
        observations = "Il annonçait toutes les positions adverses et jouait comme un Immortel."
        interaction = await self.run_report(observations=observations)
        # Dossier publié dans le salon des modérateurs, avec les boutons de validation
        self.channel.send.assert_awaited_once()
        sent = self.channel.send.await_args.kwargs
        dossier = sent["embed"]
        self.assertEqual(dossier.title, "🗂️ Dossier n°1 — Suspect#EUW")
        self.assertEqual(field(dossier, "Riot ID"), "Suspect#EUW")
        self.assertRegex(field(dossier, "Date"), r"^<t:\d+:f>$")
        self.assertEqual(field(dossier, "Signalé par"), "<@42>")
        self.assertIn(f"**{self.smurf.suspicion.index}/100", field(dossier, "Indice de suspicion"))
        self.assertEqual(field(dossier, "Stats anormales").count("• "), len(self.smurf.suspicion.signals))
        self.assertIn("**K/D :**", field(dossier, "Stats clés"))
        matches = field(dossier, "Parties concernées").split("\n")
        self.assertEqual(len(matches), 8)
        self.assertIn("lobby)", matches[0])   # la partie la plus dominante du lobby d'abord
        self.assertIn(self.smurf.lobby.per_match[-1][0], matches[0])
        self.assertEqual(field(dossier, "Observations du membre"), observations)
        self.assertEqual(field(dossier, "Statut"), "⏳ En attente de validation par un modérateur")
        self.assertEqual(dossier.footer.text, "Analyse indicative, ne constitue pas une preuve. La décision finale appartient à Riot Games.")
        check_limits(self, dossier)
        self.assertEqual(custom_ids(sent["view"]), ["sw:review:1:ok", "sw:review:1:no"])
        self.assertIsNone(sent["view"].timeout)
        # Enregistré en base
        report = db.get_report(1)
        self.assertEqual((report["status"], report["guild_id"], report["reporter_id"], report["puuid"]),
                         ("pending", str(self.guild.id), "42", SMURF_PUUID))
        self.assertEqual((report["channel_id"], report["message_id"]), ("500", str(9000 + 500 + 1)))
        self.assertEqual(report["score"], self.smurf.suspicion.index)
        self.assertEqual(len(report["match_ids"]), 8)
        self.assertEqual(report["observations"], observations)
        self.assertEqual([s["key"] for s in report["signals"]], [s.key for s in self.smurf.suspicion.signals])
        self.assertEqual(self.activity(42, SMURF_PUUID, "report"), 1)
        # Réponse privée : comment signaler officiellement + dossier en texte brut
        self.assertEqual(len(outputs(interaction)), 1)
        out = last_output(interaction)
        self.assertTrue(out["ephemeral"])
        self.assertEqual(out["embed"].title, "✅ Dossier n°1 transmis aux modérateurs")
        guide = text_of(out["embed"])
        for text in ("**En jeu**", "**Signaler**", "Manipulation du classement / smurf", "**Support Riot**",
                     "support-valorant.riotgames.com", "ne constitue pas une preuve", "appartient à Riot Games"):
            self.assertIn(text, guide)
        check_limits(self, out["embed"])
        content = out["content"]
        self.assertLessEqual(len(content), 2000)
        self.assertIn("```text\nDossier SmurfWatch n°1\nRiot ID : Suspect#EUW", content)
        dossier_text = content.split("```text\n", 1)[1].rsplit("\n```", 1)[0]
        self.assertLessEqual(len(dossier_text), 1500)
        self.assertIn(report["match_ids"][0], dossier_text)
        self.assertIn(observations, dossier_text)
        self.assertEqual(out["view"].children[0].url, SUPPORT_URL)
        self.assertEqual(out["view"].children[0].label, "Support Riot")

    async def test_duplicate_within_7_days(self):
        await self.run_report()
        created = db.get_report(1)["created_at"]
        out = last_output(await self.run_report())
        self.assertEqual(out["content"],
                         f"Tu as déjà signalé ce compte le <t:{created}:d>. Un même compte ne peut être signalé qu'une "
                         f"fois tous les 7 jours par membre : tu pourras recommencer <t:{created + 7 * 86400}:R>.")
        self.assertEqual(self.report_count(), 1)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(self.activity(42, SMURF_PUUID, "report_refused"), 1)
        # Un autre membre peut signaler le même compte
        await self.run_report(user=make_member(43))
        self.assertEqual(self.report_count(), 2)
        # Au-delà de 7 jours, le même membre peut recommencer
        db.db_query("UPDATE reports SET created_at = datetime('now', '-8 days')")
        await self.run_report()
        self.assertEqual(self.report_count(), 3)

    async def test_harassment_block_and_single_alert(self):
        for _ in range(3):
            db.log_activity(self.guild.id, 42, SMURF_PUUID, "report_refused")
        for _ in range(2):
            out = last_output(await self.run_report())
            self.assertTrue(out["ephemeral"])
            self.assertIn("Pour éviter tout acharnement", out["content"])
            self.assertIn("les modérateurs ont été prévenus", out["content"])
        self.channel.send.assert_awaited_once()   # une seule alerte, aucun dossier
        alert = self.channel.send.await_args.kwargs["embed"]
        self.assertEqual(alert.title, "🚨 Protection anti-acharnement")
        self.assertIn("`/report`", alert.description)
        self.assertIn("3 tentatives de signalement de ce joueur en 7 jours", alert.description)
        self.assertEqual(self.report_count(), 0)
        self.assertEqual(self.analyzer.count("analyze"), 0)
        self.assertEqual(self.activity(42, SMURF_PUUID, ("report", "report_refused")), 3)   # refus non comptés
        self.assertEqual(self.activity(42, SMURF_PUUID, "alert", 24), 1)

    async def test_send_failure_discards_report(self):
        self.channel.send.side_effect = http_error(discord.Forbidden, 403)
        with self.assertLogs("smurfbot", "WARNING"):
            out = last_output(await self.run_report())
        self.assertIn("Je n'ai pas pu publier le dossier", out["content"])
        self.assertEqual(self.report_count(), 0)
        self.assertIsNone(db.last_report_by(42, SMURF_PUUID))   # le membre n'est pas bloqué 7 jours
        self.assertEqual(self.activity(42, SMURF_PUUID, "report"), 0)

    async def test_unknown_player(self):
        out = last_output(await self.run_report("Personne#000"))
        self.assertIn("Joueur introuvable", out["content"])
        self.assertTrue(out["ephemeral"])

    async def test_own_linked_account_cannot_be_reported(self):
        # R2-01 : plus d'exemption pour le compte lié ; le signaler soi-même n'a pas de sens
        db.set_linked_account(42, "Suspect", "EUW")
        out = last_output(await self.run_report())
        self.assertEqual(out["content"], self.module("cogs.reports").OWN_ACCOUNT)
        self.assertTrue(out["ephemeral"])
        self.assertEqual(self.report_count(), 0)
        self.assertEqual(self.analyzer.count("analyze"), 0)
        # Un autre membre peut toujours le signaler
        await self.run_report(user=make_member(43))
        self.assertEqual(self.report_count(), 1)

    async def test_partial_analysis_refusal_is_not_counted(self):
        # R1-F1 (d) : refus dû à une analyse incomplète (API saturée) : pas compté comme tentative
        low = dataclasses.replace(self.fixture, partial=True)
        self.analyzer.add(low)
        out = last_output(await self.run_report("TestSmurf#0001"))
        self.assertIn(embeds.PARTIAL_REFUSAL, out["embed"].description)
        self.assertEqual(self.activity(42, PLAYER_PUUID, ("report", "report_refused")), 0)

    async def test_personal_information_removed_from_observations(self):
        # R2-06 : aucune information personnelle hors jeu dans un dossier
        observations = ("Il jouait comme un Immortel. C'est Jean, insta @jd_75, mail jean.d@exemple.fr, "
                        "tel 06 12 34 56 78, son site https://exemple.fr/jean et <@123456789012345678>.")
        interaction = await self.run_report(observations=observations)
        stored = db.get_report(1)["observations"]
        for secret in ("@jd_75", "jean.d@exemple.fr", "06 12 34 56 78", "https://exemple.fr/jean", "123456789012345678"):
            self.assertNotIn(secret, stored)
        self.assertIn("Il jouait comme un Immortel.", stored)
        self.assertEqual(field(self.channel.send.await_args.kwargs["embed"], "Observations du membre"), stored)
        out = last_output(interaction)
        self.assertNotIn("@jd_75", out["content"])
        self.assertIn("ont été retirées de tes observations", field(out["embed"], "ℹ️ Observations"))

    async def test_clean_observations_are_kept_as_is(self):
        observations = "Score 13-5, il annonçait tout. Riot ID vu en jeu : Suspect#EUW, ACS 1,8× le lobby."
        out = last_output(await self.run_report(observations=observations))
        self.assertEqual(db.get_report(1)["observations"], observations)
        self.assertIsNone(field(out["embed"], "ℹ️ Observations"))

    async def test_moderators_see_previous_reports(self):
        # R2-09 : les modérateurs voient qu'un compte a déjà plusieurs dossiers (signalements en groupe ?)
        await self.run_report(user=make_member(42))
        self.assertIsNone(field(self.channel.send.await_args.kwargs["embed"], "Autres dossiers sur ce compte"))
        await self.run_report(user=make_member(43))
        self.assertIn("1 dossier déjà ouvert", field(self.channel.send.await_args.kwargs["embed"],
                                                      "Autres dossiers sur ce compte"))

    async def test_unexpected_send_error_still_discards_report(self):
        self.channel.send.side_effect = RuntimeError("panne inattendue")
        with self.assertLogs("smurfbot", "ERROR"):
            out = last_output(await self.run_report())
        self.assertEqual(self.report_count(), 0)    # le membre n'est pas bloqué 7 jours par une panne du bot
        self.assertTrue(out["ephemeral"])


class RedactionTests(unittest.TestCase):
    def test_redact_personal(self):
        from core.discord_helpers import redact_personal, REDACTED
        cases = {
            "va sur www.exemple.com stp": "va sur [retiré] stp",
            "discord.gg/abcd": REDACTED,
            "contact : a.b+c@mail.co.uk": f"contact : {REDACTED}",
            "insta @pseudo.75 !": f"insta {REDACTED} !",
            "ip 192.168.1.20": f"ip {REDACTED}",
            "+33 6 12 34 56 78": REDACTED,
            "0612345678": REDACTED,
        }
        for text, expected in cases.items():
            self.assertEqual(redact_personal(text), (expected, True), text)
        for clean in ("Score 13-5 sur Ascent", "Pseudo#EUW, 1,8× le lobby", "@A site", ""):
            self.assertEqual(redact_personal(clean), (clean, False), clean)
        self.assertEqual(redact_personal(None), ("", False))


# ================= Validation des dossiers =================
class ReviewButtonTests(ReportTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.channel = make_channel(self.guild)
        await self.run_report()
        self.message = SimpleNamespace(id=9501, embeds=[self.channel.send.await_args.kwargs["embed"]])

    async def click(self, action, user, permissions=None, guild=None):
        interaction = self.interaction(user=user, permissions=permissions, message=self.message, guild=guild or self.guild)
        cls = self.module("cogs.reports").ReviewButton
        match = cls.__discord_ui_compiled_template__.fullmatch(f"sw:review:1:{action}")
        button = await cls.from_custom_id(interaction, None, match)
        self.assertEqual((button.report_id, button.action), (1, action))
        await button.callback(interaction)
        return interaction

    async def test_non_moderator_refused(self):
        interaction = await self.click("ok", make_member(50))
        out = last_output(interaction)
        self.assertEqual(out["content"], "❌ Seuls les modérateurs peuvent valider ou rejeter un dossier.")
        self.assertTrue(out["ephemeral"])
        self.assertEqual(db.get_report(1)["status"], "pending")

    async def test_moderator_validates_then_already_handled(self):
        mod = make_member(77, manage_messages=True)
        interaction = await self.click("ok", mod, permissions=discord.Permissions(manage_messages=True))
        kind, kwargs = interaction.response.calls[0]
        self.assertEqual(kind, "edit_message")
        status = field(kwargs["embed"], "Statut")
        self.assertRegex(status, r"^✅ Validé par <@77> le <t:\d+:f>$")
        self.assertEqual(kwargs["embed"].title, "🗂️ Dossier n°1 — Suspect#EUW")
        self.assertTrue(all(item.item.disabled for item in kwargs["view"].children))
        self.assertEqual(custom_ids(kwargs["view"]), ["sw:review:1:ok", "sw:review:1:no"])
        report = db.get_report(1)
        self.assertEqual((report["status"], report["reviewed_by"]), ("validated", "77"))
        # Deuxième clic (autre modérateur, autre bouton) : déjà traité
        admin = make_member(78, administrator=True)
        interaction = await self.click("no", admin)
        self.assertEqual(last_output(interaction)["content"], "Ce dossier a déjà été traité.")
        self.assertEqual(db.get_report(1)["status"], "validated")

    async def test_reject(self):
        interaction = await self.click("no", make_member(79, manage_guild=True))
        status = field(interaction.response.calls[0][1]["embed"], "Statut")
        self.assertTrue(status.startswith("❌ Rejeté par <@79>"))
        self.assertEqual(db.get_report(1)["status"], "rejected")

    async def test_other_guild(self):
        interaction = await self.click("ok", make_member(77, administrator=True), guild=make_guild(200))
        self.assertEqual(last_output(interaction)["content"], "❌ Dossier introuvable.")
        self.assertEqual(db.get_report(1)["status"], "pending")


# ================= /watchlist =================
class WatchlistTests(ReportTestCase):
    async def run_watchlist(self, statut=None, user=None, permissions=None):
        interaction = self.interaction(user=user or make_member(77), permissions=permissions)
        cog = self.cog("ReportsCog")
        choice = None
        if statut:
            choice = next(c for c in self.module("cogs.reports").STATUS_CHOICES if c.value == statut)
        await cog.watchlist.callback(cog, interaction, statut=choice)
        return last_output(interaction)

    async def test_moderators_only(self):
        out = await self.run_watchlist(user=make_member(50))
        self.assertEqual((out["content"], out["ephemeral"]), (MOD_ONLY, True))

    async def test_empty(self):
        out = await self.run_watchlist(permissions=discord.Permissions(manage_messages=True))
        self.assertEqual(out["content"], "Aucun compte signalé sur ce serveur.")
        self.assertTrue(out["ephemeral"])

    async def test_aggregated_list(self):
        make_channel(self.guild)
        await self.run_report(user=make_member(42))
        await self.run_report(user=make_member(43))
        other = self.analyzer.add(smurf_analysis("Autre", "FR1", "autre-puuid"))
        await self.run_report("Autre#FR1", user=make_member(44))
        db.review_report(3, "validated", 77)
        # Dossier d'un autre serveur : jamais affiché ici
        db.create_report(999, 45, other.account.puuid, "Autre", "FR1", 70, "eleve", [], [], None)

        mod = make_member(77, manage_messages=True)
        out = await self.run_watchlist(user=mod)
        self.assertTrue(out["ephemeral"])
        embed = out["embed"]
        lines = embed.description.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("⏳ **Suspect#EUW** — 2 dossiers par 2 membres · "
                                            f"indice max {self.smurf.suspicion.index} · dernier <t:"))
        self.assertTrue(lines[0].endswith("· n°2"))
        self.assertTrue(lines[1].startswith("✅ **Autre#FR1** — 1 dossier par 1 membre"))
        self.assertEqual(embed.footer.text, "Liste réservée aux modérateurs")
        check_limits(self, embed)

        out = await self.run_watchlist("validated", user=mod)
        self.assertEqual(out["embed"].description.count("\n"), 1)   # filtre + une ligne
        self.assertIn("Filtre : **Validés**", out["embed"].description)
        self.assertIn("Autre#FR1", out["embed"].description)
        out = await self.run_watchlist("rejected", user=mod)
        self.assertEqual(out["content"], "Aucun compte signalé avec le statut « Rejetés » sur ce serveur.")


# ================= /report-setup =================
class ReportSetupTests(ReportTestCase):
    async def run_setup(self, salon=None, user=None, guild=None):
        guild = guild or self.guild
        interaction = self.interaction(user=user or make_member(1000, manage_guild=True), guild=guild)
        cog = self.cog("ReportsCog")
        await cog.report_setup.callback(cog, interaction, salon=salon)
        return last_output(interaction)

    async def test_admins_only(self):
        out = await self.run_setup(user=make_member(50, manage_messages=True))
        self.assertIn("réservée aux administrateurs", out["content"])
        self.assertIsNone(db.get_reports_channel_id(self.guild.id))

    async def test_existing_private_channel(self):
        channel = make_channel(self.guild, channel_id=700, name="modo")
        out = await self.run_setup(channel)
        self.assertIn("✅ Les dossiers `/report` seront publiés dans <#700>.", out["content"])
        self.assertIn("rôles de modération", out["content"])
        self.assertNotIn("visible de tous", out["content"])
        self.assertEqual(db.get_reports_channel_id(self.guild.id), 700)

    async def test_public_channel_accepted_with_warning(self):
        channel = make_channel(self.guild, channel_id=701, name="general", public=True)
        out = await self.run_setup(channel)
        self.assertIn("visible de tous", out["content"])
        self.assertEqual(db.get_reports_channel_id(self.guild.id), 701)

    async def test_channel_without_bot_permissions(self):
        channel = make_channel(self.guild, channel_id=702, bot_permissions=dict(view_channel=True))
        out = await self.run_setup(channel)
        self.assertIn("il me manque", out["content"])
        self.assertIn("**Envoyer des messages**", out["content"])
        self.assertIsNone(db.get_reports_channel_id(self.guild.id))

    async def test_creates_private_channel(self):
        created = make_channel(self.guild, channel_id=800, attach=False)
        self.guild.create_text_channel.return_value = created
        with self.assertLogs("smurfbot", "INFO"):
            out = await self.run_setup()
        self.guild.create_text_channel.assert_awaited_once()
        args, kwargs = self.guild.create_text_channel.await_args
        self.assertEqual(args[0], "reports-smurf")
        overwrites = kwargs["overwrites"]
        self.assertFalse(overwrites[self.guild.default_role].view_channel)
        me = overwrites[self.guild.me]
        self.assertTrue(me.view_channel and me.send_messages and me.embed_links and me.read_message_history)
        self.assertEqual(db.get_reports_channel_id(self.guild.id), 800)
        self.assertIn("✅ Salon <#800> créé", out["content"])
        self.assertTrue(out["ephemeral"])

    async def test_reuses_existing_reports_smurf(self):
        make_channel(self.guild, channel_id=801)
        out = await self.run_setup()
        self.guild.create_text_channel.assert_not_awaited()
        self.assertEqual(db.get_reports_channel_id(self.guild.id), 801)
        self.assertIn("<#801>", out["content"])

    async def test_needs_manage_channels(self):
        guild = make_guild(manage_channels=False)
        out = await self.run_setup(guild=guild)
        self.assertIn("**Gérer les salons**", out["content"])
        guild.create_text_channel.assert_not_awaited()

    async def test_creation_failure(self):
        self.guild.create_text_channel.side_effect = http_error(discord.Forbidden, 403)
        with self.assertLogs("smurfbot", "WARNING"):
            out = await self.run_setup()
        self.assertIn("Impossible de créer le salon", out["content"])
        self.assertIsNone(db.get_reports_channel_id(self.guild.id))


if __name__ == "__main__":
    unittest.main()
