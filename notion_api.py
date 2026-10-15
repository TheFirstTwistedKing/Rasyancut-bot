"""노션 세계관 페이지를 읽어 오는 모듈.

루트 페이지 아래의 문서와 그 하위 페이지를 끝까지(단계 제한 내에서) 따라 내려가 읽는다.
- 모든 페이지는 '상위 / 하위' 경로 이름으로 따로 조회할 수 있다.
- 기본은 그 문서에 쓰인 내용만 보여주고, 하위 페이지는 이름(버튼)으로만 이어 준다.
- 노션의 굵게·기울임·링크 등 서식과 이미지를 살려서 보여준다.
- 제목에 비공개 표시(🔒)가 있는 문서와 그 하위 문서는 관리자 외에는 어디에도 나오지 않는다.
- 검색은 영문명·별칭·초성·오타·띄어쓰기 차이까지 찾는다.
- 내용이 없는 페이지는 '내용 없음'으로 표시한다.
"""
import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field

from notion_client import AsyncClient
from notion_client.errors import APIResponseError
from notion_client.helpers import async_collect_paginated_api

from textutil import (FUZZY_THRESHOLD, choseong, escape_plain, is_choseong_query, md_text, name_keys, normalize,
                      parse_aliases, plain_text, similarity)

VERSION = "2026-10-15 서식·이미지·비공개·검색 개선"   # 시작할 때 콘솔에 찍혀, 어떤 버전이 실행 중인지 확인할 수 있다.
# False: 문서를 열면 그 문서에 쓰인 내용만 보여주고 하위 페이지는 '📄 이름'으로만 표시한다.
# True : 하위 페이지의 내용까지 본문 순서대로 함께 보여준다. (하위 페이지는 어느 쪽이든 /설정 이름 으로 따로 열 수 있다.)
INLINE_SUBPAGES = False
CACHE_TTL = 300        # 초. 노션 수정 내용이 최대 5분 뒤 반영돼요.
MAX_DEPTH = 3          # 토글·들여쓰기 같은 하위 블록을 읽는 깊이
MAX_PAGE_DEPTH = 6     # 페이지 안의 페이지를 따라 내려가는 최대 단계
MAX_PAGES = 300        # 한 번에 읽을 최대 문서 수 (노션 요청 제한과 대기 시간 보호)
IMAGE_URL_LIFETIME = 1800   # 초. 노션이 올린 이미지 주소는 1시간쯤 뒤 만료되므로 이보다 오래되면 열 때 새로 받는다.
EMPTY = "내용 없음"
MORE_VALUE = "__more__"   # 자동완성 마지막 '더 있어요' 줄을 고르면 /설정이 받는 값
CHILD_MARK = "\x00child:"   # 본문 속 하위 페이지 자리를 나타내는 내부 표식
LIST_MARK = "\x00list:"     # 봇이 덧붙인 '하위 문서 이름' 줄. 화면에는 보이지만 검색 대상이 아니다.
IMG_MARK = "\x00img:"       # 본문 속 이미지 자리를 나타내는 내부 표식
# 제목에 이 표시가 들어 있으면 비공개 문서다. 환경변수 PRIVATE_MARKERS(쉼표 구분)로 바꿀 수 있다.
PRIVATE_MARKERS = tuple(m.strip() for m in (os.getenv("PRIVATE_MARKERS") or "🔒,[비공개]").split(",") if m.strip())


def is_private_title(title: str) -> bool:
    return any(marker in title for marker in PRIVATE_MARKERS)


@dataclass
class ImageRef:
    block_id: str
    container_id: str     # 이 이미지가 들어 있는 블록(보통 페이지 자신). 주소를 새로 받을 때 다시 읽는 대상.
    url: str
    caption: str
    expiring: bool        # 노션이 올린 파일이면 주소가 만료된다. 외부 주소는 그대로 쓴다.
    fetched: float


