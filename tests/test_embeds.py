import dataclasses
import time
import unittest
from types import SimpleNamespace

import discord

from core import embeds
from core.analysis import Account, MmrInfo, LobbyPlayer, LobbyMatch, RankComparison, aggregate, parse_match_v4
from core.scoring import Signal, Suspicion, CRITERIA, LEVELS, quick_match_index
from tests import load_fixture
from tests.test_cogs import fixture_analysis, smurf_analysis, check_limits, field, text_of

LONG_NAME = "ẞüñíçødé_Ωmega*1"   # 16 caractères, avec de la mise en forme Discord
LONG_TAG = "ÆØÅ_1"


def worst_case_analysis():
    # Tous les critères déclenchés, libellés et contextes très longs, pseudo unicode de 16 caractères
    base = smurf_analysis(LONG_NAME, LONG_TAG)
    signals = [Signal(key, f"Critère {key} : " + "x" * 220, points, points) for key, points in CRITERIA]
    suspicion = Suspicion(index=100, level="tres_eleve", signals=signals,
                          missing=[f"Donnée {i} : " + "y" * 200 for i in range(9)],
                          notes=[f"Note {i} : " + "z" * 200 for i in range(5)], capped=False)
    return dataclasses.replace(base, suspicion=suspicion, from_cache=True, analyzed_at=int(time.time()) - 400)


class HelperTests(unittest.TestCase):
    def test_clip_and_join(self):
        self.assertEqual(embeds.clip("abcdef", 4), "abc…")
        self.assertEqual(embeds.clip("abc", 4), "abc")
        self.assertEqual(embeds.clip(None, 4), "")
        text = embeds.join_lines([f"ligne {i:02d}" for i in range(50)], 100)
        self.assertLessEqual(len(text), 100)
        self.assertTrue(text.endswith("autre(s)"))
        self.assertEqual(embeds.join_lines(["a", "b"], 100), "a\nb")
        self.assertEqual(embeds.join_lines([], 100), "")

    def test_escape_and_plural(self):
        self.assertEqual(embeds.riot("Nom_x", "T*1"), "Nom\\_x#T\\*1")
        self.assertEqual(embeds.escape("a`b|c~d\\e"), "a\\`b\\|c\\~d\\\\e")
        self.assertEqual(embeds.plural(0, "partie"), "partie")
        self.assertEqual(embeds.plural(1, "partie"), "partie")
        self.assertEqual(embeds.plural(2, "partie"), "parties")

    def test_fit_enforces_total(self):
        embed = discord.Embed(title="t" * 300, description="d" * 5000)
        for i in range(30):
            embed.add_field(name=f"champ {i}", value="v" * 2000)
        embed.set_footer(text="f" * 3000)
        check_limits(self, embeds.fit(embed))

    def test_display_rank(self):
        self.assertEqual(embeds.display_rank("Gold 2"), "Or 2")
        self.assertEqual(embeds.display_rank("Immortal 3"), "Immortel 3")
        self.assertEqual(embeds.display_rank("Platine 1"), "Platine 1")
        self.assertEqual(embeds.display_rank(None), "rang inconnu")
        self.assertEqual(embeds.display_rank("Inconnu"), "Inconnu")


