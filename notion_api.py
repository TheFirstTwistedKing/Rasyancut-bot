"""노션 세계관 페이지를 읽어 오는 모듈.

루트 페이지 아래의 문서와 그 하위 페이지를 끝까지(단계 제한 내에서) 따라 내려가 읽는다.
- 모든 페이지는 '상위 / 하위' 경로 이름으로 따로 조회할 수 있다.
- 한 페이지를 보여줄 때는 그 아래 하위 페이지의 내용도 본문 순서대로 함께 보여준다.
- 내용이 없는 페이지는 '내용 없음'으로 표시한다.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass

from notion_client import AsyncClient
from notion_client.errors import APIResponseError
from notion_client.helpers import async_collect_paginated_api

CACHE_TTL = 300        # 초. 노션 수정 내용이 최대 5분 뒤 반영돼요.
MAX_DEPTH = 3          # 토글·들여쓰기 같은 하위 블록을 읽는 깊이
MAX_PAGE_DEPTH = 6     # 페이지 안의 페이지를 따라 내려가는 최대 단계
MAX_PAGES = 300        # 한 번에 읽을 최대 문서 수 (노션 요청 제한과 대기 시간 보호)
EMPTY = "내용 없음"
CHILD_MARK = "\x00child:"   # 본문 속 하위 페이지 자리를 나타내는 내부 표식


@dataclass
class LorePage:
    id: str
    title: str      # 이 페이지의 이름
    path: str       # '상위 / 하위' 형태의 전체 이름. 최상위 문서는 title과 같다.
    depth: int      # 0 = 최상위 문서
    raw: str        # 본문. 하위 페이지 자리는 내부 표식으로 남겨 둔다.


@dataclass
class _Ctx:
    path: str
    level: int
    registry: dict
    budget: list
    descend: bool = True   # False면 하위 페이지를 읽지 않고 이름만 적는다(루트 본문용)


def _rich(rich_text: list) -> str:
    return "".join(t.get("plain_text", "") for t in rich_text)


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _rich(prop["title"]) or "(제목 없음)"
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


def short_name(path: str, limit: int = 100) -> str:
    """디스코드 선택지 이름은 100자까지다. 길면 앞(상위)을 줄이고 구분에 가장 중요한 마지막 이름을 남긴다."""
    if len(path) <= limit:
        return path
    parts = path.split(" / ")
    name = parts[-1]
    if len(name) > limit:
        return name[:limit - 1] + "…"
    prefix = "… / "
    for part in reversed(parts[:-1]):
        if len(prefix + part + " / " + name) > limit:
            break
        name = part + " / " + name
    return prefix + name


def _clean(lines: list[str]) -> str:
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


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
    async def _child_page(self, b: dict, ctx: _Ctx) -> str:
        """하위 페이지를 읽어 registry에 등록하고, 본문에 끼울 표식 줄을 돌려준다."""
        title = b["child_page"].get("title") or "(제목 없음)"
        if not ctx.descend:
            return f"📄 {title}"
        if ctx.level + 1 > MAX_PAGE_DEPTH or ctx.budget[0] <= 0:
            return f"📄 {title} (더 내려가지 않음)"
        ctx.budget[0] -= 1
        path = _unique(ctx.registry, f"{ctx.path} / {title}")
        ctx.registry[path] = None  # 트리 순서를 지키기 위해 자리를 먼저 예약
        try:
            lines = await self._blocks_to_lines(b["id"], 0, _Ctx(path, ctx.level + 1, ctx.registry, ctx.budget))
        except Exception as exc:  # 한 페이지를 못 읽어도 나머지는 계속 보여준다.
            logging.warning("하위 페이지를 읽지 못했어요: %s (%r)", path, exc)
            ctx.registry.pop(path, None)
            return f"📄 {title} (읽지 못함)"
        ctx.registry[path] = LorePage(b["id"], title, path, ctx.level + 1, _clean(lines))
        return CHILD_MARK + path

    async def _blocks_to_lines(self, block_id: str, depth: int, ctx: _Ctx) -> list[str]:
        lines: list[str] = []
        number = 0
        for b in await self._children(block_id):
            t = b["type"]
            data = b.get(t, {})
            text = _rich(data.get("rich_text", []))
            pad = "  " * depth
            number = number + 1 if t == "numbered_list_item" else 0

            if t == "heading_1":
                lines += ["", f"__**{text}**__"]
            elif t in ("heading_2", "heading_3"):
                lines += ["", f"**{text}**"]
            elif t == "bulleted_list_item":
                lines.append(f"{pad}• {text}")
            elif t == "numbered_list_item":
                lines.append(f"{pad}{number}. {text}")
            elif t == "to_do":
                lines.append(f"{pad}{'☑' if data.get('checked') else '☐'} {text}")
            elif t in ("quote", "callout"):
                lines.append(f"> {text}")
            elif t == "divider":
                lines.append("────────")
            elif t == "code":
                lines.append(f"```\n{text}\n```")
            elif t == "table_row":
                lines.append(" | ".join(_rich(c) for c in data.get("cells", [])))
            elif t == "child_page":
                lines.append(await self._child_page(b, ctx))
            elif t in ("paragraph", "toggle"):
                lines.append(f"{pad}{text}")

            if b.get("has_children") and t != "child_page" and depth < MAX_DEPTH:
                lines += await self._blocks_to_lines(b["id"], depth + 1, ctx)
        return lines

    async def refresh(self, force: bool = False):
        async with self._lock:
            fresh = time.time() - self._loaded_at < CACHE_TTL
            if self._pages and fresh and not force:
                return

            root = await self._call(self.client.pages.retrieve, page_id=self.root_id)
            root_key = _norm(self.root_id)

            # 1단계: 최상위 문서 후보를 모은다. id -> 제목(없으면 None)
            targets: dict[str, str | None] = {}
            for b in await self._children(self.root_id):
                t = b["type"]
                data = b.get(t, {})
                if t == "child_page":
                    targets[b["id"]] = data.get("title") or None
                elif t == "link_to_page" and data.get("type") == "page_id":
                    targets.setdefault(data["page_id"], None)
                for r in data.get("rich_text", []):
                    m = r.get("mention")
                    if m and m.get("type") == "page":
                        targets[m["page"]["id"]] = r.get("plain_text") or None
            targets = {k: v for k, v in targets.items() if _norm(k) != root_key}

            # 2단계: 최상위 문서의 이름(경로)을 겹치지 않게 정한다.
            tops: list[tuple[str, str, bool]] = []   # (id, path, 하위 페이지를 읽을지)
            taken: set[str] = set()
            if self.include_root:
                path = _unique(taken, _page_title(root))
                taken.add(path)
                tops.append((self.root_id, path, False))
            for pid, title in targets.items():
                if not title:
                    title = _page_title(await self._call(self.client.pages.retrieve, page_id=pid))
                path = _unique(taken, title)
                taken.add(path)
                tops.append((pid, path, True))

            # 3단계: 문서마다 하위 페이지까지 읽는다. 문서끼리는 동시에, 문서 안에서는 차례로.
            budget = [MAX_PAGES]

            async def load(pid: str, path: str, descend: bool) -> dict[str, LorePage]:
                if budget[0] <= 0:
                    raise RuntimeError("문서 수 제한")
                budget[0] -= 1
                registry: dict[str, LorePage] = {}
                lines = await self._blocks_to_lines(pid, 0, _Ctx(path, 0, registry, budget, descend))
                top = LorePage(pid, path, path, 0, _clean(lines))
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
    def titles(self) -> list[str]:
        """모든 문서의 경로 이름. 상위 문서 바로 뒤에 그 하위 문서가 온다."""
        return list(self._pages)

    def top_titles(self) -> list[str]:
        return [p.path for p in self._pages.values() if p.depth == 0]

    def tree_lines(self) -> list[str]:
        lines = []
        for page in self._pages.values():
            lines.append(f"• {page.title}" if page.depth == 0 else f"{'　' * (page.depth - 1)}└ {page.title}")
        return lines

    def render(self, page: LorePage, level: int = 0) -> str:
        """페이지 본문에 하위 페이지의 내용을 순서대로 끼워 넣은 표시용 글. 비어 있으면 '내용 없음'."""
        lines: list[str] = []
        for line in page.raw.split("\n"):
            if line.startswith(CHILD_MARK):
                child = self._pages.get(line[len(CHILD_MARK):])
                if child is None or level >= MAX_PAGE_DEPTH:
                    continue
                marker = "└" * level + (" " if level else "")
                lines += ["", f"**{marker}📄 {child.title}**", self.render(child, level + 1)]
            else:
                lines.append(line)
        return _clean(lines) or EMPTY

    def own_text(self, page: LorePage) -> str:
        """하위 페이지 내용을 뺀 이 페이지만의 본문(검색용)."""
        return _clean([ln for ln in page.raw.split("\n") if not ln.startswith(CHILD_MARK)])

    def get(self, query: str) -> LorePage | None:
        raw = query.strip()
        if not raw:
            return None
        by_id = self._by_id.get(_norm(raw))
        if by_id:
            return self._pages[by_id]
        q = raw.lower()
        for path, page in self._pages.items():
            if path.lower() == q:
                return page
        for path, page in self._pages.items():
            if page.title.lower() == q:
                return page
        for path, page in self._pages.items():
            if q in path.lower():
                return page
        return None

    def suggest(self, current: str, limit: int = 25) -> list[tuple[str, str]]:
        """자동완성용 (표시 이름, 값). 값은 문서 id라서 이름이 길어도 정확히 찾아진다."""
        cur = current.strip().lower()
        first, second, third = [], [], []
        for path, page in self._pages.items():
            item = (short_name(path), _norm(page.id))
            title = page.title.lower()
            if not cur or title.startswith(cur) or path.lower().startswith(cur):
                first.append(item)          # 이 문서 자신의 이름이 시작부터 맞음
            elif cur in title:
                second.append(item)         # 이 문서 자신의 이름에 포함
            elif cur in path.lower():
                third.append(item)          # 상위 문서 이름에만 포함(그 아래 문서들)
        return (first + second + third)[:limit]

    def search(self, keyword: str, limit: int = 10) -> list[tuple[LorePage, str, int]]:
        """각 문서 자신의 본문에서만 찾는다. 하위 페이지의 내용은 그 하위 문서 항목으로 나온다."""
        k = keyword.strip().lower()
        if not k:
            return []
        results = []
        for page in self._pages.values():
            own = self.own_text(page)
            low = own.lower()
            count = low.count(k)
            if count or k in page.path.lower():
                i = low.find(k)
                if i >= 0:
                    snippet = own[max(0, i - 40): i + len(k) + 60]
                else:
                    snippet = own[:80] or EMPTY
                results.append((page, snippet.replace("\n", " "), count))
        results.sort(key=lambda r: -r[2])
        return results[:limit]
