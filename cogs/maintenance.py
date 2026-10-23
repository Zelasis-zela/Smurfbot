import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import logging

from core import db

log = logging.getLogger("smurfbot")

BACKUP_TIME = datetime.time(hour=4, minute=0, tzinfo=datetime.timezone.utc)   # sauvegarde quotidienne
ACTIVITY_KEEP_DAYS = 30   # journal anti-acharnement : les fenêtres utilisées ne dépassent pas 7 jours

class MaintenanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.backup_loop.start()

    async def cog_unload(self):
        self.backup_loop.cancel()

    @tasks.loop(time=BACKUP_TIME)
    async def backup_loop(self):
        # Une exception non rattrapée arrêterait la boucle définitivement
        try:
            target = await asyncio.to_thread(db.backup_database)
            log.info("💾 Base sauvegardée dans %s", target)
        except Exception:
            log.exception("Échec de la sauvegarde de la base")
        # Qui a consulté qui : rien n'est gardé plus longtemps que nécessaire
        try:
            removed = await asyncio.to_thread(db.prune_activity, ACTIVITY_KEEP_DAYS)
            if removed:
                log.info("🧹 %s ligne(s) du journal anti-acharnement supprimée(s)", removed)
        except Exception:
            log.exception("Échec du nettoyage du journal anti-acharnement")

    @backup_loop.before_loop
    async def before_backup_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        # Le bot a été retiré : on oublie les suivis et les réglages de ce serveur
        try:
            removed = db.purge_guild(guild.id)
            log.info("🚪 Retiré du serveur %s : %s ligne(s) de suivi/réglages supprimée(s)", guild.id, removed)
        except Exception:
            log.exception("Échec du nettoyage du serveur %s", guild.id)

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command):
        name = getattr(command, "qualified_name", None) or getattr(command, "name", "?")
        log.info("/%s utilisée (serveur %s)", name, interaction.guild_id)

async def setup(bot):
    await bot.add_cog(MaintenanceCog(bot))
