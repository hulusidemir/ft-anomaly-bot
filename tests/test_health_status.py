import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


class FakeScheduler:
    def __init__(self, running=True, job_ids=()):
        self.running = running
        self._jobs = [
            SimpleNamespace(id=job_id, next_run_time=None)
            for job_id in job_ids
        ]

    def get_jobs(self):
        return list(self._jobs)


class HealthStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_scheduler_and_required_jobs(self):
        scheduler = FakeScheduler(
            job_ids=("anomaly_scan", "finished_match_scan", "notification_retry")
        )
        with patch.object(main, "scheduler", scheduler):
            result = await main.api_status()

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["service_status"], "running")
        self.assertTrue(result["scheduler_running"])
        self.assertTrue(result["anomaly_scan_job_present"])
        self.assertTrue(result["notification_retry_job_present"])
        self.assertEqual(
            {job["id"] for job in result["scheduler_jobs"]},
            {"anomaly_scan", "finished_match_scan", "notification_retry"},
        )

    async def test_recent_successful_scan_is_healthy(self):
        scheduler = FakeScheduler(
            job_ids=("anomaly_scan", "notification_retry")
        )
        now = time.time()
        runtime = {
            "last_anomaly_scan_at": now - 5,
            "last_successful_live_fetch_at": now - 6,
            "last_anomaly_scan_error": None,
            "last_live_match_count": 4,
            "last_processed_match_count": 4,
        }
        with (
            patch.object(main, "scheduler", scheduler),
            patch.multiple(main.workers, **runtime, create=True),
            patch.object(main.time, "time", return_value=now),
        ):
            result = await main.api_status()

        self.assertEqual(result["health"], "healthy")
        self.assertEqual(result["last_live_match_count"], 4)
        self.assertEqual(result["last_processed_match_count"], 4)

    async def test_scan_error_is_degraded(self):
        scheduler = FakeScheduler(
            job_ids=("anomaly_scan", "notification_retry")
        )
        with (
            patch.object(main, "scheduler", scheduler),
            patch.multiple(
                main.workers,
                last_anomaly_scan_at=time.time(),
                last_anomaly_scan_error="fetch failed",
                create=True,
            ),
        ):
            result = await main.api_status()

        self.assertEqual(result["health"], "degraded")
        self.assertEqual(result["last_scan_error"], "fetch failed")

    async def test_stale_scan_is_degraded(self):
        scheduler = FakeScheduler(
            job_ids=("anomaly_scan", "notification_retry")
        )
        stale_at = time.time() - main.config.SCAN_INTERVAL_SECONDS * 3
        with (
            patch.object(main, "scheduler", scheduler),
            patch.multiple(
                main.workers,
                last_anomaly_scan_at=stale_at,
                last_anomaly_scan_error=None,
                create=True,
            ),
        ):
            result = await main.api_status()

        self.assertEqual(result["health"], "degraded")

    async def test_unknown_runtime_values_are_none(self):
        scheduler = FakeScheduler(
            job_ids=("anomaly_scan", "notification_retry")
        )
        with patch.object(main, "scheduler", scheduler):
            result = await main.api_status()

        self.assertEqual(result["health"], "degraded")
        self.assertIsNone(result["last_anomaly_scan_at"])
        self.assertIsNone(result["last_successful_live_fetch_at"])
        self.assertIsNone(result["last_live_match_count"])
        self.assertIsNone(result["last_processed_match_count"])
        self.assertIsNone(result["last_scan_error"])
        self.assertIsNone(result["seconds_since_successful_scan"])

    async def test_status_route_is_preserved(self):
        routes = {route.path for route in main.app.routes}
        self.assertIn("/api/status", routes)


if __name__ == "__main__":
    unittest.main()
