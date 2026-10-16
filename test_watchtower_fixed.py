"""Automated functional tests for the WatchTower Flask application.

Copy this file into the WatchTower project folder, then run:
    python test_watchtower.py

These tests use Flask's test client and mocked webpage responses. They do not
make repeated requests to real websites and do not modify your normal database.
"""

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
import sqlite3
from unittest.mock import patch

# Ensure app.py and templates/ are found when this script is run from the
# WatchTower project directory or by its absolute path.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import app as watchtower  # noqa: E402

# The project uses ``with db() as conn``, but sqlite3's connection context
# manager commits/rolls back without closing the connection. Close each test
# connection explicitly so Windows can remove the temporary database.
@contextmanager
def test_db():
    conn = sqlite3.connect(watchtower.DB, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()

watchtower.db = test_db

URL = "https://example.com/"
VERSION_1 = "Market Bulletin Market status Open Company ABC Technologies Share price 100 Market update Normal trading"
VERSION_2 = "Market Bulletin Market status Open Company ABC Technologies Share price 105 Market update Trading volume increased Announcement Quarterly results released"


class WatchTowerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="watchtower_test_")
        watchtower.DB = os.path.join(self.temp_dir.name, "test_website.db")
        watchtower.create_tables()
        watchtower.app.config.update(TESTING=True)
        self.client = watchtower.app.test_client()

    def tearDown(self):
        # Close Flask's test client before removing the temporary DB folder.
        self.client = None
        self.temp_dir.cleanup()

    def post_save(self, content=VERSION_1, url=URL):
        with patch.object(watchtower, "fetch_text", return_value=(content, None)):
            return self.client.post("/", data={
                "action": "save",
                "name": "Stock Market Test",
                "url": url,
                "check_interval": "5",
            })

    def get_counts(self):
        with watchtower.db() as conn:
            websites = conn.execute("SELECT COUNT(*) FROM websites").fetchone()[0]
            snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            history = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        return websites, snapshots, history

    def test_01_dashboard_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Monitoring dashboard", response.data)
        self.assertEqual(self.get_counts(), (0, 0, 0))

    def test_02_save_baseline_creates_website_and_snapshot(self):
        response = self.post_save()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Baseline saved", response.data)
        self.assertEqual(self.get_counts(), (1, 1, 1))

    def test_03_unchanged_page_does_not_create_snapshot(self):
        self.post_save(VERSION_1)
        with patch.object(watchtower, "fetch_text", return_value=(VERSION_1, None)):
            response = self.client.post("/", data={"action": "check", "url": URL})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No changes detected", response.data)
        self.assertEqual(self.get_counts()[1], 1)

    def test_04_changed_page_updates_status_diff_and_snapshot(self):
        self.post_save(VERSION_1)
        with patch.object(watchtower, "fetch_text", return_value=(VERSION_2, None)):
            response = self.client.post("/", data={"action": "check", "url": URL})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Changes detected", response.data)
        self.assertIn(b"105", response.data)
        self.assertEqual(self.get_counts()[1], 2)
        with watchtower.db() as conn:
            row = conn.execute("SELECT baseline,status FROM websites WHERE url=?", (URL,)).fetchone()
        self.assertEqual(row["baseline"], VERSION_2)
        self.assertEqual(row["status"], "Changes detected")

    def test_05_second_check_after_change_is_unchanged(self):
        self.post_save(VERSION_1)
        with patch.object(watchtower, "fetch_text", return_value=(VERSION_2, None)):
            self.client.post("/", data={"action": "check", "url": URL})
        with patch.object(watchtower, "fetch_text", return_value=(VERSION_2, None)):
            response = self.client.post("/", data={"action": "check", "url": URL})
        self.assertIn(b"No changes detected", response.data)
        self.assertEqual(self.get_counts()[1], 2)

    def test_06_check_before_baseline_shows_error(self):
        response = self.client.post("/", data={"action": "check", "url": URL})
        self.assertIn(b"Save a baseline", response.data)
        self.assertEqual(self.get_counts()[0], 0)

    def test_07_localhost_is_rejected(self):
        # Real fetch_text validation rejects localhost before making an HTTP request.
        response = self.client.post("/", data={
            "action": "save", "name": "Local test",
            "url": "http://localhost:5000", "check_interval": "5",
        })
        self.assertIn(b"Local and internal hostnames are not supported", response.data)
        self.assertEqual(self.get_counts()[0], 0)

    def test_08_fetch_failure_sets_error_status_and_history(self):
        self.post_save()
        with patch.object(watchtower, "fetch_text", return_value=(None, "Simulated test failure")):
            response = self.client.post("/", data={"action": "check", "url": URL})
        self.assertIn(b"Simulated test failure", response.data)
        with watchtower.db() as conn:
            row = conn.execute("SELECT status FROM websites WHERE url=?", (URL,)).fetchone()
            event = conn.execute("SELECT result,details FROM history ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], "Error")
        self.assertEqual(event["result"], "Error")
        self.assertIn("Simulated test failure", event["details"])

    def test_09_compare_two_versions_of_same_website(self):
        self.post_save(VERSION_1)
        with patch.object(watchtower, "fetch_text", return_value=(VERSION_2, None)):
            self.client.post("/", data={"action": "check", "url": URL})
        with watchtower.db() as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM snapshots WHERE url=? ORDER BY id", (URL,)
            ).fetchall()]
        response = self.client.post("/", data={
            "action": "compare", "version_a": str(ids[0]), "version_b": str(ids[1])
        })
        self.assertIn(b"Version comparison completed", response.data)
        self.assertIn(b"100", response.data)
        self.assertIn(b"105", response.data)

    def test_10_comparing_same_version_is_rejected(self):
        self.post_save()
        with watchtower.db() as conn:
            snapshot_id = conn.execute("SELECT id FROM snapshots LIMIT 1").fetchone()[0]
        response = self.client.post("/", data={
            "action": "compare", "version_a": str(snapshot_id), "version_b": str(snapshot_id)
        })
        self.assertIn(b"Choose two different versions", response.data)

    def test_11_invalid_version_ids_are_rejected(self):
        response = self.client.post("/", data={
            "action": "compare", "version_a": "999999", "version_b": "888888"
        })
        self.assertIn(b"Select two valid saved versions", response.data)

    def test_12_snapshot_hash_changes_with_content(self):
        self.assertTrue(watchtower.snapshot(URL, VERSION_1))
        self.assertFalse(watchtower.snapshot(URL, VERSION_1))
        self.assertTrue(watchtower.snapshot(URL, VERSION_2))
        self.assertEqual(self.get_counts()[1], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
