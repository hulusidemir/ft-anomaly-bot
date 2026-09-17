import os
import tempfile
import unittest

import db


class NotificationDeliveryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.previous_path = db.DATABASE_PATH
        db.DATABASE_PATH = self.database_path
        await db.init_db()

    async def asyncTearDown(self):
        await db.close_db()
        db.DATABASE_PATH = self.previous_path
        os.unlink(self.database_path)

    async def test_pending_creation_and_recipient_dedupe(self):
        first, created = await db.create_notification_delivery(
            10, "chat-1", "alert", now=100.0
        )
        duplicate, duplicate_created = await db.create_notification_delivery(
            10, "chat-1", "changed alert", now=101.0
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(first["state"], "PENDING")
        self.assertEqual(first["attempt_count"], 0)
        self.assertIsNone(first["sent_at"])
        self.assertEqual(duplicate["message_text"], "alert")

    async def test_failed_retry_uses_same_row_and_preserves_retry_metadata(self):
        delivery, _ = await db.create_notification_delivery(
            11, "chat-1", "alert", now=100.0
        )
        failed = await db.record_notification_failure(
            delivery["id"], "network down", next_retry_at=200.0, now=110.0
        )
        retried = await db.record_notification_failure(
            delivery["id"], "still down", next_retry_at=300.0, now=210.0
        )

        self.assertEqual(failed["id"], delivery["id"])
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(failed["attempt_count"], 1)
        self.assertEqual(failed["last_error"], "network down")
        self.assertEqual(failed["next_attempt_at"], 200.0)
        self.assertEqual(retried["id"], delivery["id"])
        self.assertEqual(retried["attempt_count"], 2)
        self.assertEqual(retried["last_error"], "still down")
        self.assertEqual(retried["next_attempt_at"], 300.0)
        self.assertIsNone(retried["sent_at"])

    async def test_sent_clears_retry_state_and_is_not_reopened(self):
        delivery, _ = await db.create_notification_delivery(
            12, "chat-1", "alert", now=100.0
        )
        await db.record_notification_failure(
            delivery["id"], "temporary", next_retry_at=200.0, now=110.0
        )
        sent = await db.mark_notification_sent(
            delivery["id"], telegram_message_id=55, sent_at=150.0, now=150.0
        )
        duplicate, created = await db.create_notification_delivery(
            12, "chat-1", "again", now=160.0
        )
        after_sent_failure = await db.record_notification_failure(
            delivery["id"], "late failure", next_retry_at=170.0, now=160.0
        )

        self.assertEqual(sent["state"], "SENT")
        self.assertEqual(sent["attempt_count"], 1)
        self.assertEqual(sent["sent_at"], 150.0)
        self.assertIsNone(sent["last_error"])
        self.assertIsNone(sent["next_attempt_at"])
        self.assertEqual(sent["telegram_message_id"], 55)
        self.assertFalse(created)
        self.assertEqual(duplicate["state"], "SENT")
        self.assertEqual(after_sent_failure["state"], "SENT")
        self.assertEqual(after_sent_failure["attempt_count"], 1)

    async def test_due_query_returns_only_ready_pending_or_failed_rows(self):
        pending, _ = await db.create_notification_delivery(
            20, "chat-1", "pending", now=100.0
        )
        due_failed, _ = await db.create_notification_delivery(
            21, "chat-1", "due", now=101.0
        )
        future_failed, _ = await db.create_notification_delivery(
            22, "chat-1", "future", now=102.0
        )
        sent, _ = await db.create_notification_delivery(
            23, "chat-1", "sent", now=103.0
        )
        await db.record_notification_failure(
            due_failed["id"], "retry", next_retry_at=150.0, now=110.0
        )
        await db.record_notification_failure(
            future_failed["id"], "retry", next_retry_at=250.0, now=111.0
        )
        await db.mark_notification_sent(sent["id"], sent_at=120.0, now=120.0)

        due = await db.get_due_notification_deliveries(now=200.0)
        self.assertEqual(
            [(row["anomaly_id"], row["state"]) for row in due],
            [(pending["anomaly_id"], "PENDING"), (due_failed["anomaly_id"], "FAILED")],
        )
        self.assertNotIn(future_failed["anomaly_id"], [row["anomaly_id"] for row in due])
        self.assertNotIn(sent["anomaly_id"], [row["anomaly_id"] for row in due])


if __name__ == "__main__":
    unittest.main()