class AnalysisEmbedTests(unittest.TestCase):
    def test_spec_format(self):
        analysis = fixture_analysis()
        embed = embeds.analysis_embed(analysis)
        self.assertEqual(embed.title, "🔍 Analyse de TestSmurf#0001")
        self.assertEqual(embed.footer.text, "Analyse indicative, ne constitue pas une preuve.")
        self.assertEqual(embed.colour.value, LEVELS[0][5])
        self.assertEqual(embed.thumbnail.url, "https://media.valorant-api.com/competitivetiers/"
                                              "03621f52-342b-cf4e-4f86-9350a49c6d04/15/smallicon.png")
        self.assertEqual(int(embed.timestamp.timestamp()), analysis.analyzed_at)
        profile = field(embed, "Profil")
        self.assertIn("**Rang actuel :** Platine 1 (", profile)
        self.assertIn("**Pic :** ", profile)
        self.assertIn("**Niveau du compte :** 331", profile)
        self.assertIn("**Parties classées :** ", profile)
        stats = field(embed, "Stats clés (20 dernières parties)")
        self.assertRegex(stats, r"\*\*K/D :\*\* \d,\d\d\n\*\*HS % :\*\* \d+,\d % *\n\*\*ACS :\*\* \d+\n"
                                r"\*\*ADR :\*\* \d+\n\*\*Winrate :\*\* \d+ % \(\d+V-\d+D\)")
        self.assertEqual(field(embed, "Indice de suspicion"), "🟢 **0/100 — Faible**\nProfil cohérent avec le rang")
        self.assertEqual(field(embed, "Signaux détectés"), "Aucun signal détecté.")
        self.assertIn("• Compte ancien (niveau 331)", field(embed, "Contexte"))
        check_limits(self, embed)

    def test_levels_and_colours(self):
        base = fixture_analysis()
        for index, (_, _, emoji, label, _, colour) in zip((10, 45, 70, 95), LEVELS):
            suspicion = dataclasses.replace(base.suspicion, index=index)
            embed = embeds.analysis_embed(dataclasses.replace(base, suspicion=suspicion))
            self.assertEqual(embed.colour.value, colour)
            self.assertTrue(field(embed, "Indice de suspicion").startswith(f"{emoji} **{index}/100 — {label}**"))
            self.assertEqual("/report" in embed.description, index >= 61)

    def test_worst_case_stays_within_limits(self):
        analysis = worst_case_analysis()
        embed = embeds.analysis_embed(analysis, now=analysis.analyzed_at + 400)
        check_limits(self, embed)
        self.assertEqual(embed.title, f"🔍 Analyse de {LONG_NAME}#{LONG_TAG}")
        self.assertIn("Résultat en cache (analyse d'il y a 6 min)", embed.description)
        self.assertEqual(field(embed, "Signaux détectés").count("• "), 9)
        self.assertIn("⚠️ Donnée manquante", field(embed, "Contexte"))
        self.assertEqual(field(embed, "Contexte").count("\n") + 1, 14)   # toutes les lignes gardées, raccourcies

    def test_missing_data_is_said(self):
        base = fixture_analysis()
        account = dataclasses.replace(base.account, level=None)
        empty = dataclasses.replace(base, account=account, mmr=None, matches=[], agg=aggregate([]))
        embed = embeds.analysis_embed(empty)
        self.assertEqual(field(embed, "Stats clés"), "Aucune partie classée récente.")
        profile = field(embed, "Profil")
        self.assertEqual(profile.count("inconnu"), 4)
        self.assertIsNone(embed.thumbnail.url)
        placements = MmrInfo(0, None, None, None, 3, None, None, 5, [])
        embed = embeds.analysis_embed(dataclasses.replace(base, mmr=placements))
        self.assertIn("**Rang actuel :** En placements (encore 3 parties)", field(embed, "Profil"))

    def test_matches_embed(self):
        embed = embeds.matches_embed(worst_case_analysis())
        check_limits(self, embed)
        self.assertEqual(embed.description.count("\n") + 1, 16)


