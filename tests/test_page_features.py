"""서식·이미지·별칭·비공개·검색 테스트. 가짜 노션 데이터로 읽기부터 조회까지 확인한다."""
import copy
import time
import unittest

import notion_api
from notion_api import EMPTY, IMG_MARK, MORE_VALUE, NotionLore, _norm
from tests.test_notion_tree import make_lore, page_block, para, rt


def part(text, href=None, **annotations):
    return {"plain_text": text, "href": href, "annotations": annotations}


def block(i, kind, parts, children=False, **extra):
    return {"id": i, "type": kind, "has_children": children, kind: {"rich_text": parts, **extra}}


def image(i, url, caption="", kind="file"):
    return {"id": i, "type": "image", "has_children": False,
            "image": {"type": kind, kind: {"url": url}, "caption": rt(caption) if caption else []}}


def titled(pid, title):
    return {"properties": {"title": {"type": "title", "title": rt(title)}}}


SACRED = "성역의 경계(Sacred Boundary)"
TREE = {
    "root": [page_block("S", SACRED), page_block("H", "🔒 미공개 설정"), page_block("D", "[비공개] 초안"),
             page_block("P", "공개 문서")],
    "S": [
        block("s1", "heading_1", rt("성역의 경계 *개요*")),
        block("s2", "paragraph", [part("굵게", bold=True), part(" 와 "), part("링크", "https://example.org/x"),
                                  part(" snake_case")]),
        block("s3", "quote", rt("첫 줄\n둘째 줄")),
        block("s4", "code", rt("a ``` b")),
        block("s5", "bulleted_list_item", rt("항목")),
        block("s6", "numbered_list_item", rt("첫째")),
        block("s7", "numbered_list_item", rt("둘째")),
        block("s8", "to_do", rt("할 일"), checked=True),
        {"id": "tb", "type": "table", "has_children": True, "table": {}},
        {"id": "bm", "type": "bookmark", "has_children": False, "bookmark": {"url": "https://example.org/page"}},
        block("s9", "paragraph", rt("별칭: 성지, Sanctum")),
        image("im1", "https://files.example/img1.png", "지도"),
        block("tg", "toggle", rt("더 보기"), children=True),
        page_block("S1", "솔스티스(Solstice)"), page_block("S2", "🔒 비밀 지도"), page_block("S3", "공개 하위"),
    ],
    "tb": [{"id": "r1", "type": "table_row", "has_children": False,
            "table_row": {"cells": [rt("이름"), [part("값", bold=True)]]}},
           {"id": "r2", "type": "table_row", "has_children": False, "table_row": {"cells": [rt("a"), rt("b")]}}],
    "tg": [image("im2", "https://cdn.example/ext.png", "", "external"), block("tg1", "paragraph", rt("토글 안"))],
    "S1": [block("a1", "paragraph", rt("태양이 멈추는 곳"))],
    "S2": [block("b1", "paragraph", rt("비밀 지도 내용"))],
    "S3": [block("c1", "paragraph", rt("공개 하위 내용"))],
    "H": [block("h1", "paragraph", rt("비밀 내용")), page_block("H1", "숨은 하위")],
    "H1": [block("h2", "paragraph", rt("더 깊은 비밀")), page_block("H1a", "가장 깊은 곳")],
    "H1a": [block("h3", "paragraph", rt("바닥"))],
    "D": [block("d1", "paragraph", rt("초안 내용"))],
    "P": [block("p1", "paragraph", rt("공개 내용 입니다"))],
}


def load(tree=None, copy_tree=True, **kw):
    """copy_tree=False면 넘겨 준 dict를 그대로 써서, 읽은 뒤에 노션 내용을 바꾸는 상황을 흉내 낼 수 있다."""
    import tests.test_notion_tree as base
    base.TITLES["root"] = "라시안컷(Rasyancut)"
    tree = TREE if tree is None else tree
    return make_lore(copy.deepcopy(tree) if copy_tree else tree, **kw)


class FormattingInPagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lore = load()
        await self.lore.refresh()
        self.page = self.lore.get(SACRED)

    async def test_display_text_keeps_notion_formatting(self):
        raw = self.page.raw
        self.assertIn("__**성역의 경계 \\*개요\\***__", raw)            # 제목은 글자만, 서식 문자는 이스케이프
        self.assertIn("**굵게** 와 [링크](https://example.org/x) snake\\_case", raw)
        self.assertIn("> 첫 줄\n> 둘째 줄", raw)                          # 여러 줄 인용은 줄마다 > 를 붙인다.
        self.assertIn("```\na ``\u200b` b\n```", raw)                     # 코드 블록 안의 ``` 가 블록을 끝내지 못한다.
        self.assertIn("• 항목", raw)
        self.assertIn("1. 첫째\n2. 둘째", raw)
        self.assertIn("☑ 할 일", raw)
        self.assertIn("이름 | **값**\na | b", raw)                        # 표
        self.assertIn("🔗 https://example.org/page", raw)                 # 북마크
        self.assertIn("토글 안", raw)                                     # 토글 안쪽 글도 읽는다.

    async def test_search_text_has_no_markers(self):
        plain = self.page.plain
        for marker in ["**", "\\_", "\\*", "[링크]", "\x00"]:
            self.assertNotIn(marker, plain, marker)
        self.assertIn("굵게 와 링크 snake_case", plain)
        self.assertIn("첫 줄\n둘째 줄", plain)
        self.assertIn("지도", plain)                                       # 이미지 설명(캡션)도 검색된다.
        self.assertIn("토글 안", plain)

    async def test_bold_word_is_found_by_search_even_though_display_has_markers(self):
        self.assertEqual([h.page.label for h in self.lore.search_hits("굵게 와 링크")][:1], [SACRED])


class ImagesInPagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lore = load()
        await self.lore.refresh()
        self.page = self.lore.get(SACRED)

    async def test_images_are_collected_in_order_with_container_and_kind(self):
        first, second = self.page.images
        self.assertEqual((first.url, first.caption, first.expiring, first.container_id),
                         ("https://files.example/img1.png", "지도", True, "S"))
        self.assertEqual((second.url, second.caption, second.expiring, second.container_id),
                         ("https://cdn.example/ext.png", "", False, "tg"))     # 토글 안 이미지는 그 토글에서 다시 읽는다.

    async def test_markers_stay_in_render_lines_and_become_captions_in_render(self):
        marked = self.lore.render_lines(self.page)
        self.assertEqual([ln for ln in marked if ln.startswith(IMG_MARK)], [f"{IMG_MARK}0", f"{IMG_MARK}1"])
        text = self.lore.render(self.page)
        self.assertIn("🖼️ 지도", text)
        self.assertIn("🖼️ 이미지", text)                                   # 설명이 없는 이미지
        self.assertNotIn("\x00", text)

    async def test_missing_urls_and_unknown_image_types_are_skipped(self):
        tree = copy.deepcopy(TREE)
        tree["P"] = [{"id": "x1", "type": "image", "has_children": False, "image": {"type": "file", "file": {}}},
                     {"id": "x2", "type": "image", "has_children": False, "image": {"type": "weird"}},
                     para("p1", "글")]
        lore = load(tree)
        await lore.refresh()
        self.assertEqual(lore.get("공개 문서").images, [])
        self.assertEqual(lore.render(lore.get("공개 문서")), "글")


class FreshImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tree = copy.deepcopy(TREE)
        self.lore = load(self.tree, copy_tree=False)
        await self.lore.refresh()
        self.page = self.lore.get(SACRED)
        self.calls = []
        original = self.lore._children

        async def counting(block_id):
            self.calls.append(block_id)
            return await original(block_id)
        self.lore._children = counting

    def age(self, seconds):
        for ref in self.page.images:
            ref.fetched -= seconds

    async def test_recent_images_are_not_requested_again(self):
        await self.lore.fresh_images(self.page)
        self.assertEqual(self.calls, [])

    async def test_expired_notion_hosted_images_get_new_urls_with_one_request_per_container(self):
        self.tree["S"][11]["image"]["file"]["url"] = "https://files.example/img1-NEW.png"
        self.age(3600)
        refs = await self.lore.fresh_images(self.page)
        self.assertEqual(self.calls, ["S"])                               # 외부 이미지(토글 안)는 다시 읽지 않는다.
        self.assertEqual(refs[0].url, "https://files.example/img1-NEW.png")
        self.assertEqual(refs[1].url, "https://cdn.example/ext.png")
        self.assertLess(time.time() - refs[0].fetched, 5)
        await self.lore.fresh_images(self.page)                           # 방금 받았으니 다시 받지 않는다.
        self.assertEqual(self.calls, ["S"])

    async def test_images_in_different_containers_and_duplicates(self):
        tree = copy.deepcopy(TREE)
        tree["S"].insert(12, image("im3", "https://files.example/img3.png", "셋"))
        tree["tg"][0] = image("im2", "https://files.example/in-toggle.png")
        lore = load(tree)
        await lore.refresh()
        page = lore.get(SACRED)
        calls = []
        original = lore._children

        async def counting(block_id):
            calls.append(block_id)
            return await original(block_id)
        lore._children = counting
        for ref in page.images:
            ref.fetched -= 3600
        await lore.fresh_images(page)
        self.assertEqual(sorted(calls), ["S", "tg"])                      # 같은 곳의 이미지 둘은 한 번에

    async def test_failure_keeps_the_old_address(self):
        async def failing(block_id):
            raise RuntimeError("노션 오류")
        self.lore._children = failing
        self.age(3600)
        old = self.page.images[0].url
        refs = await self.lore.fresh_images(self.page)
        self.assertEqual(refs[0].url, old)
        self.assertGreater(time.time() - refs[0].fetched, 3000)           # 실패했으니 시각도 그대로 → 다음에 다시 시도

    async def test_image_deleted_in_notion_keeps_the_old_address(self):
        del self.tree["S"][11]
        self.age(3600)
        refs = await self.lore.fresh_images(self.page)
        self.assertEqual(refs[0].url, "https://files.example/img1.png")


