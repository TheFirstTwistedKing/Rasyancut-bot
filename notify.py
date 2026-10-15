"""노션 문서 변경 알림: 본문 지문 비교와 저장. 디스코드·노션에 접속하지 않는 순수 로직이다."""
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import discord

from notion_api import _norm

SETTLE_SECONDS = 120    # 같은 내용으로 이만큼 지속되어야 알린다. 노션에서 글을 쓰는 도중의 자동 저장을 걸러낸다.
MAX_LIST = 15           # 한 번의 알림에 이름을 몇 개까지 적을지


def snapshot_of(lore) -> dict:
    """공개 문서만의 현재 상태. 비공개 문서는 알림에도 이름이 나가지 않게 처음부터 뺀다."""
    return {_norm(p.id): {"title": p.label, "hash": p.digest} for p in lore.pages()}


def baseline(fresh: dict, now: float) -> dict:
    """처음 켤 때: 지금 있는 문서는 이미 알린 것으로 본다(켜자마자 전체가 쏟아지지 않게)."""
    return {pid: {"title": i["title"], "hash": i["hash"], "seen": i["hash"], "seen_at": now} for pid, i in fresh.items()}


def diff_snapshots(old: dict, fresh: dict, now: float, settle: float = SETTLE_SECONDS):
    """(새 문서 이름들, 수정된 문서 이름들, 갱신된 저장 상태).

    내용이 바뀐 것을 처음 본 때부터 settle초 동안 그대로여야 알린다. 그 사이 또 바뀌면 시간을 다시 잰다.
    이미 알린 내용(hash)과 같아지면 알리지 않는다. 사라진 문서는 조용히 뺀다.
    """
    added, edited, new = [], [], {}
    for pid, info in fresh.items():
        title, current = info["title"], info["hash"]
        prev = old.get(pid)
        if prev is None:     # 새 문서: 아직 알리지 않음(hash=None)
            new[pid] = {"title": title, "hash": None, "seen": current, "seen_at": now}
        elif prev["seen"] != current:
            new[pid] = {**prev, "title": title, "seen": current, "seen_at": now}
        elif prev["hash"] != current and now - prev["seen_at"] >= settle:
            (added if prev["hash"] is None else edited).append(title)
            new[pid] = {"title": title, "hash": current, "seen": current, "seen_at": prev["seen_at"]}
        else:
            new[pid] = {**prev, "title": title}
    return added, edited, new


@dataclass
class Section:
    label: str                  # 세계관 / 캐릭터
    command: str                # 자세히 볼 명령어
    added: list = field(default_factory=list)
    edited: list = field(default_factory=list)


def _names(items: list[str]) -> str:
    shown = [f"• {name}" for name in items[:MAX_LIST]]
    if len(items) > MAX_LIST:
        shown.append(f"… 외 {len(items) - MAX_LIST}개")
    return "\n".join(shown)


def build_embed(sections: list[Section], color: int) -> discord.Embed | None:
    sections = [s for s in sections if s.added or s.edited]
    if not sections:
        return None
    blocks = []
    for s in sections:
        if s.added:
            blocks.append(f"**🆕 {s.label} 새 문서 ({len(s.added)})**\n{_names(s.added)}")
        if s.edited:
            blocks.append(f"**✏️ {s.label} 수정된 문서 ({len(s.edited)})**\n{_names(s.edited)}")
    embed = discord.Embed(title="📢 노션 문서 업데이트", description="\n\n".join(blocks)[:4000], color=color)
    embed.set_footer(text="자세히 보려면 " + " · ".join(f"{s.command} 이름" for s in sections))
    return embed


class NotifyStore:
    """알림 채널과 마지막 상태를 JSON 파일에 저장한다. 저장은 임시 파일에 쓴 뒤 바꿔치기해서 중간에 끊겨도 깨지지 않는다."""

    def __init__(self, path):
        self.path = Path(path)
        self.channels_by_guild: dict[str, int] = {}
        self.snapshots: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logging.warning("알림 설정 파일을 읽지 못해 새로 시작해요: %s", self.path)
            return
        channels, snapshots = data.get("channels"), data.get("snapshots")
        if isinstance(channels, dict):
            self.channels_by_guild = {str(k): int(v) for k, v in channels.items() if str(v).isdigit() or isinstance(v, int)}
        if isinstance(snapshots, dict):
            self.snapshots = {str(k): v for k, v in snapshots.items() if isinstance(v, dict)}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"channels": self.channels_by_guild, "snapshots": self.snapshots}, ensure_ascii=False)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".notify-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def channels(self) -> dict[int, int]:
        return {int(g): c for g, c in self.channels_by_guild.items()}

    def channel_for(self, guild_id: int) -> int | None:
        return self.channels_by_guild.get(str(guild_id))

    def set_channel(self, guild_id: int, channel_id: int):
        self.channels_by_guild[str(guild_id)] = int(channel_id)
        self.save()

    def clear_channel(self, guild_id: int) -> bool:
        removed = self.channels_by_guild.pop(str(guild_id), None) is not None
        if removed:
            self.save()
        return removed

    def snapshot(self, key: str) -> dict | None:
        return self.snapshots.get(key)

    def set_snapshot(self, key: str, value: dict):
        self.snapshots[key] = value
