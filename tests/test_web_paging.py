import base64
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import config
from db import models
from uploader import gdrive
from web import app as web_app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"ytvault:pw").decode("ascii")}


class PagingAndSearchTests(unittest.TestCase):
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
        # 45건: 1~44번은 완료된 "영상 n", 45번은 이름에 % 가 들어간 진행 중 작업
        for n in range(1, 45):
            job_id = models.create_job(f"https://youtu.be/v{n}", 1)
            models.update_job(job_id, title=f"영상 {n}", status=models.STATUS_DONE, created_at=n)
        job_id = models.create_job("https://youtu.be/pct", 1)
        models.update_job(job_id, title="100% 라이브 NMIXX", status=models.STATUS_DOWNLOADING, created_at=45)
        self.client = TestClient(web_app.app)

    def get(self, **params):
        response = self.client.get("/api/jobs", params=params, headers=AUTH)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_first_page_is_newest_twenty(self):
        data = self.get()
        self.assertEqual((data["total"], data["pages"], data["page"], data["size"]), (45, 3, 1, 20))
        self.assertEqual([j["id"] for j in data["items"]], list(range(45, 25, -1)))

    def test_last_page_and_out_of_range_page(self):
        self.assertEqual([j["id"] for j in self.get(page=3)["items"]], [5, 4, 3, 2, 1])
        self.assertEqual(self.get(page=99)["page"], 3)
        self.assertEqual(self.get(page=-1)["page"], 1)

    def test_page_size_is_capped(self):
        self.assertEqual(self.get(size=1000)["size"], 100)
        self.assertEqual(len(self.get(size=1000)["items"]), 45)

    def test_search_by_title_url_and_number(self):
        self.assertEqual([j["id"] for j in self.get(q="영상 4")["items"]], [44, 43, 42, 41, 40, 4])
        self.assertEqual([j["id"] for j in self.get(q="nmixx")["items"]], [45])  # 영문은 대소문자 무시
        self.assertEqual([j["id"] for j in self.get(q="youtu.be/v12")["items"]], [12])
        self.assertIn(7, [j["id"] for j in self.get(q="#7")["items"]])

    def test_like_wildcards_are_literal(self):
        self.assertEqual([j["id"] for j in self.get(q="%")["items"]], [45])
        self.assertEqual(self.get(q="_")["total"], 0)

    def test_search_results_are_paged(self):
        data = self.get(q="영상", page=2)
        self.assertEqual((data["total"], data["pages"], data["page"]), (44, 3, 2))
        self.assertEqual(data["query"], "영상")

    def test_counts_cover_all_jobs_regardless_of_search(self):
        self.assertEqual(self.get(q="nmixx")["counts"], {"done": 44, "downloading": 1})

    def test_overlong_query_is_rejected(self):
        response = self.client.get("/api/jobs", params={"q": "x" * 101}, headers=AUTH)
        self.assertEqual(response.status_code, 422)

    def test_index_renders_requested_page_and_query(self):
        response = self.client.get("/", params={"page": 2, "q": "영상"}, headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"page": 2', response.text)
        self.assertIn('"query": "\\uc601\\uc0c1"', response.text)  # tojson은 비ASCII를 이스케이프한다


class UploadChunkTests(unittest.TestCase):
    def test_upload_uses_16mb_chunks(self):
        request = MagicMock()
        request.next_chunk.return_value = (None, {"id": "file-id"})
        service = MagicMock()
        service.files.return_value.create.return_value = request
        with patch.object(gdrive, "MediaFileUpload") as media:
            gdrive._upload_single_file(service, "video.mp4", "video.mp4", "folder")
        self.assertEqual(media.call_args.kwargs["chunksize"], 16 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
