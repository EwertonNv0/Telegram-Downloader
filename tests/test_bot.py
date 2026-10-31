import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

os.environ["BOT_TOKEN"] = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ALLOWED_USER_IDS"] = "123456789"

import bot


class ValidateUrlTests(unittest.TestCase):
    @patch(
        "bot.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    def test_accepts_public_host(self, _getaddrinfo):
        self.assertTrue(bot.validate_url("https://example.com/watch?v=1"))

    @patch(
        "bot.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ],
    )
    def test_rejects_host_with_any_non_public_address(self, _getaddrinfo):
        self.assertFalse(bot.validate_url("https://example.com"))

    @patch("bot.socket.getaddrinfo")
    def test_rejects_invalid_schemes_credentials_and_local_hosts(self, getaddrinfo):
        for url in (
            "file:///etc/passwd",
            "https://user:password@example.com",
            "http://localhost",
            "http://printer.local",
            "http://example.com:invalid",
        ):
            with self.subTest(url=url):
                self.assertFalse(bot.validate_url(url))
        getaddrinfo.assert_not_called()


class ConfigurationAndDownloadTests(unittest.TestCase):
    def test_file_size_configuration_must_be_positive_integer(self):
        self.assertEqual(bot.parse_max_file_mb("49"), 49)
        for value in ("0", "-1", "invalid"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                bot.parse_max_file_mb(value)

    def test_download_size_hook_rejects_oversized_download(self):
        bot.check_download_size({"status": "downloading", "downloaded_bytes": 1})
        with self.assertRaises(bot.DownloadTooLarge):
            bot.check_download_size(
                {"status": "downloading", "downloaded_bytes": bot.MAX_FILE_BYTES + 1}
            )

    def test_cloud_api_limit_is_applied_even_when_download_limit_is_1gb(self):
        configured_limit = 1024 * 1024 * 1024
        cloud_limit = 49 * 1024 * 1024
        self.assertEqual(min(configured_limit, cloud_limit), cloud_limit)
        with (
            patch.object(bot, "TELEGRAM_API_BASE_URL", ""),
            patch.object(bot, "MAX_FILE_MB", 1024),
            patch.object(bot, "MAX_FILE_BYTES", configured_limit),
            patch.object(bot, "DOWNLOAD_LIMIT_BYTES", cloud_limit),
            self.assertRaises(bot.DownloadTooLarge) as error,
        ):
            bot.check_download_size({"status": "downloading", "downloaded_bytes": cloud_limit + 1})
        self.assertIn("Bot API", str(error.exception))

    def test_local_api_configuration_uses_aiogram_local_api_server(self):
        local_bot = bot.create_bot(
            "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "http://127.0.0.1:8081",
        )
        try:
            self.assertTrue(local_bot.session.api.is_local)
            self.assertEqual(
                local_bot.session.api.base,
                "http://127.0.0.1:8081/bot{token}/{method}",
            )
        finally:
            asyncio.run(local_bot.session.close())

    def test_downloads_with_the_same_title_use_distinct_directories(self):
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download):
                assert download
                self.outtmpl = self.options["outtmpl"]
                self.output = self.options["outtmpl"].replace(
                    "%(id)s", "same-id"
                ).replace("%(ext)s", "mp4")
                Path(self.output).write_bytes(b"file")
                return {"title": "same-title", "id": "same-id", "ext": "mp4"}

            def prepare_filename(self, _info):
                return self.output

        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(bot, "DOWNLOAD_DIR", Path(root)),
                patch.object(bot, "TEMP_DIR", Path(root)),
                patch.object(bot, "validate_url", return_value=True),
                patch.object(bot.yt_dlp, "YoutubeDL", FakeYoutubeDL),
            ):
                first = bot.download_media("https://example.com", "video", "best")
                second = bot.download_media("https://example.com", "video", "best")

            try:
                self.assertNotEqual(first[0].parent, second[0].parent)
                self.assertLess(len(first[0].name), 32)
                self.assertLess(len(first[0].parent.name), 12)
                self.assertTrue(first[0].is_file())
                self.assertTrue(second[0].is_file())
            finally:
                bot.cleanup_download_directory(first[2])
                bot.cleanup_download_directory(second[2])

    def test_startup_cleanup_removes_only_known_temporary_download_directories(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            stale_dirs = (
                root_path / "td-abandoned",
                root_path / "telegram-download-abandoned",
            )
            for path in stale_dirs:
                path.mkdir()
                (path / "partial.mp4.part").write_bytes(b"partial")
            preserved_dir = root_path / "user-files"
            preserved_dir.mkdir()
            preserved_file = root_path / "notes.txt"
            preserved_file.write_text("keep")
            active_dir = root_path / "td-active"
            active_dir.mkdir()
            with bot.active_download_dirs_lock:
                bot.active_download_dirs.add(active_dir.resolve())

            with patch.object(bot, "TEMP_DIR", root_path):
                try:
                    bot.cleanup_stale_downloads()
                finally:
                    with bot.active_download_dirs_lock:
                        bot.active_download_dirs.discard(active_dir.resolve())

            for path in stale_dirs:
                self.assertFalse(path.exists())
            self.assertTrue(preserved_dir.is_dir())
            self.assertTrue(preserved_file.is_file())
            self.assertTrue(active_dir.is_dir())

    def test_periodic_cleanup_runs_every_two_hours(self):
        async def run_cleanup():
            with (
                patch(
                    "bot.asyncio.sleep",
                    new_callable=AsyncMock,
                    side_effect=[None, asyncio.CancelledError],
                ) as sleep,
                patch("bot.asyncio.to_thread") as to_thread,
                self.assertRaises(asyncio.CancelledError),
            ):
                await bot.cleanup_stale_downloads_periodically()
            self.assertEqual(
                sleep.await_args_list,
                [
                    call(bot.DOWNLOAD_CLEANUP_INTERVAL_SECONDS),
                    call(bot.DOWNLOAD_CLEANUP_INTERVAL_SECONDS),
                ],
            )
            to_thread.assert_awaited_once_with(bot.cleanup_stale_downloads)

        asyncio.run(run_cleanup())

    def test_expired_pending_requests_are_removed(self):
        bot.pending.clear()
        bot.pending[(1, 1)] = ("https://example.com", 0)
        bot.pending[(1, 2)] = ("https://example.com", 100)
        with patch("bot.time.monotonic", return_value=bot.PENDING_TTL_SECONDS + 1):
            bot.cleanup_pending()
        self.assertNotIn((1, 1), bot.pending)
        self.assertIn((1, 2), bot.pending)
        bot.pending.clear()


if __name__ == "__main__":
    unittest.main()
