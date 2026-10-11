"""healthcheck.py 판정 테스트. 임시 DB·임시 폴더와 모의 웹/디스크만 쓰고 외부로 나가는 요청은 없다."""

import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import healthcheck
import healthcheck_config
from db import models

GB = 2**30
_Usage = namedtuple("_Usage", "total used free")


class HealthcheckTestBase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.now = time.time()
        self.cfg = SimpleNamespace(**{k: getattr(healthcheck_config, k) for k in dir(healthcheck_config) if k.isupper()})
        self.cfg.STATE_PATH = os.path.join(self.dir, "health_state.json")
        self.cfg.DB_PATH = os.path.join(self.dir, "test.db")
        self.cfg.DOWNLOAD_DIR = os.path.join(self.dir, "downloads")
        self.cfg.DRIVE_TOKEN_PATH = os.path.join(self.dir, "token.json")
        self.cfg.DEGRADED_STATUS = "up"
        self.cfg.KUMA_PUSH_URL = ""
        os.makedirs(self.cfg.DOWNLOAD_DIR)
        with open(self.cfg.DRIVE_TOKEN_PATH, "w") as f:
            json.dump({"refresh_token": "x"}, f)
        os.utime(self.cfg.DRIVE_TOKEN_PATH, (self.now - 86400, self.now - 86400))

        db_patch = patch.object(models, "DB_PATH", self.cfg.DB_PATH)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        models.init_db()

        self.web = {"code": 401, "error": None, "ms": 12.0}
        self.usage = _Usage(total=228 * GB, used=30 * GB, free=198 * GB)
        for p in (
            patch.object(healthcheck, "check_web", side_effect=lambda *a: self.web),
            patch.object(healthcheck.shutil, "disk_usage", side_effect=lambda *a: self.usage),
        ):
            p.start()
            self.addCleanup(p.stop)

        # 정상 대기 중인 ytvault가 남긴 상태
        self.state = {
            "pid": 1, "started_at": self.now - 3600, "written_at": self.now - 5,
            "worker": {"last_tick": self.now - 3, "current_job": None, "busy_since": None,
                       "loop_errors": 0, "last_error": None, "last_error_at": None},
            "bot": {"last_ok": self.now - 4, "consecutive_errors": 0, "last_error": None,
                    "last_error_kind": None, "last_error_at": None},
        }

    # --- helpers ---------------------------------------------------------
    def write_state(self):
        with open(self.cfg.STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.state, f)

    def add_job(self, status, ago=0, **fields):
        job_id = models.create_job(f"https://youtu.be/{status}{time.time_ns()}", 1)
        fields = {"status": status, "updated_at": self.now - ago, **fields}
        with closing(sqlite3.connect(self.cfg.DB_PATH)) as conn, conn:
            conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         [*fields.values(), job_id])
        return job_id

    def check(self, write_state=True):
        if write_state:
            self.write_state()
        facts = healthcheck.collect(self.now, self.cfg)
        return healthcheck.evaluate(facts, self.now, self.cfg)


class NormalTests(HealthcheckTestBase):
    def test_idle(self):  # 1. 정상 대기
        report = self.check()
        self.assertEqual(report.state, healthcheck.IDLE)
        self.assertEqual(report.kuma_status, "up")
        self.assertEqual(report.findings, [])
        self.assertTrue(report.msg.startswith("IDLE | no pending jobs | bot OK | worker OK"), report.msg)
        self.assertEqual(report.ping, 12.0)

    def test_bot_without_messages_is_ok(self):  # 2. 메시지가 없어도 폴링이 성공하면 정상
        self.state["bot"]["consecutive_errors"] = 1
        self.state["bot"].update(last_error="TimedOut: Timed out", last_error_kind="network",
                                 last_error_at=self.now - 30)  # 일시적 오류 뒤 회복
        report = self.check()
        self.assertEqual(report.state, healthcheck.IDLE)
        self.assertIn("bot OK", report.msg)

    def test_new_job_waiting(self):  # 3. 새 작업 생성 직후
        self.add_job(models.STATUS_PENDING, ago=2)
        report = self.check()
        self.assertEqual(report.state, healthcheck.HEALTHY)
        self.assertEqual(report.kuma_status, "up")
        self.assertIn("queue 1", report.msg)

    def test_download_in_progress(self):  # 4. 다운로드 정상 진행
        job = self.add_job(models.STATUS_DOWNLOADING, ago=3, stage="downloading", progress=45.2)
        self.state["worker"].update(current_job=job, busy_since=self.now - 600, last_tick=self.now - 600)
        report = self.check()
        self.assertEqual(report.state, healthcheck.HEALTHY)
        self.assertIn(f"downloading #{job} 45% | progress OK", report.msg)

    def test_upload_in_progress(self):  # 6. 업로드 정상 진행 (큰 파일이라 오래 걸려도 진행 중이면 정상)
        job = self.add_job(models.STATUS_UPLOADING, ago=4, stage="uploading", progress=80)
        self.state["worker"].update(current_job=job, busy_since=self.now - 3 * 3600, last_tick=self.now - 3 * 3600)
        report = self.check()
        self.assertEqual(report.state, healthcheck.HEALTHY)
        self.assertIn(f"uploading #{job} 80%", report.msg)

    def test_awaiting_resolution_choice_is_not_a_problem(self):
        self.add_job(models.STATUS_AWAITING_CHOICE, ago=300)
        report = self.check()
        self.assertEqual(report.kuma_status, "up")
        self.assertEqual(report.findings, [])

    def test_startup_grace(self):
        self.state["started_at"] = self.now - 20
        self.state["bot"]["last_ok"] = None
        self.state["worker"]["last_tick"] = None
        report = self.check()
        self.assertEqual(report.findings, [])


