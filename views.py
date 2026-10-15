"""문서 화면: 서식 있는 본문, 이미지, 하위·상위 문서로 이동하는 버튼과 선택창."""
import logging
import re
from dataclasses import dataclass, field

import discord

import access
from helpers import COLOR, ensure_loaded, send_tree, split_text
from notion_api import EMPTY, IMG_MARK, MORE_VALUE, short_name

IMAGES_PER_MESSAGE = 9      # 메시지 하나에 임베드는 10개까지: 본문 1 + 이미지 9
CAPTION_LIMIT = 100
SELECT_LIMIT = 25           # 선택창 항목 수 한도


@dataclass
class DocPage:
    text: str
    images: list = field(default_factory=list)


def build_pages(lore, page, include_private: bool) -> list[DocPage]:
    """문서 하나를 디스코드 임베드 한도에 맞게 나눈다. 이미지는 그 이미지가 있던 부분의 화면에 붙는다."""
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lore.render_lines(page, include_private))).strip()
    pages: list[DocPage] = []
    for chunk in split_text(text):
        kept, images = [], []
        for line in chunk.split("\n"):
            if line.startswith(IMG_MARK):
                images.append(page.images[int(line[len(IMG_MARK):])])
            else:
                kept.append(line)
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
        head, rest = images[:IMAGES_PER_MESSAGE], images[IMAGES_PER_MESSAGE:]
        pages.append(DocPage(body or f"🖼️ 이미지 {len(images)}장", head))
        while rest:      # 한 화면에 이미지가 너무 많으면 이어서 나눈다.
            pages.append(DocPage("🖼️ 이미지 (이어서)", rest[:IMAGES_PER_MESSAGE]))
            rest = rest[IMAGES_PER_MESSAGE:]
    return pages or [DocPage(EMPTY)]


def image_embed(ref) -> discord.Embed:
    embed = discord.Embed(color=COLOR)
    embed.set_image(url=ref.url)
    if ref.caption:
        embed.set_footer(text=ref.caption[:CAPTION_LIMIT])
    return embed


