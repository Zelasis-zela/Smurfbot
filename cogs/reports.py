import time
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import db, embeds
from core.discord_helpers import (
    RiotId, RIOT_ID_HELP, INVALID_RIOT_ID, MOD_ONLY, REPORTS_CHANNEL_NAME, SUPPORT_URL,
    parse_riot_id, is_own_account, riot_id_autocomplete, is_moderator, can_manage_guild,
    find_reports_channel, reports_channel_problem, everyone_can_view, bot_missing_permissions,
    missing_permissions_text, reply_error, store_analysis, check_guard, redact_personal,
)
from core.scoring import REPORT_THRESHOLD

log = logging.getLogger("smurfbot")

REPORT_COOLDOWN = 30.0    # secondes entre deux /report par membre
REPORT_DAYS = 7           # un même compte, une fois tous les 7 jours par membre
STATUS_CHOICES = [
    app_commands.Choice(name="En attente", value="pending"),
    app_commands.Choice(name="Validés", value="validated"),
    app_commands.Choice(name="Rejetés", value="rejected"),
    app_commands.Choice(name="Tous", value="all"),
]
ACCESS_REMINDER = ("Pense à donner l'accès à tes rôles de modération (Voir le salon, Envoyer des messages) : "
                   "seuls eux doivent voir les dossiers.")
SEND_FAILED = ("❌ Je n'ai pas pu publier le dossier dans le salon des modérateurs. "
               "Réessaie plus tard ou préviens un administrateur.")
OWN_ACCOUNT = "❌ Tu ne peux pas signaler ton propre compte lié (`/unlink` pour le délier s'il ne t'appartient pas)."


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sw:review:(?P<id>[0-9]+):(?P<action>ok|no)"):
    # Validation d'un dossier par un modérateur ; persistant (fonctionne après un redémarrage)
    def __init__(self, report_id, action, *, disabled=False):
        ok = action == "ok"
        super().__init__(discord.ui.Button(
            label="✅ Valider" if ok else "❌ Rejeter",
            style=discord.ButtonStyle.success if ok else discord.ButtonStyle.danger,
            custom_id=f"sw:review:{report_id}:{action}",
            disabled=disabled,
        ))
        self.report_id = int(report_id)
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction):
        if not is_moderator(interaction):
            return await interaction.response.send_message(
                "❌ Seuls les modérateurs peuvent valider ou rejeter un dossier.", ephemeral=True)
        report = db.get_report(self.report_id)
        if report is None or report["guild_id"] != str(interaction.guild_id):
            return await interaction.response.send_message("❌ Dossier introuvable.", ephemeral=True)
        status = "validated" if self.action == "ok" else "rejected"
        if not db.review_report(self.report_id, status, interaction.user.id):
            return await interaction.response.send_message("Ce dossier a déjà été traité.", ephemeral=True)
        reviewed_at = (db.get_report(self.report_id) or {}).get("reviewed_at") or int(time.time())
        text = embeds.review_status_text(status, interaction.user.mention, reviewed_at)
        log.info("Dossier n°%s %s par %s", self.report_id, status, interaction.user.id)
        message = interaction.message
        if message is not None and message.embeds:
            await interaction.response.edit_message(embed=embeds.with_status(message.embeds[0], text),
                                                    view=review_view(self.report_id, disabled=True))
        else:
            await interaction.response.send_message(f"Dossier n°{self.report_id} : {text}", ephemeral=True)


def review_view(report_id, disabled=False):
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewButton(report_id, "ok", disabled=disabled))
    view.add_item(ReviewButton(report_id, "no", disabled=disabled))
    return view


def support_view():
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Support Riot", style=discord.ButtonStyle.link, url=SUPPORT_URL))
    return view


class ReportsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(ReviewButton)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(ReviewButton)

    # ---------- /report ----------
    @app_commands.command(name="report", description=f"Prépare un dossier de signalement pour les modérateurs (indice ≥ {REPORT_THRESHOLD})")
    @app_commands.describe(riot_id=RIOT_ID_HELP,
                           observations="Ce que tu as constaté en jeu (facultatif, aucune info personnelle)")
    @app_commands.autocomplete(riot_id=riot_id_autocomplete)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, REPORT_COOLDOWN, key=lambda i: i.user.id)
    async def report(self, interaction: discord.Interaction, riot_id: RiotId,
                     observations: Optional[app_commands.Range[str, 0, 1000]] = None):
        channel = find_reports_channel(interaction.guild)
        problem = reports_channel_problem(channel)
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)
        target = parse_riot_id(riot_id)
        if target is None:
            return await interaction.response.send_message(INVALID_RIOT_ID, ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await self.create_dossier(interaction, channel, target, (observations or "").strip())
        except Exception as e:
            await reply_error(interaction, e)

    async def create_dossier(self, interaction, channel, target, observations):
        user, guild_id, followup = interaction.user, interaction.guild_id, interaction.followup
        account = await self.bot.analyzer.find_account(*target)

        def log_attempt(kind):
            db.log_activity(guild_id, user.id, account.puuid, kind)

        # /link n'est pas vérifié : aucune exemption anti-acharnement, et on ne signale pas son propre compte
        if is_own_account(user.id, account):
            return await followup.send(OWN_ACCOUNT, ephemeral=True)
        refusal = await check_guard(interaction, account, "report")
        if refusal:
            return await followup.send(refusal, ephemeral=True)
        previous = db.last_report_by(user.id, account.puuid, REPORT_DAYS)
        if previous:
            log_attempt("report_refused")
            return await followup.send(embeds.duplicate_report_text(previous["created_at"], REPORT_DAYS), ephemeral=True)

        analysis = await self.bot.analyzer.analyze(*target)
        store_analysis(analysis, guild_id)
        suspicion = analysis.suspicion
        if suspicion.index < REPORT_THRESHOLD:
            if not analysis.partial:   # analyse incomplète (API saturée) : le membre n'y est pour rien
                log_attempt("report_refused")
            return await followup.send(embed=embeds.report_refused_embed(analysis), ephemeral=True)

        # Aucune information personnelle hors jeu dans un dossier : liens, contacts, pseudos de réseaux retirés
        observations, redacted = redact_personal(observations)
        match_ids = [row["match_id"] for row in embeds.notable_matches(analysis)]
        previous_reports = db.count_reports(guild_id, account.puuid)
        report_id = db.create_report(guild_id, user.id, account.puuid, account.name, account.tag, suspicion.index,
                                     suspicion.level, suspicion.signals, match_ids, observations or None)
        created_at = (db.get_report(report_id) or {}).get("created_at") or int(time.time())
        try:
            dossier = embeds.dossier_embed(report_id, analysis, user, observations, created_at, previous_reports)
            message = await channel.send(embed=dossier, view=review_view(report_id))
        except Exception as e:
            # Dossier jamais publié : retiré, pour ne pas bloquer le membre pendant 7 jours
            db.delete_unpublished_report(report_id)
            if not isinstance(e, discord.HTTPException):
                raise
            log.warning("Dossier n°%s non publié dans le salon %s : %s", report_id, channel.id, e)
            return await followup.send(SEND_FAILED, ephemeral=True)
        db.set_report_message(report_id, channel.id, message.id)
        log_attempt("report")
        log.info("Dossier n°%s créé sur le serveur %s (indice %s)", report_id, guild_id, suspicion.index)
        text = embeds.dossier_text(report_id, analysis, observations, created_at)
        await followup.send(content=embeds.dossier_message(text), embed=embeds.official_report_embed(report_id, redacted),
                            view=support_view(), ephemeral=True)

    # ---------- /watchlist ----------
    @app_commands.command(name="watchlist", description="[Modérateurs] Comptes signalés sur ce serveur")
    @app_commands.describe(statut="Filtrer les dossiers par statut (par défaut : tous)")
    @app_commands.choices(statut=STATUS_CHOICES)
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def watchlist(self, interaction: discord.Interaction, statut: Optional[app_commands.Choice[str]] = None):
        if not is_moderator(interaction):
            return await interaction.response.send_message(MOD_ONLY, ephemeral=True)
        status = None if statut is None or statut.value == "all" else statut.value
        rows = db.list_reported(interaction.guild_id, status)
        if not rows:
            msg = "Aucun compte signalé sur ce serveur." if status is None else \
                f"Aucun compte signalé avec le statut « {statut.name} » sur ce serveur."
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.send_message(embed=embeds.watchlist_embed(rows, statut.name if status else None),
                                                ephemeral=True)

    # ---------- /report-setup ----------
    @app_commands.command(name="report-setup", description="[Admin] Configure le salon privé des dossiers de signalement")
    @app_commands.describe(salon="Salon existant à utiliser (vide : créer #reports-smurf)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def report_setup(self, interaction: discord.Interaction, salon: Optional[discord.TextChannel] = None):
        if not can_manage_guild(interaction):
            return await interaction.response.send_message(
                "❌ Cette commande est réservée aux administrateurs (permission « Gérer le serveur »).", ephemeral=True)
        guild = interaction.guild
        if salon is None:
            salon = discord.utils.find(lambda c: c.name.casefold() == REPORTS_CHANNEL_NAME, guild.text_channels)
        if salon is not None:
            return await interaction.response.send_message(self.use_channel(guild, salon), ephemeral=True)

        if not guild.me.guild_permissions.manage_channels:
            return await interaction.response.send_message(
                "❌ Il me manque la permission **Gérer les salons** pour créer #reports-smurf. Crée toi-même un salon "
                "privé réservé aux modérateurs, puis relance `/report-setup` en le choisissant.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  read_message_history=True),
        }
        try:
            channel = await guild.create_text_channel(REPORTS_CHANNEL_NAME, overwrites=overwrites,
                                                      reason="SmurfWatch : salon privé des dossiers /report")
        except discord.HTTPException as e:
            log.warning("Création de #%s impossible sur le serveur %s : %s", REPORTS_CHANNEL_NAME, guild.id, e)
            return await interaction.followup.send(
                "❌ Impossible de créer le salon. Vérifie mes permissions, ou crée toi-même un salon privé puis "
                "relance `/report-setup` en le choisissant.", ephemeral=True)
        db.set_reports_channel_id(guild.id, channel.id)
        log.info("Salon des dossiers créé sur le serveur %s : %s", guild.id, channel.id)
        await interaction.followup.send(
            f"✅ Salon {channel.mention} créé (invisible pour @everyone) et enregistré pour les dossiers `/report`.\n"
            f"{ACCESS_REMINDER}", ephemeral=True)

    def use_channel(self, guild, channel):
        # Enregistre un salon existant ; texte de réponse pour l'administrateur
        missing = bot_missing_permissions(channel)
        if missing:
            return missing_permissions_text(channel, missing)
        db.set_reports_channel_id(guild.id, channel.id)
        log.info("Salon des dossiers enregistré sur le serveur %s : %s", guild.id, channel.id)
        text = f"✅ Les dossiers `/report` seront publiés dans {channel.mention}."
        if everyone_can_view(channel):
            text += ("\n⚠️ Ce salon est visible de tous : les dossiers doivent rester réservés aux modérateurs. "
                     "Rends-le privé, sinon `/report` refusera d'y publier.")
        return f"{text}\n{ACCESS_REMINDER}"


async def setup(bot):
    await bot.add_cog(ReportsCog(bot))
