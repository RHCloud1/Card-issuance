from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from scripts.backup import backup_database


ROOT = Path(__file__).resolve().parents[1]


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / '.review-artifacts' / 'tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='backup-test-', dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'source.sqlite3'

    def seed_database(self):
        connection = sqlite3.connect(self.source)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE inventory (id INTEGER PRIMARY KEY, content TEXT)')
        connection.execute('INSERT INTO inventory (content) VALUES (?)', ('committed-card',))
        connection.commit()
        return connection

    def assert_valid_snapshot(self, path):
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('SELECT content FROM inventory').fetchall(), [('committed-card',)])

    def test_snapshot_contains_committed_wal_and_excludes_uncommitted_write(self):
        connection = self.seed_database()
        try:
            wal = Path(str(self.source) + '-wal')
            self.assertGreater(wal.stat().st_size, 0)
            connection.execute('INSERT INTO inventory (content) VALUES (?)', ('uncommitted-card',))
            destination = self.directory / 'snapshot.sqlite3'
            backup_database(self.source, destination)
            self.assert_valid_snapshot(destination)
        finally:
            connection.rollback()
            connection.close()

    def test_missing_source_preserves_existing_backup_and_does_not_create_source(self):
        missing = self.directory / 'missing.sqlite3'
        destination = self.directory / 'existing.sqlite3'
        destination.write_bytes(b'previous-backup')
        with self.assertRaises((FileNotFoundError, ValueError, sqlite3.Error)):
            backup_database(missing, destination)
        self.assertFalse(missing.exists())
        self.assertEqual(destination.read_bytes(), b'previous-backup')

    def test_cannot_replace_live_database_with_its_own_backup(self):
        connection = self.seed_database()
        try:
            with self.assertRaises(ValueError):
                backup_database(self.source, self.source.parent / '.' / self.source.name)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM inventory').fetchone()[0], 1)
        finally:
            connection.close()

    def test_stdout_is_a_usable_database_without_log_text(self):
        connection = self.seed_database()
        try:
            environment = os.environ.copy()
            environment.update(TEMP=str(self.directory), TMP=str(self.directory), TMPDIR=str(self.directory))
            result = subprocess.run(
                [sys.executable, str(ROOT / 'scripts' / 'backup.py'), '--source', str(self.source), '--output', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertTrue(result.stdout.startswith(b'SQLite format 3\x00'))
            destination = self.directory / 'streamed.sqlite3'
            destination.write_bytes(result.stdout)
            self.assert_valid_snapshot(destination)
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