class AliasAndDigestTests(unittest.IsolatedAsyncioTestCase):
    async def test_aliases_come_from_the_alias_line(self):
        lore = load()
        await lore.refresh()
        self.assertEqual(lore.get(SACRED).aliases, ["성지", "Sanctum"])
        self.assertEqual(lore.get("공개 문서").aliases, [])

    async def test_digest_is_stable_and_changes_only_with_real_content(self):
        base = load()
        await base.refresh()
        same = load()
        await same.refresh()
        self.assertEqual(base.get(SACRED).digest, same.get(SACRED).digest)
        self.assertEqual(len(base.get(SACRED).digest), 16)

        changed = copy.deepcopy(TREE)
        changed["S"][1]["paragraph"]["rich_text"][2]["plain_text"] = "링크!"
        lore = load(changed)
        await lore.refresh()
        self.assertNotEqual(base.get(SACRED).digest, lore.get(SACRED).digest)
        self.assertEqual(base.get("공개 문서").digest, lore.get("공개 문서").digest)     # 다른 문서는 그대로

        # 이미지 주소만 바뀐 것(읽을 때마다 달라지는 서명 주소)은 변경이 아니다.
        rotated = copy.deepcopy(TREE)
        rotated["S"][11]["image"]["file"]["url"] = "https://files.example/img1.png?signature=ABC"
        lore = load(rotated)
        await lore.refresh()
        self.assertEqual(base.get(SACRED).digest, lore.get(SACRED).digest)
        # 이미지가 추가·삭제되거나 설명이 바뀌면 변경이다.
        for edit in (lambda t: t["S"].insert(12, image("im9", "https://x/9.png")),
                     lambda t: t["S"][11]["image"].update(caption=rt("다른 설명")),
                     lambda t: t["S"].__delitem__(11)):
            tree = copy.deepcopy(TREE)
            edit(tree)
            lore = load(tree)
            await lore.refresh()
            self.assertNotEqual(base.get(SACRED).digest, lore.get(SACRED).digest)

    async def test_adding_removing_or_renaming_a_child_page_does_not_change_the_parent_digest(self):
        base = load()
        await base.refresh()
        added = copy.deepcopy(TREE)
        added["root"].append(page_block("N", "새 문서"))
        added["N"] = [para("n", "새 내용")]
        added["S"].append(page_block("N2", "새 하위"))
        added["N2"] = [para("n2", "내용")]
        renamed = copy.deepcopy(TREE)
        renamed["S"][13] = page_block("S1", "이름이 바뀐 솔스티스")
        removed = copy.deepcopy(TREE)
        del removed["S"][15]
        for name, tree in [("추가", added), ("이름 변경", renamed), ("삭제", removed)]:
            lore = load(tree)
            await lore.refresh()
            for label in ["라시안컷", SACRED, "공개 문서"]:
                self.assertEqual(base.get(label).digest, lore.get(label).digest, f"{name}: {label}")

    async def test_a_child_page_change_does_not_change_the_parent_digest(self):
        base = load()
        await base.refresh()
        tree = copy.deepcopy(TREE)
        tree["S1"][0]["paragraph"]["rich_text"] = rt("완전히 다른 내용")
        lore = load(tree)
        await lore.refresh()
        self.assertEqual(base.get(SACRED).digest, lore.get(SACRED).digest)
        self.assertNotEqual(base.get("솔스티스").digest, lore.get("솔스티스").digest)


