import io
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp

import config
from db import models
from uploader import gdrive
from worker import downloader


class _StopDownload(Exception):
    pass


class FilenameTests(unittest.TestCase):
    def output_template(self):
        captured = {}

        def fake_client(options):
            captured.update(options)
            raise _StopDownload

        with patch.object(downloader.yt_dlp, "YoutubeDL", side_effect=fake_client):
            with self.assertRaises(_StopDownload):
                downloader._download({"id": 1, "url": "https://youtu.be/x"})
        return captured["outtmpl"]

    def test_long_korean_title_fits_linux_filename_limit(self):
        info = {"title": "가" * 100, "id": "oBuSSAu9E7w", "ext": "mp4"}
        path = yt_dlp.YoutubeDL({"outtmpl": self.output_template()}).prepare_filename(info)
        name = os.path.basename(path)
        for suffix in (".f399.mp4.part", ".ko.vtt"):
            self.assertLessEqual(len((os.path.splitext(name)[0] + suffix).encode("utf-8")), 255)
        self.assertTrue(name.startswith("가"))
        self.assertTrue(name.endswith(" [oBuSSAu9E7w].mp4"))

    def test_short_title_is_unchanged(self):
        info = {"title": "Me at the zoo", "id": "jNQXAC9IVRw", "ext": "mp4"}
        path = yt_dlp.YoutubeDL({"outtmpl": self.output_template()}).prepare_filename(info)
        self.assertEqual(os.path.basename(path), "Me at the zoo [jNQXAC9IVRw].mp4")


class RetryTests(unittest.TestCase):
    def test_linux_network_errors_are_retryable(self):
        for error in (
            ConnectionAbortedError(103, "Software caused connection abort"),
            ConnectionResetError(104, "Connection reset by peer"),
            BrokenPipeError(32, "Broken pipe"),
            TimeoutError("The read operation timed out"),
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
            OSError("[Errno 103] Software caused connection abort"),
        ):
            with self.subTest(error=error):
                self.assertTrue(gdrive._is_retryable(error))

    def test_other_errors_are_not_retryable(self):
        self.assertFalse(gdrive._is_retryable(ValueError("bad folder id")))


class PathTests(unittest.TestCase):
    def test_paths_do_not_depend_on_working_directory(self):
        self.assertTrue(os.path.isabs(config.DOWNLOAD_DIR))
        self.assertTrue(os.path.isabs(config.DB_PATH))
        self.assertEqual(gdrive._get_token_path(), os.path.join(config.BASE_DIR, "token.json"))

    def test_absolute_paths_are_kept(self):
        absolute = os.path.abspath(os.path.join(os.sep, "srv", "ytvault.db"))
        self.assertEqual(config.project_path(absolute), absolute)


class _TempDbTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        db_patch = patch.object(models, "DB_PATH", os.path.join(self.directory, "test.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        models.init_db()


class RequeueTests(_TempDbTestCase):
    def test_interrupted_jobs_return_to_pending(self):
        statuses = [models.STATUS_DOWNLOADING, models.STATUS_UPLOADING, models.STATUS_NOTIFYING,
                    models.STATUS_DONE, models.STATUS_FAILED, models.STATUS_PENDING]
        ids = []
        for status in statuses:
            job_id = models.create_job(f"https://youtu.be/{status}", 1)
            models.update_job(job_id, status=status)
            ids.append(job_id)

        self.assertEqual(models.requeue_interrupted_jobs(), 3)
        self.assertEqual(
            [models.get_job(job_id)["status"] for job_id in ids],
            ["pending", "pending", "pending", "done", "failed", "pending"],
        )


class FailureCleanupTests(_TempDbTestCase):
    def test_failed_upload_removes_downloaded_file(self):
        video = Path(self.directory) / "video [id].mp4"
        video.write_bytes(b"data")
        job_id = models.create_job("https://youtu.be/id", 1)
        models.update_job(job_id, max_height=0)  # 해상도 확인 단계는 건너뛴다
        notices = []

        with patch.object(downloader, "_download", return_value=(str(video), "title", "", "")), \
                patch.object(downloader, "GEMINI_API_KEY", ""), \
                patch.object(downloader, "upload_video_package", side_effect=RuntimeError("drive down")):
            downloader._process_job(models.get_job(job_id), lambda chat_id, text: notices.append(text))

        self.assertFalse(video.exists())
        self.assertEqual(models.get_job(job_id)["status"], models.STATUS_FAILED)
        self.assertIn("drive down", notices[0])


class HeadlessAuthTests(unittest.TestCase):
    def test_missing_token_fails_instead_of_waiting_for_browser(self):
        with patch.object(gdrive, "_service", None), \
                patch.object(gdrive, "_get_token_path", return_value=os.path.join("missing", "token.json")), \
                patch.object(gdrive.sys, "stdin", io.StringIO()), \
                patch.object(gdrive.InstalledAppFlow, "from_client_secrets_file") as flow:
            with self.assertRaisesRegex(RuntimeError, "token.json"):
                gdrive._get_service()
        flow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
