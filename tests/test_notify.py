"""노션 변경 알림 테스트. 실제 노션·디스코드에 접속하지 않는다."""
import copy
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

from cogs.notify import Notify, interval_minutes
from notify import (MAX_LIST, SETTLE_SECONDS, NotifyStore, Section, baseline, build_embed, diff_snapshots,
                    snapshot_of)
from tests.test_notion_tree import page_block, para
from tests.test_page_features import TREE, load


def snap(**pages):
    return {pid: {"title": f"문서 {pid}", "hash": h} for pid, h in pages.items()}


class DiffTests(unittest.TestCase):
    def test_baseline_marks_everything_as_already_announced(self):
        state = baseline(snap(a="h1", b="h2"), 100)
        self.assertEqual(state["a"], {"title": "문서 a", "hash": "h1", "seen": "h1", "seen_at": 100})
        self.assertEqual(diff_snapshots(state, snap(a="h1", b="h2"), 5000), ([], [], state))

    def test_new_document_is_announced_only_after_it_stays_the_same(self):
        state = baseline(snap(a="h1"), 0)
        added, edited, state = diff_snapshots(state, snap(a="h1", b="n1"), 100)
        self.assertEqual((added, edited), ([], []))                          # 처음 본 순간에는 알리지 않는다.
        self.assertIsNone(state["b"]["hash"])
        added, edited, state = diff_snapshots(state, snap(a="h1", b="n1"), 100 + SETTLE_SECONDS - 1)
        self.assertEqual((added, edited), ([], []))                          # 아직 안정되지 않았다.
        added, edited, state = diff_snapshots(state, snap(a="h1", b="n1"), 100 + SETTLE_SECONDS)
        self.assertEqual((added, edited), (["문서 b"], []))                  # 정확히 기준 시간이면 알린다.
        self.assertEqual(state["b"]["hash"], "n1")
        again = diff_snapshots(state, snap(a="h1", b="n1"), 9999)
        self.assertEqual(again[:2], ([], []))                                # 한 번만 알린다.

    def test_edits_are_announced_once_after_the_writing_stops(self):
        state = baseline(snap(a="h1"), 0)
        _, _, state = diff_snapshots(state, snap(a="h2"), 100)
        _, _, state = diff_snapshots(state, snap(a="h3"), 160)               # 쓰는 중에 또 바뀜 → 시간을 다시 잰다.
        added, edited, state = diff_snapshots(state, snap(a="h3"), 160 + SETTLE_SECONDS - 1)
        self.assertEqual((added, edited), ([], []))
        added, edited, state = diff_snapshots(state, snap(a="h3"), 160 + SETTLE_SECONDS)
        self.assertEqual((added, edited), ([], ["문서 a"]))
        self.assertEqual(diff_snapshots(state, snap(a="h3"), 99999)[:2], ([], []))

    def test_a_change_that_is_reverted_is_never_announced(self):
        state = baseline(snap(a="h1"), 0)
        _, _, state = diff_snapshots(state, snap(a="h2"), 100)
        for now in (400, 800, 5000):
            added, edited, state = diff_snapshots(state, snap(a="h1"), now)
            self.assertEqual((added, edited), ([], []), now)

    def test_renames_update_silently_and_removed_pages_are_forgotten(self):
        state = baseline(snap(a="h1", b="h2"), 0)
        renamed = {"a": {"title": "새 이름", "hash": "h1"}}                  # b는 사라짐(삭제·비공개로 전환)
        added, edited, state = diff_snapshots(state, renamed, 500)
        self.assertEqual((added, edited), ([], []))
        self.assertEqual(state["a"]["title"], "새 이름")
        self.assertNotIn("b", state)
        _, _, state = diff_snapshots(state, {**renamed, "b": {"title": "문서 b", "hash": "h2"}}, 600)
        added, _, _ = diff_snapshots(state, {**renamed, "b": {"title": "문서 b", "hash": "h2"}}, 600 + SETTLE_SECONDS)
        self.assertEqual(added, ["문서 b"])                                   # 다시 나타나면 새 문서로 본다.

    def test_several_changes_keep_the_page_order(self):
        state = baseline(snap(a="1", b="1", c="1"), 0)
        fresh = snap(a="2", b="1", c="2", d="1", e="1")
        _, _, state = diff_snapshots(state, fresh, 100)
        added, edited, _ = diff_snapshots(state, fresh, 100 + SETTLE_SECONDS)
        self.assertEqual((added, edited), (["문서 d", "문서 e"], ["문서 a", "문서 c"]))

    def test_custom_settle_time(self):
        state = baseline(snap(a="1"), 0)
        _, _, state = diff_snapshots(state, snap(a="2"), 10, settle=5)
        self.assertEqual(diff_snapshots(state, snap(a="2"), 14, settle=5)[1], [])
        self.assertEqual(diff_snapshots(state, snap(a="2"), 15, settle=5)[1], ["문서 a"])


