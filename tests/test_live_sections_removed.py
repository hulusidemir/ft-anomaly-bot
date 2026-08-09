import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
from main import app


class LiveSectionsRemovalTests(unittest.IsolatedAsyncioTestCase):
    def test_live_section_routes_are_not_registered(self):
        paths = {route.path for route in app.routes}

        self.assertNotIn("/api/live-matches", paths)
        self.assertNotIn("/api/live-matches-2", paths)
        self.assertNotIn("/api/live-detections", paths)
        self.assertNotIn("/api/trigger/live-scan", paths)
        self.assertIn("/api/anomalies/{event_id}/details", paths)

    def test_dashboard_has_no_removed_tabs(self):
        dashboard = Path("templates/dashboard.html").read_text(encoding="utf-8")

        self.assertNotIn('data-tab="live"', dashboard)
        self.assertNotIn('data-tab="live2"', dashboard)
        self.assertNotIn('data-tab="detections"', dashboard)

    async def test_init_removes_legacy_live_action_table(self):
        previous_path = db.DATABASE_PATH
        previous_db = db._db
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE live_match_actions (event_id TEXT PRIMARY KEY)"
            )
            connection.commit()
            connection.close()

            db.DATABASE_PATH = database_path
            db._db = None
            await db.init_db()

            connection = sqlite3.connect(database_path)
            row = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'live_match_actions'"
            ).fetchone()
            connection.close()
            self.assertIsNone(row)
        finally:
            await db.close_db()
            db.DATABASE_PATH = previous_path
            db._db = previous_db
            os.unlink(database_path)


if __name__ == "__main__":
    unittest.main()
