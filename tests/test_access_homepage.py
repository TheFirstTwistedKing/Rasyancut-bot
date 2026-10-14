"""홈페이지 명령어와 서버별 접근 규칙 테스트. 실제 디스코드 상호작용 객체를 명령 처리 경로(tree._call)에 통과시킨다."""
import os
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock
from unittest.mock import AsyncMock

import discord

import access
import main
from access import ADMIN_ONLY_GUILD_IDS, allowed
from cogs.homepage import DEFAULT_URL, homepage_url

RESTRICTED = next(iter(ADMIN_ONLY_GUILD_IDS))
OTHER = 111111111111111111
ADMIN = discord.Permissions(administrator=True).value
MEMBER = discord.Permissions(send_messages=True, view_channel=True).value
MANAGER = discord.Permissions(manage_guild=True, manage_channels=True, manage_roles=True, ban_members=True).value
ROOT = Path(__file__).resolve().parent.parent


def fake(guild_id, perms, user_id=20, owner_id=1):
    return NS(guild_id=guild_id, user=NS(id=user_id), guild=NS(owner_id=owner_id),
              permissions=discord.Permissions(perms))


class AllowedRuleTests(unittest.TestCase):
    def test_only_administrators_in_the_restricted_server(self):
        self.assertTrue(allowed(fake(RESTRICTED, ADMIN)))
        self.assertFalse(allowed(fake(RESTRICTED, MEMBER)))
        self.assertFalse(allowed(fake(RESTRICTED, 0)))
        # 서버 관리·채널 관리·역할 관리·차단 권한이 있어도 관리자(Administrator)가 아니면 안 된다.
        self.assertFalse(allowed(fake(RESTRICTED, MANAGER)))

    def test_server_owner_is_allowed_even_without_the_administrator_bit(self):
        self.assertTrue(allowed(fake(RESTRICTED, MEMBER, user_id=7, owner_id=7)))
        self.assertFalse(allowed(fake(RESTRICTED, MEMBER, user_id=7, owner_id=8)))
        self.assertFalse(allowed(NS(guild_id=RESTRICTED, user=NS(id=7), guild=None, permissions=discord.Permissions(MEMBER))))

    def test_everything_else_is_unrestricted(self):
        self.assertTrue(allowed(fake(OTHER, 0)))
        self.assertTrue(allowed(fake(OTHER, MEMBER)))
        self.assertTrue(allowed(NS(guild_id=None, user=NS(id=1), guild=None, permissions=None)))   # DM
        self.assertTrue(allowed(fake(222, 0), admin_only=frozenset({333})))
        self.assertFalse(allowed(fake(222, 0), admin_only=frozenset({222})))


class HomepageUrlTests(unittest.TestCase):
    def test_default_and_override(self):
        with mock.patch.dict(os.environ, {'HOMEPAGE_URL': ''}):
            self.assertEqual(homepage_url(), 'https://thefirsttwistedking.github.io/Rasyancut-homepage/')
            self.assertEqual(DEFAULT_URL, homepage_url())
        with mock.patch.dict(os.environ, {'HOMEPAGE_URL': ' https://example.org/home '}):
            self.assertEqual(homepage_url(), 'https://example.org/home')
        for bad in ['javascript:alert(1)', 'example.org', 'ftp://example.org', '   ']:
            with mock.patch.dict(os.environ, {'HOMEPAGE_URL': bad}):
                self.assertEqual(homepage_url(), DEFAULT_URL, bad)


class RealInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = {'NOTION_TOKEN': 'x', 'NOTION_ROOT_PAGE_ID': 'root', 'DISCORD_TOKEN': 'x', 'HOMEPAGE_URL': ''}
        with mock.patch.dict(os.environ, env):
            self.bot = main.WorldBot()
        self.state = self.bot._connection
        self.state.user = discord.ClientUser(state=self.state, data={
            'id': '999', 'username': 'bot', 'discriminator': '0', 'avatar': None, 'bot': True})
        for extension in ('cogs.lore', 'cogs.search', 'cogs.character', 'cogs.homepage'):
            await self.bot.load_extension(extension)
        patcher = mock.patch.object(discord.InteractionResponse, 'send_message', new_callable=AsyncMock)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(discord.InteractionResponse, 'autocomplete', new_callable=AsyncMock)
        self.autocomplete = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(discord.InteractionResponse, 'defer', new_callable=AsyncMock)
        self.defer = patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.bot.close()

    def interaction(self, guild_id, perms, name='홈페이지', kind=2, options=None):
        payload = {'id': '1', 'application_id': '2', 'type': kind, 'token': 'tok', 'version': 1,
                   'guild_id': str(guild_id), 'channel_id': '5', 'locale': 'ko', 'guild_locale': 'ko',
                   'app_permissions': '0', 'attachment_size_limit': 10485760, 'entitlements': [],
                   'authorizing_integration_owners': {}, 'context': 0,
                   'data': {'id': '3', 'name': name, 'type': 1, 'options': options or []},
                   'member': {'user': {'id': '20', 'username': 'u', 'discriminator': '0', 'avatar': None},
                              'roles': [], 'joined_at': '2026-01-01T00:00:00+00:00', 'deaf': False,
                              'mute': False, 'flags': 0, 'permissions': str(perms)}}
        return discord.Interaction(data=payload, state=self.state)

    async def test_check_is_installed_on_the_command_tree(self):
        self.assertEqual(self.bot.tree.interaction_check, self.bot.check_access)
        names = {c.name for c in self.bot.tree.get_commands()}
        self.assertTrue({'홈페이지', '목록', '설정', '설정추가', '검색', '캐릭터', '새로고침'} <= names)

    async def test_admin_gets_the_homepage_link_and_button(self):
        await self.bot.tree._call(self.interaction(RESTRICTED, ADMIN))
        self.send.assert_awaited_once()
        kwargs = self.send.await_args.kwargs
        self.assertNotIn('ephemeral', kwargs)                        # 공개 메시지: 다른 사람도 눌러 볼 수 있다.
        embed, view = kwargs['embed'], kwargs['view']
        self.assertEqual((embed.title, embed.url, embed.description), ('라시안컷 홈페이지', DEFAULT_URL, DEFAULT_URL))
        (button,) = view.children
        self.assertEqual((button.style, button.url, button.label), (discord.ButtonStyle.link, DEFAULT_URL, '홈페이지 열기'))

    async def test_everyone_can_use_it_in_other_servers(self):
        for perms in (0, MEMBER, ADMIN):
            self.send.reset_mock()
            await self.bot.tree._call(self.interaction(OTHER, perms))
            self.assertEqual(self.send.await_args.kwargs['embed'].url, DEFAULT_URL, perms)

    async def test_non_admins_in_the_restricted_server_cannot_use_any_command(self):
        for command in self.bot.tree.get_commands():
            for perms in (0, MEMBER, MANAGER):
                self.send.reset_mock()
                self.defer.reset_mock()
                interaction = self.interaction(RESTRICTED, perms, name=command.name)
                await self.bot.tree._call(interaction)
                self.send.assert_awaited_once()
                self.assertEqual(self.send.await_args, mock.call('이 명령어를 사용할 권한이 없어요.', ephemeral=True),
                                 f'{command.name} / {perms}')                  # 본인에게만 보이는 거절 메시지
                self.defer.assert_not_awaited()                                 # 명령 본문은 실행되지 않았다.
                self.assertTrue(interaction.command_failed)

    async def test_admins_in_the_restricted_server_can_still_run_commands(self):
        interaction = self.interaction(RESTRICTED, ADMIN, name='목록')
        with mock.patch('cogs.lore.ensure_loaded', new=AsyncMock(return_value=True)), \
                mock.patch('cogs.lore.send_tree', new=AsyncMock()) as shown:
            await self.bot.tree._call(interaction)
        shown.assert_awaited_once()
        self.assertFalse(interaction.command_failed)

    async def test_autocomplete_is_blocked_for_non_admins_without_any_reply(self):
        options = [{'name': '이름', 'type': 3, 'value': '나', 'focused': True}]
        with mock.patch.object(self.bot.lore, 'suggest', return_value=[]) as suggest:
            await self.bot.tree._call(self.interaction(RESTRICTED, MEMBER, name='설정', kind=4, options=options))
            self.send.assert_not_awaited()
            self.autocomplete.assert_not_awaited()
            suggest.assert_not_called()                              # 문서 이름 목록이 만들어지지도 않는다.
            await self.bot.tree._call(self.interaction(RESTRICTED, ADMIN, name='설정', kind=4, options=options))
            suggest.assert_called_once_with('나')
            self.autocomplete.assert_awaited_once()
            suggest.reset_mock()
            self.autocomplete.reset_mock()
            await self.bot.tree._call(self.interaction(OTHER, MEMBER, name='설정', kind=4, options=options))
            suggest.assert_called_once_with('나')

    async def test_denial_survives_a_failed_reply(self):
        with mock.patch.object(discord.InteractionResponse, 'send_message', new_callable=AsyncMock,
                               side_effect=discord.HTTPException(NS(status=404, reason='x'), 'Unknown interaction')):
            self.assertFalse(await self.bot.check_access(self.interaction(RESTRICTED, MEMBER)))

    async def test_the_rule_is_not_written_in_descriptions_or_docs(self):
        needle = str(RESTRICTED)
        for command in self.bot.tree.walk_commands():
            self.assertNotIn(needle, command.description)
            self.assertNotIn(needle, command.name)
        homepage = self.bot.tree.get_command('홈페이지')
        self.assertNotIn('관리자', homepage.description)
        for path in [ROOT / 'README.md', ROOT / '.env.example'] + sorted((ROOT / 'cogs').glob('*.py')) + [ROOT / 'helpers.py']:
            self.assertNotIn(needle, path.read_text(encoding='utf-8'), path.name)
        for path in [ROOT / 'README.md', ROOT / '.env.example']:
            self.assertNotIn('네칼', path.read_text(encoding='utf-8'), path.name)


if __name__ == '__main__':
    unittest.main()
