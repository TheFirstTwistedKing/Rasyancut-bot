"""하위 페이지 전체 읽기 · '내용 없음' 표시 테스트. 실제 노션에 연결하지 않고 가짜 데이터로 검증한다."""
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import notion_api
from cogs.character import Character
from cogs.lore import Lore
from cogs.search import Search
from helpers import send_paged
from notion_api import EMPTY, NotionLore, short_name


def rt(s):
    return [{"plain_text": s}]


def para(i, text):
    return {"id": i, "type": "paragraph", "has_children": False, "paragraph": {"rich_text": rt(text)}}


def page_block(i, title):
    return {"id": i, "type": "child_page", "has_children": True, "child_page": {"title": title}}


def bullet(i, text, children=False):
    return {"id": i, "type": "bulleted_list_item", "has_children": children,
            "bulleted_list_item": {"rich_text": rt(text)}}


def link(i, target):
    return {"id": i, "type": "link_to_page", "has_children": False,
            "link_to_page": {"type": "page_id", "page_id": target}}


# 루트 ─ 빛과 어둠(A) ─ 나스켄티아(A1) ─ 동쪽 지점(A1a, 비어 있음)
#                      └ 서쪽 지점(A1b)
#      ─ 비어 있는 문서(B)
#      ─ 목차만 있는 문서(C) ─ 하위(C1)
#      ─ 개요(D) / 개요(E)  (이름이 같은 문서)
#      ─ 링크로 연결된 문서(L) ─ 링크 하위(L1)
TREE = {
    "root": [para("r1", "라시안컷은 차원 단위다."), page_block("A", "빛과 어둠"), page_block("B", "비어 있는 문서"),
             page_block("C", "목차만 있는 문서"), page_block("D", "개요"), page_block("E", "개요"), link("lk", "L")],
    "A": [para("a1", "빛과 어둠 본문."), page_block("A1", "나스켄티아"), para("a2", "빛과 어둠 마지막 문장.")],
    "A1": [para("x1", "나스켄티아에는 두 지점이 있다."), page_block("A1a", "동쪽 지점"), page_block("A1b", "서쪽 지점")],
    "A1a": [],
    "A1b": [para("w1", "서쪽 지점은 어둠이 시작되는 곳."), bullet("w2", "토글 항목", True)],
    "w2": [para("w3", "토글 안의 글")],
    "B": [],
    "C": [page_block("C1", "하위 하나")],
    "C1": [para("c1", "하위 하나의 내용")],
    "D": [para("d1", "첫 번째 개요")],
    "E": [para("e1", "두 번째 개요")],
    "L": [para("l1", "링크로 연결된 문서"), page_block("L1", "링크 하위")],
    "L1": [para("m1", "링크 하위 내용")],
}
TITLES = {"root": "라시안컷(Rasyancut)", "L": "링크 문서"}


def make_lore(tree=None, **kw):
    tree = tree or TREE
    lore = NotionLore("fake", "root", **kw)

    async def fake_call(func, *a, **k):
        pid = k["page_id"]
        return {"properties": {"title": {"type": "title", "title": rt(TITLES[pid])}}}

    async def fake_children(block_id):
        value = tree[block_id]
        if isinstance(value, Exception):
            raise value
        return value
    lore._call, lore._children = fake_call, fake_children
    return lore


class TreeReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lore = make_lore()
        await self.lore.refresh()

    async def test_every_level_is_registered_in_tree_order(self):
        self.assertEqual(self.lore.titles(), [
            '라시안컷(Rasyancut)', '빛과 어둠', '빛과 어둠 / 나스켄티아', '빛과 어둠 / 나스켄티아 / 동쪽 지점',
            '빛과 어둠 / 나스켄티아 / 서쪽 지점', '비어 있는 문서', '목차만 있는 문서', '목차만 있는 문서 / 하위 하나',
            '개요', '개요 (2)', '링크 문서', '링크 문서 / 링크 하위'])
        self.assertEqual(self.lore.top_titles(), ['라시안컷(Rasyancut)', '빛과 어둠', '비어 있는 문서', '목차만 있는 문서',
                                                  '개요', '개요 (2)', '링크 문서'])

    async def test_parent_shows_all_descendants_in_document_order(self):
        text = self.lore.render(self.lore.get('빛과 어둠'))
        order = [text.index(s) for s in ['빛과 어둠 본문.', '📄 나스켄티아', '나스켄티아에는 두 지점이 있다.',
                                         '└ 📄 동쪽 지점', '└ 📄 서쪽 지점', '서쪽 지점은 어둠이 시작되는 곳.',
                                         '토글 안의 글', '빛과 어둠 마지막 문장.']]
        self.assertEqual(order, sorted(order))           # 본문 중간에 있던 하위 페이지가 제자리에 나온다.
        self.assertIn('토글 안의 글', text)                 # 토글 안의 글도 읽는다.

    async def test_empty_pages_say_nothing_here(self):
        self.assertEqual(self.lore.render(self.lore.get('비어 있는 문서')), EMPTY)
        text = self.lore.render(self.lore.get('빛과 어둠'))
        east = text.index('└ 📄 동쪽 지점')
        self.assertTrue(text[east:].split('\n', 1)[1].lstrip().startswith(EMPTY))   # 빈 하위 페이지 밑에 표시
        self.assertEqual(self.lore.render(self.lore.get('빛과 어둠 / 나스켄티아 / 동쪽 지점')), EMPTY)

    async def test_page_with_only_subpages_is_not_called_empty(self):
        text = self.lore.render(self.lore.get('목차만 있는 문서'))
        self.assertIn('하위 하나의 내용', text)
        self.assertNotIn(EMPTY, text)

    async def test_nested_page_opens_by_itself_with_its_own_descendants(self):
        text = self.lore.render(self.lore.get('빛과 어둠 / 나스켄티아'))
        self.assertIn('📄 동쪽 지점', text)                 # 이 페이지 기준 1단계 표시
        self.assertNotIn('└ 📄', text.split('📄 동쪽 지점')[0])
        self.assertNotIn('빛과 어둠 본문', text)             # 상위 내용은 섞이지 않는다.

    async def test_root_lists_children_without_inlining_them(self):
        text = self.lore.render(self.lore.get('라시안컷'))
        self.assertIn('라시안컷은 차원 단위다.', text)
        self.assertIn('📄 빛과 어둠', text)
        self.assertNotIn('빛과 어둠 본문', text)

    async def test_linked_pages_also_bring_their_subpages(self):
        self.assertIn('링크 하위 내용', self.lore.render(self.lore.get('링크 문서')))

    async def test_lookup_by_id_path_leaf_and_partial(self):
        by_path = self.lore.get('빛과 어둠 / 나스켄티아')
        self.assertEqual(by_path.id, 'A1')
        self.assertIs(self.lore.get('A1'), by_path)                       # 자동완성 값(id)
        self.assertIs(self.lore.get('나스켄티아'), by_path)                # 마지막 이름만
        self.assertIs(self.lore.get('서쪽'), self.lore.get('빛과 어둠 / 나스켄티아 / 서쪽 지점'))
        self.assertIsNone(self.lore.get('없는 문서'))
        self.assertIsNone(self.lore.get('  '))
        self.assertEqual(self.lore.get('개요 (2)').id, 'E')              # 이름이 같은 문서는 구분된다.

    async def test_autocomplete_suggestions(self):
        names = dict(self.lore.suggest('나스'))
        self.assertEqual(list(names), ['나스켄티아'])                    # 상위 이름 없이 이 문서 이름만
        everything = self.lore.suggest('')
        self.assertEqual(len(everything), 12)
        self.assertTrue(all(len(n) <= 100 and ' / ' not in n for n, _ in everything))
        self.assertEqual(self.lore.suggest('개요')[0][0], '개요')            # 앞부분 일치가 먼저
        self.assertEqual(self.lore.get(self.lore.suggest('서쪽')[0][1]).title, '서쪽 지점')

    async def test_search_hits_the_page_that_owns_the_text_only(self):
        hits = [p.path for p, _, _ in self.lore.search('두 지점')]
        self.assertEqual(hits, ['빛과 어둠 / 나스켄티아'])                  # 상위 페이지가 중복으로 나오지 않는다.
        hits = {p.path: s for p, s, _ in self.lore.search('동쪽')}
        self.assertEqual(hits['빛과 어둠 / 나스켄티아 / 동쪽 지점'], EMPTY)   # 제목만 맞고 내용이 없는 페이지

    async def test_title_match_in_search_uses_only_the_pages_own_name(self):
        labels = {p.label for p, _, _ in self.lore.search('빛과 어둠')}
        self.assertEqual(labels, {'빛과 어둠'})   # 상위 이름이 같다는 이유로 하위 문서가 따라 나오지 않는다.

    async def test_tree_listing_is_indented(self):
        lines = self.lore.tree_lines()
        self.assertEqual(lines[0], '• 라시안컷(Rasyancut)')
        self.assertEqual(lines[2], '└ 나스켄티아')
        self.assertEqual(lines[3], '　└ 동쪽 지점')


LONG = "페이스리아와 오비투아리바의 차원의 틈(Interspace of Faithria and Obitoariba)"
LONG_TREE = {
    "root": [page_block("g", "차원의 틈(Interspace)")],
    "g": [page_block("gw", "관문(Gateway)")],
    "gw": [page_block("rift", "협곡 관문(Rift - Gateway)")],
    "rift": [page_block("f", LONG)],
    "f": [page_block("k1", "첫 번째 하위"), page_block("k2", "두 번째 하위"), page_block("k3", "세 번째 하위")],
    "k1": [para("a", "1")], "k2": [para("b", "2")], "k3": [para("c", "3")],
}


class LongNameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lore = make_lore(LONG_TREE)
        await self.lore.refresh()

    async def test_list_shows_only_each_pages_own_name(self):
        names = [name for name, _ in self.lore.suggest('')]
        self.assertEqual(names, ['라시안컷(Rasyancut)', '차원의 틈(Interspace)', '관문(Gateway)', '협곡 관문(Rift - Gateway)',
                                 LONG, '첫 번째 하위', '두 번째 하위', '세 번째 하위'])
        self.assertTrue(all(len(n) <= 100 for n in names))

    async def test_short_name_rules(self):
        self.assertEqual(short_name('가 / 나'), '가 / 나')
        self.assertEqual(short_name('x' * 100), 'x' * 100)
        cut = short_name('가' * 150)
        self.assertEqual((len(cut), cut[-1]), (100, '…'))

    async def test_query_matches_only_the_pages_own_name(self):
        labels = [self.lore.get(v).label for _, v in self.lore.suggest('관문')]
        # 이름에 '관문'이 있는 두 문서만. 상위 이름이 '관문'이라서 걸리던 하위 문서는 나오지 않는다.
        self.assertEqual(labels, ['관문(Gateway)', '협곡 관문(Rift - Gateway)'])
        self.assertEqual(self.lore.suggest('없는 이름'), [])

    async def test_typing_a_path_still_narrows_to_that_branch(self):
        query = '차원의 틈(Interspace) / 관문(Gateway) / 협곡'
        labels = [self.lore.get(v).label for _, v in self.lore.suggest(query)]
        self.assertEqual(labels, ['협곡 관문(Rift - Gateway)', LONG, '첫 번째 하위', '두 번째 하위', '세 번째 하위'])


class RankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_starting_with_the_query_comes_first_then_names_containing_it(self):
        tree = {
            "root": [page_block("A", "아 모음"), page_block("B", "모음 두번째"), page_block("C", "관련 없음")],
            "A": [page_block("A1", "가나다")], "A1": [para("a", "1")], "B": [para("b", "2")], "C": [para("c", "3")],
        }
        lore = make_lore(tree)
        await lore.refresh()
        labels = [lore.get(v).label for _, v in lore.suggest('모음')]
        self.assertEqual(labels, ['모음 두번째', '아 모음'])   # 시작 일치가 먼저, 포함은 그 다음, '가나다'는 제외

    async def test_same_name_under_the_same_parent_stays_distinguishable(self):
        tree = {"root": [page_block("P", "상위")], "P": [page_block("X1", "개요"), page_block("X2", "개요")],
                "X1": [para("a", "첫째")], "X2": [para("b", "둘째")]}
        lore = make_lore(tree)
        await lore.refresh()
        self.assertEqual([n for n, _ in lore.suggest('개요')], ['개요', '개요 (2)'])
        self.assertEqual(lore.get('개요 (2)').id, 'X2')
        self.assertEqual(lore.render(lore.get('개요 (2)')), '둘째')

    async def test_same_name_under_different_parents_opens_the_right_page_by_id(self):
        tree = {"root": [page_block("P", "상위 하나"), page_block("Q", "상위 둘")],
                "P": [page_block("X1", "개요")], "Q": [page_block("X2", "개요")],
                "X1": [para("a", "하나의 개요")], "X2": [para("b", "둘의 개요")]}
        lore = make_lore(tree)
        await lore.refresh()
        choices = lore.suggest('개요')
        self.assertEqual([n for n, _ in choices], ['개요', '개요'])   # 화면에는 이름만 보인다.
        self.assertEqual([lore.render(lore.get(v)) for _, v in choices], ['하나의 개요', '둘의 개요'])


class LimitsAndFailuresTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreadable_subpage_does_not_break_the_parent(self):
        tree = {**TREE, "A1b": RuntimeError("접근 불가")}
        lore = make_lore(tree)
        await lore.refresh()
        text = lore.render(lore.get('빛과 어둠'))
        self.assertIn('서쪽 지점 (읽지 못함)', text)
        self.assertIn('나스켄티아에는 두 지점이 있다.', text)
        self.assertNotIn('빛과 어둠 / 나스켄티아 / 서쪽 지점', lore.titles())

    async def test_depth_limit(self):
        original = notion_api.MAX_PAGE_DEPTH
        notion_api.MAX_PAGE_DEPTH = 1
        try:
            lore = make_lore()
            await lore.refresh()
        finally:
            notion_api.MAX_PAGE_DEPTH = original
        self.assertIn('빛과 어둠 / 나스켄티아', lore.titles())
        self.assertNotIn('빛과 어둠 / 나스켄티아 / 동쪽 지점', lore.titles())
        self.assertIn('동쪽 지점 (더 내려가지 않음)', lore.render(lore.get('빛과 어둠')))

    async def test_page_budget_limit(self):
        original = notion_api.MAX_PAGES
        notion_api.MAX_PAGES = 4
        try:
            lore = make_lore()
            await lore.refresh()
        finally:
            notion_api.MAX_PAGES = original
        self.assertLessEqual(len(lore.titles()), 4)

    async def test_character_instance_excludes_the_index_page(self):
        lore = make_lore(include_root=False)
        await lore.refresh()
        self.assertNotIn('라시안컷(Rasyancut)', lore.titles())
        self.assertIn('빛과 어둠 / 나스켄티아', lore.titles())

    async def test_cycle_like_links_cannot_loop(self):
        # 하위 페이지가 상위를 링크해도 child_page 블록만 따라 내려가므로 반복되지 않는다.
        tree = {**TREE, "A1b": [link("back", "A"), para("w1", "본문")]}
        lore = make_lore(tree)
        await lore.refresh()
        self.assertEqual(len(lore.titles()), 12)


class CommandOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lore = make_lore()
        await self.lore.refresh()   # 봇은 시작할 때 한 번 불러온다. 자동완성은 이 캐시만 읽는다.
        self.bot = NS(lore=self.lore, characters=self.lore)
        self.i = NS(response=NS(defer=AsyncMock(), is_done=lambda: True), followup=NS(send=AsyncMock()))

    def sent_embed(self):
        return self.i.followup.send.call_args.kwargs['embed']

    async def test_setting_command_shows_nested_content_and_path_title(self):
        cog = Lore(self.bot)
        await Lore.show.callback(cog, self.i, 'A1')
        embed = self.sent_embed()
        self.assertEqual(embed.title, '나스켄티아')   # 상위 이름 없이 이 문서 이름만
        self.assertIn('서쪽 지점은 어둠이 시작되는 곳.', embed.description)
        self.assertIn(EMPTY, embed.description)

    async def test_setting_command_on_an_empty_page(self):
        await Lore.show.callback(Lore(self.bot), self.i, '비어 있는 문서')
        self.assertEqual(self.sent_embed().description, EMPTY)

    async def test_missing_page_message_and_list_command(self):
        cog = Lore(self.bot)
        await Lore.show.callback(cog, self.i, '존재하지 않음')
        self.assertIn('찾지 못했어요', self.i.followup.send.call_args.args[0])
        await Lore.list_docs.callback(cog, self.i)
        description = self.sent_embed().description
        self.assertIn('└ 나스켄티아', description)
        self.assertIn('읽으려면 /설정 이름', description)

    async def test_character_and_search_commands(self):
        await Character.show.callback(Character(self.bot), self.i, '링크 하위')
        self.assertIn('링크 하위 내용', self.sent_embed().description)
        self.assertEqual(self.sent_embed().title, '링크 하위')   # 상위 이름 없이 이 문서 이름만
        await Search.search.callback(Search(self.bot), self.i, '두 지점')
        description = self.sent_embed().description
        self.assertIn('**나스켄티아**', description)
        self.assertNotIn('빛과 어둠 /', description)
        self.assertEqual(description.count('**'), 2)   # 결과가 한 건만 나온다.

    async def test_autocomplete_returns_ids_as_values(self):
        cog = Lore(self.bot)
        choices = await Lore.show_autocomplete(cog, self.i, '서쪽')
        self.assertEqual([c.name for c in choices], ['서쪽 지점'])
        self.assertEqual(choices[0].value, 'a1b')   # 값은 정규화한 문서 id라서 긴 이름이어도 정확히 열린다.
        self.assertEqual(self.lore.get(choices[0].value).title, '서쪽 지점')
        self.assertLessEqual(len(await Lore.show_autocomplete(cog, self.i, '')), 25)

    async def test_long_tree_text_is_paginated_not_cut(self):
        tree = {**TREE, "A1b": [para(f"p{n}", "가" * 900) for n in range(12)]}
        lore = make_lore(tree)
        await lore.refresh()
        text = lore.render(lore.get('빛과 어둠'))
        await send_paged(self.i, '제목', text)
        view = self.i.followup.send.call_args.kwargs['view']
        self.assertGreater(len(view.chunks), 1)
        self.assertEqual(sum(c.count('가') for c in view.chunks), 12 * 900)
        view.stop()
        await send_paged(self.i, '제목', '')
        self.assertEqual(self.sent_embed().description, '내용 없음')


if __name__ == '__main__':
    unittest.main()
