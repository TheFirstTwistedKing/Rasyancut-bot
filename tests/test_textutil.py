"""노션 서식 변환과 한글 검색 도구 테스트."""
import unicodedata
import unittest

from textutil import (FUZZY_THRESHOLD, choseong, is_choseong_query, md_text, name_keys, normalize, parse_aliases,
                      plain_text, similarity)


def part(text, href=None, **annotations):
    return {"plain_text": text, "href": href, "annotations": annotations}


class MarkdownTests(unittest.TestCase):
    def test_each_annotation(self):
        self.assertEqual(md_text([part("굵게", bold=True)]), "**굵게**")
        self.assertEqual(md_text([part("기울임", italic=True)]), "*기울임*")
        self.assertEqual(md_text([part("취소", strikethrough=True)]), "~~취소~~")
        self.assertEqual(md_text([part("밑줄", underline=True)]), "__밑줄__")
        self.assertEqual(md_text([part("코드", code=True)]), "`코드`")
        self.assertEqual(md_text([part("그냥")]), "그냥")
        self.assertEqual(md_text([{"plain_text": "주석 없음"}]), "주석 없음")     # annotations 키가 없어도 된다.

    def test_combinations_nest_in_a_discord_friendly_order(self):
        self.assertEqual(md_text([part("셋", bold=True, italic=True)]), "***셋***")
        self.assertEqual(md_text([part("전부", bold=True, italic=True, strikethrough=True, underline=True)]),
                         "***~~__전부__~~***")
        self.assertEqual(md_text([part("굵은 코드", bold=True, code=True)]), "`굵은 코드`")   # 코드 안에서는 다른 서식이 없다.

    def test_whitespace_goes_outside_the_markers(self):
        # '** 굵게 **'처럼 공백이 붙으면 디스코드가 서식을 무시하므로 공백은 표시 밖으로 뺀다.
        self.assertEqual(md_text([part(" 굵게 ", bold=True), part("이어짐")]), " **굵게** 이어짐")
        self.assertEqual(md_text([part("앞", bold=True), part(" "), part("뒤", bold=True)]), "**앞** **뒤**")
        self.assertEqual(md_text([part("   ", bold=True)]), "   ")          # 공백뿐이면 표시를 만들지 않는다.
        self.assertEqual(md_text([part("", bold=True)]), "")

    def test_adjacent_segments_with_the_same_format_are_merged(self):
        self.assertEqual(md_text([part("가", bold=True), part("나", bold=True), part("다", bold=True)]), "**가나다**")
        self.assertEqual(md_text([part("가", bold=True), part("나", italic=True)]), "**가***나*")
        link = "https://example.org/a"
        self.assertEqual(md_text([part("가", link), part("나", link)]), f"[가나]({link})")
        self.assertEqual(md_text([part("가", link), part("나", "https://example.org/b")]),
                         "[가](https://example.org/a)[나](https://example.org/b)")

    def test_links(self):
        self.assertEqual(md_text([part("홈페이지", "https://example.org/x")]), "[홈페이지](https://example.org/x)")
        self.assertEqual(md_text([part("굵은 링크", "https://example.org/x", bold=True)]),
                         "[**굵은 링크**](https://example.org/x)")
        self.assertEqual(md_text([part("괄호", "https://example.org/a_(b)")]), "[괄호](https://example.org/a_(b%29)")
        self.assertEqual(md_text([part("공백", "https://example.org/a b")]), "[공백](https://example.org/a%20b)")
        for unsafe in ["javascript:alert(1)", "/3f1dfec2df6e80a59e74f56a38f035e3", "notion://page", "mailto:a@b.c", ""]:
            self.assertEqual(md_text([part("글자만", unsafe)]), "글자만", unsafe)     # 열 수 있는 http(s) 주소만 링크로 쓴다.

    def test_markdown_characters_in_the_text_are_escaped(self):
        self.assertEqual(md_text([part("snake_case_name")]), "snake\\_case\\_name")
        self.assertEqual(md_text([part("a*b*c")]), "a\\*b\\*c")
        self.assertEqual(md_text([part("||비밀||")]), "\\|\\|비밀\\|\\|")
        self.assertEqual(md_text([part("> 인용")]), "\\> 인용")
        self.assertEqual(md_text([part("# 제목")]), "\\# 제목")
        self.assertEqual(md_text([part("[링크](http://x)")]), "\\[링크](http://x)")
        self.assertEqual(md_text([part("굵은_글", bold=True)]), "**굵은\\_글**")     # 서식 안에서도 이스케이프
        self.assertEqual(md_text([part("https://example.org/a_b_c")]), "https://example.org/a_b_c")   # 주소는 그대로

    def test_code_with_backticks_and_plain_text(self):
        self.assertEqual(md_text([part("a`b", code=True)]), "`` a`b ``")
        self.assertEqual(md_text([part("a_b*c", code=True)]), "`a_b*c`")          # 코드 안은 이스케이프하지 않는다.
        self.assertEqual(plain_text([part("가", bold=True), part("나", "https://x.y"), part("다")]), "가나다")
        self.assertEqual(plain_text([]), "")


