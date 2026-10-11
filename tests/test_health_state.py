"""ytvault 쪽 계측(health_state.py, 워커 루프, 봇 폴링 기록) 테스트."""

import asyncio
import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from telegram.error import NetworkError
from telegram.request import HTTPXRequest

import config
import health_state
from bot import telegram_bot
from worker import downloader


class HealthStateTestBase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.path = os.path.join(self.dir, "health_state.json")
        p = patch.object(config, "HEALTH_STATE_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)
        importlib.reload(health_state)  # 모듈 전역 상태를 새로 시작

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class WriterTests(HealthStateTestBase):
    def test_flush_writes_atomically(self):
        health_state.flush()
        state = self.read()
        self.assertEqual(state["pid"], os.getpid())
        self.assertIsNotNone(state["written_at"])
        self.assertEqual(os.listdir(self.dir), ["health_state.json"])  # 임시 파일이 남지 않는다

    def test_routine_updates_are_throttled(self):
        health_state.flush()
        health_state.worker_tick()
        self.assertIsNone(self.read()["worker"]["last_tick"])  # 15초 안이라 아직 쓰지 않음
        health_state.worker_job_started(7)  # 상태 변화는 바로 쓴다
        state = self.read()["worker"]
        self.assertEqual(state["current_job"], 7)
        self.assertIsNotNone(state["last_tick"])
        health_state.worker_job_finished()
        self.assertIsNone(self.read()["worker"]["current_job"])

    def test_write_failure_does_not_raise(self):
        with patch.object(config, "HEALTH_STATE_PATH", os.path.join(self.dir, "missing", "state.json")):
            with self.assertLogs("health_state", "WARNING"):
                health_state.worker_job_started(1)
                health_state.worker_job_finished()

    def test_errors_are_redacted(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"}):
            health_state.bot_poll_error("network", RuntimeError("failed for 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        bot = self.read()["bot"]
        self.assertNotIn("ABCDEFGHIJ", bot["last_error"])
        self.assertEqual(bot["consecutive_errors"], 1)

    def test_recovery_resets_counters_immediately(self):
        health_state.flush()
        health_state.bot_poll_error("conflict", "HTTP 409")
        health_state.bot_poll_ok()
        bot = self.read()["bot"]
        self.assertEqual(bot["consecutive_errors"], 0)
        self.assertIsNotNone(bot["last_ok"])


class BotPollTrackingTests(HealthStateTestBase):
    def poll(self, result):
        async def fake_do_request(self, *args, **kwargs):
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(HTTPXRequest, "do_request", fake_do_request):
            request = telegram_bot._PollTrackingRequest(connection_pool_size=1)
            return asyncio.run(request.do_request(url="https://api.telegram.org/x/getUpdates", method="POST"))

    def test_successful_poll_without_messages(self):
        self.assertEqual(self.poll((200, b'{"ok":true,"result":[]}')), (200, b'{"ok":true,"result":[]}'))
        health_state.flush()
        self.assertIsNotNone(self.read()["bot"]["last_ok"])

    def test_error_kinds(self):
        for code, kind in ((401, "auth"), (409, "conflict"), (429, "rate_limit"), (502, "server")):
            with self.subTest(code=code):
                self.poll((code, b"{}"))  # 응답은 그대로 돌려줘 python-telegram-bot이 처리한다
                self.assertEqual(self.read()["bot"]["last_error_kind"], kind)

    def test_network_error_is_recorded_and_reraised(self):
        with self.assertRaises(NetworkError):
            self.poll(NetworkError("httpx.ConnectError: All connection attempts failed"))
        bot = self.read()["bot"]
        self.assertEqual(bot["last_error_kind"], "network")
        self.assertIn("All connection attempts failed", bot["last_error"])


class WorkerLoopTests(HealthStateTestBase):
    def run_worker(self, pending_results, process=None):
        """get_pending_jobs가 차례로 pending_results를 돌려주고 나면 워커를 멈춘다."""
        results = list(pending_results)

        def fake_pending():
            result = results.pop(0)
            if not results:
                downloader.stop_worker()
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(downloader, "get_pending_jobs", side_effect=fake_pending), \
                patch.object(downloader, "expire_resolution_choices", return_value=[]), \
                patch.object(downloader, "_process_job", side_effect=process), \
                patch.object(downloader.time, "sleep"):
            downloader.run_worker(poll_interval=0)

    def test_db_error_does_not_kill_worker(self):
        with self.assertLogs("worker.downloader", "ERROR"):
            self.run_worker([RuntimeError("database is locked"), RuntimeError("database is locked")])
        worker = self.read()["worker"]
        self.assertEqual(worker["loop_errors"], 2)
        self.assertIn("database is locked", worker["last_error"])

    def test_tick_after_error_resets_count(self):
        with self.assertLogs("worker.downloader", "ERROR"):
            self.run_worker([RuntimeError("database is locked"), []])
        self.assertEqual(self.read()["worker"]["loop_errors"], 0)

    def test_job_is_tracked_while_processing(self):
        seen = []

        def process(job, notify_callback):
            seen.append(self.read()["worker"]["current_job"])

        self.run_worker([[{"id": 42}]], process)
        self.assertEqual(seen, [42])
        self.assertIsNone(self.read()["worker"]["current_job"])

    def test_job_crash_clears_current_job(self):
        with self.assertLogs("worker.downloader", "ERROR"):
            self.run_worker([[{"id": 5}]], RuntimeError("update_job failed"))
        worker = self.read()["worker"]
        self.assertIsNone(worker["current_job"])
        self.assertEqual(worker["loop_errors"], 1)


if __name__ == "__main__":
    unittest.main()
