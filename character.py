import discord
from discord import app_commands
from discord.ext import commands

from helpers import ensure_loaded, send_paged


class Character(commands.Cog):
    """NOTION_CHARACTER_PAGE_ID 아래의 하위 페이지를 캐릭터 시트로 읽는다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="캐릭터", description="캐릭터 시트를 보여줘요")
    @app_commands.rename(name="이름")
    @app_commands.describe(name="캐릭터 이름 (일부만 입력해도 돼요)")
    async def show(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        source = self.bot.characters
        if source is None:
            await interaction.followup.send("캐릭터 페이지가 아직 설정되지 않았어요. (`NOTION_CHARACTER_PAGE_ID`)")
            return
        if not await ensure_loaded(interaction, source):
            return
        page = source.get(name)
        if page is None:
            names = ", ".join(source.top_titles()[:15]) or "없음"
            await interaction.followup.send(f"'{name[:50]}' 캐릭터를 찾지 못했어요.\n등록된 캐릭터: {names}")
            return
        await send_paged(interaction, page.path, source.render(page))

    @show.autocomplete("name")
    async def show_autocomplete(self, interaction: discord.Interaction, current: str):
        if self.bot.characters is None:
            return []
        return [app_commands.Choice(name=name, value=value)
                for name, value in self.bot.characters.suggest(current)]


async def setup(bot: commands.Bot):
    await bot.add_cog(Character(bot))