class CompareEmbedTests(unittest.TestCase):
    def test_fixture(self):
        analysis = fixture_analysis()
        embed = embeds.compare_embed(analysis)
        check_limits(self, embed)
        table = embed.description.split("```text\n")[1].split("\n```")[0].split("\n")
        self.assertEqual(len(table), 6)
        self.assertEqual(table[0].split(), ["Joueur", "Moyenne", "Écart"])
        self.assertTrue(table[2].startswith("HS %"))
        self.assertIn(" pts", table[2])
        self.assertIn("×", table[1])
        self.assertTrue(table[5].startswith("Niveau (méd.)"))   # R2-11 : la colonne « Moyenne » est une médiane ici
        self.assertEqual(len(table[5].split()), 4)              # pas d'écart pour le niveau
        self.assertEqual(embed.title, "📊 TestSmurf#0001 face à la moyenne de son rang")

    def test_scopes_and_missing_values(self):
        analysis = fixture_analysis()
        lobby = RankComparison("lobby", "joueurs de ses lobbys", 1, 9,
                               {"kd": 1.0, "hs_pct": None, "acs": 200, "adr": 140, "level": 80},
                               {"kd": 2.0, "hs_pct": 30.0, "acs": 300, "adr": None, "level": None})
        embed = embeds.compare_embed(dataclasses.replace(analysis, comparison=lobby))
        # R2-11 : une ligne par joueur et par partie, et un titre qui dit à quoi il est comparé
        self.assertIn("l'ensemble des joueurs croisés dans sa dernière partie (9 performances de joueurs)", embed.description)
        self.assertEqual(embed.title, "📊 TestSmurf#0001 face à la moyenne de ses lobbys")
        self.assertIn("sur 2 stats sur 2 comparables", field(embed, "Lecture"))
        near = dataclasses.replace(lobby, scope="near", label="Or 1 à Platine 1", matches=5)
        embed = embeds.compare_embed(dataclasses.replace(analysis, comparison=near))
        self.assertIn("aux joueurs **Or 1 à Platine 1** croisés dans ses 5 dernières parties", embed.description)
        self.assertEqual(embed.title, "📊 TestSmurf#0001 face à la moyenne de son rang")
        check_limits(self, embed)
        below = dataclasses.replace(lobby, player={"kd": 0.5, "hs_pct": 10.0, "acs": 100, "adr": 80, "level": 10},
                                    ref={"kd": 1.0, "hs_pct": 20.0, "acs": 200, "adr": 140, "level": 80})
        reading = field(embeds.compare_embed(dataclasses.replace(analysis, comparison=below)), "Lecture")
        self.assertTrue(reading.startswith("Au-dessus de la moyenne sur aucune stat (sur 4)."), reading)


class SmurfMatchEmbedTests(unittest.TestCase):
    def test_fixture_match(self):
        match = parse_match_v4(load_fixture("match_v4_list0"))
        scored = [(p, *quick_match_index(p, match)) for p in match.players]
        embed = embeds.smurf_match_embed(fixture_analysis().account, match, scored)
        check_limits(self, embed)
        for f in embed.fields:
            indexes = [int(line.split("**")[1]) for line in f.value.split("\n")]
            self.assertEqual(indexes, sorted(indexes, reverse=True))
            self.assertTrue(all(0 <= i <= 60 for i in indexes))

    def test_worst_case(self):
        players = [LobbyPlayer(f"p{i}", "red" if i < 5 else "blue", None, "Tejo" if i % 2 else "Miks", 24, 1,
                               LONG_NAME, LONG_TAG, 40, 1, 5, 9000, 60, 5, 0, 6000, 26) for i in range(10)]
        players.append(LobbyPlayer("x", "", None, "Veto", 0, None, LONG_NAME, LONG_TAG, 0, 0, 0, 0, 0, 0, 0, 0, 26))
        match = LobbyMatch("m", "Ascent", int(time.time()), 26, players, {"red": 13, "blue": 13})
        scored = [(p, 60, []) for p in players]
        account = Account("p0", LONG_NAME, LONG_TAG, "eu", 1, None)
        embed = embeds.smurf_match_embed(account, match, scored)
        check_limits(self, embed)
        self.assertEqual([f.name for f in embed.fields],
                         ["🔴 Équipe Rouge — 13 manches", "🔵 Équipe Bleue — 13 manches", "Autres joueurs"])
        self.assertIn("🦸", text_of(embed))
        self.assertIn("ẞüñíçødé\\_Ωmega\\*1", text_of(embed))