class EmbedTests(unittest.TestCase):
    def test_sections_and_footer(self):
        embed = build_embed([Section("세계관", "/설정", ["새 글"], ["고친 글", "또 고친 글"]),
                             Section("캐릭터", "/캐릭터", [], ["인물"]), Section("비어 있음", "/x")], 0xE9D98B)
        self.assertEqual(embed.title, "📢 노션 문서 업데이트")
        self.assertIn("**🆕 세계관 새 문서 (1)**\n• 새 글", embed.description)
        self.assertIn("**✏️ 세계관 수정된 문서 (2)**\n• 고친 글\n• 또 고친 글", embed.description)
        self.assertIn("**✏️ 캐릭터 수정된 문서 (1)**\n• 인물", embed.description)
        self.assertNotIn("비어 있음", embed.description)
        self.assertEqual(embed.footer.text, "자세히 보려면 /설정 이름 · /캐릭터 이름")

    def test_nothing_to_say_and_long_lists(self):
        self.assertIsNone(build_embed([], 0))
        self.assertIsNone(build_embed([Section("세계관", "/설정")], 0))
        names = [f"문서 {n}" for n in range(MAX_LIST + 7)]
        embed = build_embed([Section("세계관", "/설정", names, [])], 0)
        self.assertEqual(embed.description.count("• 문서"), MAX_LIST)
        self.assertIn(f"({len(names)})", embed.description)
        self.assertIn("… 외 7개", embed.description)
        huge = [Section(f"구역{n}", "/설정", ["가" * 200] * MAX_LIST, ["나" * 200] * MAX_LIST) for n in range(5)]
        self.assertLessEqual(len(build_embed(huge, 0).description), 4000)