class FailureTests(HealthcheckTestBase):
    def test_single_download_error_is_degraded(self):  # 5. 다운로드 오류 1건
        self.add_job(models.STATUS_DONE, ago=7200)
        job = self.add_job(models.STATUS_FAILED, ago=600, error_msg="ERROR: [youtube] x: HTTP Error 403: Forbidden")
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)
        self.assertEqual(report.kuma_status, "up")
        self.assertIn(f"1 failed in 6h (last #{job}: ERROR: [youtube] x: HTTP Error 403: Forbidden)", report.msg)

    def test_degraded_can_be_reported_down(self):
        self.add_job(models.STATUS_FAILED, ago=600, error_msg="boom")
        self.cfg.DEGRADED_STATUS = "down"
        self.assertEqual(self.check().kuma_status, "down")

    def test_old_failures_are_ignored(self):
        self.add_job(models.STATUS_FAILED, ago=2 * 86400, error_msg="boom")
        self.assertEqual(self.check().state, healthcheck.IDLE)

    def test_repeated_failures_are_unhealthy(self):
        self.add_job(models.STATUS_DONE, ago=7200)
        for ago in (1800, 1200, 600):
            last = self.add_job(models.STATUS_FAILED, ago=ago, error_msg="ERROR: Unable to download")
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn(f"3 jobs failed in a row (last #{last})", report.msg)

    def test_drive_auth_failure_is_unhealthy(self):  # 7. 업로드 인증 실패
        self.add_job(models.STATUS_DONE, ago=7200)
        job = self.add_job(models.STATUS_FAILED, ago=600,
                           error_msg="('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant'})")
        report = self.check()
        self.assertEqual(report.state, healthcheck.UNHEALTHY)
        self.assertEqual(report.kuma_status, "down")
        self.assertIn(f"Drive authentication error (job #{job})", report.msg)

    def test_drive_auth_failure_stays_until_fixed(self):
        # 오래전 실패라도 그 뒤로 성공한 업로드가 없고 토큰도 그대로면 계속 장애
        os.utime(self.cfg.DRIVE_TOKEN_PATH, (self.now - 5 * 86400, self.now - 5 * 86400))
        self.add_job(models.STATUS_FAILED, ago=3 * 86400, error_msg="RuntimeError: Google Drive 인증 토큰이 없거나 갱신할 수 없습니다")
        self.assertEqual(self.check().kuma_status, "down")

    def test_renewed_token_downgrades_auth_failure(self):
        self.add_job(models.STATUS_FAILED, ago=600, error_msg="invalid_grant")
        os.utime(self.cfg.DRIVE_TOKEN_PATH, (self.now - 60, self.now - 60))
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)
        self.assertIn("token renewed, waiting for next upload", report.msg)

    def test_missing_drive_token(self):
        os.remove(self.cfg.DRIVE_TOKEN_PATH)
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("Drive token missing", report.msg)

    def test_queue_not_processed(self):  # 8. 작업 큐 정체: 워커는 도는데 대기 작업이 안 빠짐
        self.add_job(models.STATUS_PENDING, ago=600)
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("queue not processed 10m", report.msg)

    def test_long_queue_behind_busy_worker_is_only_degraded(self):
        job = self.add_job(models.STATUS_DOWNLOADING, ago=2, stage="downloading", progress=10)
        self.add_job(models.STATUS_PENDING, ago=3 * 3600)
        self.state["worker"].update(current_job=job, busy_since=self.now - 4 * 3600)
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)
        self.assertIn("queue waiting 3h", report.msg)

    def test_worker_stopped(self):  # 9. Worker 중단
        self.state["worker"]["last_tick"] = self.now - 300
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("worker stalled 5m", report.msg)
        self.assertIn("bot OK", report.msg)

    def test_worker_loop_errors(self):
        self.state["worker"].update(loop_errors=1, last_error="OperationalError: database is locked")
        self.assertEqual(self.check().state, healthcheck.DEGRADED)
        self.state["worker"]["loop_errors"] = 3
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("worker error x3: OperationalError: database is locked", report.msg)

    def test_download_stalled(self):
        job = self.add_job(models.STATUS_DOWNLOADING, ago=15 * 60, stage="downloading", progress=30)
        self.state["worker"].update(current_job=job, busy_since=self.now - 3600)
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)
        self.assertIn(f"job #{job} no progress 15m (downloading)", report.msg)

        with closing(sqlite3.connect(self.cfg.DB_PATH)) as conn, conn:
            conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (self.now - 40 * 60, job))
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn(f"job #{job} stalled 40m (downloading)", report.msg)

    def test_bot_polling_down(self):
        self.state["bot"].update(last_ok=self.now - 150, consecutive_errors=20, last_error="NetworkError: x",
                                 last_error_kind="network", last_error_at=self.now - 5)
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)
        self.assertIn("bot polling down 2m (NetworkError: x)", report.msg)
        self.state["bot"].update(last_ok=self.now - 600, last_error_kind="conflict")
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("bot polling down 10m (another instance is polling)", report.msg)

    def test_bot_token_rejected(self):
        self.state["bot"].update(last_error="HTTP 401", last_error_kind="auth", last_error_at=self.now - 1)
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("bot token rejected by Telegram", report.msg)

    def test_process_not_running(self):
        self.state["written_at"] = self.now - 900
        self.state["worker"]["last_tick"] = self.now - 900
        self.state["bot"]["last_ok"] = self.now - 900
        self.web = {"code": None, "error": "Connection refused", "ms": None}
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("ytvault not responding 15m (stopped or hung)", report.msg)
        self.assertNotIn("bot polling down", report.msg)

    def test_missing_state_file(self):
        report = self.check(write_state=False)
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("no health state (missing)", report.msg)

    def test_orphaned_active_job(self):
        job = self.add_job(models.STATUS_UPLOADING, ago=900, stage="uploading")
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)
        self.assertIn(f"job #{job} stuck in uploading", report.msg)

    def test_unreadable_database(self):
        self.cfg.DB_PATH = os.path.join(self.dir, "missing.db")
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("database unreadable", report.msg)
        self.assertFalse(os.path.exists(self.cfg.DB_PATH))  # 읽기 전용이라 새로 만들지 않는다


