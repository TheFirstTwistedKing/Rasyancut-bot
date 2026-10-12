"""임베드 출력과 공통 유틸."""
import logging

import discord

# 라시안컷 팔레트: 금색에 가까운 연노랑
COLOR = 0xE9D98B
CHUNK_LIMIT = 3500  # 임베드 설명 최대 4096자보다 여유 있게


def split_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """긴 글을 줄 단위로 limit 이하 조각으로 나눈다."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur.strip():
        chunks.append(cur)
    return chunks


class PagedEmbed(discord.ui.View):
    """◀ ▶ 버튼으로 넘겨 보는 임베드 (모바일에서도 쓰기 편함)."""

    def __init__(self, title: str, chunks: list[str]):
        super().__init__(timeout=300)
        self.title = title
        self.chunks = chunks
        self.index = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def embed(self) -> discord.Embed:
        e = discord.Embed(title=self.title, description=self.chunks[self.index], color=COLOR)
        if len(self.chunks) > 1:
            e.set_footer(text=f"{self.index + 1} / {len(self.chunks)}")
        return e

    def _sync_buttons(self):
        self.prev_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.chunks) - 1

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_paged(interaction: discord.Interaction, title: str, text: str):
    chunks = split_text(text) or ["내용 없음"]
    view = PagedEmbed(title[:250], chunks)
    if len(chunks) == 1:
        await interaction.followup.send(embed=view.embed())
        return
    view.message = await interaction.followup.send(embed=view.embed(), view=view, wait=True)


async def send_tree(interaction: discord.Interaction, lore, title: str):
    """모든 문서의 이름을 들여쓰기로 보여준다(긴 목록은 ◀ ▶ 버튼으로 넘김)."""
    lines = lore.tree_lines()
    text = "\n".join(lines) + "\n\n읽으려면 /설정 이름" if lines else "문서가 없어요."
    await send_paged(interaction, title, text)


async def ensure_loaded(interaction: discord.Interaction, lore, force: bool = False) -> bool:
    """노션 문서를 (필요하면) 불러온다. 실패하면 안내 메시지를 보내고 False."""
    try:
        await lore.refresh(force=force)
        return True
    except Exception:
        logging.exception("노션 불러오기 실패")
        await interaction.followup.send(
            "노션에서 문서를 불러오지 못했어요. 통합(Integration)이 라시안컷 페이지에 "
            "연결돼 있는지, 토큰과 페이지 ID가 맞는지 확인해 주세요."
        )
        return False
