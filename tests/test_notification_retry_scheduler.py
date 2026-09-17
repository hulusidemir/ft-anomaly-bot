import unittest
from unittest.mock import AsyncMock, patch

import main
import workers


class RecordingScheduler:
    def __init__(self):
        self.jobs = {}
        self.start_count = 0
        self.shutdown_count = 0

    def add_job(self, func, trigger, **options):
        job_id = options["id"]
        if job_id in self.jobs and not options.get("replace_existing"):
            raise AssertionError(f"duplicate job registration: {job_id}")
        self.jobs[job_id] = {
            "func": func,
            "trigger": trigger,
            "options": options,
        }

    def start(self):
        self.start_count += 1

    def shutdown(self, wait=False):
        self.shutdown_count += 1


class NotificationRetrySchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_job_registration_is_periodic_and_idempotent(self):
        scheduler = RecordingScheduler()
        with (
            patch.object(main, "scheduler", scheduler),
            patch.object(main, "init_db", new_callable=AsyncMock),
            patch.object(main, "close_db", new_callable=AsyncMock),
            patch.object(main, "send_telegram", new_callable=AsyncMock),
            patch.object(main.scraper, "close", new_callable=AsyncMock),
        ):
            async with main.lifespan(main.app):
                pass

        self.assertEqual(
            set(scheduler.jobs),
            {"anomaly_scan", "finished_match_scan", "notification_retry"},
        )
        retry_job = scheduler.jobs["notification_retry"]
        self.assertIs(retry_job["func"], workers.notification_retry_scan)
        self.assertEqual(retry_job["trigger"], "interval")
        self.assertEqual(retry_job["options"]["seconds"], 60)
        self.assertEqual(retry_job["options"]["id"], "notification_retry")
        self.assertTrue(retry_job["options"]["replace_existing"])

        scheduler.add_job(
            retry_job["func"],
            retry_job["trigger"],
            **retry_job["options"],
        )
        self.assertEqual(len(scheduler.jobs), 3)
        self.assertEqual(scheduler.start_count, 1)
        self.assertEqual(scheduler.shutdown_count, 1)


if __name__ == "__main__":
    unittest.main()
