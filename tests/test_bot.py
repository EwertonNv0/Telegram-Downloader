import asyncio
import os
import shutil
import socket
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

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

    def test_bot_configures_a_long_request_timeout_for_large_uploads(self):
        configured_bot = bot.create_bot("123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        try:
            self.assertEqual(
                configured_bot.session.timeout,
                bot.TELEGRAM_SESSION_TIMEOUT_SECONDS,
            )
        finally:
            asyncio.run(configured_bot.session.close())

    def test_upload_retries_transient_network_failure_without_redownloading(self):
        class FakeNetworkError(Exception):
            pass

        message = MagicMock()
        message.answer_document = AsyncMock(
            side_effect=[FakeNetworkError("timeout"), None]
        )
        path = Path("video.mp4")

        async def send():
            with (
                patch.object(bot, "TelegramNetworkError", FakeNetworkError),
                patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep,
            ):
                await bot.send_media_with_retries(message, path, "video", "Video")
            sleep.assert_awaited_once_with(bot.TELEGRAM_UPLOAD_RETRY_BASE_SECONDS)
            self.assertEqual(message.answer_document.await_count, 2)
            self.assertEqual(
                message.answer_document.await_args_list[0].kwargs["document"].path,
                path,
            )
            self.assertEqual(
                message.answer_document.await_args_list[1].kwargs["document"].path,
                path,
            )
            self.assertEqual(
                message.answer_document.await_args_list[0].kwargs["request_timeout"],
                bot.TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )

        asyncio.run(send())

    def test_cleanup_retries_temporary_windows_file_lock(self):
        with tempfile.TemporaryDirectory() as root:
            job_dir = Path(root) / "td-locked"
            job_dir.mkdir()
            with bot.active_download_dirs_lock:
                bot.active_download_dirs.add(job_dir.resolve())

            attempts = 0
            actual_rmtree = shutil.rmtree

            def rmtree_after_transient_lock(path):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("file in use")
                actual_rmtree(path)

            with (
                patch(
                    "bot.shutil.rmtree",
                    side_effect=rmtree_after_transient_lock,
                ) as rmtree,
                patch("bot.time.sleep"),
            ):
                self.assertTrue(bot.cleanup_download_directory(job_dir))
            self.assertEqual(rmtree.call_count, 3)
            self.assertFalse(job_dir.exists())
            with bot.active_download_dirs_lock:
                self.assertNotIn(job_dir.resolve(), bot.active_download_dirs)

    def test_failed_cleanup_preserves_recent_file_for_periodic_retry(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            job_dir = root_path / "td-locked"
            job_dir.mkdir()
            partial = job_dir / "video.mp4"
            partial.write_bytes(b"keep")
            with bot.active_download_dirs_lock:
                bot.active_download_dirs.add(job_dir.resolve())
            with (
                patch("bot.shutil.rmtree", side_effect=PermissionError("file in use")),
                patch("bot.time.sleep"),
                patch.object(bot, "TEMP_DIR", root_path),
            ):
                self.assertFalse(bot.cleanup_download_directory(job_dir))
                bot.cleanup_stale_downloads()
            self.assertTrue(partial.is_file())

    def test_failed_telegram_upload_keeps_downloaded_file(self):
        async def run_upload():
            with tempfile.TemporaryDirectory() as root:
                job_dir = Path(root) / "td-kept"
                job_dir.mkdir()
                video = job_dir / "video.mp4"
                video.write_bytes(b"video")
                with bot.active_download_dirs_lock:
                    bot.active_download_dirs.add(job_dir.resolve())

                callback = MagicMock()
                callback.from_user.id = 123456789
                callback.data = "video:best"
                callback.message.message_id = 42
                callback.message.chat.id = 99
                callback.answer = AsyncMock()
                callback.message.edit_text = AsyncMock()
                callback.message.answer = AsyncMock()
                callback.message.delete = AsyncMock()
                bot.pending[(123456789, 42)] = (
                    "https://example.com/video",
                    time.monotonic(),
                )
                try:
                    with (
                        patch.object(
                            bot,
                            "download_media",
                            return_value=(video, {"title": "Video"}, job_dir),
                        ),
                        patch.object(bot.bot, "send_chat_action", new_callable=AsyncMock),
                        patch.object(
                            bot,
                            "send_media_with_retries",
                            new_callable=AsyncMock,
                            side_effect=RuntimeError("Telegram unavailable"),
                        ),
                    ):
                        await bot.choose_download(callback)

                    self.assertTrue(video.is_file())
                    messages = [
                        awaited_call.args[0]
                        for awaited_call in callback.message.answer.await_args_list
                    ]
                    self.assertTrue(
                        any("Mantive o arquivo temporariamente" in text for text in messages)
                    )
                    with bot.active_download_dirs_lock:
                        self.assertNotIn(job_dir.resolve(), bot.active_download_dirs)
                finally:
                    bot.pending.pop((123456789, 42), None)

        asyncio.run(run_upload())

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
                stale_time = time.time() - bot.DOWNLOAD_CLEANUP_INTERVAL_SECONDS - 1
                os.utime(path, (stale_time, stale_time))
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
