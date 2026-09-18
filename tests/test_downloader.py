import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker import downloader


class SubtitleTests(unittest.TestCase):
    def fetch(self, responses):
        requested = []
        with tempfile.TemporaryDirectory() as directory:
            base = str(Path(directory) / "video [id]")

            def make_client(options):
                lang = options["subtitleslangs"][0]
                requested.append(lang)

                class Client:
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False

                    def download(self, urls):
                        response = responses.get(lang)
                        if isinstance(response, Exception):
                            raise response
                        if response is not None:
                            Path(f"{base}.{lang}.vtt").write_text(response, encoding="utf-8")

                return Client()

            with patch.object(downloader.yt_dlp, "YoutubeDL", side_effect=make_client):
                result = downloader._fetch_subtitles("https://example.com/video", base)
            self.assertEqual(list(Path(directory).glob("*.vtt")), [])
        return result, requested

    def test_korean_success_skips_english(self):
        self.assertEqual(self.fetch({"ko": "WEBVTT\n\n00:00.000 --> 00:01.000\nhello"}),
                         ("hello", ["ko"]))

    def test_empty_korean_falls_back_to_english(self):
        self.assertEqual(self.fetch({"ko": "WEBVTT\n", "en": "English text"}),
                         ("English text", ["ko", "en"]))

    def test_missing_subtitles_is_nonfatal(self):
        self.assertEqual(self.fetch({}), ("", ["ko", "en"]))

    def test_rate_limit_stops_further_requests(self):
        error = downloader.yt_dlp.utils.DownloadError("HTTP Error 429: Too Many Requests")
        self.assertEqual(self.fetch({"ko": error}), ("", ["ko"]))


class FfmpegTests(unittest.TestCase):
    def test_path_and_local_install_support_separate_streams(self):
        for location in (None, "tools/ffmpeg"):
            with self.subTest(location=location), patch.object(downloader, "_ffmpeg_dir", return_value=location):
                self.assertTrue(downloader._has_ffmpeg())
                self.assertIn("bestvideo", downloader._video_format())

    def test_missing_ffmpeg_uses_combined_stream(self):
        with patch.object(downloader, "_ffmpeg_dir", return_value=""):
            self.assertFalse(downloader._has_ffmpeg())
            self.assertNotIn("bestvideo", downloader._video_format())


if __name__ == "__main__":
    unittest.main()