class StoreTests(unittest.TestCase):
    def test_roundtrip_and_channel_management(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "notify.json"
            store = NotifyStore(path)
            self.assertEqual(store.channels(), {})
            store.set_channel(100, 555)
            store.set_channel(200, 666)
            store.set_snapshot("lore", {"a": {"title": "가", "hash": "1"}})
            store.save()
            again = NotifyStore(path)
            self.assertEqual(again.channels(), {100: 555, 200: 666})
            self.assertEqual(again.channel_for(100), 555)
            self.assertIsNone(again.channel_for(300))
            self.assertEqual(again.snapshot("lore")["a"]["title"], "가")
            self.assertIsNone(again.snapshot("characters"))
            self.assertTrue(again.clear_channel(100))
            self.assertFalse(again.clear_channel(100))
            self.assertEqual(NotifyStore(path).channels(), {200: 666})        # 해제도 저장된다.

    def test_a_failed_write_leaves_the_old_file_and_no_temp_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "notify.json"
            store = NotifyStore(path)
            store.set_channel(100, 555)
            with mock.patch("os.replace", side_effect=OSError("디스크 오류")):
                with self.assertRaises(OSError):
                    store.set_channel(100, 999)
            self.assertEqual(json.loads(path.read_text())["channels"], {"100": 555})
            self.assertEqual([p.name for p in Path(temp).iterdir()], ["notify.json"])

    def test_damaged_or_odd_files_start_fresh_without_crashing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "notify.json"
            path.write_text("{ 깨진 파일")
            with self.assertLogs(level=logging.WARNING):
                self.assertEqual(NotifyStore(path).channels(), {})
            path.write_text(json.dumps({"channels": ["이상한"], "snapshots": 5}))
            store = NotifyStore(path)
            self.assertEqual((store.channels(), store.snapshots), ({}, {}))
            path.write_text(json.dumps({"channels": {"1": 2, "3": "x"}, "snapshots": {"lore": {"a": 1}, "bad": 7}}))
            store = NotifyStore(path)
            self.assertEqual(store.channels(), {1: 2})
            self.assertEqual(list(store.snapshots), ["lore"])


class FakeBot:
    def __init__(self, lore, characters):
        self.lore, self.characters = lore, characters
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def wait_until_ready(self):
        return None


def channel(cid=777, perms=None, send=None):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id, ch.mention = cid, f"<#{cid}>"
    ch.send = send or AsyncMock()
    allowed = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    ch.permissions_for = lambda who: allowed if perms is None else perms
    return ch


class FlowFixture(unittest.IsolatedAsyncioTestCase):
    """가짜 노션 두 개(세계관·캐릭터)와 임시 저장소로 알림 기능을 시험하는 공통 준비."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = copy.deepcopy(TREE)
        self.lore = load(self.tree, copy_tree=False)
        self.chars_tree = {"root": [page_block("A", "공개 캐릭터"), page_block("B", "🔒 숨은 캐릭터")],
                           "A": [para("a", "인물")], "B": [para("b", "비밀 인물")]}
        self.characters = load(self.chars_tree, copy_tree=False, include_root=False)
        await self.lore.refresh()
        await self.characters.refresh()
        self.bot = FakeBot(self.lore, self.characters)
        self.store = NotifyStore(Path(self.temp.name) / "notify.json")
        self.cog = Notify(self.bot, self.store)

    async def check(self, now):
        for source in (self.lore, self.characters):
            source._loaded_at = 0          # 5분 캐시를 건너뛰고 노션을 다시 읽게 한다.
        return await self.cog.check_once(now)



class CogFlowTests(FlowFixture):
    async def test_first_run_only_records_the_current_state(self):
        self.assertEqual(await self.check(1000), [])
        self.assertIsNotNone(self.store.snapshot("lore"))
        self.assertIsNotNone(self.store.snapshot("characters"))
        self.assertTrue(self.store.path.exists())
        self.assertEqual(await self.check(1000 + 10 ** 6), [])                # 아무것도 안 바뀌면 영원히 조용하다.

    async def test_new_and_edited_documents_are_announced_after_they_settle(self):
        await self.check(1000)
        self.tree["root"].append(page_block("N", "새로 쓴 문서"))
        self.tree["N"] = [para("n1", "새 내용")]
        self.tree["P"][0]["paragraph"]["rich_text"][0]["plain_text"] = "공개 내용이 고쳐졌다"
        self.assertEqual(await self.check(1100), [])                          # 쓰는 중일 수 있어 아직 알리지 않는다.
        (section,) = await self.check(1100 + SETTLE_SECONDS)
        self.assertEqual((section.label, section.command), ("세계관", "/설정"))
        self.assertEqual((section.added, section.edited), (["새로 쓴 문서"], ["공개 문서"]))
        self.assertEqual(await self.check(1100 + 10 * SETTLE_SECONDS), [])   # 한 번만

    async def test_characters_are_a_separate_section(self):
        await self.check(1000)
        self.chars_tree["A"][0]["paragraph"]["rich_text"][0]["plain_text"] = "고친 인물"
        await self.check(1100)
        (section,) = await self.check(1100 + SETTLE_SECONDS)
        self.assertEqual((section.label, section.command, section.edited), ("캐릭터", "/캐릭터", ["공개 캐릭터"]))

    async def test_private_documents_are_never_announced_and_publishing_one_is(self):
        await self.check(1000)
        self.tree["H"][0]["paragraph"]["rich_text"][0]["plain_text"] = "비공개 문서가 고쳐졌다"
        self.tree["root"].append(page_block("N2", "🔒 새 비공개"))
        self.tree["N2"] = [para("x", "x")]
        self.chars_tree["B"][0]["paragraph"]["rich_text"][0]["plain_text"] = "비밀 인물이 고쳐졌다"
        await self.check(1100)
        self.assertEqual(await self.check(1100 + SETTLE_SECONDS), [])
        saved = json.dumps(self.store.snapshots, ensure_ascii=False)
        for secret in ["미공개", "비공개", "숨은", "N2"]:
            self.assertNotIn(secret, saved)                                  # 저장 파일에도 비공개 이름이 없다.
        self.tree["root"][1] = page_block("H", "공개로 바꿈")                  # 🔒를 떼면 새 문서로 알린다.
        await self.check(2000)
        (section,) = await self.check(2000 + SETTLE_SECONDS)
        self.assertIn("공개로 바꿈", section.added)

    async def test_turning_a_page_private_is_not_reported_as_an_edit(self):
        await self.check(1000)
        self.tree["root"][3] = page_block("P", "🔒 공개 문서")
        await self.check(1100)
        self.assertEqual(await self.check(1100 + SETTLE_SECONDS), [])

    async def test_snapshot_is_saved_before_sending_so_failures_do_not_repeat_announcements(self):
        await self.check(1000)
        self.tree["P"][0]["paragraph"]["rich_text"][0]["plain_text"] = "또 고침"
        await self.check(1100)
        sections = await self.check(1100 + SETTLE_SECONDS)
        self.assertEqual(sections[0].edited, ["공개 문서"])
        reloaded = NotifyStore(self.store.path)
        self.assertEqual(reloaded.snapshot("lore")["p"]["hash"], self.store.snapshot("lore")["p"]["hash"])
        self.assertEqual(await self.check(1100 + 3 * SETTLE_SECONDS), [])   # 전송이 실패했다고 해도 다시 알리지 않는다.

    async def test_announce_reaches_every_configured_channel_and_survives_problems(self):
        good, locked = channel(777), channel(888, send=AsyncMock(side_effect=discord.Forbidden(NS(status=403, reason="x"), "no")))
        self.bot.channels = {777: good, 888: locked}
        self.store.set_channel(1, 777)
        self.store.set_channel(2, 888)
        self.store.set_channel(3, 999)                                        # 사라진 채널
        embed_sections = [Section("세계관", "/설정", ["새 글"], [])]
        with self.assertLogs("notify", level=logging.WARNING) as logs:
            await self.cog.announce(embed_sections)
        text = "\n".join(logs.output)
        self.assertIn("서버 3", text)
        self.assertIn("서버 2", text)
        sent = good.send.await_args.kwargs
        self.assertEqual(sent["embed"].title, "📢 노션 문서 업데이트")
        self.assertEqual((sent["allowed_mentions"].everyone, sent["allowed_mentions"].roles, sent["allowed_mentions"].users),
                         (False, False, False))                               # 알림이 누구도 멘션하지 않는다.
        await self.cog.announce([])
        self.assertEqual(good.send.await_count, 1)

    async def test_poll_does_nothing_without_channels_and_never_dies_on_errors(self):
        with mock.patch.object(self.lore, "refresh", new=AsyncMock()) as refresh:
            await self.cog.poll.coro(self.cog)
        refresh.assert_not_awaited()                                          # 알림을 안 쓰면 노션을 추가로 읽지 않는다.
        self.store.set_channel(1, 777)
        self.bot.channels = {777: channel(777)}
        with mock.patch.object(self.lore, "refresh", new=AsyncMock(side_effect=RuntimeError("노션 점검 중"))):
            with self.assertLogs("notify", level=logging.ERROR):
                await self.cog.poll.coro(self.cog)                            # 예외가 나도 반복이 멈추지 않는다.
        await self.check(1000)
        self.tree["P"][0]["paragraph"]["rich_text"][0]["plain_text"] = "고침"
        await self.check(1100)
        with mock.patch.object(self.cog, "check_once", new=AsyncMock(
                return_value=[Section("세계관", "/설정", [], ["공개 문서"])])):
            await self.cog.poll.coro(self.cog)
        self.bot.channels[777].send.assert_awaited_once()

    async def test_loop_lifecycle(self):
        with mock.patch.object(self.cog.poll, "start") as start, mock.patch.object(self.cog.poll, "cancel") as cancel:
            await self.cog.cog_load()
            await self.cog.cog_unload()
        start.assert_called_once()
        cancel.assert_called_once()
        self.assertEqual(self.cog.poll.minutes, interval_minutes())


def command_interaction(guild_id=100):
    return NS(guild_id=guild_id, guild=NS(me=NS(id=999)), response=NS(send_message=AsyncMock(), defer=AsyncMock()),
              followup=NS(send=AsyncMock()))


class CommandTests(FlowFixture):
    async def test_show_current_setting(self):
        i = command_interaction()
        await Notify.set_channel.callback(self.cog, i, None)
        self.assertIn("아직 없어요", i.response.send_message.await_args.args[0])
        self.store.set_channel(100, 777)
        i = command_interaction()
        await Notify.set_channel.callback(self.cog, i, None)
        self.assertIn("<#777>", i.response.send_message.await_args.args[0])
        self.assertTrue(i.response.send_message.await_args.kwargs["ephemeral"])

    async def test_setting_a_channel_records_the_baseline_and_explains_the_rules(self):
        i = command_interaction()
        await Notify.set_channel.callback(self.cog, i, channel(777))
        i.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertEqual(self.store.channel_for(100), 777)
        self.assertIsNotNone(self.store.snapshot("lore"))                      # 지금 있는 문서가 한꺼번에 알림으로 나오지 않는다.
        self.assertIsNotNone(self.store.snapshot("characters"))
        text = i.followup.send.await_args.args[0]
        self.assertIn("<#777>", text)
        self.assertIn("작성이 끝난 뒤", text)
        self.assertIn("비공개 문서는 알리지 않아요", text)
        self.assertIn(f"{interval_minutes()}분마다", text)
        await self.check(5000)                                                 # 설정 직후 첫 확인에서도 조용하다.
        self.assertEqual(await self.check(5000 + 10 ** 6), [])

    async def test_existing_snapshot_is_not_overwritten_when_the_channel_changes(self):
        await self.check(1000)
        saved = copy.deepcopy(self.store.snapshot("lore"))
        self.tree["P"][0]["paragraph"]["rich_text"][0]["plain_text"] = "고침"
        i = command_interaction()
        await Notify.set_channel.callback(self.cog, i, channel(888))
        self.assertEqual(self.store.snapshot("lore"), saved)                   # 바뀐 내용이 있어도 기준을 덮어쓰지 않는다.

    async def test_a_channel_the_bot_cannot_use_is_refused(self):
        for perms in (discord.Permissions(view_channel=True, send_messages=True),
                      discord.Permissions(view_channel=True, embed_links=True),
                      discord.Permissions(send_messages=True, embed_links=True), discord.Permissions.none()):
            i = command_interaction()
            await Notify.set_channel.callback(self.cog, i, channel(777, perms=perms))
            self.assertIn("보내거나 임베드를 올릴 수 없어요", i.response.send_message.await_args.args[0])
            i.response.defer.assert_not_awaited()
        self.assertIsNone(self.store.channel_for(100))

    async def test_notion_failure_while_setting_does_not_block_the_setting(self):
        i = command_interaction()
        with mock.patch.object(self.lore, "refresh", new=AsyncMock(side_effect=RuntimeError("노션 오류"))):
            with self.assertLogs(level=logging.ERROR):
                await Notify.set_channel.callback(self.cog, i, channel(777))
        self.assertEqual(self.store.channel_for(100), 777)

    async def test_turning_it_off(self):
        self.store.set_channel(100, 777)
        i = command_interaction()
        await Notify.clear_channel.callback(self.cog, i)
        self.assertEqual(i.response.send_message.await_args.args[0], "알림을 껐어요.")
        self.assertIsNone(self.store.channel_for(100))
        i = command_interaction()
        await Notify.clear_channel.callback(self.cog, i)
        self.assertEqual(i.response.send_message.await_args.args[0], "켜져 있는 알림이 없어요.")

    async def test_servers_are_independent(self):
        self.store.set_channel(100, 777)
        self.store.set_channel(200, 888)
        await Notify.clear_channel.callback(self.cog, command_interaction(100))
        self.assertEqual(self.store.channels(), {200: 888})

    async def test_permission_errors_and_other_errors(self):
        i = command_interaction()
        await self.cog.cog_app_command_error(i, app_commands.MissingPermissions(["manage_guild"]))
        self.assertIn("서버 관리 권한", i.response.send_message.await_args.args[0])
        with self.assertRaises(RuntimeError):
            await self.cog.cog_app_command_error(i, RuntimeError("다른 오류"))

    async def test_commands_require_manage_server_permission(self):
        for command in (Notify.set_channel, Notify.clear_channel):
            checks = [c.__qualname__ for c in command.checks]
            self.assertTrue(any("has_permissions" in name for name in checks), command.name)
            self.assertEqual(command.default_permissions.manage_guild, True)
            self.assertTrue(command.guild_only)


class IntervalTests(unittest.TestCase):
    def test_interval_is_clamped(self):
        for raw, expected in [(None, 10), ("", 10), ("5", 5), ("1", 3), ("0", 3), ("99999", 1440), ("abc", 10), ("-5", 3)]:
            with mock.patch.dict(os.environ, {"NOTIFY_INTERVAL_MIN": raw or ""}):
                self.assertEqual(interval_minutes(), expected, raw)

    def test_data_dir_is_configurable(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"DATA_DIR": temp}):
            async def build():
                return Notify(FakeBot(None, None))
            import asyncio
            cog = asyncio.run(build())
            self.assertEqual(cog.store.path, Path(temp) / "notify.json")


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_contains_only_public_documents(self):
        lore = load()
        await lore.refresh()
        snapshot = snapshot_of(lore)
        self.assertEqual(len(snapshot), 5)
        self.assertEqual(sorted(i["title"] for i in snapshot.values()),
                         sorted(["라시안컷(Rasyancut)", "성역의 경계(Sacred Boundary)", "솔스티스(Solstice)", "공개 하위", "공개 문서"]))
        self.assertTrue(all(len(i["hash"]) == 16 for i in snapshot.values()))


if __name__ == "__main__":
    unittest.main()