class StorageAndWebTests(HealthcheckTestBase):
    def test_low_disk(self):
        self.usage = _Usage(total=228 * GB, used=213 * GB, free=15 * GB)
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)
        self.assertIn("disk 15GB free (93% used)", report.msg)
        self.assertEqual(report.msg.count("GB free"), 1)
        self.usage = _Usage(total=228 * GB, used=225 * GB, free=3 * GB)
        report = self.check()
        self.assertEqual(report.kuma_status, "down")
        self.assertIn("disk low: 3.0GB free", report.msg)

    def test_leftover_files_are_reported_not_deleted(self):
        path = os.path.join(self.cfg.DOWNLOAD_DIR, "old [abc].f399.mp4.part")
        with open(path, "wb") as f:
            f.write(b"x" * 1024)
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)
        self.assertIn("leftover files 1", report.msg)
        self.assertTrue(os.path.exists(path))

    def test_files_during_download_are_expected(self):
        job = self.add_job(models.STATUS_DOWNLOADING, ago=1, stage="downloading", progress=5)
        self.state["worker"].update(current_job=job, busy_since=self.now - 60)
        with open(os.path.join(self.cfg.DOWNLOAD_DIR, "a.part"), "wb") as f:
            f.write(b"x")
        self.assertEqual(self.check().state, healthcheck.HEALTHY)

    def test_web_states(self):
        for web, text in (
            ({"code": None, "error": "timed out", "ms": None}, "web UI not responding (timed out)"),
            ({"code": 503, "error": None, "ms": 3}, "web UI disabled (WEB_PASSWORD empty)"),
            ({"code": 200, "error": None, "ms": 3}, "web UI answers without login"),
        ):
            with self.subTest(text=text):
                self.web = web
                report = self.check()
                self.assertEqual(report.state, healthcheck.DEGRADED)
                self.assertEqual(report.kuma_status, "up")  # 웹만 문제면 핵심 기능은 정상
                self.assertIn(text, report.msg)