class DossierTests(unittest.TestCase):
    def test_worst_case_dossier(self):
        analysis = worst_case_analysis()
        reporter = SimpleNamespace(mention="<@123456789012345678>")
        observations = "Observation détaillée ``` avec un bloc. " * 40
        observations = observations[:1000]
        now = int(time.time())
        embed = embeds.dossier_embed(99999, analysis, reporter, observations, now)
        check_limits(self, embed)
        self.assertEqual(embed.title, f"🗂️ Dossier n°99999 — {LONG_NAME}#{LONG_TAG}")
        self.assertEqual(field(embed, "Statut"), embeds.STATUS_PENDING)
        self.assertLessEqual(len(field(embed, "Parties concernées").split("\n")), 8)
        text = embeds.dossier_text(99999, analysis, observations, now)
        self.assertLessEqual(len(text), 1500)
        self.assertNotIn("```", text)
        self.assertTrue(text.startswith("Dossier SmurfWatch n°99999\nRiot ID : "))
        message = embeds.dossier_message(text)
        self.assertLessEqual(len(message), 2000)
        self.assertEqual(message.count("```"), 2)
        check_limits(self, embeds.official_report_embed(99999))
        check_limits(self, embeds.report_refused_embed(analysis))

    def test_status_update(self):
        embed = embeds.dossier_embed(1, smurf_analysis(), SimpleNamespace(mention="<@1>"), "", int(time.time()))
        self.assertEqual(field(embed, "Observations du membre"), "Aucune.")
        updated = embeds.with_status(embed, embeds.review_status_text("validated", "<@2>", 1792700000))
        self.assertEqual(field(updated, "Statut"), "✅ Validé par <@2> le <t:1792700000:f>")
        self.assertEqual(field(embed, "Statut"), embeds.STATUS_PENDING)   # l'original n'est pas modifié
        self.assertEqual([f.name for f in updated.fields], [f.name for f in embed.fields])
        self.assertEqual(embeds.review_status_text("rejected", "<@2>", None), "❌ Rejeté par <@2>")

    def test_notable_matches(self):
        analysis = smurf_analysis()
        rows = embeds.notable_matches(analysis)
        self.assertEqual(len(rows), 8)
        lobby_ids = [r[0] for r in sorted(analysis.lobby.per_match, key=lambda r: r[1], reverse=True)]
        self.assertEqual([r["match_id"] for r in rows[:5]], lobby_ids)
        self.assertTrue(all(r["ratio"] is None for r in rows[5:]))
        self.assertEqual(len({r["match_id"] for r in rows}), 8)

    def test_duplicate_text(self):
        text = embeds.duplicate_report_text(1792700000)
        self.assertIn("<t:1792700000:d>", text)
        self.assertIn(f"<t:{1792700000 + 7 * 86400}:R>", text)


class ListEmbedTests(unittest.TestCase):
    def test_watchlist_truncated(self):
        rows = [{"puuid": f"p{i}", "name": LONG_NAME, "tag": LONG_TAG, "reports": 3, "reporters": 2, "max_score": 88,
                 "last_at": 1792700000, "last_id": 1000 + i, "pending": i % 2, "validated": 1, "rejected": 0}
                for i in range(30)]
        embed = embeds.watchlist_embed(rows, "Tous")
        check_limits(self, embed)
        lines = embed.description.split("\n")
        self.assertEqual(lines[-1], "… et 10 autres")
        self.assertEqual(len(lines), 1 + 20 + 1)
        self.assertTrue(lines[1].startswith("✅ "))
        self.assertTrue(lines[2].startswith("⏳ "))
        self.assertIn("3 dossiers par 2 membres · indice max 88 · dernier <t:1792700000:R> · n°1000", lines[1])

    def test_history(self):
        rows = [(LONG_NAME, LONG_TAG, 70 - i, "Gold 2" if i > 5 else "Or 2", 1.5, 1792700000 - i * 3600, 20 if i < 5 else None,
                 2 if i < 5 else None) for i in range(10)]
        embed = embeds.history_embed(rows, 42)
        check_limits(self, embed)
        self.assertIn("+4 points", field(embed, "Évolution"))   # 66 (plus ancienne) -> 70 (plus récente), nouveau calcul
        self.assertEqual(embed.description.count("(ancien calcul)"), 5)
        self.assertIn("42 analyses enregistrées", embed.footer.text)

    def test_help(self):
        embed = embeds.help_embed()
        check_limits(self, embed)
        text = text_of(embed)
        for name in embeds.help_commands():
            self.assertIn(f"`/{name}`", text)
        partial = text_of(embeds.help_embed({"check", "help"}))
        self.assertIn("`/check`", partial)
        self.assertNotIn("`/report`", partial)
        self.assertNotIn("Compte", partial)

    def test_suivis(self):
        watches = [{"name": LONG_NAME, "tag": LONG_TAG, "last_rank": "Gold 3", "channel_id": "1" * 19, "user_id": "2" * 19}] * 5
        check_limits(self, embeds.suivis_embed(watches, 5, 30))

    def test_harassment(self):
        account = Account("p", LONG_NAME, LONG_TAG, "eu", 1, None)
        embed = embeds.harassment_alert_embed(SimpleNamespace(mention="<@1>"), account, "6 analyses de ce joueur en 24 h", "check")
        check_limits(self, embed)
        self.assertIn("déjà 6 analyses de ce joueur en 24 h", embed.description)
        self.assertTrue(embeds.harassment_refusal("check", True).endswith("les modérateurs ont été prévenus."))
        self.assertIn("nouveau dossier", embeds.harassment_refusal("report", False))


