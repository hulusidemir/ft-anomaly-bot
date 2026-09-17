import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import db
import main
import workers
from scraper import MatchResult, SofascoreScraper


class DatabaseConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, 'DATABASE_PATH', os.path.join(self.directory.name, 'test.db'))
        self.path_patch.start()
        self.locks = [patch.object(db, '_write_lock', asyncio.Lock()),
                      patch.object(db, '_connection_slots', asyncio.Semaphore(4))]
        for lock in self.locks:
            lock.start()
        await db.init_db()

    async def asyncTearDown(self):
        for lock in reversed(self.locks):
            lock.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    async def insert(self, event='1', score=0):
        return await db.insert_anomaly(event, 'Home', 'Away', score, score, 60, 'League', 'A', ['test'], {})

    async def test_concurrent_duplicate_insert_has_one_notification_owner(self):
        results = await asyncio.gather(*(self.insert() for _ in range(20)))
        self.assertEqual(sum(is_new for _, is_new, _ in results), 1)
        self.assertEqual(len({row_id for row_id, _, _ in results}), 1)
        self.assertIsNotNone(results[0][0])
        self.assertEqual(len(await db.get_anomalies()), 1)

    async def test_concurrent_score_changes_have_unique_alert_numbers(self):
        results = await asyncio.gather(*(self.insert(score=i) for i in range(12)))
        self.assertEqual({number for _, _, number in results}, set(range(1, 13)))
        self.assertTrue(all(is_new for _, is_new, _ in results))

    async def test_cancelled_transaction_is_invisible_and_rolled_back(self):
        row_id, _, _ = await self.insert()
        started = asyncio.Event()

        async def writer():
            async with db.get_db(write=True) as connection:
                await connection.execute('UPDATE anomalies SET status = ?', ('ignored',))
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(writer())
        await started.wait()
        self.assertEqual((await db.get_anomalies())[0]['status'], 'new')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await db.update_anomaly_status(row_id, 'following')
        self.assertEqual((await db.get_anomalies())[0]['status'], 'following')

    async def test_pending_batches_are_capped_and_fair(self):
        for i in range(65):
            await self.insert(event=str(i))
        first = await db.get_pending_anomaly_match_ids(limit=1000)
        second = await db.get_pending_anomaly_match_ids()
        self.assertEqual(first, [str(i) for i in range(50)])
        self.assertEqual(len(second), 50)
        self.assertEqual(second[:15], [str(i) for i in range(50, 65)])

    async def test_init_is_repeatable(self):
        await self.insert()
        await db.init_db()
        self.assertEqual(len(await db.get_anomalies()), 1)


class WorkerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_preserves_finished_result_and_cancels_slow_check(self):
        cancelled = asyncio.Event()

        async def result(event):
            if event == 'fast':
                return MatchResult(event, True, 2, 1, 'finished', 'Ended')
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(workers, '_finished_match_lock', asyncio.Lock()),
            patch.object(workers, 'FINISHED_MATCH_SCAN_TIMEOUT_SECONDS', 0.05),
            patch.object(workers, 'get_pending_anomaly_match_ids', AsyncMock(return_value=['fast', 'slow'])) as pending,
            patch.object(workers.scraper, 'get_match_result', side_effect=result),
            patch.object(workers, 'finalize_match_anomalies', AsyncMock(return_value=1)) as finalize,
        ):
            report = await workers.finished_match_scan()
        pending.assert_awaited_once_with(limit=50)
        finalize.assert_awaited_once_with('fast', 2, 1)
        self.assertEqual(report['archived'], 1)
        self.assertEqual(report['errors'], 1)
        self.assertTrue(cancelled.is_set())

    async def test_scan_cancellation_cleans_up_child_requests(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def result(event):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(workers, '_finished_match_lock', asyncio.Lock()),
            patch.object(workers, 'get_pending_anomaly_match_ids', AsyncMock(return_value=['slow'])),
            patch.object(workers.scraper, 'get_match_result', side_effect=result),
        ):
            task = asyncio.create_task(workers.finished_match_scan())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())


class CacheSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._anomaly_details_cache.clear()
        main._anomaly_details_locks.clear()

    async def test_fresh_burst_is_capped_and_cache_hits_refresh_lru(self):
        with patch.object(main.scraper, 'get_anomaly_match_details', AsyncMock(side_effect=lambda event: {'id': event})) as fetch:
            await asyncio.gather(*(main.api_anomaly_match_details(str(i)) for i in range(400)))
            await main.api_anomaly_match_details('0')
            await main.api_anomaly_match_details('400')
            self.assertEqual(len(main._anomaly_details_cache), 400)
            self.assertIn('0', main._anomaly_details_cache)
            self.assertNotIn('1', main._anomaly_details_cache)
            self.assertEqual(fetch.await_count, 401)
        self.assertEqual(len(main._anomaly_details_locks), 0)

    async def test_same_event_requests_share_one_fetch(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def details(event):
            started.set()
            await release.wait()
            return {'id': event}

        with patch.object(main.scraper, 'get_anomaly_match_details', side_effect=details) as fetch:
            tasks = [asyncio.create_task(main.api_anomaly_match_details('shared')) for _ in range(10)]
            await started.wait()
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(fetch.await_count, 1)
        self.assertTrue(all(result == {'id': 'shared'} for result in results))
        self.assertEqual(len(main._anomaly_details_locks), 0)

    async def test_expired_entry_is_refetched(self):
        main._anomaly_details_cache['old'] = {'data': {}, 'ts': 0}
        with patch.object(main.scraper, 'get_anomaly_match_details', AsyncMock(return_value={'fresh': True})) as fetch:
            self.assertEqual(await main.api_anomaly_match_details('old'), {'fresh': True})
        fetch.assert_awaited_once()


class RotationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_rotations_close_once_and_skip_stale_failures(self):
        scraper = SofascoreScraper()
        old_session = AsyncMock()
        scraper._session = old_session
        with patch('scraper.time.monotonic', return_value=100):
            await asyncio.gather(*(scraper._rotate_session(old_session) for _ in range(10)))
            await scraper._rotate_session()
        old_session.close.assert_awaited_once()
        new_session = AsyncMock()
        scraper._session = new_session
        with patch('scraper.time.monotonic', return_value=110):
            await scraper._rotate_session(old_session)
            new_session.close.assert_not_awaited()
            await scraper._rotate_session(new_session)
        new_session.close.assert_awaited_once()

    async def test_category_failure_keeps_other_results(self):
        scraper = SofascoreScraper()
        scraper._fetch_json = AsyncMock(return_value={'categories': [
            {'category': {'id': 1}, 'totalEvents': 1},
            {'category': {'id': 2}, 'totalEvents': 1},
        ]})
        session = AsyncMock()

        async def get(url, **kwargs):
            if '/category/1/' in url:
                raise ValueError('failed category')
            response = unittest.mock.Mock(status_code=200)
            response.json.return_value = {'events': [{'id': 2}]}
            return response

        session.get.side_effect = get
        scraper._get_session = AsyncMock(return_value=session)
        # Even an exception escaping the retry loop must not lose good results.
        with patch('scraper.asyncio.sleep', AsyncMock(side_effect=RuntimeError('retry failed'))):
            result = await scraper._fetch_upcoming_by_category('2026-09-16')
        self.assertEqual(result, {'events': [{'id': 2}]})
