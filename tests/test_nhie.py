from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from truth_or_dare_bot.config import Config
from truth_or_dare_bot.__main__ import TruthDareBot
from truth_or_dare_bot.storage import Store


def legacy_schema(identity="INTEGER PRIMARY KEY", timestamp="CURRENT_TIMESTAMP"):
    return [
        f"""CREATE TABLE prompts (id {identity},
            kind TEXT NOT NULL CHECK(kind IN ('truth','dare')),
            intensity INTEGER NOT NULL CHECK(intensity BETWEEN 1 AND 5),
            theme TEXT, text TEXT NOT NULL UNIQUE)""",
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        f"""CREATE TABLE history (id {identity}, prompt_id INTEGER NOT NULL REFERENCES prompts(id),
            posted_at TEXT NOT NULL DEFAULT {timestamp})""",
        "CREATE TABLE scheduled_days (day TEXT PRIMARY KEY, status TEXT NOT NULL)",
    ]


def legacy_rows(execute):
    execute("INSERT INTO prompts VALUES(41,'truth',2,'fun','Existing truth')")
    execute("INSERT INTO prompts VALUES(99,'dare',3,'creative','Existing dare')")
    execute("INSERT INTO history VALUES(7,41,'2026-09-12 19:00:00')")
    execute("INSERT INTO settings VALUES('mode','truth')")
    execute("INSERT INTO settings VALUES('post_time','19:00')")
    execute("INSERT INTO scheduled_days VALUES('2026-09-12','posted')")


class SQLiteMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for sql in legacy_schema(): conn.execute(sql)
            legacy_rows(conn.execute)
            conn.execute("CREATE INDEX prompts_theme ON prompts(theme)")
            conn.execute("""CREATE TRIGGER prompt_added AFTER INSERT ON prompts BEGIN
                INSERT INTO settings(key,value) VALUES('last_added',NEW.text)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value; END""")
        self.before = self.snapshot()

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in ('prompts', 'history', 'settings', 'scheduled_days')}

    def test_existing_rows_dependencies_and_constraints_survive_upgrade_and_restart(self):
        store = Store(self.path)
        try:
            self.assertEqual(self.snapshot(), self.before)
            self.assertEqual(store.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(store.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(store.add_prompts([('nhie', 2, 'fun', 'Never have I ever tested NHIE.')]), 1)
            prompt = store.choose_prompt('nhie', 'FUN', 2)
            self.assertGreater(prompt.id, 99)
            self.assertEqual(store.get_setting('last_added'), prompt.text)
            store.record_post(prompt.id)
            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction():
                    store.execute("INSERT INTO prompts(kind,intensity,text) VALUES('invalid',1,'bad')")
            with self.assertRaises(sqlite3.IntegrityError):
                with store.transaction(): store.execute('DELETE FROM prompts WHERE id=41')
            index = store.execute("SELECT name FROM sqlite_schema WHERE name='prompts_theme'").fetchone()
            self.assertIsNotNone(index)
        finally: store.close()
        after = self.snapshot()
        reopened = Store(self.path)
        try:
            self.assertEqual(self.snapshot(), after)
            self.assertEqual(reopened.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 1)
            self.assertEqual(reopened.choose_prompt('nhie').text, prompt.text)
        finally: reopened.close()

    def test_failure_after_table_replacement_rolls_back_and_allows_retry(self):
        original = Store.execute
        def fail_marker(store, sql, values=()):
            if sql == 'INSERT INTO schema_migrations(version) VALUES(1)':
                raise RuntimeError('injected migration failure')
            return original(store, sql, values)
        with patch.object(Store, 'execute', fail_marker):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                Store(self.path)
        self.assertEqual(self.snapshot(), self.before)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_schema WHERE name='prompts_nhie_upgrade'").fetchone())
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO prompts(kind,intensity,text) VALUES('nhie',1,'not migrated')")
        recovered = Store(self.path)
        try: self.assertEqual(recovered.add_prompts([('nhie', 1, None, 'Retry works')]), 1)
        finally: recovered.close()

    def test_unexpected_column_stops_without_losing_data(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("ALTER TABLE prompts ADD COLUMN custom TEXT DEFAULT 'keep me'")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'Unexpected prompt schema'):
            Store(self.path)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_startups_apply_migration_once(self):
        barrier = threading.Barrier(2)
        def start():
            barrier.wait(timeout=10)
            store = Store(self.path)
            try: return store.count_prompts()
            finally: store.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start) for _ in range(2)]
            self.assertEqual([future.result(timeout=20) for future in futures], [2, 2])
        self.assertEqual(self.snapshot(), self.before)


