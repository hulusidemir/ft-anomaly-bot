import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import db
import workers


class NotificationRetryWorkerTests(unittest.IsolatedAsyncioTestCase):
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

    async def _delivery(self, anomaly_id, chat_id="chat-1", text="alert"):
        delivery, _ = await db.create_notification_delivery(
            anomaly_id, chat_id, text, now=100.0
        )
        return delivery

    async def test_due_failed_delivery_is_retried(self):
        delivery = await self._delivery(1)
        await db.record_notification_failure(
            delivery["id"], "offline", next_retry_at=150.0, now=110.0
        )
        send = AsyncMock()

        with patch.object(workers, "send_telegram", send):
            await workers.notification_retry_scan()

        send.assert_awaited_once_with("alert", anomaly_id=1)

    async def test_due_pending_delivery_is_retried(self):
        send = AsyncMock()
        await self._delivery(2)

        with patch.object(workers, "send_telegram", send):
            await workers.notification_retry_scan()

        send.assert_awaited_once_with("alert", anomaly_id=2)

    async def test_sent_delivery_is_not_retried(self):
        delivery = await self._delivery(3)
        await db.mark_notification_sent(delivery["id"], telegram_message_id=10)
        send = AsyncMock()

        with patch.object(workers, "send_telegram", send):
            await workers.notification_retry_scan()

        send.assert_not_awaited()

    async def test_future_retry_is_not_retried(self):
        delivery = await self._delivery(4)
        await db.record_notification_failure(
            delivery["id"], "offline", next_retry_at=9999999999.0
        )
        send = AsyncMock()

        with patch.object(workers, "send_telegram", send):
            await workers.notification_retry_scan()

        send.assert_not_awaited()

    async def test_one_delivery_error_does_not_block_the_next(self):
        first = await self._delivery(5, chat_id="chat-1", text="first")
        second = await self._delivery(6, chat_id="chat-2", text="second")
        send = AsyncMock(side_effect=[RuntimeError("offline"), None])

        with patch.object(workers, "send_telegram", send):
            await workers.notification_retry_scan()

        self.assertEqual(send.await_count, 2)
        self.assertEqual(
            [call.args for call in send.await_args_list],
            [(first["message_text"],), (second["message_text"],)],
        )

    async def test_retry_does_not_create_duplicate_delivery_row(self):
        delivery = await self._delivery(7)
        send = AsyncMock()

        with patch.object(workers, "send_telegram", send):
            await workers.notification_retry_scan()

        async with db.get_db() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM notification_deliveries WHERE id = ?",
                (delivery["id"],),
            )
            self.assertEqual((await cursor.fetchone())[0], 1)

    async def test_no_due_deliveries_completes_cleanly(self):
        send = AsyncMock()

        with patch.object(workers, "send_telegram", send):
            result = await workers.notification_retry_scan()

        self.assertIsNone(result)
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
