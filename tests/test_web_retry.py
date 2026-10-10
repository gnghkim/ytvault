import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import config
from db import models
from web import app as web_app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"ytvault:pw").decode("ascii")}
RETRY = {**AUTH, web_app.CSRF_HEADER: "1"}


class WebRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for p in (
            patch.object(models, "DB_PATH", os.path.join(directory.name, "test.db")),
            patch.object(config, "WEB_USERNAME", "ytvault"),
            patch.object(config, "WEB_PASSWORD", "pw"),
        ):
            p.start()
            self.addCleanup(p.stop)
        models.init_db()
        self.failed = models.create_job("https://youtu.be/failed", 1)
        models.update_job(self.failed, status=models.STATUS_FAILED, error_msg="HTTP Error 403: Forbidden",
                          max_height=1440, file_path="/tmp/x.mp4")
        self.done = models.create_job("https://youtu.be/done", 1)
        models.update_job(self.done, status=models.STATUS_DONE)
        self.client = TestClient(web_app.app)

    def retry(self, job_id, headers=RETRY):
        return self.client.post(f"/api/jobs/{job_id}/retry", headers=headers)

    def test_failed_job_goes_back_to_queue(self):
        response = self.retry(self.failed)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")
        job = models.get_job(self.failed)
        self.assertEqual(job["status"], models.STATUS_PENDING)
        self.assertIsNone(job["error_msg"])
        self.assertIsNone(job["file_path"])
        self.assertEqual(job["max_height"], 1440)  # 고른 해상도는 다시 묻지 않는다

    def test_second_click_is_a_conflict(self):
        self.assertEqual(self.retry(self.failed).status_code, 200)
        self.assertEqual(self.retry(self.failed).status_code, 409)

    def test_only_failed_jobs_can_be_retried(self):
        response = self.retry(self.done)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(models.get_job(self.done)["status"], models.STATUS_DONE)

    def test_unknown_job(self):
        self.assertEqual(self.retry(999).status_code, 404)

    def test_requires_login(self):
        self.assertEqual(self.retry(self.failed, headers={web_app.CSRF_HEADER: "1"}).status_code, 401)
        self.assertEqual(models.get_job(self.failed)["status"], models.STATUS_FAILED)

    def test_cross_site_post_without_header_is_refused(self):
        # 다른 사이트의 폼 POST에도 브라우저가 Basic 인증을 붙일 수 있지만, 사용자 정의 헤더는 붙일 수 없다
        self.assertEqual(self.retry(self.failed, headers=AUTH).status_code, 403)
        self.assertEqual(models.get_job(self.failed)["status"], models.STATUS_FAILED)

    def test_get_does_not_retry(self):
        self.assertEqual(self.client.get(f"/api/jobs/{self.failed}/retry", headers=RETRY).status_code, 405)
        self.assertEqual(models.get_job(self.failed)["status"], models.STATUS_FAILED)

    def test_dashboard_has_retry_button_for_failed_rows(self):
        html = self.client.get("/", headers=AUTH).text
        self.assertIn("data-retry", html)
        self.assertIn("X-YTVault-Request", html)


if __name__ == "__main__":
    unittest.main()
