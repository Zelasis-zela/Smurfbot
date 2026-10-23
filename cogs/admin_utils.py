import discord
from discord.ext import commands
from discord import app_commands
import logging

log = logging.getLogger("smurfbot")

OWNER_ID = "1318227569396682834"

class BroadcastConfirmView(discord.ui.View):
    def __init__(self, cog, embed, origin):
        super().__init__(timeout=120)
        self.cog = cog
        self.embed = embed
        self.origin = origin  # interaction de la commande, pour modifier l'aperçu à l'expiration

    @discord.ui.button(label="✅ Envoyer à tous les serveurs", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="📤 Diffusion en cours…", view=None)
        success_count, fail_count = await self.cog.send_broadcast(self.embed)
        await interaction.edit_original_response(content=f"✅ Diffusion terminée !\n- Envoyé avec succès : **{success_count}** serveurs\n- Échecs : **{fail_count}** serveurs")

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ Diffusion annulée, rien n'a été envoyé.", embed=None, view=None)

    async def on_timeout(self):
        try:
            await self.origin.edit_original_response(content="⌛ Confirmation expirée, rien n'a été envoyé.", embed=None, view=None)
        except discord.HTTPException:
            pass

class AdminUtilsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="botstats", description="Affiche les statistiques d'utilisation du bot")
    async def botstats(self, interaction: discord.Interaction):
        server_count = len(self.bot.guilds)
        embed = discord.Embed(
            title="📊 Statistiques de SmurfWatch",
            description=f"Le bot est actuellement utilisé sur **{server_count}** serveurs Discord !",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="broadcast", description="[Admin] Diffuser un message de mise à jour sur tous les serveurs")
    @app_commands.describe(message="Le message de mise à jour à envoyer")
    async def broadcast(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 4000]):
        if str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message("❌ Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True)

        embed = discord.Embed(
            title="📢 Mise à jour de SmurfWatch",
            description=message,
            color=discord.Color.gold()
        )
        embed.set_footer(text="Merci d'utiliser le bot !")

        # Rien n'est envoyé tant que le bouton de confirmation n'a pas été cliqué
        await interaction.response.send_message(
            content=f"👀 **Aperçu** — ce message sera envoyé sur **{len(self.bot.guilds)}** serveurs. Confirmer ?",
            embed=embed,
            view=BroadcastConfirmView(self, embed, interaction),
            ephemeral=True
        )

    async def send_broadcast(self, embed):
        success_count, fail_count = 0, 0

        for guild in self.bot.guilds:
            target_channel = None
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    if any(keyword in channel.name.lower() for keyword in ["general", "annonce", "update", "bot", "chat"]):
                        target_channel = channel
                        break

            if not target_channel:
                for channel in guild.text_channels:
                    if channel.permissions_for(guild.me).send_messages:
                        target_channel = channel
                        break

            if target_channel:
                try:
                    await target_channel.send(embed=embed)
                    success_count += 1
                except Exception:
                    fail_count += 1
            else:
                fail_count += 1

        log.info("📢 Broadcast terminé : %s serveurs atteints, %s échecs", success_count, fail_count)
        return success_count, fail_count

async def setup(bot):
    await bot.add_cog(AdminUtilsCog(bot))
