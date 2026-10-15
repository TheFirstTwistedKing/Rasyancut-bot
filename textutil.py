"""노션 글 서식을 디스코드 서식으로 바꾸고, 한글 검색(초성·오타)을 돕는 도구. 네트워크를 쓰지 않는다."""
import re
import unicodedata
from difflib import SequenceMatcher

import discord

# ---------------------------------------------------------------- 서식 변환
_ANNOTATION_KEYS = ("bold", "italic", "strikethrough", "underline", "code")


def plain_text(rich_text: list) -> str:
    """서식 없이 글자만."""
    return "".join(t.get("plain_text", "") for t in rich_text)


def _safe_url(href: str | None) -> str | None:
    """디스코드에서 열 수 있는 http(s) 주소만 링크로 쓴다. 노션 내부 상대 주소 등은 글자만 보여준다."""
    if not href or not href.lower().startswith(("http://", "https://")):
        return None
    return href.replace(")", "%29").replace(" ", "%20")


def _wrap(core: str, ann: dict, href: str | None) -> str:
    """공백을 표시 밖으로 빼서 감싼다. (`** 굵게 **`처럼 공백이 붙으면 디스코드가 서식을 무시한다.)"""
    stripped = core.strip()
    if not stripped:
        return core
    lead, trail = core[:len(core) - len(core.lstrip())], core[len(core.rstrip()):]
    if ann.get("code"):
        body = f"`` {stripped} ``" if "`" in stripped else f"`{stripped}`"
    else:
        body = discord.utils.escape_markdown(stripped)
        if ann.get("underline"):
            body = f"__{body}__"
        if ann.get("strikethrough"):
            body = f"~~{body}~~"
        if ann.get("italic"):
            body = f"*{body}*"
        if ann.get("bold"):
            body = f"**{body}**"
    url = _safe_url(href)
    if url:
        body = f"[{body}]({url})"
    return f"{lead}{body}{trail}"


def md_text(rich_text: list) -> str:
    """노션의 굵게·기울임·취소선·밑줄·코드·링크를 디스코드 서식으로 바꾼다. 글 속의 서식 문자는 이스케이프한다."""
    groups: list[tuple[tuple, str]] = []
    for part in rich_text:
        ann = part.get("annotations") or {}
        key = tuple(bool(ann.get(k)) for k in _ANNOTATION_KEYS) + (part.get("href"),)
        text = part.get("plain_text", "")
        if groups and groups[-1][0] == key:      # 같은 서식이 이어지면 합쳐서 `****` 같은 깨진 표시를 막는다.
            groups[-1] = (key, groups[-1][1] + text)
        else:
            groups.append((key, text))
    out = []
    for key, text in groups:
        ann = dict(zip(_ANNOTATION_KEYS, key[:-1]))
        out.append(_wrap(text, ann, key[-1]))
    return "".join(out)


def escape_plain(text: str) -> str:
    return discord.utils.escape_markdown(text)


# ---------------------------------------------------------------- 별칭
_ALIAS_LINE = re.compile(r"^\s*(?:별칭|이명|별명|aliases?)\s*[:：]\s*(.+?)\s*$", re.I)
_ALIAS_SPLIT = re.compile(r"[,，、/·;；]")


def parse_aliases(plain: str, limit: int = 10) -> list[str]:
    """본문에 `별칭: A, B` 한 줄이 있으면 별칭 목록으로 쓴다. (그 줄은 본문에도 그대로 보인다.)"""
    for line in plain.split("\n"):
        match = _ALIAS_LINE.match(line)
        if match:
            names = [n.strip() for n in _ALIAS_SPLIT.split(match.group(1))]
            return [n[:60] for n in names if n][:limit]
    return []


# ---------------------------------------------------------------- 검색 정규화·초성·오타
_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
FUZZY_THRESHOLD = 0.78


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower().strip()


def choseong(text: str) -> str:
    """'성역의 경계(Sacred)' -> 'ㅅㅇㅇㄱㄱsacred'. 공백·기호는 뺀다."""
    out = []
    for ch in text:
        if "가" <= ch <= "힣":
            out.append(_CHO[(ord(ch) - 0xAC00) // 588])
        elif "ㄱ" <= ch <= "ㅎ":
            out.append(ch)
        elif ch.isalnum():
            out.append(ch.lower())
    return "".join(out)


def is_choseong_query(query: str) -> bool:
    compact = query.replace(" ", "")
    return bool(compact) and all("ㄱ" <= c <= "ㅎ" for c in compact)


def similarity(a: str, b: str) -> float:
    """자모 단위 비교라서 '켓/컷'처럼 모음 하나가 다른 오타도 높게 나온다."""
    return SequenceMatcher(None, unicodedata.normalize("NFD", normalize(a)),
                           unicodedata.normalize("NFD", normalize(b))).ratio()


def name_keys(label: str, aliases: list[str]) -> tuple[list[str], list[str]]:
    """검색에 쓸 이름들. (제목에서 나온 것, 별칭에서 나온 것)을 따로 돌려준다.

    '성역의 경계(Sacred Boundary)' -> 제목 쪽: 전체, '성역의 경계', 'sacred boundary'
    """
    own = [normalize(label)]
    base = re.sub(r"\s*\([^()]*\)", "", label).strip()
    if base and normalize(base) not in own:
        own.append(normalize(base))
    for inner in re.findall(r"\(([^()]+)\)", label):
        inner = inner.strip()
        if inner and not inner.isdigit() and normalize(inner) not in own:   # '(2)' 같은 중복 표시는 제외
            own.append(normalize(inner))
    extra = [normalize(a) for a in aliases if a.strip()]
    return own, [a for a in extra if a not in own]
