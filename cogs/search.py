import discord
from discord import app_commands
from discord.ext import commands

from helpers import COLOR, ensure_loaded


class Search(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="검색", description="세계관 문서에서 키워드를 찾아요")
    @app_commands.rename(keyword="키워드")
    @app_commands.describe(keyword="찾을 단어")
    async def search(self, interaction: discord.Interaction, keyword: str):
        await interaction.response.defer()
        if not await ensure_loaded(interaction, self.bot.lore):
            return
        results = self.bot.lore.search(keyword)
        shown = keyword[:50]
        if not results:
            await interaction.followup.send(f"'{shown}'에 대한 결과가 없어요.")
            return
        blocks = []
        for page, snippet, count in results:
            label = f"{count}회" if count else "제목 일치"
            blocks.append(f"**{page.title}** · {label}\n…{snippet}…")
        embed = discord.Embed(title=f"검색: {shown}", description="\n\n".join(blocks), color=COLOR)
        embed.set_footer(text="자세히 보려면 /설정 이름")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Search(bot))
