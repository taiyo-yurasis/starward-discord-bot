import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import storage
import main

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.old_path = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / 'battles.db'
        storage._conn = None

    def tearDown(self):
        if storage._conn:
            storage._conn.close()
            storage._conn = None
        storage.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_migration_existing_rows_and_repeat(self):
        con = sqlite3.connect(storage.DB_PATH)
        con.execute('CREATE TABLE battles (battle_id TEXT PRIMARY KEY, season_code TEXT, played_at INTEGER, saved_at INTEGER NOT NULL, result TEXT, is_mvp INTEGER, character TEXT, partner_character TEXT, enemy_characters TEXT, score_before INTEGER, score_after INTEGER, score_diff INTEGER, damage INTEGER, kills INTEGER, deaths INTEGER, round_time INTEGER, raw_list TEXT, raw_detail TEXT)')
        con.execute("INSERT INTO battles (battle_id,saved_at,result) VALUES ('old',1,'WIN')")
        con.commit(); con.close()
        storage.init_db()
        self.assertEqual(storage.pending_notifications(), [])
        storage.init_db()
        self.assertEqual(storage.count_battles(), 1)

    def test_pending_only_after_successful_detail_and_retries(self):
        storage.init_db()
        row = {c: None for c in storage.COLUMNS}
        row.update(battle_id='n', result='WIN', saved_at=10)
        self.assertTrue(storage.insert_battle(row, notification_pending=True))
        self.assertFalse(storage.insert_battle(row, notification_pending=True))
        self.assertEqual(len(storage.pending_notifications(1)), 1)
        self.assertTrue(storage.mark_notification_sent('n'))
        self.assertEqual(storage.pending_notifications(), [])


class ApiTests(unittest.TestCase):
    def test_list_distinguishes_empty_and_failure(self):
        self.assertEqual(main.parse_battle_list_response({"code": 0, "data": []}), [])
        self.assertIsNone(main.parse_battle_list_response({"code": 9, "data": []}))
        self.assertIsNone(main.parse_battle_list_response({"code": 0, "data": None}))

    def test_season_error_code_is_not_success(self):
        self.assertFalse(main.api_ok({"code": 4, "data": []}))
        self.assertTrue(main.api_ok({"code": 0, "data": []}))

if __name__ == '__main__': unittest.main()
