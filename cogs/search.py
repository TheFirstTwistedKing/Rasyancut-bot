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
        hits = self.bot.lore.search_hits(keyword)     # 비공개 문서는 누가 검색해도 나오지 않는다.
        shown = keyword[:50]
        if not hits:
            await interaction.followup.send(f"'{shown}'에 대한 결과가 없어요.")
            return
        labels = {"title": "제목 일치", "alias": "별칭 일치", "initial": "초성 일치", "similar": "비슷한 이름"}
        blocks = []
        for hit in hits:
            label = f"{hit.count}회" if hit.kind == "text" else labels[hit.kind] + (f" · 본문 {hit.count}회" if hit.count else "")
            blocks.append(f"**{hit.page.label}** · {label}\n…{hit.snippet}…")
        description = "\n\n".join(blocks)
        if all(hit.kind == "similar" for hit in hits):
            description = "정확히 맞는 결과가 없어서 비슷한 이름을 보여줘요.\n\n" + description
        embed = discord.Embed(title=f"검색: {shown}", description=description, color=COLOR)
        embed.set_footer(text="자세히 보려면 /설정 이름")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Search(bot))