class DocView(discord.ui.View):
    """문서 화면. ◀ ▶ 로 넘겨 보고, ⬆ 로 상위 문서, 선택창으로 하위 문서로 이동한다. 명령을 쓴 사람만 조작할 수 있다."""

    def __init__(self, lore, page, owner_id: int, private_view: bool):
        super().__init__(timeout=600)
        self.lore = lore
        self.owner_id = owner_id
        self.private_view = private_view   # 비공개 문서를 보는 화면(본인에게만 보임)이면 비공개 하위 문서도 이어 준다.
        self.page = page
        self.pages: list[DocPage] = []
        self.index = 0
        self.interaction: discord.Interaction | None = None

    async def load(self, page):
        self.page = page
        await self.lore.fresh_images(page)    # 노션 이미지 주소는 만료되므로 열 때 새로 받는다.
        self.pages = build_pages(self.lore, page, self.private_view)
        self.index = 0
        self.rebuild()

    def embeds(self) -> list[discord.Embed]:
        doc = self.pages[self.index]
        main = discord.Embed(title=self.page.label[:250], description=doc.text, color=COLOR)
        if len(self.pages) > 1:
            main.set_footer(text=f"{self.index + 1} / {len(self.pages)}")
        return [main] + [image_embed(ref) for ref in doc.images]

    def has_components(self) -> bool:
        return bool(self.children)

    # ---- 구성 ----
    def _button(self, label, callback, disabled=False):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        button.callback = callback
        return button

    def rebuild(self):
        self.clear_items()
        if len(self.pages) > 1:
            self.add_item(self._button("◀ 이전", self.on_prev, self.index == 0))
            self.add_item(self._button("다음 ▶", self.on_next, self.index >= len(self.pages) - 1))
        if self.lore.parent_of(self.page, self.private_view) is not None:
            self.add_item(self._button("⬆ 상위 문서", self.on_up))
        children = self.lore.children_of(self.page, self.private_view)
        if children:
            shown = children[:SELECT_LIMIT]
            note = f"하위 문서 ({len(children)}개)" if len(children) <= SELECT_LIMIT \
                else f"하위 문서 (처음 {SELECT_LIMIT}개 / 전체 {len(children)}개)"
            select = discord.ui.Select(
                placeholder=note,
                options=[discord.SelectOption(label=short_name(c.label), value=c.id.replace("-", "").lower())
                         for c in shown])
            select.callback = self.on_select
            self.add_item(select)

    # ---- 조작 ----
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("명령어를 쓴 사람만 조작할 수 있어요. 직접 /설정을 써 보세요.", ephemeral=True)
        return False

    async def _redraw(self, interaction: discord.Interaction):
        self.rebuild()
        await interaction.response.edit_message(embeds=self.embeds(), view=self)

    async def on_prev(self, interaction: discord.Interaction):
        self.index = max(0, self.index - 1)
        await self._redraw(interaction)

    async def on_next(self, interaction: discord.Interaction):
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._redraw(interaction)

    async def _go(self, interaction: discord.Interaction, target):
        if target is None:
            await interaction.response.send_message("그 문서를 열 수 없어요.", ephemeral=True)
            return
        await interaction.response.defer()          # 이미지 주소를 새로 받느라 3초를 넘길 수 있다.
        await self.load(target)
        await interaction.edit_original_response(embeds=self.embeds(), view=self)

    async def on_up(self, interaction: discord.Interaction):
        await self._go(interaction, self.lore.parent_of(self.page, self.private_view))

    async def on_select(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        await self._go(interaction, self.lore.get(value, include_private=self.private_view))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.interaction:
            try:
                await self.interaction.edit_original_response(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        logging.exception("문서 화면 처리 중 오류", exc_info=error)


async def send_doc(interaction: discord.Interaction, lore, page, private_view: bool):
    view = DocView(lore, page, interaction.user.id, private_view)
    await view.load(page)
    if not view.has_components():
        view.stop()
        await interaction.followup.send(embeds=view.embeds(), ephemeral=private_view)
        return
    view.interaction = interaction
    await interaction.followup.send(embeds=view.embeds(), view=view, ephemeral=private_view, wait=True)


async def open_document(interaction: discord.Interaction, source, name: str, *, list_title: str, not_found=None):
    """/설정·/캐릭터 공통 흐름.

    - 비공개 문서는 관리자만 찾을 수 있고, 열면 본인에게만 보인다(공개 메시지에는 절대 나오지 않는다).
    - 못 찾으면 비슷한 이름(오타)을 알려 준다. 이 안내는 공개 메시지라서 비공개 문서는 포함하지 않는다.
    """
    admin = access.is_admin(interaction)
    stripped = name.strip()
    early = None if stripped == MORE_VALUE else source.get(name, include_private=admin)
    private_view = bool(early and early.private)       # 응답을 보내기 전에 공개 여부를 정한다.
    await interaction.response.defer(ephemeral=private_view)
    if not await ensure_loaded(interaction, source):
        return
    if stripped == MORE_VALUE:       # 자동완성의 '… 외 N개 더 있어요'를 고른 경우
        await send_tree(interaction, source, list_title)
        return
    page = source.get(name, include_private=admin)
    if page is None:
        similar = [p.label for p in source.similar(name)]
        text = not_found(name, similar) if not_found else (
            f"'{name[:50]}' 문서를 찾지 못했어요." + (f"\n혹시 이걸 찾으셨나요? {', '.join(similar)}" if similar else "")
            + "\n`/목록`으로 확인해 보세요.")
        await interaction.followup.send(text)
        return
    if page.private and not private_view:    # 갱신 사이에 비공개로 바뀐 경우: 공개 메시지에 내용을 싣지 않는다.
        await interaction.followup.send("이 문서는 보는 사람이 제한돼요. 명령어를 한 번 더 입력해 주세요.")
        return
    await send_doc(interaction, source, page, page.private)
