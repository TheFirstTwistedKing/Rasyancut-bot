import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from helpers import COLOR
from notify import NotifyStore, Section, baseline, build_embed, diff_snapshots, snapshot_of

BASE = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("notify")


def interval_minutes() -> int:
    """NOTIFY_INTERVAL_MIN(분). 3~1440 사이, 기본 10. SETTLE(2분)보다 짧게 하면 변경을 놓칠 수 있어 3분 아래는 막는다."""
    try:
        value = int(os.getenv("NOTIFY_INTERVAL_MIN") or 10)
    except ValueError:
        value = 10
    return min(max(value, 3), 1440)


class Notify(commands.Cog):
    """노션 문서가 새로 생기거나 고쳐지면 정해 둔 채널에 알려준다."""

    def __init__(self, bot: commands.Bot, store: NotifyStore | None = None):
        self.bot = bot
        data_dir = Path(os.getenv("DATA_DIR") or BASE / "data")
        self.store = store or NotifyStore(data_dir / "notify.json")
        self.poll.change_interval(minutes=interval_minutes())

    async def cog_load(self):
        self.poll.start()

    async def cog_unload(self):
        self.poll.cancel()

    def sources(self):
        return [("lore", "세계관", "/설정", self.bot.lore), ("characters", "캐릭터", "/캐릭터", self.bot.characters)]

    async def check_once(self, now: float | None = None) -> list[Section]:
        """노션을 읽어 바뀐 문서를 찾는다. 알릴 것이 있으면 구역 목록을 돌려준다."""
        now = time.time() if now is None else now
        sections = []
        for key, label, command, source in self.sources():
            if source is None:
                continue
            await source.refresh()
            fresh = snapshot_of(source)
            old = self.store.snapshot(key)
            if old is None:
                self.store.set_snapshot(key, baseline(fresh, now))
                continue
            added, edited, new = diff_snapshots(old, fresh, now)
            self.store.set_snapshot(key, new)
            if added or edited:
                sections.append(Section(label, command, added, edited))
        self.store.save()      # 보내기 전에 저장한다: 전송에 실패해도 같은 알림이 반복되지 않는다.
        return sections

    async def announce(self, sections: list[Section]):
        embed = build_embed(sections, COLOR)
        if embed is None:
            return
        for guild_id, channel_id in self.store.channels().items():
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                LOG.warning("서버 %s의 알림 채널 %s을(를) 찾을 수 없어요.", guild_id, channel_id)
                continue
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                LOG.warning("서버 %s 알림 전송 실패: HTTP %s / %s", guild_id, exc.status, exc.code)

    @tasks.loop(minutes=10)
    async def poll(self):
        if not self.store.channels():
            return
        try:
            sections = await self.check_once()
            await self.announce(sections)
        except Exception:   # 한 번 실패해도 반복이 멈추지 않게 기록만 하고 다음에 다시 시도한다.
            LOG.exception("노션 변경 확인 중 오류")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="알림채널", description="노션 문서가 바뀌면 알려줄 채널을 정해요 (서버 관리자)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.rename(channel="채널")
    @app_commands.describe(channel="알림을 받을 채널 (비워 두면 현재 설정을 보여줘요)")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        current = self.store.channel_for(interaction.guild_id)
        if channel is None:
            text = (f"지금은 <#{current}>로 알려줘요. 바꾸려면 `/알림채널 채널:#채널`, 끄려면 `/알림해제`."
                    if current else "알림 채널이 아직 없어요. `/알림채널 채널:#채널`로 정해 주세요.")
            await interaction.response.send_message(text, ephemeral=True)
            return
        me = interaction.guild.me
        permissions = channel.permissions_for(me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            await interaction.response.send_message(
                f"{channel.mention}에서 봇이 메시지를 보내거나 임베드를 올릴 수 없어요. 채널 권한을 확인해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:      # 지금 있는 문서는 이미 알린 것으로 쳐서, 켜자마자 전체가 쏟아지지 않게 한다.
            for key, _, _, source in self.sources():
                if source is not None:
                    await source.refresh()
                    if self.store.snapshot(key) is None:
                        self.store.set_snapshot(key, baseline(snapshot_of(source), time.time()))
        except Exception:
            LOG.exception("알림 시작 전 노션 읽기 실패")
        self.store.set_channel(interaction.guild_id, channel.id)
        await interaction.followup.send(
            f"{channel.mention}로 노션 문서 알림을 보낼게요. {interval_minutes()}분마다 확인하고, "
            "글을 쓰는 도중이 아니라 **작성이 끝난 뒤**에 새 문서와 수정된 문서를 알려줘요. 비공개 문서는 알리지 않아요.")

    @app_commands.command(name="알림해제", description="노션 문서 알림을 꺼요 (서버 관리자)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clear_channel(self, interaction: discord.Interaction):
        removed = self.store.clear_channel(interaction.guild_id)
        await interaction.response.send_message("알림을 껐어요." if removed else "켜져 있는 알림이 없어요.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("서버 관리 권한이 있는 사람만 쓸 수 있어요.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Notify(bot))