class NHIEBehavior(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.bot = TruthDareBot(Config(1, 2), self.store)
        self.channel = MagicMock()
        self.channel.send = AsyncMock()
        self.bot.target_channel = AsyncMock(return_value=self.channel)
        self.now = datetime(2026, 9, 13, 19, 5, tzinfo=ZoneInfo('America/Chicago'))
        self.bot.local_now = lambda: self.now

    async def asyncTearDown(self):
        await self.bot.close()
        self.store.close()
        self.tmp.cleanup()

    def interaction(self):
        i = MagicMock()
        i.response.send_message = AsyncMock()
        i.response.defer = AsyncMock()
        i.followup.send = AsyncMock()
        return i

    async def test_nhie_is_separate_filtered_and_included_in_mixed(self):
        self.assertIsNone(self.store.choose_prompt('nhie'))
        self.assertEqual(self.store.add_prompts([(' NHIE ', 2, 'FUN', 'Never have I ever tried this.')]), 1)
        prompt = self.store.choose_prompt('nhie', 'fun', 2)
        self.assertEqual(prompt.kind, 'nhie')
        self.assertIsNone(self.store.choose_prompt('nhie', 'fun', 1))
        self.assertIsNone(self.store.choose_prompt('nhie', 'missing'))
        sequence = []
        for _ in range(7):
            selected = self.store.choose_prompt('mixed')
            sequence.append(selected)
            self.store.record_post(selected.id)
        self.assertEqual(len({p.id for p in sequence}), 7)
        self.assertEqual({p.kind for p in sequence}, {'truth', 'dare', 'nhie'})
        self.assertEqual(self.store.choose_prompt('truth').kind, 'truth')
        self.assertEqual(self.store.choose_prompt('dare').kind, 'dare')

    async def test_add_import_and_invalid_batch_are_atomic(self):
        self.bot.require_admin = AsyncMock(return_value=True)
        i = self.interaction()
        await self.bot.tree.get_command('add-prompt').callback(i, 'nhie', 1, 'Never have I ever added one.', 'fun')
        self.assertEqual(self.store.count_prompts(), 7)
        self.assertEqual(i.response.send_message.call_args.args[0], 'Prompt added.')
        await self.bot.tree.get_command('import-prompts').callback(i,
            'NHIE|2|stories|Never have I ever imported one.\ntruth|1|fun|Imported truth')
        self.assertEqual(self.store.count_prompts(), 9)
        await self.bot.tree.get_command('import-prompts').callback(i,
            'nhie|1|fun|Must not persist\ninvalid|1|fun|Rejected')
        self.assertEqual(self.store.count_prompts(), 9)
        self.assertIn('Nothing imported', i.response.send_message.call_args.args[0])
        await self.bot.tree.get_command('add-prompt').callback(i, 'nhie', 1, 'Never have I ever added one.', 'fun')
        self.assertEqual(self.store.count_prompts(), 9)

    async def test_nhie_commands_remain_admin_only(self):
        self.bot.require_admin = AsyncMock(return_value=False)
        i = self.interaction()
        await self.bot.tree.get_command('add-prompt').callback(i, 'nhie', 1, 'Blocked')
        await self.bot.tree.get_command('import-prompts').callback(i, 'nhie|1||Blocked import')
        await self.bot.tree.get_command('configure').callback(i, 'nhie')
        self.assertEqual(self.store.count_prompts(), 6)
        self.assertIsNone(self.store.get_setting('mode'))

    async def test_manual_nhie_posts_full_title_text_and_records_delivery(self):
        self.store.add_prompts([('nhie', 3, 'stories', 'Never have I ever done this.')])
        i = self.interaction()
        await self.bot.tree.get_command('ask-now').callback(i, 'nhie', 'stories', 3)
        self.channel.send.assert_awaited_once()
        embed = self.channel.send.call_args.kwargs['embed']
        self.assertEqual(embed.title, 'Never Have I Ever • Level 3')
        self.assertEqual(embed.description, 'Never have I ever done this.')
        self.assertEqual(self.store.execute('SELECT COUNT(*) AS n FROM history').fetchone()['n'], 1)
        self.assertEqual(i.followup.send.call_args.args[0], 'Prompt posted.')

    async def test_empty_nhie_pool_does_not_post_or_change_daily_filters(self):
        self.bot.require_admin = AsyncMock(return_value=True)
        self.store.set_setting('mode', 'truth')
        i = self.interaction()
        await self.bot.tree.get_command('ask-now').callback(i, 'nhie')
        self.assertEqual(i.followup.send.call_args.args[0], 'No prompt matches those filters.')
        await self.bot.tree.get_command('configure').callback(i, 'nhie')
        self.assertEqual(self.store.get_setting('mode'), 'truth')
        self.channel.send.assert_not_called()

    async def test_daily_nhie_configuration_survives_restart_and_posts_once(self):
        self.bot.require_admin = AsyncMock(return_value=True)
        self.store.add_prompts([('nhie', 2, 'fun', 'Never have I ever scheduled this.')])
        i = self.interaction()
        await self.bot.tree.get_command('configure').callback(i, 'nhie', 'fun', 2)
        self.store.close()
        self.store = Store(self.path)
        self.bot.store = self.store
        self.assertEqual(self.store.get_setting('mode'), 'nhie')
        await self.bot.scheduled_tick(self.now)
        await self.bot.scheduled_tick(self.now)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.call_args.kwargs['embed'].title, 'Never Have I Ever • Level 2')
        self.assertEqual(self.store.get_setting('last_post_date'), '2026-09-13')
        await self.bot.tree.get_command('settings').callback(i)
        self.assertIn('Mode: Never Have I Ever', i.response.send_message.call_args.args[0])

    async def test_discord_registration_includes_nhie_across_commands(self):
        for name, parameter, expected in [
            ('ask-now', 'mode', ['mixed', 'truth', 'dare', 'nhie']),
            ('configure', 'mode', ['mixed', 'truth', 'dare', 'nhie']),
            ('add-prompt', 'kind', ['truth', 'dare', 'nhie']),
        ]:
            payload = self.bot.tree.get_command(name).to_dict(self.bot.tree)
            self.assertTrue(1 <= len(payload['description']) <= 100)
            for option in payload['options']:
                self.assertTrue(1 <= len(option['description']) <= 100)
            option = next(o for o in payload['options'] if o['name'] == parameter)
            self.assertEqual([c['value'] for c in option['choices']], expected)
            self.assertIn('Never Have I Ever', option['choices'][-1]['name'])
        self.assertEqual(len(self.bot.tree.get_commands()), 9)


if __name__ == '__main__': unittest.main()
