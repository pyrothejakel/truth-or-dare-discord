import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo
import discord
from truth_or_dare_bot.config import Config, clock_time, timezone_name, in_quiet_hours
from truth_or_dare_bot.storage import Store
from truth_or_dare_bot.__main__ import TruthDareBot


class StorageBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_invalid_batch_is_atomic_even_after_later_commit(self):
        with self.assertRaises(ValueError):
            self.store.add_prompts([('truth', 1, None, 'must not leak'), ('invalid', 1, None, 'bad')])
        self.store.set_setting('paused', 'true')
        self.assertEqual(self.store.count_prompts(), 6)
        self.assertFalse(self.store.conn.in_transaction)

    def test_invalid_lengths_and_intensities(self):
        for row in [('truth', 0, '', 'test'), ('truth', 6, '', 'test'),
                    ('truth', True, '', 'test'), ('truth', 1.5, '', 'test'),
                    ('truth', 1, 'x'*81, 'test'), ('truth', 1, '', 'x'*3501), ('truth', 1, '', ' ' )]:
            with self.subTest(row=row[:2]), self.assertRaises(ValueError): self.store.add_prompts([row])
        self.assertEqual(self.store.count_prompts(), 6)

    def test_dedup_and_normalization(self):
        self.assertEqual(self.store.add_prompts([(' TRUTH ', 1, ' FUN ', ' test ')]), 1)
        self.assertEqual(self.store.add_prompts([('truth', 1, 'fun', 'test')]), 0)
        self.assertEqual(self.store.choose_prompt('truth', 'FUN', 1).text, 'test')

    def test_cycle_has_no_repeat_until_exhausted_and_reuses_oldest(self):
        sequence = []
        for _ in range(6):
            p = self.store.choose_prompt(); sequence.append(p.id); self.store.record_post(p.id)
        self.assertEqual(len(set(sequence)), 6)
        self.assertEqual(self.store.choose_prompt().id, sequence[0])

    def test_durable_daily_claim_across_connections(self):
        other = Store(self.path)
        try:
            self.assertTrue(self.store.claim_day('2026-09-13'))
            self.assertFalse(other.claim_day('2026-09-13'))
            self.store.complete_day('2026-09-13')
            self.assertEqual(other.get_setting('last_post_date'), '2026-09-13')
        finally: other.close()

    def test_invalid_filters_do_not_select_or_mutate(self):
        for mode, level in [('bad', None), ('mixed', 0), ('truth', 6)]:
            with self.assertRaises(ValueError): self.store.choose_prompt(mode, intensity=level)
        self.assertEqual(self.store.execute('SELECT COUNT(*) AS n FROM history').fetchone()['n'], 0)


class Configuration(unittest.TestCase):
    def test_clock_and_timezone_validation(self):
        for value in ['7:00', '24:00', '19:60', 'noon', ' 19:00']:
            with self.assertRaises(ValueError): clock_time(value)
        self.assertEqual(clock_time('19:00'), '19:00')
        with self.assertRaises(ValueError): timezone_name('Not/AZone')

    def test_quiet_hours_cross_midnight_and_boundaries(self):
        for hour, minute, expected in [(21, 59, False), (22, 0, True), (23, 0, True), (0, 0, True), (6, 59, True), (7, 0, False)]:
            self.assertEqual(in_quiet_hours(datetime(2026,9,13,hour,minute), '22:00', '07:00'), expected)
        self.assertFalse(in_quiet_hours(datetime(2026,9,13,23), '', ''))
        self.assertTrue(in_quiet_hours(datetime(2026,9,13,12), '11:00', '13:00'))

    def test_cloud_refuses_ephemeral_storage(self):
        with patch.dict(os.environ, {'GUILD_ID':'1','CHANNEL_ID':'2','REQUIRE_POSTGRES':'true'}, clear=True):
            with self.assertRaises(ValueError): Config.from_env()

    def test_missing_or_invalid_ids_fail_without_secret_output(self):
        for value in ['', '-1', 'bad', '0']:
            with patch.dict(os.environ, {'GUILD_ID':value,'CHANNEL_ID':'2'}, clear=True):
                with self.assertRaisesRegex(ValueError, 'GUILD_ID'): Config.from_env()


