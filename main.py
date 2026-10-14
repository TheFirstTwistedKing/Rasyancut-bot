import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import access
from notion_api import NotionLore

load_dotenv()
logging.basicConfig(level=logging.INFO)


class WorldBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        token = os.environ["NOTION_TOKEN"]
        self.lore = NotionLore(token, os.environ["NOTION_ROOT_PAGE_ID"])
        # 캐릭터 전용 상위 페이지가 정해졌을 때만 사용 (없으면 None)
        char_root = os.getenv("NOTION_CHARACTER_PAGE_ID")
        self.characters = NotionLore(token, char_root, include_root=False) if char_root else None
        self.tree.interaction_check = self.check_access

    async def check_access(self, interaction):
        """모든 명령어와 자동완성 요청에 먼저 적용되는 검사."""
        if access.allowed(interaction):
            return True
        if interaction.type is not discord.InteractionType.autocomplete:   # 자동완성에는 답장할 수 없다.
            try:
                await interaction.response.send_message("이 명령어를 사용할 권한이 없어요.", ephemeral=True)
            except discord.HTTPException:
                pass
        return False

    async def setup_hook(self):
        await self.load_extension("cogs.lore")
        await self.load_extension("cogs.search")
        await self.load_extension("cogs.character")
        await self.load_extension("cogs.homepage")

        # 쉼표로 서버 ID를 여러 개 적을 수 있어요 (예: 111,222). 비우면 전체 서버에 등록.
        raw = os.getenv("DISCORD_GUILD_ID", "")
        guild_ids = [int(x) for x in raw.replace(" ", "").split(",") if x]
        if guild_ids:
            for gid in guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                try:
                    await self.tree.sync(guild=guild)
                except discord.Forbidden:
                    logging.error("서버 %s 에 접근할 수 없어요. 서버 ID와 봇 초대(applications.commands)를 확인하세요.", gid)
        else:
            await self.tree.sync()

        for name, source in (("문서", self.lore), ("캐릭터", self.characters)):
            if source is None:
                continue
            try:
                await source.refresh()
                logging.info("노션 %s %d개 불러옴", name, len(source.titles()))
            except Exception:
                logging.exception("시작 시 노션 %s 불러오기 실패 - 통합 연결과 토큰을 확인하세요", name)

    async def on_ready(self):
        logging.info("로그인: %s", self.user)


if __name__ == "__main__":
    WorldBot().run(os.environ["DISCORD_TOKEN"])