@dataclass
class LorePage:
    id: str
    title: str      # 이 페이지의 이름
    path: str       # '상위 / 하위' 형태의 전체 이름. 최상위 문서는 title과 같다.
    depth: int      # 0 = 최상위 문서
    raw: str        # 본문(디스코드 서식). 하위 페이지·이미지 자리는 내부 표식으로 남겨 둔다.
    label: str = ""  # 화면에 보여줄 이름. 상위 이름 없이 이 페이지 이름만. 같은 상위 아래 이름이 겹치면 '(2)'가 붙는다.
    plain: str = ""  # 서식 없는 이 페이지만의 본문(검색용)
    images: list = field(default_factory=list)
    private: bool = False
    parent: str | None = None     # 상위 문서의 path. 최상위 문서는 대표 페이지(있을 때) 또는 None.
    aliases: list = field(default_factory=list)
    digest: str = ""       # 본문이 바뀌었는지 알아보는 지문. 이미지 주소는 읽을 때마다 달라지므로 넣지 않는다.

    def __post_init__(self):
        if not self.label:
            self.label = self.title
        self.keys, self.alias_keys = name_keys(self.label, self.aliases)


@dataclass
class Hit:
    page: LorePage
    snippet: str
    count: int
    kind: str      # text(본문) · title(이름) · alias(별칭) · initial(초성) · similar(비슷한 이름)


@dataclass
class _Ctx:
    path: str
    level: int
    registry: dict
    budget: list
    descend: bool = True   # False면 하위 페이지를 읽지 않고 이름만 적는다(루트 본문용)
    images: list = field(default_factory=list)
    private: bool = False


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return plain_text(prop["title"]) or "(제목 없음)"
    return "(제목 없음)"


