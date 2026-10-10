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

# (제목, 상태) — 위에서부터 오래된 순
JOBS = [
    ("완료 A", models.STATUS_DONE),
    ("완료 B 직캠", models.STATUS_DONE),
    ("실패 직캠", models.STATUS_FAILED),
    ("실패 2", models.STATUS_FAILED),
    ("다운로드 중", models.STATUS_DOWNLOADING),
    ("업로드 중 직캠", models.STATUS_UPLOADING),
    ("알림 중", models.STATUS_NOTIFYING),
    ("선택 대기", models.STATUS_AWAITING_CHOICE),
    ("대기", models.STATUS_PENDING),
]


class StatusFilterTests(unittest.TestCase):
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
        for n, (title, status) in enumerate(JOBS, start=1):
            job_id = models.create_job(f"https://youtu.be/{n}", 1)
            models.update_job(job_id, title=title, status=status, created_at=n)
        self.client = TestClient(web_app.app)

    def titles(self, **params):
        data = self.client.get("/api/jobs", params=params, headers=AUTH).json()
        return [j["title"] for j in data["items"]], data

    def test_each_card_filter(self):
        expected = {
            "done": ["완료 B 직캠", "완료 A"],
            "failed": ["실패 2", "실패 직캠"],
            "active": ["알림 중", "업로드 중 직캠", "다운로드 중"],  # 다운로드·업로드·마무리 모두 "진행 중"
            "awaiting_choice": ["선택 대기"],
            "pending": ["대기"],
        }
        for status, titles in expected.items():
            with self.subTest(status=status):
                got, data = self.titles(status=status)
                self.assertEqual(got, titles)
                self.assertEqual((data["status"], data["total"]), (status, len(titles)))

    def test_no_or_unknown_filter_shows_everything(self):
        for params in ({}, {"status": ""}, {"status": "bogus"}, {"status": "downloading"}):
            with self.subTest(params=params):
                got, data = self.titles(**params)
                self.assertEqual(data["total"], len(JOBS))
                self.assertEqual(data["status"], "")

    def test_filter_combines_with_search(self):
        got, data = self.titles(status="failed", q="직캠")
        self.assertEqual(got, ["실패 직캠"])
        got, _ = self.titles(status="active", q="직캠")
        self.assertEqual(got, ["업로드 중 직캠"])

    def test_counts_ignore_the_filter(self):
        _, data = self.titles(status="failed")
        self.assertEqual(sum(data["counts"].values()), len(JOBS))

    def test_filtered_results_are_paged(self):
        _, data = self.titles(status="active", size=2, page=2)
        self.assertEqual((data["page"], data["pages"], data["total"], len(data["items"])), (2, 2, 3, 1))

    def test_index_keeps_filter_from_address_bar(self):
        html = self.client.get("/", params={"status": "failed"}, headers=AUTH).text
        self.assertIn('"status": "failed"', html)
        self.assertIn("data-filter", html)


if __name__ == "__main__":
    unittest.main()
