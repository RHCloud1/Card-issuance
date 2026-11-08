"""Exercise the shared SQLite bootstrap under independent worker processes."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / ".review-artifacts" / "tmp"
WORKER = r"""
import os
import sys
import time
from pathlib import Path
from app.main import create_app

barrier = Path(sys.argv[1])
(barrier / ('ready-' + str(os.getpid()))).write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 15
while not (barrier / 'start').exists():
    if time.monotonic() > deadline:
        raise TimeoutError('parent did not release startup barrier')
    time.sleep(0.01)
create_app()
create_app()
print('bootstrap complete')
"""


class InitializationTests(unittest.TestCase):
    def test_fresh_database_bootstrap_is_idempotent_across_processes(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bootstrap-", dir=TEMP_ROOT) as folder:
            directory = Path(folder)
            database = directory / "fresh.sqlite3"
            environment = dict(os.environ)
            environment.update({
                "DATABASE_PATH": str(database),
                "APP_SECRET": "independent-worker-test-secret",
                "ADMIN_USERNAME": "worker-admin@example.com",
                "ADMIN_PASSWORD": "worker-test-password",
                "ADMIN_PATH": "/worker-admin",
                "ENABLE_MOCK_PAYMENTS": "false",
                "TEMP": folder,
                "TMP": folder,
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            processes = []
            try:
                for _ in range(4):
                    processes.append(subprocess.Popen(
                        [sys.executable, "-c", WORKER, folder],
                        cwd=ROOT, env=environment, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    ))
                deadline = time.monotonic() + 15
                while len(list(directory.glob("ready-*"))) < len(processes):
                    if any(process.poll() is not None for process in processes):
                        failures = [process.communicate() for process in processes if process.poll() is not None]
                        self.fail(f"A bootstrap worker exited before the barrier: {failures}")
                    if time.monotonic() > deadline:
                        self.fail("Bootstrap workers did not reach their shared barrier")
                    time.sleep(0.01)
                # All four workers now race to initialize the same nonexistent database.
                (directory / "start").write_text("go", encoding="utf-8")
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
                    self.assertIn("bootstrap complete", stdout)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT username FROM admin_users").fetchone()[0], "worker-admin@example.com")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0], 1)
                providers = connection.execute("SELECT code FROM payment_providers ORDER BY code").fetchall()
                self.assertEqual(providers, [("aggregate",), ("alipay",), ("mock",), ("usdt_trc20",), ("wechat",)])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
