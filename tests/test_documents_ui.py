"""문서 화면(이미지·이동 버튼·선택창)과 /설정·/캐릭터·/검색·/비공개목록 흐름 테스트."""
import copy
import unittest
from types import SimpleNamespace as NS
from unittest import mock
from unittest.mock import AsyncMock

import discord

import views
from cogs.character import Character
from cogs.lore import Lore
from cogs.search import Search
from helpers import split_text
from notion_api import EMPTY, IMG_MARK, MORE_VALUE, PRIVATE_MARKERS
from tests.test_notion_tree import page_block, para
from tests.test_page_features import SACRED, TREE, block, image, load
from views import DocView, build_pages, open_document, send_doc

ADMIN = discord.Permissions(administrator=True)
MEMBER = discord.Permissions(send_messages=True)
PRIVATE_NAMES = ["🔒 미공개 설정", "숨은 하위", "가장 깊은 곳", "[비공개] 초안", "🔒 비밀 지도", "비밀 내용", "더 깊은 비밀",
                 "초안 내용", "비밀 지도 내용"]


def interaction(user_id=20, admin=False, guild_id=555, owner_id=1):
    return NS(guild_id=guild_id, user=NS(id=user_id), guild=NS(owner_id=owner_id),
              permissions=ADMIN if admin else MEMBER, data={},
              response=NS(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: True),
              followup=NS(send=AsyncMock()), edit_original_response=AsyncMock())


def sent_text(i):
    """followup.send로 나간 모든 글(본문·제목·선택창 이름까지)을 한 덩어리로."""
    out = []
    for call in i.followup.send.call_args_list:
        for arg in call.args:
            out.append(str(arg))
        for embed in call.kwargs.get("embeds", []) + ([call.kwargs["embed"]] if "embed" in call.kwargs else []):
            out += [embed.title or "", embed.description or "", embed.footer.text or ""]
        view = call.kwargs.get("view")
        if view is not None:
            for item in view.children:
                out.append(getattr(item, "label", "") or "")
                out += [o.label for o in getattr(item, "options", [])]
    return "\n".join(out)


class BuildPagesTests(unittest.IsolatedAsyncioTestCase):
    async def lore_with(self, blocks):
        lore = load({"root": [page_block("X", "문서")], "X": blocks})
        await lore.refresh()
        return lore, lore.get("문서")

    async def test_images_attach_to_the_screen_where_they_appear(self):
        blocks = [para("p0", "가" * 1500), image("a", "https://x/a.png", "그림 A"), para("p1", "나" * 1500),
                  para("p2", "다" * 1500), para("p3", "라" * 1500), image("b", "https://x/b.png"),
                  para("p4", "마" * 1500), para("p5", "바" * 1500)]
        lore, page = await self.lore_with(blocks)
        pages = build_pages(lore, page, False)
        self.assertEqual(len(pages), 3)
        self.assertEqual([[im.url for im in p.images] for p in pages], [["https://x/a.png"], ["https://x/b.png"], []])
        for p in pages:
            self.assertNotIn("\x00", p.text)                    # 내부 표식은 화면에 나오지 않는다.
            self.assertLessEqual(len(p.text), 3500)

    async def test_more_than_nine_images_continue_on_extra_screens(self):
        lore, page = await self.lore_with([image(f"i{n}", f"https://x/{n}.png") for n in range(12)])
        pages = build_pages(lore, page, False)
        self.assertEqual([len(p.images) for p in pages], [9, 3])
        self.assertEqual(pages[0].text, "🖼️ 이미지 12장")
        self.assertEqual(pages[1].text, "🖼️ 이미지 (이어서)")

    async def test_image_only_empty_and_text_pages(self):
        lore, page = await self.lore_with([image("i", "https://x/i.png", "설명")])
        (only,) = build_pages(lore, page, False)
        self.assertEqual((only.text, len(only.images)), ("🖼️ 이미지 1장", 1))
        lore, page = await self.lore_with([])
        self.assertEqual(build_pages(lore, page, False), [views.DocPage(EMPTY)])
        lore, page = await self.lore_with([para("p", "그냥 글")])
        self.assertEqual(build_pages(lore, page, False), [views.DocPage("그냥 글")])

    async def test_blank_lines_are_collapsed_and_marker_lines_removed(self):
        lore, page = await self.lore_with([para("a", "앞"), para("b", ""), para("c", ""), para("d", ""),
                                           image("i", "https://x/i.png"), para("e", "뒤")])
        (doc,) = build_pages(lore, page, False)
        self.assertEqual(doc.text, "앞\n\n뒤")