class WatchTextTests(unittest.TestCase):
    def watch(self, rank="Or 3", elo=1190):
        return {"name": "Joueur_1", "tag": "EUW", "last_rank": rank, "last_elo": elo}

    def test_changes(self):
        snap = {"tier": 15, "rank": "Platine 1", "elo": 1231, "games_needed": 0}
        self.assertEqual(embeds.watch_change_text(self.watch(), snap),
                         "📈 **Joueur\\_1#EUW** est monté de **Or 3** à **Platine 1**.")
        self.assertEqual(embeds.watch_change_text(self.watch("Gold 3"), snap),
                         "📈 **Joueur\\_1#EUW** est monté de **Or 3** à **Platine 1**.")
        self.assertEqual(embeds.watch_change_text(self.watch("Diamond 1", 1800), snap),
                         "📉 **Joueur\\_1#EUW** est descendu de **Diamant 1** à **Platine 1**.")
        self.assertEqual(embeds.watch_change_text(self.watch("Platinum 1", 1250), snap),
                         "🎮 **Joueur\\_1#EUW** a rejoué en classé : **−19 RR** (Platine 1).")
        self.assertIsNone(embeds.watch_change_text(self.watch("Platine 1", 1231), snap))
        self.assertIsNone(embeds.watch_change_text(self.watch(None, None), snap))
        self.assertIsNone(embeds.watch_change_text(self.watch(), dict(snap, games_needed=2)))
        self.assertIsNone(embeds.watch_change_text(self.watch(), dict(snap, tier=0)))
        self.assertIsNone(embeds.watch_change_text(self.watch(), None))
        # Rang précédent inconnu : seule la variation de RR est annoncée
        self.assertIn("+41 RR", embeds.watch_change_text(self.watch("???"), snap))


