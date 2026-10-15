import discord
from discord import app_commands
from discord.ext import commands

import access
from views import open_document


class Character(commands.Cog):
    """NOTION_CHARACTER_PAGE_ID 아래의 하위 페이지를 캐릭터 시트로 읽는다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="캐릭터", description="캐릭터 시트를 보여줘요")
    @app_commands.rename(name="이름")
    @app_commands.describe(name="캐릭터 이름 (일부만 입력해도 돼요)")
    async def show(self, interaction: discord.Interaction, name: str):
        source = self.bot.characters
        if source is None:
            await interaction.response.send_message("캐릭터 페이지가 아직 설정되지 않았어요. (`NOTION_CHARACTER_PAGE_ID`)")
            return

        def not_found(query, similar):
            names = ", ".join(source.top_titles()[:15]) or "없음"
            hint = f"\n혹시 이걸 찾으셨나요? {', '.join(similar)}" if similar else ""
            return f"'{query[:50]}' 캐릭터를 찾지 못했어요.{hint}\n등록된 캐릭터: {names}"
        await open_document(interaction, source, name, list_title="캐릭터", not_found=not_found)

    @show.autocomplete("name")
    async def show_autocomplete(self, interaction: discord.Interaction, current: str):
        if self.bot.characters is None:
            return []
        return [app_commands.Choice(name=name, value=value)
                for name, value in self.bot.characters.suggest(current, include_private=access.is_admin(interaction))]


async def setup(bot: commands.Bot):
    await bot.add_cog(Character(bot))