class DiscordLimitsTests(unittest.IsolatedAsyncioTestCase):
    """디스코드 한도: 메시지당 임베드 10개, 임베드 글자 합계 6000자, 본문 4096자, 선택창 항목 규칙."""

    async def test_worst_case_message_fits_within_discord_limits(self):
        blocks = [para("p0", "가" * 1700), image("a", "https://x/a.png", "가" * 300)]
        blocks += [image(f"i{n}", f"https://x/{n}.png", "나" * 300) for n in range(11)] + [para("p1", "다" * 1700)]
        lore = load({"root": [page_block("X", "긴" * 300)], "X": blocks}, include_root=False)
        await lore.refresh()
        view = DocView(lore, lore.get("긴" * 100), 20, False)
        await view.load(view.page)
        self.addCleanup(view.stop)
        self.assertGreaterEqual(len(view.pages), 2)
        for index in range(len(view.pages)):
            view.index = index
            embeds = view.embeds()
            self.assertLessEqual(len(embeds), 10, index)
            self.assertLessEqual(sum(len(e) for e in embeds), 6000, index)
            self.assertLessEqual(len(embeds[0].description), 4096)
            self.assertLessEqual(len(embeds[0].title), 256)
            for embed in embeds[1:]:
                self.assertLessEqual(len(embed.footer.text or ""), 100)

    async def test_components_follow_discord_rules(self):
        tree = {"root": [page_block("A", "부모")], "A": [page_block(f"c{n}", "자" * 150 + str(n)) for n in range(40)],
                **{f"c{n}": [para("x", "내용")] for n in range(40)}}
        lore = load(tree, include_root=False)
        await lore.refresh()
        view = DocView(lore, lore.get("부모"), 20, False)
        await view.load(view.page)
        self.addCleanup(view.stop)
        select = next(c for c in view.children if isinstance(c, discord.ui.Select))
        self.assertLessEqual(len(select.options), 25)
        self.assertLessEqual(len(select.placeholder), 150)
        self.assertEqual(len({o.value for o in select.options}), len(select.options))
        for option in select.options:
            self.assertLessEqual(len(option.label), 100)
            self.assertLessEqual(len(option.value), 100)
        payload = view.to_components()                      # 디스코드로 보낼 실제 형식으로 바꿔 본다.
        self.assertLessEqual(len(payload), 5)
        for button in (c for c in view.children if isinstance(c, discord.ui.Button)):
            self.assertLessEqual(len(button.label), 80)


class DocViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lore = load()
        await self.lore.refresh()

    async def view_for(self, name, private_view=False, owner=20):
        view = DocView(self.lore, self.lore.get(name, include_private=True), owner, private_view)
        await view.load(view.page)
        self.addCleanup(view.stop)
        return view

    @staticmethod
    def select_of(view):
        return next((c for c in view.children if isinstance(c, discord.ui.Select)), None)

    @staticmethod
    def labels(view):
        return [c.label for c in view.children if isinstance(c, discord.ui.Button)]

    async def test_main_embed_images_and_components_of_a_public_page(self):
        view = await self.view_for(SACRED)
        main, *images = view.embeds()
        self.assertEqual(main.title, SACRED)
        self.assertIn("**굵게** 와 [링크](https://example.org/x)", main.description)
        self.assertEqual([e.image.url for e in images], ["https://files.example/img1.png", "https://cdn.example/ext.png"])
        self.assertEqual([e.footer.text for e in images], ["지도", None])
        self.assertEqual(self.labels(view), ["⬆ 상위 문서"])             # 한 화면이라 ◀ ▶ 는 없다.
        options = self.select_of(view).options
        self.assertEqual([o.label for o in options], ["솔스티스(Solstice)", "공개 하위"])   # 비공개 하위는 없다.
        self.assertEqual([o.value for o in options], ["s1", "s3"])
        self.assertIn("하위 문서 (2개)", self.select_of(view).placeholder)

    async def test_private_children_appear_only_in_a_private_view(self):
        public = await self.view_for(SACRED, private_view=False)
        private = await self.view_for(SACRED, private_view=True)
        self.assertNotIn("🔒 비밀 지도", [o.label for o in self.select_of(public).options])
        self.assertIn("🔒 비밀 지도", [o.label for o in self.select_of(private).options])
        self.assertNotIn("📄 🔒 비밀 지도", public.embeds()[0].description)
        self.assertIn("📄 🔒 비밀 지도", private.embeds()[0].description)

    async def test_private_page_view_links(self):
        view = await self.view_for("🔒 미공개 설정", private_view=True)
        self.assertEqual([o.label for o in self.select_of(view).options], ["숨은 하위"])
        self.assertEqual(self.labels(view), ["⬆ 상위 문서"])               # 공개 대표 페이지로 올라가는 길
        deep = await self.view_for("가장 깊은 곳", private_view=True)
        self.assertIsNone(self.select_of(deep))
        self.assertEqual(self.labels(deep), ["⬆ 상위 문서"])
        hidden_from_public = DocView(self.lore, self.lore.get("숨은 하위", include_private=True), 20, False)
        await hidden_from_public.load(hidden_from_public.page)
        self.assertEqual(hidden_from_public.pages[0].text, EMPTY)           # 공개 화면으로는 내용이 나오지 않는다.
        self.assertEqual(self.labels(hidden_from_public), [])

    async def test_no_components_for_a_standalone_single_screen_page(self):
        lore = load({"root": [page_block("A", "혼자")], "A": [para("a", "글")]}, include_root=False)
        await lore.refresh()
        view = DocView(lore, lore.get("혼자"), 20, False)
        await view.load(view.page)
        self.assertFalse(view.has_components())

    async def test_paging_buttons(self):
        lore = load({"root": [page_block("A", "긴 문서")],
                     "A": [para(f"p{n}", f"{n}" * 1500) for n in range(6)]}, include_root=False)
        await lore.refresh()
        view = DocView(lore, lore.get("긴 문서"), 20, False)
        await view.load(view.page)
        self.addCleanup(view.stop)
        self.assertEqual(len(view.pages), 3)
        prev, nxt = view.children[0], view.children[1]
        self.assertEqual((prev.label, prev.disabled, nxt.label, nxt.disabled), ("◀ 이전", True, "다음 ▶", False))
        i = interaction()
        await nxt.callback(i)
        self.assertEqual(view.index, 1)
        kwargs = i.response.edit_message.await_args.kwargs
        self.assertIs(kwargs["view"], view)
        self.assertEqual(kwargs["embeds"][0].footer.text, "2 / 3")
        await view.children[1].callback(interaction())
        self.assertEqual((view.index, view.children[1].disabled, view.children[0].disabled), (2, True, False))
        await view.children[1].callback(interaction())                       # 끝에서 더 넘겨도 범위를 벗어나지 않는다.
        self.assertEqual(view.index, 2)
        await view.children[0].callback(interaction())
        await view.children[0].callback(interaction())
        await view.children[0].callback(interaction())
        self.assertEqual(view.index, 0)

    async def test_select_is_limited_to_25_and_says_so(self):
        tree = {"root": [page_block("A", "부모")], "A": [page_block(f"c{n}", f"자식 {n}") for n in range(30)],
                **{f"c{n}": [para("x", "내용")] for n in range(30)}}
        lore = load(tree, include_root=False)
        await lore.refresh()
        view = DocView(lore, lore.get("부모"), 20, False)
        await view.load(view.page)
        self.addCleanup(view.stop)
        select = self.select_of(view)
        self.assertEqual(len(select.options), 25)
        self.assertIn("처음 25개 / 전체 30개", select.placeholder)

    async def test_only_the_person_who_ran_the_command_can_use_it(self):
        view = await self.view_for(SACRED, owner=20)
        mine, other = interaction(user_id=20), interaction(user_id=99)
        self.assertTrue(await view.interaction_check(mine))
        self.assertFalse(await view.interaction_check(other))
        self.assertEqual(other.response.send_message.await_args.kwargs, {"ephemeral": True})
        self.assertIn("명령어를 쓴 사람만", other.response.send_message.await_args.args[0])

    async def test_select_moves_to_a_child_and_up_returns(self):
        view = await self.view_for(SACRED)
        i = interaction()
        i.data = {"values": ["s1"]}
        with mock.patch.object(self.lore, "fresh_images", wraps=self.lore.fresh_images) as fresh:
            await self.select_of(view).callback(i)
        i.response.defer.assert_awaited_once()                               # 3초 제한을 넘길 수 있어 먼저 응답한다.
        kwargs = i.edit_original_response.await_args.kwargs
        self.assertIs(kwargs["view"], view)
        self.assertEqual(kwargs["embeds"][0].title, "솔스티스(Solstice)")
        self.assertIn("태양이 멈추는 곳", kwargs["embeds"][0].description)
        self.assertEqual(view.page.label, "솔스티스(Solstice)")
        fresh.assert_awaited_once_with(view.page)                            # 이동할 때도 이미지 주소를 새로 확인한다.
        self.assertIsNone(self.select_of(view))                              # 하위가 없는 문서
        back = interaction()
        await view.children[-1].callback(back)
        self.assertEqual(view.page.label, SACRED)
        self.assertEqual(back.edit_original_response.await_args.kwargs["embeds"][0].title, SACRED)

    async def test_up_from_a_top_level_document_goes_to_the_main_page(self):
        view = await self.view_for(SACRED)
        i = interaction()
        await view.children[0].callback(i)
        self.assertEqual(view.page.label, "라시안컷(Rasyancut)")
        self.assertEqual([o.label for o in self.select_of(view).options], [SACRED, "공개 문서"])
        self.assertEqual(self.labels(view), [])                              # 대표 페이지 위는 없다.

    async def test_a_select_value_that_points_to_a_hidden_page_is_refused(self):
        view = await self.view_for(SACRED, private_view=False)
        for value in ["s2", "h", "h1", "d", "없는-id"]:
            i = interaction()
            i.data = {"values": [value]}
            await view.on_select(i)
            i.response.send_message.assert_awaited_once_with("그 문서를 열 수 없어요.", ephemeral=True)
            i.edit_original_response.assert_not_awaited()
        self.assertEqual(view.page.label, SACRED)

    async def test_timeout_disables_the_buttons(self):
        view = await self.view_for(SACRED)
        view.interaction = interaction()
        await view.on_timeout()
        self.assertTrue(all(item.disabled for item in view.children))
        view.interaction.edit_original_response.assert_awaited_once_with(view=view)
        view.interaction.edit_original_response.side_effect = discord.HTTPException(NS(status=404, reason="x"), "gone")
        await view.on_timeout()                                              # 이미 지워진 메시지라도 오류로 끝나지 않는다.

    async def test_send_doc_chooses_between_plain_and_interactive_messages(self):
        i = interaction()
        await send_doc(i, self.lore, self.lore.get(SACRED), private_view=False)
        kwargs = i.followup.send.await_args.kwargs
        self.assertEqual((kwargs["ephemeral"], kwargs["wait"]), (False, True))
        self.assertIsInstance(kwargs["view"], DocView)
        self.assertIs(kwargs["view"].interaction, i)
        kwargs["view"].stop()
        lore = load({"root": [page_block("A", "혼자")], "A": [para("a", "글")]}, include_root=False)
        await lore.refresh()
        solo = interaction()
        await send_doc(solo, lore, lore.get("혼자"), private_view=False)
        self.assertNotIn("view", solo.followup.send.await_args.kwargs)
        priv = interaction(admin=True)
        await send_doc(priv, self.lore, self.lore.get("H", include_private=True), private_view=True)
        self.assertTrue(priv.followup.send.await_args.kwargs["ephemeral"])
        priv.followup.send.await_args.kwargs["view"].stop()


class OpenDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lore = load()
        await self.lore.refresh()

    async def open(self, name, admin=False, source=None, **kw):
        i = interaction(admin=admin)
        await open_document(i, source or self.lore, name, list_title="세계관 문서", **kw)
        view = i.followup.send.await_args.kwargs.get("view") if i.followup.send.await_args else None
        if view is not None:
            self.addCleanup(view.stop)
        return i

    async def test_public_document_is_a_public_message_for_everyone(self):
        for admin in (False, True):
            i = await self.open("솔스티스", admin=admin)
            i.response.defer.assert_awaited_once_with(ephemeral=False)
            self.assertFalse(i.followup.send.await_args.kwargs["ephemeral"])
            self.assertIn("태양이 멈추는 곳", sent_text(i))

    async def test_non_admins_cannot_find_private_documents_by_any_means(self):
        for name in ["🔒 미공개 설정", "미공개", "숨은 하위", "[비공개] 초안", "H", "d", "미공계", "ㅁㄱㄱㅅㅈ", "가장 깊은 곳"]:
            i = await self.open(name)
            i.response.defer.assert_awaited_once_with(ephemeral=False)
            text = sent_text(i)
            self.assertIn("찾지 못했어요", text, name)
            for hidden in PRIVATE_NAMES:
                self.assertNotIn(hidden, text.replace(name, ""), f"{name} -> {hidden}")      # 안내 문구에도 비공개 이름이 없다.
            self.assertNotIn("embeds", i.followup.send.await_args.kwargs)

    async def test_admin_opens_a_private_document_privately(self):
        i = await self.open("미공개 설정", admin=True)
        i.response.defer.assert_awaited_once_with(ephemeral=True)           # 응답하기 전에 '본인에게만'으로 정한다.
        kwargs = i.followup.send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(kwargs["embeds"][0].title, "🔒 미공개 설정")
        self.assertIn("비밀 내용", kwargs["embeds"][0].description)
        self.assertEqual([o.label for o in kwargs["view"].children[-1].options], ["숨은 하위"])
        deeper = await self.open("가장 깊은 곳", admin=True)
        deeper.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertIn("바닥", sent_text(deeper))

    async def test_admins_public_messages_never_carry_private_names(self):
        i = await self.open(SACRED, admin=True)                             # 공개 문서를 관리자가 열어도 공개 메시지
        i.response.defer.assert_awaited_once_with(ephemeral=False)
        text = sent_text(i)
        for hidden in PRIVATE_NAMES:
            self.assertNotIn(hidden, text)
        miss = await self.open("미공계 설정 없음 문서", admin=True)
        for hidden in PRIVATE_NAMES:
            self.assertNotIn(hidden, sent_text(miss))

    async def test_a_page_that_became_private_during_the_refresh_is_not_posted_publicly(self):
        page = self.lore.get("공개 문서")

        async def flip(interaction_, source, force=False):
            page.private = True
            return True
        with mock.patch("views.ensure_loaded", side_effect=flip):
            i = await self.open("공개 문서", admin=True)
        i.response.defer.assert_awaited_once_with(ephemeral=False)           # 이미 공개로 응답을 시작한 뒤
        self.assertIn("보는 사람이 제한돼요", i.followup.send.await_args.args[0])
        self.assertNotIn("공개 내용", sent_text(i))                          # 내용은 싣지 않는다.

    async def test_not_found_suggests_similar_public_names(self):
        i = await self.open("솔리리아")
        lore = load({"root": [page_block("L", "솔라리아(Solaria)")], "L": [para("l", "태양")]})
        await lore.refresh()
        i = await self.open("솔리리아", source=lore)
        text = i.followup.send.await_args.args[0]
        self.assertIn("찾지 못했어요", text)
        self.assertIn("혹시 이걸 찾으셨나요? 솔라리아(Solaria)", text)
        self.assertIn("/목록", text)

    async def test_more_row_shows_the_public_tree_even_to_admins(self):
        for admin in (False, True):
            i = await self.open(MORE_VALUE, admin=admin)
            i.response.defer.assert_awaited_once_with(ephemeral=False)
            text = sent_text(i)
            self.assertIn("• 성역의 경계(Sacred Boundary)", text)
            self.assertIn("읽으려면 /설정 이름", text)
            for hidden in PRIVATE_NAMES:
                self.assertNotIn(hidden, text)

    async def test_load_failure_is_reported_and_stops(self):
        with mock.patch("views.ensure_loaded", new=AsyncMock(return_value=False)):
            i = await self.open("공개 문서")
        i.followup.send.assert_not_awaited()


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lore = load()
        await self.lore.refresh()
        chars = {"root": [page_block("A", "공개 캐릭터"), page_block("B", "🔒 숨은 캐릭터")],
                 "A": [para("a", "아주 공개된 인물")], "B": [para("b", "비밀 인물 이야기")]}
        self.characters = load(chars, include_root=False)
        await self.characters.refresh()
        self.bot = NS(lore=self.lore, characters=self.characters)

    async def run_command(self, command, *args, admin=False):
        i = interaction(admin=admin)
        await command.callback(*args[:1], i, *args[1:])
        view = i.followup.send.await_args.kwargs.get("view") if i.followup.send.await_args else None
        if view is not None:
            self.addCleanup(view.stop)
        return i

    async def test_setting_and_character_commands(self):
        i = await self.run_command(Lore.show, Lore(self.bot), "공개 하위")
        self.assertIn("공개 하위 내용", sent_text(i))
        i = await self.run_command(Character.show, Character(self.bot), "공개 캐릭터")
        self.assertIn("아주 공개된 인물", sent_text(i))
        for name, admin in [("숨은 캐릭터", False), ("비밀 인물", False)]:
            i = await self.run_command(Character.show, Character(self.bot), name, admin=admin)
            self.assertNotIn("비밀 인물 이야기", sent_text(i))
            self.assertIn("등록된 캐릭터: 공개 캐릭터", i.followup.send.await_args.args[0])
        i = await self.run_command(Character.show, Character(self.bot), "숨은 캐릭터", admin=True)
        self.assertIn("비밀 인물 이야기", sent_text(i))
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        none = NS(lore=self.lore, characters=None)
        i = interaction()
        await Character.show.callback(Character(none), i, "아무거나")
        self.assertIn("NOTION_CHARACTER_PAGE_ID", i.response.send_message.await_args.args[0])

    async def test_character_not_found_message_has_similar_names_and_the_public_roster(self):
        i = await self.run_command(Character.show, Character(self.bot), "공개 캐리터")
        text = i.followup.send.await_args.args[0]
        self.assertIn("혹시 이걸 찾으셨나요? 공개 캐릭터", text)
        self.assertIn("등록된 캐릭터: 공개 캐릭터", text)
        self.assertNotIn("숨은", text)

    async def test_autocomplete_shows_private_documents_to_admins_only(self):
        for cog, command in ((Lore(self.bot), Lore.show_autocomplete), (Character(self.bot), Character.show_autocomplete)):
            query = "미공개" if isinstance(cog, Lore) else "숨은"
            normal = await command(cog, interaction(), query)
            admin = await command(cog, interaction(admin=True), query)
            self.assertEqual([c.name for c in normal if "🔒" in c.name], [])
            self.assertEqual(len([c for c in admin if "🔒" in c.name]), 1)
            self.assertTrue(all(len(c.name) <= 100 and len(c.value) <= 100 for c in normal + admin))
        self.assertEqual(await Character.show_autocomplete(Character(NS(lore=self.lore, characters=None)), interaction(), "a"), [])

    async def test_list_and_search_never_show_private_documents_even_to_admins(self):
        for admin in (False, True):
            i = await self.run_command(Lore.list_docs, Lore(self.bot), admin=admin)
            text = sent_text(i)
            self.assertIn("• 성역의 경계(Sacred Boundary)", text)
            for hidden in PRIVATE_NAMES:
                self.assertNotIn(hidden, text)
            for keyword in ["비밀 내용", "초안", "비밀 지도", "미공개"]:
                i = await self.run_command(Search.search, Search(self.bot), keyword, admin=admin)
                text = sent_text(i).replace(keyword, "")          # 사용자가 직접 친 검색어를 되풀이하는 것은 제외
                for hidden in PRIVATE_NAMES:
                    self.assertNotIn(hidden, text, f"{keyword}")

    async def test_private_list_command(self):
        i = interaction(admin=True)
        await Lore.private_docs.callback(Lore(self.bot), i)
        i.response.defer.assert_awaited_once_with(ephemeral=True)
        text = sent_text(i)
        self.assertIn("**세계관 문서 (5개)**", text)
        self.assertIn("• 🔒 미공개 설정", text)
        self.assertIn(f"• {SACRED} / 🔒 비밀 지도", text)
        self.assertIn("**캐릭터 (1개)**", text)
        self.assertIn("• 🔒 숨은 캐릭터", text)
        self.assertIn(" 또는 ".join(PRIVATE_MARKERS), text)
        self.assertIn("본인에게만", text)

        nobody = interaction(admin=False)
        await Lore.private_docs.callback(Lore(self.bot), nobody)
        nobody.response.defer.assert_not_awaited()
        nobody.followup.send.assert_not_awaited()
        self.assertEqual(nobody.response.send_message.await_args.kwargs, {"ephemeral": True})
        self.assertNotIn("비밀", str(nobody.response.send_message.await_args))

        clean = load({"root": [page_block("A", "공개")], "A": [para("a", "글")]})
        await clean.refresh()
        i = interaction(admin=True)
        await Lore.private_docs.callback(Lore(NS(lore=clean, characters=None)), i)
        self.assertIn("비공개 문서가 없어요", sent_text(i))

    async def test_search_result_labels_and_notes(self):
        lore = load({"root": [page_block(p, t) for p, t in [("S", SACRED), ("L", "솔라리아(Solaria)")]],
                     "S": [para("s", "빛이 닿지 않는 곳"), para("s2", "별칭: 성지")], "L": [para("l", "태양의 땅")]})
        await lore.refresh()
        bot = NS(lore=lore, characters=None)
        def description(i):
            return i.followup.send.await_args.kwargs["embed"].description
        i = await self.run_command(Search.search, Search(bot), "닿지")
        self.assertIn("**성역의 경계(Sacred Boundary)** · 1회", description(i))
        i = await self.run_command(Search.search, Search(bot), "sacred")
        self.assertIn("제목 일치", description(i))
        i = await self.run_command(Search.search, Search(bot), "성지")
        self.assertIn("별칭 일치 · 본문 1회", description(i))
        i = await self.run_command(Search.search, Search(bot), "ㅅㄹㄹㅇ")
        self.assertIn("초성 일치", description(i))
        i = await self.run_command(Search.search, Search(bot), "솔리리아")
        self.assertIn("비슷한 이름", description(i))
        self.assertTrue(description(i).startswith("정확히 맞는 결과가 없어서 비슷한 이름을 보여줘요."))
        i = await self.run_command(Search.search, Search(bot), "전혀 없는 말")
        self.assertIn("결과가 없어요", i.followup.send.await_args.args[0])


class SplitTextCompatibilityTests(unittest.TestCase):
    def test_split_text_keeps_marker_lines_intact(self):
        text = "\n".join(["가" * 1500, f"{IMG_MARK}0", "나" * 1500, "다" * 1500])
        chunks = split_text(text)
        self.assertEqual(len(chunks), 2)
        self.assertIn(f"{IMG_MARK}0", chunks[0])


if __name__ == "__main__":
    unittest.main()