def _norm(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def _unique(taken, path: str) -> str:
    if path not in taken:
        return path
    n = 2
    while f"{path} ({n})" in taken:
        n += 1
    return f"{path} ({n})"


def short_name(label: str, limit: int = 100) -> str:
    """디스코드 선택지 이름은 100자까지다. 넘으면 끝을 줄인다."""
    return label if len(label) <= limit else label[:limit - 1] + "…"


def _clean(lines: list[str]) -> str:
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _image_url(block: dict) -> tuple[str, bool] | None:
    data = block.get("image") or {}
    if data.get("type") == "external":
        return (data.get("external") or {}).get("url"), False
    if data.get("type") == "file":
        return (data.get("file") or {}).get("url"), True
    return None


def _quote(md: str) -> str:
    return "\n".join(f"> {line}" for line in md.split("\n"))


class NotionLore:
    def __init__(self, token: str, root_page_id: str, include_root: bool = True):
        self.client = AsyncClient(auth=token)
        self.root_id = root_page_id
        # True면 루트 페이지 본문도 하나의 문서로 취급 (캐릭터 목록 페이지는 False)
        self.include_root = include_root
        self._pages: dict[str, LorePage] = {}
        self._by_id: dict[str, str] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        # 노션 API는 평균 초당 3회 제한이라 동시 요청과 간격을 제한한다.
        self._sem = asyncio.Semaphore(2)

    # ---------------- 노션 호출 ----------------
    async def _call(self, func, *args, **kwargs):
        for attempt in range(4):
            try:
                async with self._sem:
                    result = await func(*args, **kwargs)
                    await asyncio.sleep(0.4)
                return result
            except APIResponseError as e:
                if e.code == "rate_limited" and attempt < 3:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise

    async def _children(self, block_id: str) -> list[dict]:
        return await self._call(
            async_collect_paginated_api,
            self.client.blocks.children.list,
            block_id=block_id,
        )

    # ---------------- 읽기 ----------------
    def _make_page(self, pid, title, path, depth, lines, plain_lines, ctx, label=None, parent=None):
        plain = _clean(plain_lines)
        raw = _clean(lines)
        # 지문에는 이 페이지에 직접 쓴 내용만 넣는다. 하위 문서가 생기거나 이름이 바뀌어도 상위 문서가 수정된 것으로 보지 않는다.
        written = "\n".join(ln for ln in raw.split("\n") if not ln.startswith((CHILD_MARK, LIST_MARK)))
        fingerprint = "\n".join([label or title, written] + [im.block_id + im.caption for im in ctx.images])
        return LorePage(pid, title, path, depth, raw, label or "", plain, ctx.images, ctx.private, parent,
                        parse_aliases(plain), hashlib.sha1(fingerprint.encode()).hexdigest()[:16])

    async def _child_page(self, b: dict, ctx: _Ctx) -> str:
        """하위 페이지를 읽어 registry에 등록하고, 본문에 끼울 표식 줄을 돌려준다."""
        title = b["child_page"].get("title") or "(제목 없음)"
        if not ctx.descend:
            return LIST_MARK + title
        if ctx.level + 1 > MAX_PAGE_DEPTH or ctx.budget[0] <= 0:
            return f"{LIST_MARK}{title} (더 내려가지 않음)"
        ctx.budget[0] -= 1
        path = _unique(ctx.registry, f"{ctx.path} / {title}")
        ctx.registry[path] = None  # 트리 순서를 지키기 위해 자리를 먼저 예약
        child = _Ctx(path, ctx.level + 1, ctx.registry, ctx.budget, True, [], ctx.private or is_private_title(title))
        try:
            lines, plain_lines = await self._blocks_to_lines(b["id"], 0, child)
        except Exception as exc:  # 한 페이지를 못 읽어도 나머지는 계속 보여준다.
            logging.warning("하위 페이지를 읽지 못했어요: %s (%r)", path, exc)
            ctx.registry.pop(path, None)
            return f"{LIST_MARK}{title} (읽지 못함)"
        label = path[len(ctx.path) + 3:]   # '상위 / ' 뒤의 이름(겹침 표시 포함)
        ctx.registry[path] = self._make_page(b["id"], title, path, ctx.level + 1, lines, plain_lines, child,
                                             label, ctx.path)
        return CHILD_MARK + path

    async def _blocks_to_lines(self, block_id: str, depth: int, ctx: _Ctx) -> tuple[list[str], list[str]]:
        """(디스코드 서식 줄들, 서식 없는 줄들)"""
        lines: list[str] = []
        plain_lines: list[str] = []
        number = 0

        def add(display: str, plain: str | None = None):
            lines.append(display)
            plain_lines.append(display if plain is None else plain)

        for b in await self._children(block_id):
            t = b["type"]
            data = b.get(t, {})
            rich = data.get("rich_text", [])
            md, plain = md_text(rich), plain_text(rich)
            pad = "  " * depth
            number = number + 1 if t == "numbered_list_item" else 0

            if t == "heading_1":
                add("", ""); add(f"__**{escape_plain(plain)}**__", plain)
            elif t in ("heading_2", "heading_3"):
                add("", ""); add(f"**{escape_plain(plain)}**", plain)
            elif t == "bulleted_list_item":
                add(f"{pad}• {md}", f"{pad}{plain}")
            elif t == "numbered_list_item":
                add(f"{pad}{number}. {md}", f"{pad}{plain}")
            elif t == "to_do":
                add(f"{pad}{'☑' if data.get('checked') else '☐'} {md}", f"{pad}{plain}")
            elif t in ("quote", "callout"):
                add(_quote(md), plain)
            elif t == "divider":
                add("────────", "")
            elif t == "code":
                add("```\n" + plain.replace("```", "``\u200b`") + "\n```", plain)
            elif t == "table_row":
                add(" | ".join(md_text(c) for c in data.get("cells", [])),
                    " | ".join(plain_text(c) for c in data.get("cells", [])))
            elif t == "child_page":
                lines.append(await self._child_page(b, ctx))
            elif t in ("paragraph", "toggle"):
                add(f"{pad}{md}", f"{pad}{plain}")
            elif t == "image":
                found = _image_url(b)
                if found and found[0]:
                    caption = plain_text(data.get("caption", []))
                    ctx.images.append(ImageRef(b["id"], block_id, found[0], caption, found[1], time.time()))
                    lines.append(f"{IMG_MARK}{len(ctx.images) - 1}")
                    if caption:
                        plain_lines.append(caption)
            elif t in ("bookmark", "embed", "link_preview") and data.get("url"):
                add(f"🔗 {data['url']}", "")

            if b.get("has_children") and t != "child_page" and depth < MAX_DEPTH:
                sub_lines, sub_plain = await self._blocks_to_lines(b["id"], depth + 1, ctx)
                lines += sub_lines
                plain_lines += sub_plain
        return lines, plain_lines

    async def refresh(self, force: bool = False):
        async with self._lock:
            fresh = time.time() - self._loaded_at < CACHE_TTL
            if self._pages and fresh and not force:
                return

            root = await self._call(self.client.pages.retrieve, page_id=self.root_id)
            root_key = _norm(self.root_id)

            # 1단계: 최상위 문서 후보를 모은다. id -> 제목(없으면 None)
            targets: dict[str, str | None] = {}

            def candidate(pid, title):
                targets[pid] = targets.get(pid) or title

            for b in await self._children(self.root_id):
                t = b["type"]
                data = b.get(t, {})
                if t == "child_page":
                    candidate(b["id"], data.get("title") or None)
                elif t == "link_to_page" and data.get("type") == "page_id":
                    candidate(data["page_id"], None)
                for r in data.get("rich_text", []):
                    m = r.get("mention")
                    if m and m.get("type") == "page":
                        candidate(m["page"]["id"], r.get("plain_text") or None)
            targets = {k: v for k, v in targets.items() if _norm(k) != root_key}

            # 2단계: 최상위 문서의 이름(경로)을 겹치지 않게 정한다.
            tops: list[tuple] = []   # (id, path, 하위 페이지를 읽을지, 비공개 여부, 상위 path)
            taken: set[str] = set()
            root_path = None
            if self.include_root:
                root_path = _unique(taken, _page_title(root))
                taken.add(root_path)
                tops.append((self.root_id, root_path, False, is_private_title(root_path), None))
            for pid, title in targets.items():
                if not title:
                    title = _page_title(await self._call(self.client.pages.retrieve, page_id=pid))
                path = _unique(taken, title)
                taken.add(path)
                tops.append((pid, path, True, is_private_title(title), root_path))

            # 3단계: 문서마다 하위 페이지까지 읽는다. 문서끼리는 동시에, 문서 안에서는 차례로.
            budget = [MAX_PAGES]

            async def load(pid, path, descend, private, parent) -> dict[str, LorePage]:
                if budget[0] <= 0:
                    raise RuntimeError("문서 수 제한")
                budget[0] -= 1
                registry: dict[str, LorePage] = {}
                ctx = _Ctx(path, 0, registry, budget, descend, [], private)
                lines, plain_lines = await self._blocks_to_lines(pid, 0, ctx)
                top = self._make_page(pid, path, path, 0, lines, plain_lines, ctx, None, parent)
                return {path: top, **registry}

            results = await asyncio.gather(*(load(*t) for t in tops), return_exceptions=True)
            pages: dict[str, LorePage] = {}
            for result in results:
                if isinstance(result, dict):
                    pages.update(result)
                else:
                    logging.warning("문서 하나를 불러오지 못했어요: %r", result)
            if budget[0] <= 0:
                logging.warning("문서 수 제한(%d개)에 도달해 일부 하위 페이지를 읽지 않았어요.", MAX_PAGES)

            self._pages = pages
            self._by_id = {_norm(p.id): path for path, p in pages.items()}
            self._loaded_at = time.time()
            logging.info("노션 읽기 모듈 버전: %s / 문서 %d개 (비공개 %d개)", VERSION, len(pages),
                         sum(p.private for p in pages.values()))

    async def fresh_images(self, page: LorePage) -> list[ImageRef]:
        """노션이 올린 이미지 주소는 1시간쯤 뒤 만료된다. 오래된 주소는 이 페이지를 다시 읽어 새 주소로 바꾼다."""
        now = time.time()
        stale = [im for im in page.images if im.expiring and now - im.fetched >= IMAGE_URL_LIFETIME]
        by_container: dict[str, set[str]] = {}
        for im in stale:
            by_container.setdefault(im.container_id, set()).add(im.block_id)
        for container, wanted in by_container.items():
            try:
                blocks = await self._children(container)
            except Exception as exc:   # 실패하면 예전 주소를 그대로 둔다. 이미지만 안 보일 뿐 문서는 열린다.
                logging.warning("이미지 주소를 새로 받지 못했어요: %r", exc)
                continue
            fresh = {b["id"]: _image_url(b) for b in blocks if b.get("type") == "image" and b["id"] in wanted}
            for im in stale:
                found = fresh.get(im.block_id) if im.container_id == container else None
                if found and found[0]:
                    im.url, im.fetched = found[0], now
        return page.images

    # ---------------- 쓰기 ----------------
    @staticmethod
    def _paragraphs(body: str) -> list[dict]:
        """본문을 노션 문단 블록으로 변환 (텍스트 1개당 2000자 제한 때문에 1900자씩 자름)."""
        blocks = []
        for line in body.replace("\r", "").split("\n"):
            for i in range(0, max(len(line), 1), 1900):
                piece = line[i:i + 1900]
                rich = [{"type": "text", "text": {"content": piece}}] if piece else []
                blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}})
        return blocks

    async def create_page(self, title: str, body: str) -> dict:
        """루트 페이지 아래에 새 하위 문서를 만든다. (통합에 '콘텐츠 삽입' 권한 필요)"""
        blocks = self._paragraphs(body)
        page = await self._call(
            self.client.pages.create,
            parent={"page_id": self.root_id},
            properties={"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            children=blocks[:100],  # 한 번에 최대 100블록
        )
        for i in range(100, len(blocks), 100):
            await self._call(
                self.client.blocks.children.append,
                block_id=page["id"],
                children=blocks[i:i + 100],
            )
        await self.refresh(force=True)
        return page

    # ---------------- 캐시 조회 (네트워크 사용 안 함) ----------------
    # 비공개 문서는 include_private=True로 부른 곳에서만 보인다. 기본값은 숨김이라, 빠뜨려도 새어 나가지 않는다.
    def _visible(self, include_private: bool = False):
        return (p for p in self._pages.values() if include_private or not p.private)

    def pages(self, include_private: bool = False) -> list[LorePage]:
        return list(self._visible(include_private))

    def titles(self, include_private: bool = False) -> list[str]:
        """모든 문서의 경로 이름. 상위 문서 바로 뒤에 그 하위 문서가 온다."""
        return [p.path for p in self._visible(include_private)]

    def top_titles(self, include_private: bool = False) -> list[str]:
        return [p.path for p in self._visible(include_private) if p.depth == 0]

    @staticmethod
    def _tree_line(page: LorePage) -> str:
        return f"• {page.label}" if page.depth == 0 else f"{'　' * (page.depth - 1)}└ {page.label}"

    def tree_lines(self, include_private: bool = False) -> list[str]:
        return [self._tree_line(p) for p in self._visible(include_private)]

    def private_lines(self) -> list[str]:
        """비공개 문서 목록(관리자용). 공개 문서 아래에 있는 비공개 문서는 어느 문서 밑인지 보이도록 전체 경로로 적는다."""
        lines, base = [], 0
        for page in self._pages.values():
            if not page.private:
                continue
            parent = self._pages.get(page.parent) if page.parent else None
            if parent is not None and parent.private:        # 비공개 문서의 하위: 들여쓰기로 이어 보여준다.
                lines.append(f"{'　' * (page.depth - base - 1)}└ {page.label}")
            else:                                             # 비공개 묶음의 시작
                base = page.depth
                lines.append(f"• {page.path}")
        return lines

    def children_of(self, page: LorePage, include_private: bool = False) -> list[LorePage]:
        return [p for p in self._visible(include_private) if p.parent == page.path]

    def parent_of(self, page: LorePage, include_private: bool = False) -> LorePage | None:
        parent = self._pages.get(page.parent) if page.parent else None
        return parent if parent is not None and (include_private or not parent.private) else None

    def _lines(self, page: LorePage, level: int, inline: bool, include_private: bool, keep_images: bool) -> list[str]:
        if page.private and not include_private:
            return []
        lines: list[str] = []
        for line in page.raw.split("\n"):
            if line.startswith(CHILD_MARK):
                child = self._pages.get(line[len(CHILD_MARK):])
                if child is None or (child.private and not include_private):
                    continue
                if not inline:
                    lines.append(f"📄 {child.label}")
                    continue
                if level >= MAX_PAGE_DEPTH:
                    continue
                marker = "└" * level + (" " if level else "")
                lines += ["", f"**{marker}📄 {child.label}**", self.render(child, level + 1, inline, include_private)]
            elif line.startswith(LIST_MARK):
                title = line[len(LIST_MARK):]
                if is_private_title(title) and not include_private:
                    continue
                lines.append("📄 " + title)
            elif line.startswith(IMG_MARK):
                if keep_images:
                    lines.append(line)
                else:
                    caption = page.images[int(line[len(IMG_MARK):])].caption
                    lines.append(f"🖼️ {caption}" if caption else "🖼️ 이미지")
            else:
                lines.append(line)
        return lines

    def render_lines(self, page: LorePage, include_private: bool = False, inline: bool | None = None) -> list[str]:
        """이미지 자리 표식을 남긴 줄들. 화면 구성(views.py)에서 이미지를 붙일 때 쓴다."""
        return self._lines(page, 0, INLINE_SUBPAGES if inline is None else inline, include_private, True)

    def render(self, page: LorePage, level: int = 0, inline: bool | None = None, include_private: bool = False) -> str:
        """표시용 글. 기본은 이 문서에 쓰인 내용만(하위 페이지는 이름만). 비어 있으면 '내용 없음'.

        inline=True(또는 INLINE_SUBPAGES=True)이면 하위 페이지 내용을 본문 순서대로 끼워 넣는다.
        """
        inline = INLINE_SUBPAGES if inline is None else inline
        return _clean(self._lines(page, level, inline, include_private, False)) or EMPTY

    def own_text(self, page: LorePage) -> str:
        """하위 페이지 내용을 뺀 이 페이지만의 본문, 서식 없이(검색용)."""
        return page.plain

    # ---- 이름 맞추기: 0 정확히 같음 · 1 앞부분 · 2 포함 · 3 초성 ----
    @staticmethod
    def _match(q: str, page: LorePage):
        """(단계, 별칭으로 맞았는지) 또는 None. 띄어쓰기 차이는 무시한다."""
        qs = q.replace(" ", "")
        best = None
        for keys, via_alias in ((page.keys, False), (page.alias_keys, True)):
            for key in keys:
                ks = key.replace(" ", "")
                if key == q or ks == qs:
                    tier = 0
                elif key.startswith(q) or ks.startswith(qs):
                    tier = 1
                elif q in key or qs in ks:
                    tier = 2
                else:
                    continue
                if best is None or tier < best[0]:
                    best = (tier, via_alias)
        if best is None and is_choseong_query(q):
            compact = q.replace(" ", "")
            for keys, via_alias in ((page.keys, False), (page.alias_keys, True)):
                if any(compact in choseong(key) for key in keys):
                    return (3, via_alias)
        return best

    @staticmethod
    def _fuzzy(q: str, page: LorePage) -> float:
        """오타 허용 정도(0~1). 이름 전체뿐 아니라 이름 속 단어, 앞부분(쓰다 만 입력)과도 비교한다. 한 글자짜리 입력은 비교하지 않는다."""
        if len(q.replace(" ", "")) < 2:
            return 0.0
        best = 0.0
        for key in page.keys + page.alias_keys:
            for candidate in {key, *(w for w in re.split(r"[\s/·,()\-]+", key) if len(w) >= 2)}:
                best = max(best, similarity(q, candidate))
                if len(candidate) > len(q):      # '솔리리'처럼 이름을 다 치기 전의 오타
                    best = max(best, similarity(q, candidate[:len(q)]) * 0.97)
        return best

    def get(self, query: str, include_private: bool = False) -> LorePage | None:
        raw = query.strip()
        if not raw:
            return None
        by_id = self._by_id.get(_norm(raw))
        if by_id:
            page = self._pages[by_id]
            return page if include_private or not page.private else None
        q = normalize(raw)
        pages = list(self._visible(include_private))
        for page in pages:
            if normalize(page.path) == q:
                return page
        for page in pages:
            if normalize(page.title) == q or normalize(page.label) == q:
                return page
        best = None
        for page in pages:
            match = self._match(q, page)
            if match is not None and (best is None or match[0] < best[0]):
                best = (match[0], page)
        if best:
            return best[1]
        for page in pages:
            if q in normalize(page.path):
                return page
        return None

    def similar(self, query: str, limit: int = 3, include_private: bool = False) -> list[LorePage]:
        """이름을 못 찾았을 때 보여줄 '혹시 이걸 찾으셨나요?' 후보."""
        q = normalize(query)
        scored = [(self._fuzzy(q, p), p) for p in self._visible(include_private)]
        scored = [(s, p) for s, p in scored if s >= FUZZY_THRESHOLD - 0.1]
        scored.sort(key=lambda item: -item[0])
        return [p for _, p in scored[:limit]]

    def suggest(self, current: str, limit: int = 25, include_private: bool = False) -> list[tuple[str, str]]:
        """자동완성용 (표시 이름, 값). 값은 문서 id라서 이름이 길어도 정확히 찾아진다."""
        cur = normalize(current)
        by_path = " / " in cur   # 경로를 직접 입력한 경우에만 상위 이름까지 본다
        tiers: list[list] = [[], [], [], []]
        for page in self._visible(include_private):
            if not cur:
                tiers[0].append(page)
            elif by_path:
                path = normalize(page.path)
                if path.startswith(cur):
                    tiers[1].append(page)
                elif cur in path:
                    tiers[2].append(page)
            else:
                match = self._match(cur, page)
                if match is not None:
                    tiers[match[0]].append(page)
        if not cur:
            tiers[0].sort(key=lambda p: p.depth)    # 입력이 없으면 상위 문서부터(같은 단계는 트리 순서 유지)
        ordered = [p for tier in tiers for p in tier]
        if cur and not ordered and not by_path:      # 아무것도 안 맞으면 오타로 보고 비슷한 이름을 보여준다.
            scored = [(self._fuzzy(cur, p), p) for p in self._visible(include_private)]
            ordered = [p for s, p in sorted(scored, key=lambda item: -item[0]) if s >= FUZZY_THRESHOLD]
        matches = [(short_name(p.label), _norm(p.id)) for p in ordered]
        if len(matches) <= limit:
            return matches
        # 디스코드 자동완성은 25개까지만 보인다. 잘렸다는 것을 마지막 줄로 알린다.
        extra = len(matches) - (limit - 1)
        return matches[:limit - 1] + [(f"… 외 {extra}개 더 있어요 — 글자를 입력해 찾으세요 (고르면 전체 목록)", MORE_VALUE)]

    def search_hits(self, keyword: str, limit: int = 10, include_private: bool = False) -> list[Hit]:
        """각 문서 자신의 본문과 이름·별칭에서 찾는다. 하위 페이지의 내용은 그 하위 문서 항목으로 나온다.

        띄어쓰기 차이, 영문명·별칭, 초성(ㅅㅇㅇㄱㄱ)도 찾는다. 아무것도 없으면 오타로 보고 비슷한 이름을 돌려준다.
        """
        q = normalize(keyword)
        compact = q.replace(" ", "")
        if not compact:
            return []
        pattern = re.compile(r"\s*".join(re.escape(c) for c in compact), re.I)   # 글자 사이 공백은 있어도 없어도 된다.
        hits: list[tuple[int, int, Hit]] = []
        for page in self._visible(include_private):
            own = page.plain
            count = len(pattern.findall(own))
            match = self._match(q, page)
            if not count and match is None:
                continue
            found = pattern.search(own)
            if found:
                snippet = own[max(0, found.start() - 40): found.end() + 60]
            else:
                snippet = own[:80] or EMPTY
            # 이름·별칭이 맞은 문서를 본문에 많이 나온 문서보다 앞에 둔다.
            kind = ("initial" if match[0] == 3 else "alias" if match[1] else "title") if match else "text"
            hits.append((match[0] if match else 9, -count, Hit(page, snippet.replace("\n", " "), count, kind)))
        hits.sort(key=lambda item: item[:2])
        results = [hit for _, _, hit in hits][:limit]
        if not results:
            for page in self.similar(q, 5, include_private):
                results.append(Hit(page, page.plain[:80].replace("\n", " ") or EMPTY, 0, "similar"))
        return results

    def search(self, keyword: str, limit: int = 10, include_private: bool = False) -> list[tuple[LorePage, str, int]]:
        return [(h.page, h.snippet, h.count) for h in self.search_hits(keyword, limit, include_private)]
