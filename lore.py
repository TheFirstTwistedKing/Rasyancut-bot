import logging

import discord
from discord import app_commands
from discord.ext import commands

from helpers import ensure_loaded, send_paged


class AddLoreModal(discord.ui.Modal, title="설정 추가"):
    body = discord.ui.TextInput(
        label="내용",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        placeholder="여러 줄로 써도 돼요. 줄바꿈마다 문단이 나뉘어요.",
    )

    def __init__(self, bot: commands.Bot, doc_title: str):
        super().__init__()
        self.bot = bot
        self.doc_title = doc_title

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self.bot.lore.create_page(self.doc_title, str(self.body))
        except Exception:
            logging.exception("노션 문서 추가 실패")
            await interaction.followup.send(
                "노션에 추가하지 못했어요. 통합에 '콘텐츠 삽입' 권한이 켜져 있는지 확인해 주세요."
            )
            return
        await interaction.followup.send(f"'{self.doc_title}' 문서를 추가했어요. `/설정 {self.doc_title}`로 볼 수 있어요.")


class Lore(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="목록", description="세계관 문서 목록을 보여줘요")
    async def list_docs(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await ensure_loaded(interaction, self.bot.lore):
            return
        lines = self.bot.lore.tree_lines()
        text = "\n".join(lines) + "\n\n읽으려면 /설정 이름" if lines else "문서가 없어요."
        await send_paged(interaction, "세계관 문서", text)

    @app_commands.command(name="설정", description="세계관 문서를 읽어요")
    @app_commands.rename(name="이름")
    @app_commands.describe(name="문서 이름 (일부만 입력해도 돼요)")
    async def show(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        if not await ensure_loaded(interaction, self.bot.lore):
            return
        page = self.bot.lore.get(name)
        if page is None:
            await interaction.followup.send(f"'{name[:50]}' 문서를 찾지 못했어요. `/목록`으로 확인해 보세요.")
            return
        await send_paged(interaction, page.path, self.bot.lore.render(page))

    @show.autocomplete("name")
    async def show_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=name, value=value)
                for name, value in self.bot.lore.suggest(current)]

    @app_commands.command(name="설정추가", description="새 세계관 문서를 노션에 추가해요 (서버 관리자)")
    @app_commands.rename(name="이름")
    @app_commands.describe(name="새 문서 이름")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add_doc(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 100]):
        if any(t.lower() == name.strip().lower() for t in self.bot.lore.titles()):
            await interaction.response.send_message(f"'{name}' 문서가 이미 있어요. 다른 이름을 써 주세요.", ephemeral=True)
            return
        await interaction.response.send_modal(AddLoreModal(self.bot, name.strip()))

    @app_commands.command(name="새로고침", description="노션의 최신 내용을 다시 불러와요 (서버 관리자)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reload_docs(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await ensure_loaded(interaction, self.bot.lore, force=True):
            return
        await interaction.followup.send(f"문서 {len(self.bot.lore.titles())}개를 새로 불러왔어요.")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("서버 관리 권한이 있는 사람만 쓸 수 있어요.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Lore(bot))