class TreeLinksTests(unittest.IsolatedAsyncioTestCase):
    async def test_parent_and_children(self):
        lore = load()
        await lore.refresh()
        root, sacred, solstice = lore.get("라시안컷"), lore.get(SACRED), lore.get("솔스티스")
        self.assertIsNone(root.parent)
        self.assertEqual(sacred.parent, root.path)                        # 최상위 문서의 상위는 대표 페이지
        self.assertEqual(solstice.parent, sacred.path)
        self.assertEqual([p.label for p in lore.children_of(sacred)], ["솔스티스(Solstice)", "공개 하위"])
        self.assertEqual([p.label for p in lore.children_of(sacred, include_private=True)],
                         ["솔스티스(Solstice)", "🔒 비밀 지도", "공개 하위"])
        self.assertIs(lore.parent_of(solstice), sacred)
        self.assertIs(lore.parent_of(sacred), root)
        self.assertIsNone(lore.parent_of(root))
        self.assertEqual([p.label for p in lore.children_of(root)], [SACRED, "공개 문서"])

    async def test_without_a_root_page_top_level_documents_have_no_parent(self):
        lore = load(include_root=False)
        await lore.refresh()
        self.assertIsNone(lore.get(SACRED).parent)
        self.assertIsNone(lore.parent_of(lore.get(SACRED)))
        self.assertIs(lore.parent_of(lore.get("솔스티스")), lore.get(SACRED))


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    HIDDEN = ["🔒 미공개 설정", "숨은 하위", "가장 깊은 곳", "[비공개] 초안", "🔒 비밀 지도"]

    async def asyncSetUp(self):
        self.lore = load()
        await self.lore.refresh()

    def assertNoneHidden(self, text, where):
        for name in self.HIDDEN + ["비밀 내용", "더 깊은 비밀", "초안 내용", "비밀 지도 내용"]:
            self.assertNotIn(name, text, f"{where}: {name}")

    async def test_marker_and_inheritance(self):
        flags = {p.label: p.private for p in self.lore.pages(include_private=True)}
        for name in self.HIDDEN:
            self.assertTrue(flags[name], name)
        self.assertEqual([n for n, private in flags.items() if not private],
                         ["라시안컷(Rasyancut)", SACRED, "솔스티스(Solstice)", "공개 하위", "공개 문서"])

    async def test_lists_never_contain_private_documents_by_default(self):
        self.assertNoneHidden("\n".join(self.lore.titles()), "titles")
        self.assertNoneHidden("\n".join(self.lore.top_titles()), "top_titles")
        self.assertNoneHidden("\n".join(self.lore.tree_lines()), "tree_lines")
        self.assertNoneHidden("\n".join(p.label for p in self.lore.pages()), "pages")
        self.assertNoneHidden("\n".join(n for n, _ in self.lore.suggest("")), "suggest")
        self.assertEqual(len(self.lore.pages()), 5)
        self.assertEqual(len(self.lore.pages(include_private=True)), 10)

    async def test_lookups_by_name_id_partial_and_typo_do_not_reveal_them(self):
        for page_id in ["H", "H1", "H1a", "D", "S2"]:          # 문서 id로 직접 불러도 안 된다.
            self.assertIsNone(self.lore.get(page_id), page_id)
        for query in ["🔒 미공개 설정", "미공개", "숨은 하위", "가장 깊은 곳", "[비공개] 초안", "초안", "비밀 지도",
                      "ㅁㄱㄱ", "미공계", "숨은하위", "🔒", "미공개설정", "가장 깊은", "숨운 하위"]:
            self.assertIsNone(self.lore.get(query), query)
            # 비슷한 공개 문서가 후보로 나오는 것은 괜찮다. 비공개 문서가 나오면 안 된다.
            private_ids = {_norm(p.id) for p in self.lore.pages(include_private=True) if p.private}
            self.assertFalse({value for _, value in self.lore.suggest(query)} & private_ids, query)
            self.assertFalse([p for p in self.lore.similar(query) if p.private], query)
            self.assertFalse([h for h in self.lore.search_hits(query) if h.page.private], query)
            for name, _ in self.lore.suggest(query):
                self.assertNoneHidden(name, f"suggest({query})")
        for query in ["비밀 내용", "더 깊은 비밀", "초안 내용", "비밀 지도 내용", "바닥"]:     # 본문 검색도
            self.assertEqual(self.lore.search_hits(query), [], query)

    async def test_admins_see_everything_when_asked(self):
        self.assertEqual(self.lore.get("H", include_private=True).label, "🔒 미공개 설정")
        self.assertEqual(self.lore.get("가장 깊은 곳", include_private=True).label, "가장 깊은 곳")
        self.assertIn("🔒 미공개 설정", [n for n, _ in self.lore.suggest("미공개", include_private=True)])
        self.assertIn("[비공개] 초안", [n for n, _ in self.lore.suggest("", include_private=True)])
        self.assertEqual([h.page.label for h in self.lore.search_hits("비밀 내용", include_private=True)],
                         ["🔒 미공개 설정"])
        self.assertEqual([p.label for p in self.lore.similar("미공계", include_private=True)], ["🔒 미공개 설정"])
        self.assertEqual([n for n, _ in self.lore.suggest("미공계", include_private=True)], ["🔒 미공개 설정"])
        self.assertIn("• 🔒 미공개 설정", self.lore.tree_lines(include_private=True))

    async def test_rendering_hides_private_text_names_and_links(self):
        hidden = self.lore.get("H", include_private=True)
        self.assertEqual(self.lore.render(hidden), EMPTY)                 # 기본값으로 그려도 내용은 나오지 않는다.
        self.assertEqual(self.lore.render_lines(hidden), [])
        self.assertIn("비밀 내용", self.lore.render(hidden, include_private=True))
        root = self.lore.render(self.lore.get("라시안컷"))
        self.assertIn("📄 성역의 경계(Sacred Boundary)", root)
        self.assertNoneHidden(root, "대표 페이지 목록")
        self.assertIn("📄 🔒 미공개 설정", self.lore.render(self.lore.get("라시안컷"), include_private=True))
        sacred = self.lore.render(self.lore.get(SACRED))
        self.assertIn("📄 공개 하위", sacred)
        self.assertNoneHidden(sacred, "공개 문서의 하위 목록")
        self.assertIn("📄 🔒 비밀 지도", self.lore.render(self.lore.get(SACRED), include_private=True))
        inline = self.lore.render(self.lore.get(SACRED), inline=True)           # 하위 내용을 끼워 넣는 모드에서도
        self.assertNoneHidden(inline, "inline")
        self.assertIn("공개 하위 내용", inline)

    async def test_navigation_does_not_lead_to_private_documents(self):
        sacred = self.lore.get(SACRED)
        self.assertNoneHidden("\n".join(p.label for p in self.lore.children_of(sacred)), "children_of")
        self.assertNoneHidden("\n".join(p.label for p in self.lore.children_of(self.lore.get("라시안컷"))), "root children")
        hidden_child = self.lore.get("숨은 하위", include_private=True)
        self.assertIsNone(self.lore.parent_of(hidden_child))               # 상위가 비공개면 올라가는 길도 없다.
        self.assertEqual(self.lore.children_of(self.lore.get("H", include_private=True)), [])

    async def test_private_list_for_admins_only_contains_private_documents(self):
        lines = self.lore.private_lines()
        self.assertEqual(lines, [f"• {SACRED} / 🔒 비밀 지도",     # 공개 문서 아래의 비공개 문서는 어느 문서 밑인지 보인다.
                                 "• 🔒 미공개 설정", "└ 숨은 하위", "　└ 가장 깊은 곳", "• [비공개] 초안"])

    async def test_more_row_count_ignores_private_documents(self):
        tree = {"root": [page_block(f"T{i}", f"공개 {i}") for i in range(30)] + [page_block("X", "🔒 숨김")],
                **{f"T{i}": [para("x", "내용")] for i in range(30)}, "X": [para("y", "비밀")]}
        lore = load(tree)
        await lore.refresh()
        choices = lore.suggest("")
        self.assertEqual(len(choices), 25)
        self.assertEqual(choices[-1][1], MORE_VALUE)
        self.assertIn("외 7개", choices[-1][0])                            # 대표 1 + 공개 30 = 31 → 25칸 중 24 + 7
        admin = lore.suggest("", include_private=True)
        self.assertIn("외 8개", admin[-1][0])

    async def test_the_marker_list_can_be_changed(self):
        original = notion_api.PRIVATE_MARKERS
        notion_api.PRIVATE_MARKERS = ("#숨김",)
        try:
            tree = copy.deepcopy(TREE)
            tree["root"].append(page_block("Z", "#숨김 문서"))
            tree["Z"] = [para("z", "z내용")]
            lore = load(tree)
            await lore.refresh()
        finally:
            notion_api.PRIVATE_MARKERS = original
        self.assertEqual([p.label for p in lore.pages(include_private=True) if p.private], ["#숨김 문서"])
        self.assertIsNotNone(lore.get("미공개"))                           # 🔒는 더 이상 비공개 표시가 아니다.

    async def test_characters_source_has_the_same_rule(self):
        tree = {"root": [page_block("A", "공개 캐릭터"), page_block("B", "🔒 숨은 캐릭터")],
                "A": [para("a", "a")], "B": [para("b", "비밀 인물")]}
        lore = load(tree, include_root=False)
        await lore.refresh()
        self.assertEqual(lore.top_titles(), ["공개 캐릭터"])
        self.assertIsNone(lore.get("숨은 캐릭터"))
        self.assertEqual(lore.search_hits("비밀 인물"), [])
        self.assertEqual(lore.get("숨은", include_private=True).label, "🔒 숨은 캐릭터")


class SearchFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        names = {"S": SACRED, "L": "솔라리아(Solaria)", "R": "루벨리아(Rubelria)",
                 "I": "인페르누스 공화국(Infernusria res publica)", "V": "비아레카트리아(Vialaecatria)",
                 "W": "빛과 어둠", "T": "빛이 닿는 곳"}
        bodies = {"S": ["빛이 닿지 않는 곳", "별칭: 성지, Sanctum"], "L": ["태양의 땅"], "R": ["붉은 땅. 솔라리아와 이웃"],
                  "I": ["지옥의 공화국"], "V": ["숲의 나라"], "W": ["빛과   어둠의 이야기", "빛과 어둠은 하나"],
                  "T": ["이 문서는 성지를 여러 번 말한다. 성지. 성지."]}
        tree = {"root": [page_block(pid, title) for pid, title in names.items()]}
        for pid, lines in bodies.items():
            tree[pid] = [para(f"{pid}{n}", line) for n, line in enumerate(lines)]
        self.lore = load(tree)
        await self.lore.refresh()

    def labels(self, hits):
        return [h.page.label for h in hits]

    async def test_english_name_in_parentheses(self):
        self.assertEqual([n for n, _ in self.lore.suggest("sacred")], [SACRED])
        self.assertEqual([n for n, _ in self.lore.suggest("BOUNDARY")], [SACRED])
        self.assertEqual(self.lore.get("solaria").label, "솔라리아(Solaria)")
        self.assertEqual(self.lore.get("res publica").label, "인페르누스 공화국(Infernusria res publica)")
        self.assertEqual(self.labels(self.lore.search_hits("rubelria"))[0], "루벨리아(Rubelria)")

    async def test_names_without_the_parentheses_part(self):
        self.assertEqual(self.lore.get("성역의 경계").label, SACRED)
        self.assertEqual(self.lore.get("솔라리아").label, "솔라리아(Solaria)")

    async def test_aliases_from_the_page(self):
        self.assertEqual(self.lore.get("Sanctum").label, SACRED)
        self.assertEqual(self.lore.get("성지").label, SACRED)                # 본문에 '성지'가 더 많은 문서보다 별칭이 우선
        self.assertEqual([n for n, _ in self.lore.suggest("sanct")], [SACRED])
        hits = self.lore.search_hits("성지")
        self.assertEqual((hits[0].page.label, hits[0].kind), (SACRED, "alias"))   # 별칭으로 맞은 문서가 맨 앞
        self.assertEqual(hits[1].page.label, "빛이 닿는 곳")
        self.assertEqual((hits[1].kind, hits[1].count), ("text", 3))

    async def test_initial_consonants(self):
        self.assertEqual([n for n, _ in self.lore.suggest("ㅅㅇㅇㄱㄱ")], [SACRED])
        self.assertEqual([n for n, _ in self.lore.suggest("ㅅㅇㅇ ㄱㄱ")], [SACRED])
        self.assertEqual(self.lore.get("ㅅㅇㅇㄱㄱ").label, SACRED)
        self.assertEqual(self.lore.get("ㅅㄹㄹㅇ").label, "솔라리아(Solaria)")
        hits = self.lore.search_hits("ㅅㄹㄹㅇ")
        self.assertEqual((hits[0].page.label, hits[0].kind), ("솔라리아(Solaria)", "initial"))
        self.assertEqual(self.lore.get("ㅂㅇㄹㅋㅌㄹㅇ").label, "비아레카트리아(Vialaecatria)")
        self.assertEqual(self.lore.suggest("ㅋㅋㅋㅋㅋ"), [])                # 맞는 이름이 없으면 비어 있다.

    async def test_spacing_differences(self):
        self.assertEqual([n for n, _ in self.lore.suggest("성역의경계")], [SACRED])
        self.assertEqual(self.lore.get("성역의경계").label, SACRED)
        self.assertEqual(self.lore.get("인페르누스공화국").label.split("(")[0], "인페르누스 공화국")
        hits = self.lore.search_hits("빛과어둠")
        self.assertEqual(hits[0].page.label, "빛과 어둠")                    # 이름이 맞은 문서가 먼저
        self.assertEqual(hits[0].count, 2)                                  # 본문: 공백이 여럿이어도, 없어도 같은 글로 센다.
        self.assertEqual(len(self.lore.search_hits("빛과 어둠")[0].snippet) > 0, True)

    async def test_typos_fall_back_to_similar_names(self):
        self.assertEqual([n for n, _ in self.lore.suggest("솔리리아")], ["솔라리아(Solaria)"])
        self.assertEqual([n for n, _ in self.lore.suggest("인페르노스")], ["인페르누스 공화국(Infernusria res publica)"])
        self.assertIsNone(self.lore.get("솔리리아"))                          # 오타를 마음대로 열지는 않는다.
        self.assertEqual([p.label for p in self.lore.similar("솔리리아")], ["솔라리아(Solaria)"])
        hits = self.lore.search_hits("솔리리아")
        self.assertEqual([(h.page.label, h.kind) for h in hits], [("솔라리아(Solaria)", "similar")])
        self.assertEqual(self.lore.suggest("전혀 상관없는 이름"), [])
        self.assertEqual(self.lore.similar("전혀 상관없는 이름"), [])
        self.assertEqual(self.lore.search_hits("전혀 상관없는 이름"), [])

    async def test_typo_fallback_is_used_only_when_nothing_else_matches(self):
        self.assertNotIn("similar", [h.kind for h in self.lore.search_hits("솔라")])
        self.assertEqual([n for n, _ in self.lore.suggest("솔라")], ["솔라리아(Solaria)"])    # 앞부분 일치가 있으면 그것만
        self.assertEqual(self.lore.suggest("솔"), [("솔라리아(Solaria)", self.lore.suggest("솔")[0][1])])
        self.assertEqual(self.lore.suggest("q"), [])                           # 한 글자는 오타로 비교하지 않는다.
        self.assertEqual(self.lore.similar("q"), [])
        self.assertEqual({n for n, _ in self.lore.suggest("ㅋ")}, {"라시안컷(Rasyancut)", "비아레카트리아(Vialaecatria)"})   # 초성 한 글자는 초성 검색

    async def test_ranking_by_tier(self):
        # '솔라리아': 이름이 정확히 같은 문서 → 본문에만 나오는 문서(루벨리아) 순서
        hits = self.lore.search_hits("솔라리아")
        self.assertEqual(self.labels(hits), ["솔라리아(Solaria)", "루벨리아(Rubelria)"])
        self.assertEqual([h.kind for h in hits], ["title", "text"])

    async def test_hits_have_snippets_and_old_tuple_api_still_works(self):
        hit = self.lore.search_hits("지옥")[0]
        self.assertIn("지옥의 공화국", hit.snippet)
        page, snippet, count = self.lore.search("지옥")[0]
        self.assertEqual((page.label, count), ("인페르누스 공화국(Infernusria res publica)", 1))
        self.assertEqual(self.lore.search("   "), [])
        self.assertEqual(self.lore.search_hits(""), [])
        self.assertLessEqual(len(self.lore.search_hits("빛", limit=2)), 2)

    async def test_typed_path_never_uses_typo_matching(self):
        self.assertEqual(self.lore.suggest("솔리리아 / 아무거나"), [])


if __name__ == "__main__":
    unittest.main()
