import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot import telegram_bot
from db import models
from uploader import gdrive
from web import app as web_app
from worker import downloader

MB = 2**20


def video(height, width=None, size_mb=100, ext="mp4"):
    return {"vcodec": "avc1", "acodec": "none", "height": height, "width": width or height * 16 // 9,
            "ext": ext, "filesize": size_mb * MB}


AUDIO = {"vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 30 * MB}
INFO_4K = {"title": "4K 직캠", "duration": 2065,
           "formats": [video(720, size_mb=300), video(1080, size_mb=1300), video(1440, size_mb=3200, ext="webm"),
                       video(2160, size_mb=6500), AUDIO]}
INFO_1080 = {"title": "보통 영상", "formats": [video(720), video(1080), AUDIO]}


class ResolutionOptionTests(unittest.TestCase):
    def test_4k_video_offers_every_level_with_size(self):
        self.assertEqual(
            downloader._resolution_options(INFO_4K),
            [(720, 330 * MB), (1080, 1330 * MB), (1440, 3230 * MB), (2160, 6530 * MB)],
        )

    def test_up_to_1080p_is_not_asked(self):
        self.assertEqual(downloader._resolution_options(INFO_1080), [])

    def test_vertical_1080p_short_is_not_asked(self):
        # 1080x1920 세로 영상은 짧은 변 기준 1080p
        info = {"formats": [video(1920, width=1080), AUDIO]}
        self.assertEqual(downloader._resolution_options(info), [])

    def test_missing_levels_are_merged(self):
        info = {"formats": [video(1080), video(2160), AUDIO]}
        self.assertEqual([res for res, _ in downloader._resolution_options(info)], [1080, 2160])

    def test_labels(self):
        self.assertEqual([downloader.resolution_label(h) for h in (720, 1440, 2160, 4320)],
                         ["720p", "1440p", "4K", "8K"])


class _TempDbTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        db_patch = patch.object(models, "DB_PATH", os.path.join(self.directory, "test.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        models.init_db()


class ProcessJobTests(_TempDbTestCase):
    def test_high_resolution_video_waits_for_choice(self):
        job_id = models.create_job("https://youtu.be/4k", 7)
        sent = []
        with patch.object(downloader, "_probe", return_value=INFO_4K), \
                patch.object(downloader, "_download") as download:
            downloader._process_job(models.get_job(job_id), lambda *args, **kw: sent.append((args, kw)))

        download.assert_not_called()
        job = models.get_job(job_id)
        self.assertEqual(job["status"], models.STATUS_AWAITING_CHOICE)
        self.assertEqual(job["title"], "4K 직캠")
        self.assertAlmostEqual(job["choice_deadline"], time.time() + 600, delta=5)
        (chat_id, text), kwargs = sent[0]
        self.assertEqual(chat_id, 7)
        self.assertIn("34분 25초", text)
        self.assertIn("10분 안에", text)
        self.assertEqual(
            kwargs["buttons"],
            [("720p · 약 330MB", f"res:{job_id}:720"), ("1080p · 약 1.3GB", f"res:{job_id}:1080"),
             ("1440p · 약 3.2GB", f"res:{job_id}:1440"), ("4K · 약 6.4GB", f"res:{job_id}:2160")],
        )

    def test_up_to_1080p_downloads_without_asking(self):
        job_id = models.create_job("https://youtu.be/hd", 7)
        with patch.object(downloader, "_probe", return_value=INFO_1080), \
                patch.object(downloader, "_download", side_effect=RuntimeError("stop")) as download:
            downloader._process_job(models.get_job(job_id), lambda *args, **kw: None)

        self.assertEqual(download.call_args.args[0]["max_height"], 0)
        self.assertEqual(models.get_job(job_id)["max_height"], 0)

    def test_chosen_resolution_skips_probe(self):
        job_id = models.create_job("https://youtu.be/4k", 7)
        models.request_resolution_choice(job_id, time.time() + 600)
        self.assertTrue(models.choose_resolution(job_id, 1440))
        with patch.object(downloader, "_probe") as probe, \
                patch.object(downloader, "_download", side_effect=RuntimeError("stop")) as download:
            downloader._process_job(models.get_job(job_id), lambda *args, **kw: None)

        probe.assert_not_called()
        self.assertEqual(download.call_args.args[0]["max_height"], 1440)

    def test_failed_job_clears_stage(self):
        job_id = models.create_job("https://youtu.be/hd", 7)
        models.update_job(job_id, max_height=0)
        with patch.object(downloader, "_download", side_effect=RuntimeError("boom")):
            downloader._process_job(models.get_job(job_id), lambda *args, **kw: None)
        job = models.get_job(job_id)
        self.assertEqual(job["status"], models.STATUS_FAILED)
        self.assertIsNone(job["stage"])


class ChoiceStateTests(_TempDbTestCase):
    def test_choice_is_accepted_once(self):
        job_id = models.create_job("https://youtu.be/4k", 7)
        models.request_resolution_choice(job_id, time.time() + 600)
        self.assertTrue(models.choose_resolution(job_id, 2160))
        self.assertFalse(models.choose_resolution(job_id, 720))
        job = models.get_job(job_id)
        self.assertEqual((job["status"], job["max_height"]), (models.STATUS_PENDING, 2160))

    def test_expired_choice_uses_1080p_and_notifies(self):
        late = models.create_job("https://youtu.be/late", 7)
        fresh = models.create_job("https://youtu.be/fresh", 7)
        models.request_resolution_choice(late, time.time() - 1)
        models.request_resolution_choice(fresh, time.time() + 600)
        sent = []
        downloader._expire_resolution_choices(lambda chat_id, text: sent.append(text))

        self.assertEqual(models.get_job(late)["status"], models.STATUS_PENDING)
        self.assertEqual(models.get_job(late)["max_height"], 1080)
        self.assertEqual(models.get_job(fresh)["status"], models.STATUS_AWAITING_CHOICE)
        self.assertEqual(len(sent), 1)
        self.assertIn("1080p로 저장합니다", sent[0])

    def test_restart_keeps_waiting_jobs(self):
        job_id = models.create_job("https://youtu.be/4k", 7)
        models.request_resolution_choice(job_id, time.time() + 600)
        models.requeue_interrupted_jobs()
        self.assertEqual(models.get_job(job_id)["status"], models.STATUS_AWAITING_CHOICE)

    def test_existing_database_gets_new_columns(self):
        path = os.path.join(self.directory, "old.db")
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, title TEXT,"
                     " status TEXT NOT NULL DEFAULT 'pending', error_msg TEXT, file_path TEXT, gdrive_link TEXT,"
                     " notion_url TEXT, chat_id INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("INSERT INTO jobs (url, status, created_at, updated_at) VALUES ('u', 'done', 0, 0)")
        conn.commit()
        conn.close()
        with patch.object(models, "DB_PATH", path):
            models.init_db()
            job = models.get_job(1)
        self.assertEqual(job["status"], "done")
        for column in ("max_height", "choice_deadline", "stage", "progress", "stage_detail"):
            self.assertIn(column, job)


class _Stop(Exception):
    pass


class DownloadOptionTests(unittest.TestCase):
    def captured_options(self, job):
        captured = {}

        def fake_client(options):
            captured.update(options)
            raise _Stop  # 옵션만 확인하고 멈춘다

        with patch.object(downloader.yt_dlp, "YoutubeDL", side_effect=fake_client):
            with self.assertRaises(_Stop):
                downloader._download(job)
        return captured

    def test_chosen_height_limits_by_short_side(self):
        options = self.captured_options({"id": 1, "url": "u", "max_height": 1080})
        self.assertEqual(options["format_sort"], ["res:1080"])
        self.assertTrue(options["progress_hooks"])

    def test_no_limit_keeps_best_quality(self):
        self.assertNotIn("format_sort", self.captured_options({"id": 1, "url": "u", "max_height": 0}))


class StageReporterTests(unittest.TestCase):
    def test_progress_is_throttled_but_stage_changes_are_written(self):
        writes = []
        with patch.object(downloader, "set_stage", side_effect=lambda *args: writes.append(args)):
            report = downloader._StageReporter(1, min_interval=60)
            report("downloading", 1.0, "a")
            report("downloading", 2.0, "b")   # 같은 단계, 간격이 짧아 건너뜀
            report("merging")
        self.assertEqual([w[1:3] for w in writes], [("downloading", 1.0), ("merging", None)])

    def test_transfer_detail(self):
        self.assertEqual(downloader._transfer_detail(3.1 * 2**30, 6.5 * 2**30, 4.8 * MB, 750),
                         "3.1GB / 6.5GB · 4.8MB/s · 12분 남음")


class UploadProgressTests(unittest.TestCase):
    def test_chunks_report_progress(self):
        statuses = iter([(SimpleNamespace(progress=lambda: 0.5), None), (None, {"id": "file-id"})])
        request = MagicMock()
        request.next_chunk.side_effect = lambda num_retries: next(statuses)
        service = MagicMock()
        service.files.return_value.create.return_value = request
        seen = []
        with patch.object(gdrive, "MediaFileUpload"):
            file_id = gdrive._upload_single_file(service, "video.mp4", "video.mp4", "folder", seen.append)
        self.assertEqual(file_id, "file-id")
        self.assertEqual(seen, [0.5, 1.0])


class ButtonHandlerTests(_TempDbTestCase):
    def press(self, data, chat_id=7):
        query = SimpleNamespace(
            data=data, message=SimpleNamespace(text="🎬 4K 직캠"),
            answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=chat_id))
        with patch.object(telegram_bot, "TELEGRAM_ALLOWED_CHAT_IDS", [7]):
            asyncio.run(telegram_bot._handle_resolution_choice(update, None))
        return query

    def test_button_sets_resolution(self):
        job_id = models.create_job("https://youtu.be/4k", 7)
        models.request_resolution_choice(job_id, time.time() + 600)
        query = self.press(f"res:{job_id}:1440")
        self.assertEqual(models.get_job(job_id)["max_height"], 1440)
        self.assertIn("1440p로 저장합니다", query.edit_message_text.call_args.args[0])

    def test_late_press_only_removes_buttons(self):
        job_id = models.create_job("https://youtu.be/4k", 7)
        models.request_resolution_choice(job_id, time.time() + 600)
        models.choose_resolution(job_id, 1080)
        query = self.press(f"res:{job_id}:2160")
        self.assertEqual(models.get_job(job_id)["max_height"], 1080)
        query.edit_message_reply_markup.assert_awaited_once()

    def test_other_chats_cannot_choose(self):
        job_id = models.create_job("https://youtu.be/4k", 7)
        models.request_resolution_choice(job_id, time.time() + 600)
        self.press(f"res:{job_id}:2160", chat_id=99)
        self.assertEqual(models.get_job(job_id)["status"], models.STATUS_AWAITING_CHOICE)


class WebJobTests(unittest.TestCase):
    def test_remaining_choice_time_is_computed_on_server(self):
        job = web_app._prepare_job({"id": 1, "status": "awaiting_choice", "choice_deadline": time.time() + 300,
                                    "created_at": 0, "updated_at": 0})
        self.assertNotIn("choice_deadline", job)
        self.assertAlmostEqual(job["choice_remaining"], 300, delta=2)


if __name__ == "__main__":
    unittest.main()