class RecoveryTests(HealthcheckTestBase):
    def test_back_to_normal_after_recovery(self):  # 11. 장애 복구 후 정상 복귀
        self.add_job(models.STATUS_FAILED, ago=1800, error_msg="invalid_grant")
        self.state["worker"]["last_tick"] = self.now - 600
        self.assertEqual(self.check().kuma_status, "down")

        # 재시작 후 워커가 다시 돌고, 새 토큰으로 업로드가 성공했다
        self.state["worker"]["last_tick"] = self.now - 2
        self.add_job(models.STATUS_DONE, ago=60)
        report = self.check()
        self.assertEqual(report.state, healthcheck.DEGRADED)  # 6시간 안의 실패 1건은 주의로만 남는다
        self.assertEqual(report.kuma_status, "up")
        self.assertNotIn("Drive authentication error (job", report.msg)


class _KumaHandler(BaseHTTPRequestHandler):
    requests = []
    reply = {"ok": True}

    def do_GET(self):
        type(self).requests.append(self.path)
        body = json.dumps(type(self).reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PushTests(HealthcheckTestBase):
    def start_kuma(self, reply):
        _KumaHandler.requests = []
        _KumaHandler.reply = reply
        server = HTTPServer(("127.0.0.1", 0), _KumaHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/api/push/SECRETTOKEN?status=up&msg=OK&ping="

    def test_push_sends_status_msg_and_ping(self):
        url = self.start_kuma({"ok": True})
        ok, detail = healthcheck.push(url, "down", "UNHEALTHY | worker stalled 5m | queue 3", 12.4, 5)
        self.assertTrue(ok, detail)
        self.assertEqual(len(_KumaHandler.requests), 1)
        sent = urlsplit(_KumaHandler.requests[0])
        self.assertEqual(sent.path, "/api/push/SECRETTOKEN")
        query = parse_qs(sent.query, keep_blank_values=True)
        self.assertEqual(query["status"], ["down"])
        self.assertEqual(query["msg"], ["UNHEALTHY | worker stalled 5m | queue 3"])
        self.assertEqual(query["ping"], ["12"])

    def test_main_pushes_report(self):
        self.write_state()
        self.cfg.KUMA_PUSH_URL = self.start_kuma({"ok": True})
        with patch("sys.stdout"):
            self.assertEqual(healthcheck.main([], self.cfg), 0)
        query = parse_qs(urlsplit(_KumaHandler.requests[0]).query)
        self.assertEqual(query["status"], ["up"])
        self.assertTrue(query["msg"][0].startswith("IDLE"))

    def test_kuma_rejects_push(self):
        url = self.start_kuma({"ok": False, "msg": "Monitor not found or not active."})
        ok, detail = healthcheck.push(url, "up", "IDLE", None, 5)
        self.assertFalse(ok)
        self.assertIn("Monitor not found", detail)

    def test_kuma_unreachable(self):  # 10. Uptime Kuma 연결 중단
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # 아무도 듣지 않는 포트
        self.write_state()
        self.cfg.KUMA_PUSH_URL = f"http://127.0.0.1:{port}/api/push/SECRETTOKEN"
        with patch.object(healthcheck.urllib.request, "urlopen", wraps=healthcheck.urllib.request.urlopen) as urlopen, \
                patch("sys.stdout"), patch("sys.stderr") as stderr:
            started = time.monotonic()
            code = healthcheck.main([], self.cfg)
        self.assertEqual(code, 1)  # 오류로 끝나되 예외는 없다
        self.assertEqual(urlopen.call_count, 1)  # 다시 시도하지 않는다
        self.assertLess(time.monotonic() - started, 10)
        written = "".join(str(c.args[0]) for c in stderr.write.call_args_list)
        self.assertIn("unreachable", written)
        self.assertNotIn("SECRETTOKEN", written)  # Push 토큰을 로그에 남기지 않는다

    def test_missing_push_url(self):
        self.write_state()
        with patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(healthcheck.main([], self.cfg), 2)

    def test_checker_crash_is_reported_down(self):
        url = self.start_kuma({"ok": True})
        self.cfg.KUMA_PUSH_URL = url
        with patch.object(healthcheck, "collect", side_effect=RuntimeError("boom")), patch("sys.stdout"):
            self.assertEqual(healthcheck.main([], self.cfg), 0)
        query = parse_qs(urlsplit(_KumaHandler.requests[0]).query)
        self.assertEqual(query["status"], ["down"])
        self.assertIn("healthcheck error: boom", query["msg"][0])


if __name__ == "__main__":
    unittest.main()
