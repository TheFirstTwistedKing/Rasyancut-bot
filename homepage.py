import os

import discord
from discord import app_commands
from discord.ext import commands

from helpers import COLOR

DEFAULT_URL = "https://thefirsttwistedking.github.io/Rasyancut-homepage/"


def homepage_url() -> str:
    """HOMEPAGE_URL 환경변수로 주소를 바꿀 수 있다. http(s)가 아니면 기본 주소를 쓴다."""
    url = (os.getenv("HOMEPAGE_URL") or "").strip()
    return url if url.startswith(("https://", "http://")) else DEFAULT_URL


class Homepage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="홈페이지", description="라시안컷 홈페이지를 열 수 있는 링크를 보여줘요")
    async def homepage(self, interaction: discord.Interaction):
        url = homepage_url()
        embed = discord.Embed(title="라시안컷 홈페이지", description=url, url=url, color=COLOR)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="홈페이지 열기", url=url))
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Homepage(bot))
