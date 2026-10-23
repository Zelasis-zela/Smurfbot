import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import db, embeds
from core.discord_helpers import (
    RiotId, RIOT_ID_HELP, INVALID_RIOT_ID, parse_riot_id, watch_autocomplete, private_reply, reply_error,
)
from core.henrik import HenrikError, RateLimited

log = logging.getLogger("smurfbot")

MAX_WATCH_PER_GUILD = 5   # joueurs suivis au maximum par serveur
WATCH_INTERVAL_MIN = 30   # minutes entre deux vérifications
WATCH_API_DELAY = 4.0     # secondes entre deux joueurs : au plus la moitié du quota (30 requêtes/min) de la clé
WATCH_MIN_BUDGET = 12     # appels laissés aux commandes des membres (/check en coûte 9)
WATCH_BUDGET_WAIT = 5.0   # secondes d'attente quand il reste trop peu d'appels
WATCH_BUDGET_TRIES = 12   # au plus une minute d'attente ; sinon la suite attend la prochaine vérification
WATCH_COOLDOWN = 15.0     # secondes entre deux /watch par membre


def usable_snapshot(snapshot):
    # Rang exploitable : classé et hors placements (sinon on garde l'état précédent)
    return bool(snapshot) and bool(snapshot.get("tier")) and not snapshot.get("games_needed")


class WatchCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.watch_loop.start()

    async def cog_unload(self):
        self.watch_loop.cancel()

    # ---------- Tâche de fond ----------
    @tasks.loop(minutes=WATCH_INTERVAL_MIN)
    async def watch_loop(self):
        # Une exception non rattrapée arrêterait la boucle définitivement
        try:
            await self.check_watches()
        except Exception:
            log.exception("Erreur pendant la vérification des joueurs suivis")

    @watch_loop.before_loop
    async def before_watch_loop(self):
        await self.bot.wait_until_ready()

    async def wait_for_budget(self):
        # Le suivi passe après les commandes des membres : on attend que le budget d'appels remonte
        client = getattr(self.bot, "henrik", None)
        if client is None:
            return True
        for _ in range(WATCH_BUDGET_TRIES):
            if client.budget() >= WATCH_MIN_BUDGET:
                return True
            await asyncio.sleep(WATCH_BUDGET_WAIT)
        return client.budget() >= WATCH_MIN_BUDGET

    async def check_watches(self):
        snapshots = {}
        for watch in db.get_watches():
            # Un joueur suivi sur plusieurs serveurs ne coûte qu'un seul appel API
            key = (watch["puuid"], watch["region"])
            if key not in snapshots:
                if not await self.wait_for_budget():
                    log.info("Suivi de rang reporté : l'API est très sollicitée, reprise à la prochaine vérification")
                    return
                try:
                    snapshots[key] = await self.bot.analyzer.snapshot(watch["region"] or "eu", watch["puuid"])
                except RateLimited:
                    log.warning("Suivi de rang interrompu : limite de requêtes de l'API atteinte")
                    return
                except HenrikError as e:
                    log.warning("Suivi de rang : API indisponible pour %s#%s (%s)", watch["name"], watch["tag"], e)
                    snapshots[key] = None
                await asyncio.sleep(WATCH_API_DELAY)
            snapshot = snapshots[key]
            if not usable_snapshot(snapshot):
                continue
            text = embeds.watch_change_text(watch, snapshot)
            elo = snapshot["elo"] if snapshot.get("elo") is not None else watch["last_elo"]
            db.update_watch_state(watch["guild_id"], watch["puuid"], snapshot["rank"], elo)
            if text:
                await self.notify(watch, text)

    async def notify(self, watch, text):
        channel_id = int(watch["channel_id"])
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await channel.send(embed=embeds.watch_notice_embed(text))
        except (discord.NotFound, discord.Forbidden) as e:
            db.remove_watch(watch["guild_id"], watch["puuid"])
            log.info("Suivi de %s#%s retiré : salon %s inaccessible (%s)", watch["name"], watch["tag"], channel_id, e)
        except discord.HTTPException as e:
            log.warning("Suivi de rang : envoi impossible dans le salon %s (%s)", channel_id, e)

    # ---------- Commandes ----------
    @app_commands.command(name="watch", description="Suit le rang d'un joueur : ses changements de rang sont annoncés dans ce salon")
    @app_commands.describe(riot_id=RIOT_ID_HELP)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, WATCH_COOLDOWN, key=lambda i: i.user.id)
    async def watch(self, interaction: discord.Interaction, riot_id: RiotId):
        target = parse_riot_id(riot_id)
        if target is None:
            return await interaction.response.send_message(INVALID_RIOT_ID, ephemeral=True)
        watches = db.get_watches(interaction.guild_id)
        if len(watches) >= MAX_WATCH_PER_GUILD:
            return await interaction.response.send_message(
                f"❌ Ce serveur suit déjà **{len(watches)}** joueurs (maximum {MAX_WATCH_PER_GUILD}). "
                f"Retires-en un avec `/unwatch` avant d'en ajouter un autre.", ephemeral=True)
        if not interaction.permissions.send_messages:
            return await interaction.response.send_message(
                "❌ Tu ne peux pas écrire dans ce salon : lance `/watch` dans un salon où tu peux envoyer des messages.",
                ephemeral=True)
        bot_perms = interaction.app_permissions
        if not (bot_perms.send_messages and bot_perms.embed_links):
            return await interaction.response.send_message(
                "❌ Je n'ai pas la permission d'écrire dans ce salon : lance `/watch` dans un salon où je peux "
                "envoyer des messages.", ephemeral=True)

        await interaction.response.defer()
        try:
            account = await self.bot.analyzer.find_account(*target)
            who = embeds.riot(account.name, account.tag)
            if any(w["puuid"] == account.puuid for w in watches):
                return await private_reply(interaction, f"ℹ️ **{who}** est déjà suivi sur ce serveur.", public_defer=True)
            snapshot = await self.bot.analyzer.snapshot(account.region, account.puuid)
            # Rang pas encore exploitable : le point de départ sera pris à la prochaine vérification
            start = {"rank": snapshot["rank"], "elo": snapshot["elo"]} if usable_snapshot(snapshot) else {"rank": None, "elo": None}
            player = {"puuid": account.puuid, "name": account.name, "tag": account.tag, "region": account.region}
            db.add_watch(interaction.guild_id, interaction.channel_id, interaction.user.id, player, start)
            if start["rank"]:
                current = start["rank"]
            elif snapshot and snapshot.get("games_needed"):
                current = "en placements"
            else:
                current = "inconnu"
            await interaction.followup.send(
                f"👁️ Le rang de **{who}** est maintenant suivi (rang actuel : {current}).\n"
                f"Ses changements de rang et ses nouvelles parties classées seront annoncés dans ce salon, "
                f"avec une vérification toutes les {WATCH_INTERVAL_MIN} minutes.")
        except Exception as e:
            await reply_error(interaction, e, public_defer=True)

    @app_commands.command(name="unwatch", description="Arrête le suivi du rang d'un joueur sur ce serveur")
    @app_commands.describe(riot_id="Riot ID du joueur suivi, format Nom#TAG")
    @app_commands.autocomplete(riot_id=watch_autocomplete)
    @app_commands.guild_only()
    async def unwatch(self, interaction: discord.Interaction, riot_id: RiotId):
        target = parse_riot_id(riot_id)
        if target is None:
            return await interaction.response.send_message(INVALID_RIOT_ID, ephemeral=True)
        wanted = (target[0].casefold(), target[1].casefold())
        watch = next((w for w in db.get_watches(interaction.guild_id)
                      if ((w["name"] or "").casefold(), (w["tag"] or "").casefold()) == wanted), None)
        if not watch:
            return await interaction.response.send_message(
                f"❌ **{embeds.riot(*target)}** n'est pas suivi sur ce serveur. Utilise `/suivis` pour voir la liste.",
                ephemeral=True)
        perms = getattr(interaction.user, "guild_permissions", None)
        if str(interaction.user.id) != watch["user_id"] and not (perms and (perms.manage_guild or perms.administrator)):
            return await interaction.response.send_message(
                "❌ Seul le membre qui a lancé ce suivi, ou un membre pouvant gérer le serveur, peut l'arrêter.",
                ephemeral=True)
        db.remove_watch(interaction.guild_id, watch["puuid"])
        await interaction.response.send_message(f"✅ Le rang de **{embeds.riot(watch['name'], watch['tag'])}** n'est plus suivi.")

    @app_commands.command(name="suivis", description="Joueurs dont le rang est suivi sur ce serveur")
    @app_commands.guild_only()
    async def suivis(self, interaction: discord.Interaction):
        watches = db.get_watches(interaction.guild_id)
        if not watches:
            return await interaction.response.send_message(
                "👁️ Aucun joueur suivi sur ce serveur. Utilise `/watch` pour en ajouter un.", ephemeral=True)
        await interaction.response.send_message(embed=embeds.suivis_embed(watches, MAX_WATCH_PER_GUILD, WATCH_INTERVAL_MIN))


async def setup(bot):
    await bot.add_cog(WatchCog(bot))
