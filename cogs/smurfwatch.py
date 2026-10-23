import logging
from types import SimpleNamespace
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import db, embeds
from core.discord_helpers import (
    RiotId, RIOT_ID_OR_LINKED_HELP, INVALID_RIOT_ID, parse_riot_id, resolve_riot_id,
    tracker_url, riot_id_autocomplete, private_reply, reply_error, store_analysis, check_guard, record_check,
)
from core.scoring import quick_match_index

log = logging.getLogger("smurfbot")

CHECK_COOLDOWN = 15.0     # secondes entre deux /check, /compare, /historique ou /link par membre
MATCH_COOLDOWN = 30.0     # secondes entre deux /smurf-match par membre
NO_COMPARISON = ("❌ Impossible de calculer la moyenne de son rang : le détail des parties n'est pas disponible. "
                 "Réessaie dans une minute.")
NO_COMPARISON_DETAILS = ("❌ Impossible de calculer la moyenne de son rang : le détail de ses dernières parties "
                         "n'est pas disponible. Réessaie plus tard.")
NO_MATCHES_TO_COMPARE = "❌ Aucune partie classée récente : rien à comparer pour ce joueur."
NO_RANKED_MATCH = "❌ Aucune partie classée récente pour ce joueur."
EXPIRED = "Analyse expirée : relance /check."


def no_comparison_text(analysis):
    # Pourquoi /compare n'a rien à montrer, et si réessayer peut aider
    if analysis.agg.n == 0:
        return NO_MATCHES_TO_COMPARE
    if analysis.partial:
        return NO_COMPARISON   # détail limité par le budget d'API : l'analyse n'est gardée qu'une minute
    return NO_COMPARISON_DETAILS


def not_analysed_text(name, tag):
    return f"❌ Aucune analyse de **{embeds.riot(name, tag)}** n'est enregistrée pour ce serveur. Lance d'abord `/check`."


class MatchesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sw:matches:(?P<puuid>[A-Za-z0-9_-]{1,89})"):
    # Bouton persistant : la liste vient du cache de l'analyseur (aucun appel API)
    def __init__(self, puuid):
        super().__init__(discord.ui.Button(label="📜 Dernières parties", style=discord.ButtonStyle.secondary,
                                           custom_id=f"sw:matches:{puuid}"))
        self.puuid = puuid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["puuid"])

    async def callback(self, interaction):
        analyzer = getattr(interaction.client, "analyzer", None)
        analysis = analyzer.cached_by_puuid(self.puuid) if analyzer is not None else None
        if analysis is None:
            return await interaction.response.send_message(EXPIRED, ephemeral=True)
        await interaction.response.send_message(embed=embeds.matches_embed(analysis), ephemeral=True)


def analysis_view(account):
    # Uniquement des éléments persistants : le bouton marche encore après un redémarrage du bot
    view = discord.ui.View(timeout=None)
    if MatchesButton.__discord_ui_compiled_template__.fullmatch(f"sw:matches:{account.puuid}"):   # ≤ 100 caractères
        view.add_item(MatchesButton(account.puuid))
    view.add_item(discord.ui.Button(label="Tracker.gg", style=discord.ButtonStyle.link,
                                    url=tracker_url(account.name, account.tag)))
    return view


class SmurfWatchCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(MatchesButton)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(MatchesButton)

    @property
    def analyzer(self):
        return self.bot.analyzer

    async def run_analysis(self, interaction, target, command, public):
        # Compte -> protection anti-acharnement -> analyse -> enregistrement ; None si la demande est refusée.
        # La consultation n'est comptée (record_check) qu'une fois le résultat réellement affiché.
        account = await self.analyzer.find_account(*target)
        refusal = await check_guard(interaction, account, command)
        if refusal:
            await private_reply(interaction, refusal, public_defer=public)
            return None
        analysis = await self.analyzer.analyze(*target)
        store_analysis(analysis, interaction.guild_id)
        return analysis

    # ---------- Analyse ----------
    @app_commands.command(name="check", description="Analyse un joueur Valorant et calcule son indice de suspicion de smurf")
    @app_commands.describe(riot_id=RIOT_ID_OR_LINKED_HELP, prive="Réponse visible de toi seul")
    @app_commands.autocomplete(riot_id=riot_id_autocomplete)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, CHECK_COOLDOWN, key=lambda i: i.user.id)
    async def check(self, interaction: discord.Interaction, riot_id: Optional[RiotId] = None, prive: bool = False):
        target, error = resolve_riot_id(interaction.user.id, riot_id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.defer(ephemeral=prive)
        try:
            analysis = await self.run_analysis(interaction, target, "check", public=not prive)
            if analysis is None:
                return
            await interaction.followup.send(embed=embeds.analysis_embed(analysis), view=analysis_view(analysis.account),
                                            ephemeral=prive)
            record_check(interaction, analysis.account)
        except Exception as e:
            await reply_error(interaction, e, public_defer=not prive)

    @app_commands.command(name="compare", description="Compare les stats d'un joueur à la moyenne des joueurs de son rang")
    @app_commands.describe(riot_id=RIOT_ID_OR_LINKED_HELP)
    @app_commands.autocomplete(riot_id=riot_id_autocomplete)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, CHECK_COOLDOWN, key=lambda i: i.user.id)
    async def compare(self, interaction: discord.Interaction, riot_id: Optional[RiotId] = None):
        target, error = resolve_riot_id(interaction.user.id, riot_id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.defer()
        try:
            analysis = await self.run_analysis(interaction, target, "compare", public=True)
            if analysis is None:
                return
            if analysis.comparison is None:
                # Rien d'affiché : la demande ne compte pas comme une consultation
                return await private_reply(interaction, no_comparison_text(analysis), public_defer=True)
            await interaction.followup.send(embed=embeds.compare_embed(analysis))
            record_check(interaction, analysis.account)
        except Exception as e:
            await reply_error(interaction, e, public_defer=True)

    @app_commands.command(name="smurf-match", description="Estimation rapide des 10 joueurs de la dernière partie classée d'un joueur")
    @app_commands.describe(riot_id=RIOT_ID_OR_LINKED_HELP)
    @app_commands.autocomplete(riot_id=riot_id_autocomplete)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, MATCH_COOLDOWN, key=lambda i: i.user.id)
    async def smurf_match(self, interaction: discord.Interaction, riot_id: Optional[RiotId] = None):
        target, error = resolve_riot_id(interaction.user.id, riot_id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.defer()
        try:
            # Le joueur demandé passe par la même protection anti-acharnement que /check (compte en cache : gratuit)
            account = await self.analyzer.find_account(*target)
            refusal = await check_guard(interaction, account, "smurf-match")
            if refusal:
                return await private_reply(interaction, refusal, public_defer=True)
            result = await self.analyzer.last_match(*target)
            if result is None:
                return await private_reply(interaction, NO_RANKED_MATCH, public_defer=True)
            account, match = result
            scored = [(player, *quick_match_index(player, match)) for player in match.players]
            await interaction.followup.send(embed=embeds.smurf_match_embed(account, match, scored))
            record_check(interaction, account)
        except Exception as e:
            await reply_error(interaction, e, public_defer=True)

    @app_commands.command(name="historique", description="Évolution de l'indice d'un joueur déjà analysé sur ce serveur")
    @app_commands.describe(riot_id=RIOT_ID_OR_LINKED_HELP)
    @app_commands.autocomplete(riot_id=riot_id_autocomplete)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, CHECK_COOLDOWN, key=lambda i: i.user.id)
    async def historique(self, interaction: discord.Interaction, riot_id: Optional[RiotId] = None):
        target, error = resolve_riot_id(interaction.user.id, riot_id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        player = db.find_player(target[0], target[1], guild_id=interaction.guild_id)
        if player is None:
            return await interaction.response.send_message(not_analysed_text(*target), ephemeral=True)
        await interaction.response.defer()
        try:
            # Afficher l'historique d'un joueur est une consultation comme une autre (aucun appel API ici)
            account = SimpleNamespace(puuid=player[0], name=player[1], tag=player[2])
            refusal = await check_guard(interaction, account, "historique")
            if refusal:
                return await private_reply(interaction, refusal, public_defer=True)
            rows, total = db.get_player_history(target[0], target[1], guild_id=interaction.guild_id)
            if not rows:
                return await private_reply(interaction, not_analysed_text(*target), public_defer=True)
            await interaction.followup.send(embed=embeds.history_embed(rows, total))
            record_check(interaction, account)
        except Exception as e:
            await reply_error(interaction, e, public_defer=True)

    # ---------- Aide ----------
    @app_commands.command(name="help", description="Liste des commandes de SmurfWatch")
    async def help_command(self, interaction: discord.Interaction):
        available = {command.name for command in self.bot.tree.get_commands()}
        await interaction.response.send_message(embed=embeds.help_embed(available), ephemeral=True)

    # ---------- Compte lié ----------
    @app_commands.command(name="link", description="Lie ton compte Riot pour ne plus avoir à saisir ton Riot ID")
    @app_commands.describe(riot_id="Ton Riot ID, format Nom#TAG")
    @app_commands.checks.cooldown(1, CHECK_COOLDOWN, key=lambda i: i.user.id)
    async def link(self, interaction: discord.Interaction, riot_id: RiotId):
        target = parse_riot_id(riot_id)
        if target is None:
            return await interaction.response.send_message(INVALID_RIOT_ID, ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            account = await self.analyzer.find_account(*target)
            db.set_linked_account(interaction.user.id, account.name, account.tag)
            await interaction.followup.send(
                f"🔗 Compte **{embeds.riot(account.name, account.tag)}** lié ! Tu peux maintenant utiliser `/check`, "
                f"`/compare`, `/smurf-match` et `/historique` sans rien saisir. Utilise `/unlink` pour le délier.",
                ephemeral=True)
        except Exception as e:
            await reply_error(interaction, e)

    @app_commands.command(name="unlink", description="Supprime le lien entre ton compte Discord et ton compte Riot")
    async def unlink(self, interaction: discord.Interaction):
        linked = db.delete_linked_account(interaction.user.id)
        if linked:
            msg = f"✅ Le compte **{embeds.riot(*linked)}** n'est plus lié à ton compte Discord."
        else:
            msg = "ℹ️ Aucun compte Riot n'est lié à ton compte Discord."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SmurfWatchCog(bot))
