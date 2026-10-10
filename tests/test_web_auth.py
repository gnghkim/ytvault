import base64
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import config
from web import app as web_app
from web import auth

JOB = {
    "id": 1,
    "url": "https://youtu.be/x",
    "title": "video",
    "status": "done",
    "chat_id": 42,
    "file_path": "/srv/downloads/video.mp4",
    "created_at": 0,
    "updated_at": 0,
}


def basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(config, "WEB_USERNAME", "ytvault"),
            patch.object(config, "WEB_PASSWORD", "s3cret-비번"),
            patch.object(auth, "FAILURE_DELAY_SECONDS", 0),
            patch.object(web_app, "search_jobs", return_value=[JOB]),
            patch.object(web_app, "count_jobs", return_value=1),
            patch.object(web_app, "count_jobs_by_status", return_value={"done": 1}),
            patch.object(web_app, "get_job", side_effect=lambda job_id: JOB if job_id == 1 else None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(web_app.app)

    def test_every_route_requires_credentials(self):
        for path in ("/", "/api/jobs", "/api/jobs/1", "/api/jobs/999"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertIn("Basic", response.headers["www-authenticate"])

    def test_correct_credentials_open_the_ui(self):
        headers = basic("ytvault", "s3cret-비번")
        self.assertEqual(self.client.get("/", headers=headers).status_code, 200)
        jobs = self.client.get("/api/jobs", headers=headers).json()["items"]
        self.assertEqual(jobs[0]["title"], "video")
        self.assertNotIn("chat_id", jobs[0])
        self.assertNotIn("file_path", jobs[0])

    def test_wrong_password_or_username_is_rejected(self):
        for headers in (basic("ytvault", "wrong"), basic("admin", "s3cret-비번"), basic("", "")):
            with self.assertLogs("web.auth", level="WARNING"):
                self.assertEqual(self.client.get("/api/jobs", headers=headers).status_code, 401)

    def test_ui_stays_closed_when_no_password_is_configured(self):
        with patch.object(config, "WEB_PASSWORD", ""):
            self.assertEqual(self.client.get("/").status_code, 503)
            self.assertEqual(self.client.get("/", headers=basic("ytvault", "")).status_code, 503)

    def test_generated_docs_are_not_exposed(self):
        headers = basic("ytvault", "s3cret-비번")
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path, headers=headers).status_code, 404, path)


if __name__ == "__main__":
    unittest.main()