class AliasTests(unittest.TestCase):
    def test_alias_line_formats(self):
        self.assertEqual(parse_aliases("본문\n별칭: 빛의 도시, 라이트 시티\n끝"), ["빛의 도시", "라이트 시티"])
        self.assertEqual(parse_aliases("별칭：가나, 다라"), ["가나", "다라"])                  # 전각 콜론
        self.assertEqual(parse_aliases("Aliases: Foo / Bar · Baz; Qux"), ["Foo", "Bar", "Baz", "Qux"])
        self.assertEqual(parse_aliases("  이명 : 은빛 늑대 "), ["은빛 늑대"])
        self.assertEqual(parse_aliases("별명: 하나"), ["하나"])

    def test_only_a_line_that_starts_with_the_word_counts(self):
        self.assertEqual(parse_aliases("그의 별칭: 어둠"), [])
        self.assertEqual(parse_aliases("별칭이라는 단어"), [])
        self.assertEqual(parse_aliases(""), [])
        self.assertEqual(parse_aliases("별칭:  "), [])
        self.assertEqual(parse_aliases("별칭: 첫째\n별칭: 둘째"), ["첫째"])                    # 첫 줄만

    def test_limits(self):
        many = "별칭: " + ",".join(f"이름{n}" for n in range(30))
        self.assertEqual(len(parse_aliases(many)), 10)
        self.assertEqual(len(parse_aliases("별칭: " + "가" * 200)[0]), 60)


class KoreanSearchToolsTests(unittest.TestCase):
    def test_choseong(self):
        self.assertEqual(choseong("성역의 경계"), "ㅅㅇㅇㄱㄱ")
        self.assertEqual(choseong("성역의 경계(Sacred Boundary)"), "ㅅㅇㅇㄱㄱsacredboundary")
        self.assertEqual(choseong("ㅅㅇ"), "ㅅㅇ")
        self.assertEqual(choseong("꽃 뜰"), "ㄲㄸ")
        self.assertEqual(choseong("A-1!"), "a1")
        self.assertEqual(choseong(""), "")

    def test_choseong_query_detection(self):
        for query in ["ㅅㅇㅇ", "ㅅ ㅇ", "ㄲㄸ"]:
            self.assertTrue(is_choseong_query(query), query)
        for query in ["", " ", "성역", "ㅅ역", "sa", "ㅏㅓ", "ㅅ1"]:
            self.assertFalse(is_choseong_query(query), query)

    def test_similarity_catches_typos_but_not_unrelated_names(self):
        for typo, real in [("라시안켓", "라시안컷"), ("나스켄티야", "나스켄티아"), ("솔리리아", "솔라리아"),
                           ("인페르노스", "인페르누스"), ("성역의경계", "성역의 경계"), ("RASYANCUT", "rasyancut")]:
            self.assertGreaterEqual(similarity(typo, real), FUZZY_THRESHOLD, (typo, real))
        for unrelated, real in [("없는 이름", "성역의 경계"), ("빛과 어둠", "어둠과 빛"), ("사과", "비행기")]:
            self.assertLess(similarity(unrelated, real), FUZZY_THRESHOLD, (unrelated, real))

    def test_name_keys(self):
        own, extra = name_keys("성역의 경계(Sacred Boundary)", ["성지", "Sanctum"])
        self.assertEqual(own, ["성역의 경계(sacred boundary)", "성역의 경계", "sacred boundary"])
        self.assertEqual(extra, ["성지", "sanctum"])
        own, extra = name_keys("개요 (2)", [])
        self.assertEqual(own, ["개요 (2)", "개요"])                       # '(2)' 같은 중복 표시는 이름으로 쓰지 않는다.
        own, extra = name_keys("A(B)(C)", ["a"])
        self.assertEqual(own, ["a(b)(c)", "a", "b", "c"])
        self.assertEqual(extra, [])                                       # 이미 있는 이름은 별칭에서 뺀다.
        self.assertEqual(name_keys("이름", ["", "  "]), (["이름"], []))
        decomposed = unicodedata.normalize("NFD", "성역")                 # 맥 등에서 분해된 한글로 들어와도 같은 이름으로 본다.
        self.assertNotEqual(decomposed, "성역")
        self.assertEqual(normalize(f"  {decomposed} "), "성역")
        self.assertTrue(is_choseong_query(normalize("ㅅㅇ")))             # 초성(호환 자모)은 정규화로 깨지지 않는다.


if __name__ == "__main__":
    unittest.main()
