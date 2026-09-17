import unittest
from unittest.mock import AsyncMock, patch

import notifier


class NotifierDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.delivery = {
            "id": 7,
            "state": "PENDING",
            "attempt_count": 0,
        }

    def _patch_common(self, chats=("chat-a",)):
        return patch.multiple(
            notifier,
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_IDS=chats,
        )

    async def test_pending_delivery_is_sent_and_marked_sent(self):
        create = AsyncMock(return_value=(self.delivery, True))
        mark_sent = AsyncMock()
        with (
            self._patch_common(),
            patch.object(notifier, "create_notification_delivery", create),
            patch.object(notifier, "mark_notification_sent", mark_sent),
            patch.object(notifier, "_send_to_chat", AsyncMock(return_value=101)),
        ):
            result = await notifier.send_telegram("hello", anomaly_id=42)

        self.assertEqual(result, 101)
        create.assert_awaited_once_with(42, "chat-a", "hello")
        mark_sent.assert_awaited_once_with(7, telegram_message_id=101)

    async def test_sent_delivery_is_not_sent_again(self):
        create = AsyncMock(return_value=({**self.delivery, "state": "SENT"}, False))
        send = AsyncMock(return_value=101)
        with (
            self._patch_common(),
            patch.object(notifier, "create_notification_delivery", create),
            patch.object(notifier, "_send_to_chat", send),
        ):
            result = await notifier.send_telegram("hello", anomaly_id=42)

        self.assertIsNone(result)
        send.assert_not_awaited()

    async def test_failure_records_failed_state_and_one_minute_backoff(self):
        create = AsyncMock(return_value=(self.delivery, True))
        failure = AsyncMock()
        with (
            self._patch_common(),
            patch.object(notifier, "create_notification_delivery", create),
            patch.object(notifier, "record_notification_failure", failure),
            patch.object(notifier, "_send_to_chat", AsyncMock(side_effect=RuntimeError("offline"))),
            patch.object(notifier.time, "time", return_value=1000.0),
        ):
            result = await notifier.send_telegram("hello", anomaly_id=42)

        self.assertIsNone(result)
        failure.assert_awaited_once()
        args, kwargs = failure.await_args
        self.assertEqual(args, (7, "offline"))
        self.assertEqual(kwargs["next_retry_at"], 1060.0)

    async def test_backoff_progression_is_capped_at_thirty_minutes(self):
        for attempt_count, delay in ((0, 60), (1, 120), (2, 240), (3, 480), (9, 1800)):
            delivery = {"id": 8, "state": "FAILED", "attempt_count": attempt_count}
            failure = AsyncMock()
            with (
                self._patch_common(),
                patch.object(notifier, "create_notification_delivery", AsyncMock(return_value=(delivery, False))),
                patch.object(notifier, "record_notification_failure", failure),
                patch.object(notifier, "_send_to_chat", AsyncMock(return_value=None)),
                patch.object(notifier.time, "time", return_value=500.0),
            ):
                await notifier.send_telegram("hello", anomaly_id=42)
            self.assertEqual(failure.await_args.kwargs["next_retry_at"], 500.0 + delay)

    async def test_failed_recipient_does_not_block_other_recipients(self):
        create = AsyncMock(side_effect=[
            ({"id": 1, "state": "PENDING", "attempt_count": 0}, True),
            ({"id": 2, "state": "PENDING", "attempt_count": 0}, True),
            ({"id": 3, "state": "PENDING", "attempt_count": 0}, True),
        ])
        send = AsyncMock(side_effect=[None, 202, 303])
        failure = AsyncMock()
        sent = AsyncMock()
        with (
            self._patch_common(("chat-a", "chat-b", "chat-c")),
            patch.object(notifier, "create_notification_delivery", create),
            patch.object(notifier, "_send_to_chat", send),
            patch.object(notifier, "record_notification_failure", failure),
            patch.object(notifier, "mark_notification_sent", sent),
        ):
            result = await notifier.send_telegram("hello", anomaly_id=42)

        self.assertEqual(result, 202)
        self.assertEqual(send.await_count, 3)
        failure.assert_awaited_once()
        self.assertEqual(sent.await_count, 2)

    async def test_failed_retry_can_become_sent(self):
        delivery = {"id": 9, "state": "FAILED", "attempt_count": 2}
        create = AsyncMock(return_value=(delivery, False))
        sent = AsyncMock()
        with (
            self._patch_common(),
            patch.object(notifier, "create_notification_delivery", create),
            patch.object(notifier, "_send_to_chat", AsyncMock(return_value=404)),
            patch.object(notifier, "mark_notification_sent", sent),
        ):
            result = await notifier.send_telegram("retry", anomaly_id=99)

        self.assertEqual(result, 404)
        sent.assert_awaited_once_with(9, telegram_message_id=404)


if __name__ == "__main__":
    unittest.main()