class RuntimeBehavior(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'db.sqlite3')
        self.bot = TruthDareBot(Config(1,2),self.store)
        self.channel = MagicMock()
        self.channel.send = AsyncMock()
        self.bot.target_channel = AsyncMock(return_value=self.channel)
        self.now = datetime(2026,9,13,19,5,tzinfo=ZoneInfo('America/Chicago'))
        self.bot.local_now = lambda: self.now

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_daily_catchup_once_and_restart_no_duplicate(self):
        await self.bot.scheduled_tick(self.now)
        await self.bot.scheduled_tick(self.now)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.get_setting('last_post_date'),'2026-09-13')
        self.assertEqual(self.channel.send.call_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_early_pause_and_quiet_do_not_post(self):
        await self.bot.scheduled_tick(self.now.replace(hour=18))
        self.store.set_setting('paused','true')
        await self.bot.scheduled_tick(self.now)
        self.store.set_settings({'paused':'false','quiet_start':'18:00','quiet_end':'20:00'})
        await self.bot.scheduled_tick(self.now)
        self.channel.send.assert_not_called()
        self.assertIn('Quiet',await self.bot.send_prompt(self.channel))

    async def test_ambiguous_send_is_not_retried_or_falsely_recorded(self):
        self.channel.send.side_effect = OSError('connection dropped')
        with self.assertRaises(OSError): await self.bot.scheduled_tick(self.now)
        await self.bot.scheduled_tick(self.now)
        self.channel.send.assert_awaited_once()
        self.assertIsNone(self.store.get_setting('last_post_date'))
        self.assertEqual(self.store.execute('SELECT COUNT(*) AS n FROM history').fetchone()['n'],0)

    async def test_concurrent_daily_ticks_post_once(self):
        await asyncio.gather(self.bot.scheduled_tick(self.now),self.bot.scheduled_tick(self.now))
        self.channel.send.assert_awaited_once()

    async def test_cancelled_send_keeps_claim(self):
        self.channel.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.bot.scheduled_tick(self.now)
        self.assertFalse(self.store.claim_day('2026-09-13'))

    async def test_wrong_guild_or_channel_rejected(self):
        for guild,channel in [(3,2),(1,3),(None,None)]:
            i=MagicMock(guild_id=guild,channel_id=channel)
            i.response.send_message=AsyncMock()
            self.assertFalse(await self.bot.tree.interaction_check(i))
            i.response.send_message.assert_awaited_once()

    async def test_non_admin_cannot_mutate_schedule(self):
        i=MagicMock(); i.user=MagicMock(spec=discord.Member)
        i.user.guild_permissions.manage_guild=False
        i.response.send_message=AsyncMock()
        await self.bot.tree.get_command('schedule').callback(i,'20:00','America/Chicago')
        self.assertIsNone(self.store.get_setting('post_time'))

    async def test_bad_timezone_has_clear_response_no_partial_mutation(self):
        self.bot.require_admin=AsyncMock(return_value=True)
        i=MagicMock(); i.response.send_message=AsyncMock()
        await self.bot.tree.get_command('schedule').callback(i,'20:00','Not/AZone')
        self.assertIsNone(self.store.get_setting('post_time'))
        self.assertIn('timezone', i.response.send_message.call_args.args[0])

    async def test_settings_newlines_and_command_registration(self):
        i=MagicMock(); i.response.send_message=AsyncMock()
        await self.bot.tree.get_command('settings').callback(i)
        message=i.response.send_message.call_args.args[0]
        self.assertIn('\n',message); self.assertNotIn('\\n',message)
        self.assertEqual(len(self.bot.tree.get_commands()),9)
        self.assertTrue(self.bot.intents.guilds)
        self.assertFalse(self.bot.intents.message_content)

if __name__=='__main__': unittest.main()
