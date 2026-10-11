"""노션 세계관 페이지(페이지 구조 그대로)를 읽어 오는 모듈.

루트 페이지의 하위 문서(child_page / 페이지 링크 / 페이지 멘션)를 모아
제목 → 본문 텍스트로 캐싱한다.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass

from notion_client import AsyncClient
from notion_client.errors import APIResponseError
from notion_client.helpers import async_collect_paginated_api

CACHE_TTL = 300  # 초. 노션 수정 내용이 최대 5분 뒤 반영돼요.
MAX_DEPTH = 3    # 토글/들여쓰기 하위 블록을 읽는 깊이


@dataclass
class LorePage:
    id: str
    title: str
    text: str


def _rich(rich_text: list) -> str:
    return "".join(t.get("plain_text", "") for t in rich_text)


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _rich(prop["title"]) or "(제목 없음)"
    return "(제목 없음)"


def _norm(page_id: str) -> str:
    return page_id.replace("-", "").lower()


class NotionLore:
    def __init__(self, token: str, root_page_id: str, include_root: bool = True):
        self.client = AsyncClient(auth=token)
        self.root_id = root_page_id
        # True면 루트 페이지 본문도 하나의 문서로 취급 (캐릭터 목록 페이지는 False)
        self.include_root = include_root
        self._pages: dict[str, LorePage] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        # 노션 API는 평균 초당 3회 제한이라 동시 요청과 간격을 제한한다.
        self._sem = asyncio.Semaphore(2)

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

    async def _blocks_to_lines(self, block_id: str, depth: int = 0) -> list[str]:
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
                lines.append(f"📄 {data.get('title', '')}")
            elif t in ("paragraph", "toggle"):
                lines.append(f"{pad}{text}")

            if b.get("has_children") and t != "child_page" and depth < MAX_DEPTH:
                lines += await self._blocks_to_lines(b["id"], depth + 1)
        return lines

    @staticmethod
    def _clean(lines: list[str]) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    async def refresh(self, force: bool = False):
        async with self._lock:
            fresh = time.time() - self._loaded_at < CACHE_TTL
            if self._pages and fresh and not force:
                return

            root = await self._call(self.client.pages.retrieve, page_id=self.root_id)
            root_key = _norm(self.root_id)

            # 루트의 하위 문서 후보 수집: id -> 제목(없으면 None)
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

            async def load(pid: str, title: str | None) -> LorePage:
                if not title:
                    page = await self._call(self.client.pages.retrieve, page_id=pid)
                    title = _page_title(page)
                lines = await self._blocks_to_lines(pid)
                return LorePage(pid, title, self._clean(lines))

            jobs = [load(pid, title) for pid, title in targets.items()]
            if self.include_root:
                jobs.insert(0, load(self.root_id, _page_title(root)))
            results = await asyncio.gather(*jobs, return_exceptions=True)
            pages: dict[str, LorePage] = {}
            for r in results:
                if isinstance(r, LorePage):
                    pages[r.title] = r
                else:
                    logging.warning("문서 하나를 불러오지 못했어요: %r", r)

            self._pages = pages
            self._loaded_at = time.time()

    # ---- 쓰기 ----
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

    # ---- 캐시 조회 (네트워크 사용 안 함) ----
    def titles(self) -> list[str]:
        return list(self._pages)

    def get(self, query: str) -> LorePage | None:
        q = query.strip().lower()
        if not q:
            return None
        for title, page in self._pages.items():
            if title.lower() == q:
                return page
        for title, page in self._pages.items():
            if q in title.lower():
                return page
        return None

    def search(self, keyword: str, limit: int = 10) -> list[tuple[LorePage, str, int]]:
        k = keyword.strip().lower()
        if not k:
            return []
        results = []
        for page in self._pages.values():
            low = page.text.lower()
            count = low.count(k)
            if count or k in page.title.lower():
                i = low.find(k)
                if i >= 0:
                    snippet = page.text[max(0, i - 40): i + len(k) + 60]
                else:
                    snippet = page.text[:80]
                results.append((page, snippet.replace("\n", " "), count))
        results.sort(key=lambda r: -r[2])
        return results[:limit]