class ReviewFixesEmbedTests(unittest.TestCase):
    # Régressions des relectures R1 / R2 (rappels, données manquantes, mentions de /report…)
    def test_report_hint_discourages_pile_on(self):
        embed = embeds.analysis_embed(smurf_analysis())
        self.assertIn("Si tu as toi-même affronté ce joueur", embed.description)
        self.assertIn("un seul dossier par personne suffit", embed.description)

    def test_cache_line_under_one_minute(self):
        analysis = dataclasses.replace(fixture_analysis(), from_cache=True)
        embed = embeds.analysis_embed(analysis, now=analysis.analyzed_at + 20)
        self.assertIn("(analyse d'il y a moins d'une minute)", embed.description)

    def test_smurf_match_explains_the_index(self):
        me = LobbyPlayer("me", "red", None, "Jett", 18, 20, "Moi", "EUW", 30, 10, 3, 8000, 25, 25, 0, 5200, 20)
        others = [LobbyPlayer(f"o{i}", "red" if i < 4 else "blue", None, "Sova", 14, 100, f"J{i}", "T", 15, 15, 3,
                              4000, 20, 60, 5, 2600, 20) for i in range(9)]
        match = LobbyMatch("x", "Ascent", 0, 20, [me] + others, {"red": 13, "blue": 7})
        scored = [(p, *quick_match_index(p, match)) for p in match.players]
        embed = embeds.smurf_match_embed(Account("me", "Moi", "EUW", "eu", 20, None), match, scored)
        self.assertIn(embeds.REMINDER, embed.description)
        self.assertIn("trois critères", embed.description)
        line = next(l for f in embed.fields for l in f.value.split("\n") if "⬅️" in l)
        self.assertIn("**58**/60", line)
        self.assertTrue(line.endswith(" · niveau bas, ACS/ADR, HS %"), line)   # R2-03 : les critères sont dits
        check_limits(self, embed)

    def test_old_history_rows_without_stats(self):
        # R2-04 : l'ancien bot enregistrait K/D = 0 sans stats ; ce n'est pas un vrai K/D
        old = ("Nom", "TAG", 12, "Silver 1", 0.0, 1792700000, None, None)
        self.assertIn("K/D `inconnu`", embeds.history_line(old))
        self.assertIn("K/D `1,20`", embeds.history_line(old[:4] + (1.2,) + old[5:]))
        new = ("Nom", "TAG", 12, "Argent 1", 0.0, 1792700000, 20, 2)
        self.assertIn("K/D `0,00`", embeds.history_line(new))
        self.assertIn("K/D `inconnu`", embeds.history_line(new[:4] + (None,) + new[5:]))

    def test_reminders(self):
        # R2-08 : rappel complet partout où un indice est montré
        analysis = smurf_analysis()
        rows = [("Nom", "TAG", 70, "Or 2", 1.5, 1792700000, 20, 2)]
        self.assertTrue(embeds.history_embed(rows, 1).description.startswith(embeds.REMINDER))
        dossier = embeds.dossier_embed(1, analysis, SimpleNamespace(mention="<@1>"), "", int(time.time()))
        self.assertEqual(dossier.description, embeds.REMINDER)
        self.assertIsNone(field(dossier, "Autres dossiers sur ce compte"))
        refused = embeds.report_refused_embed(fixture_analysis())
        self.assertIn("qui revient d'une pause ou qui a progressé vite", refused.description)
        self.assertNotIn("incomplète", refused.description)

    def test_dossier_flags_previous_reports(self):
        dossier = embeds.dossier_embed(3, smurf_analysis(), SimpleNamespace(mention="<@1>"), "", int(time.time()), 2)
        self.assertIn("2 dossiers déjà ouverts sur ce serveur", field(dossier, "Autres dossiers sur ce compte"))
        check_limits(self, dossier)

    def test_partial_refusal_is_explained(self):
        refused = embeds.report_refused_embed(dataclasses.replace(fixture_analysis(), partial=True))
        self.assertIn(embeds.PARTIAL_REFUSAL, refused.description)

    def test_official_report_text(self):
        embed = embeds.official_report_embed(5)
        self.assertIn("avant toute action sur le serveur", embed.description)   # R2-10
        self.assertIsNone(field(embed, "ℹ️ Observations"))
        redacted = embeds.official_report_embed(5, redacted=True)
        self.assertIn("ont été retirées de tes observations", field(redacted, "ℹ️ Observations"))
        check_limits(self, redacted)

    def test_harassment_texts(self):
        account = Account("p", "Cible", "EUW", "eu", 1, None)
        alert = embeds.harassment_alert_embed(SimpleNamespace(mention="<@1>"), account, "6 analyses", "check", linked=True)
        self.assertIn("compte lié par ce membre avec `/link`", alert.description)
        self.assertIn("son historique", embeds.harassment_refusal("historique", False))

    def test_help_lists_botstats_and_alert_channel(self):
        text = text_of(embeds.help_embed())
        self.assertIn("`/botstats`", text)          # R2-14
        self.assertNotIn("`/broadcast`", text)      # réservé au propriétaire du bot
        self.assertIn("alertes anti-acharnement", text)   # R2-17


if __name__ == "__main__":
    unittest.main()
