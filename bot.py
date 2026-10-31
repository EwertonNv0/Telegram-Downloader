import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path
from tempfile import mkdtemp
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramServerError,
)
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
ADMIN_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).resolve()
TEMP_DIR_SETTING = os.getenv("TEMP_DIR", "").strip()
TEMP_DIR = Path(TEMP_DIR_SETTING).resolve() if TEMP_DIR_SETTING else DOWNLOAD_DIR
TELEGRAM_API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "").strip().rstrip("/")


def parse_max_file_mb(value: str) -> int:
    try:
        max_file_mb = int(value)
    except ValueError as exc:
        raise RuntimeError("MAX_FILE_MB deve ser um número inteiro positivo.") from exc
    if max_file_mb <= 0:
        raise RuntimeError("MAX_FILE_MB deve ser um número inteiro positivo.")
    return max_file_mb


MAX_FILE_MB = parse_max_file_mb(os.getenv("MAX_FILE_MB", "49"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
TELEGRAM_UPLOAD_LIMIT_MB = 2000 if TELEGRAM_API_BASE_URL else 49
DOWNLOAD_LIMIT_BYTES = min(
    MAX_FILE_BYTES, TELEGRAM_UPLOAD_LIMIT_MB * 1024 * 1024
)
PENDING_TTL_SECONDS = 30 * 60
DOWNLOAD_CLEANUP_INTERVAL_SECONDS = 2 * 60 * 60
DOWNLOAD_TEMP_PREFIXES = ("td-", "telegram-download-")
TELEGRAM_SESSION_TIMEOUT_SECONDS = 120
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 30 * 60
TELEGRAM_UPLOAD_MAX_ATTEMPTS = 3
TELEGRAM_UPLOAD_RETRY_BASE_SECONDS = 2
DOWNLOAD_CLEANUP_MAX_ATTEMPTS = 4
DOWNLOAD_CLEANUP_RETRY_BASE_SECONDS = 0.25
RESTART_EXIT_CODE = 75

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media-bot")

if not BOT_TOKEN:
    raise RuntimeError("Configure BOT_TOKEN no arquivo .env.")
if not ALLOWED_USER_IDS:
    raise RuntimeError("Configure pelo menos um ID em ALLOWED_USER_IDS no arquivo .env.")


def create_bot(token: str, api_base_url: str = "") -> Bot:
    if not api_base_url:
        return Bot(token, session=AiohttpSession(timeout=TELEGRAM_SESSION_TIMEOUT_SECONDS))
    local_api = TelegramAPIServer.from_base(api_base_url, is_local=True)
    return Bot(
        token,
        session=AiohttpSession(
            api=local_api,
            timeout=TELEGRAM_SESSION_TIMEOUT_SECONDS,
        ),
    )


bot = create_bot(BOT_TOKEN, TELEGRAM_API_BASE_URL)
dp = Dispatcher()
download_slots = asyncio.Semaphore(2)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
pending: dict[tuple[int, int], tuple[str, float]] = {}
active_download_dirs: set[Path] = set()
active_download_dirs_lock = threading.Lock()
restart_requested = False


class DownloadTooLarge(Exception):
    pass


def is_authorized(user_id: int | None) -> bool:
    return user_id is not None and user_id in ALLOWED_USER_IDS


def validate_url(url: str) -> bool:
    """Reject non-public HTTP(S) destinations before handing a URL to yt-dlp."""
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        port = parsed.port
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            return False
        return all(
            ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global
            for item in addresses
        )
    except (OSError, ValueError):
        return False


def check_download_size(status: dict) -> None:
    downloaded_bytes = status.get("downloaded_bytes") or 0
    if (
        status.get("status") == "downloading"
        and downloaded_bytes > DOWNLOAD_LIMIT_BYTES
    ):
        if not TELEGRAM_API_BASE_URL and MAX_FILE_BYTES > DOWNLOAD_LIMIT_BYTES:
            raise DownloadTooLarge(
                "Este bot limita os envios pela Bot API hospedada pelo Telegram a 49 MB "
                "para evitar o limite oficial de 50 MB. "
                f"MAX_FILE_MB está em {MAX_FILE_MB} MB; configure "
                "TELEGRAM_API_BASE_URL com uma Bot API local para enviar arquivos maiores."
            )
        raise DownloadTooLarge(
            f"O download ultrapassou o limite efetivo de "
            f"{DOWNLOAD_LIMIT_BYTES // (1024 * 1024)} MB."
        )


def ydl_base():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "ffmpeg_location": shutil.which("ffmpeg") or None,
        "max_filesize": DOWNLOAD_LIMIT_BYTES,
        "progress_hooks": [check_download_size],
    }


def inspect_media(url: str):
    if not validate_url(url):
        raise ValueError("O endereço não aponta para um host público.")
    opts = ydl_base()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def cleanup_stale_downloads() -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    stale_before = time.time() - DOWNLOAD_CLEANUP_INTERVAL_SECONDS
    stale_paths = []
    with active_download_dirs_lock:
        for path in TEMP_DIR.iterdir():
            if not path.is_dir() or not path.name.startswith(DOWNLOAD_TEMP_PREFIXES):
                continue
            try:
                job_path = path.resolve()
                if job_path in active_download_dirs or path.stat().st_mtime > stale_before:
                    continue
                stale_paths.append(path)
            except OSError:
                log.exception("Falha ao verificar pasta temporária antiga: %s", path)
    for path in stale_paths:
        cleanup_download_directory(path)


def release_download_directory(job_dir: Path) -> None:
    if job_dir.is_dir():
        try:
            os.utime(job_dir, None)
        except OSError:
            log.exception("Falha ao atualizar a idade da pasta temporária: %s", job_dir)
    with active_download_dirs_lock:
        active_download_dirs.discard(job_dir.resolve())


def cleanup_download_directory(job_dir: Path) -> bool:
    job_path = job_dir.resolve()
    for attempt in range(1, DOWNLOAD_CLEANUP_MAX_ATTEMPTS + 1):
        try:
            try:
                shutil.rmtree(job_dir)
            except FileNotFoundError:
                pass
            release_download_directory(job_path)
            return True
        except OSError:
            if attempt == DOWNLOAD_CLEANUP_MAX_ATTEMPTS:
                log.exception("Falha ao limpar arquivos temporários do download: %s", job_dir)
            else:
                delay = DOWNLOAD_CLEANUP_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                log.warning(
                    "Arquivo temporário bloqueado; nova tentativa de limpeza %d/%d em %.2f s: %s",
                    attempt,
                    DOWNLOAD_CLEANUP_MAX_ATTEMPTS - 1,
                    delay,
                    job_dir,
                )
                time.sleep(delay)
    release_download_directory(job_path)
    return False


async def send_media_with_retries(message: Message, path: Path, mode: str, title: str) -> None:
    for attempt in range(1, TELEGRAM_UPLOAD_MAX_ATTEMPTS + 1):
        try:
            if mode == "audio":
                await message.answer_audio(
                    audio=FSInputFile(path),
                    title=title,
                    caption=f"🎵 {title}",
                    request_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
                )
            else:
                await message.answer_document(
                    document=FSInputFile(path),
                    caption=f"🎬 {title}",
                    request_timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
                )
            return
        except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError):
            if attempt == TELEGRAM_UPLOAD_MAX_ATTEMPTS:
                raise
            delay = TELEGRAM_UPLOAD_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "Falha transitória no envio ao Telegram; tentativa %d/%d em %d s",
                attempt + 1,
                TELEGRAM_UPLOAD_MAX_ATTEMPTS,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)


async def cleanup_stale_downloads_periodically() -> None:
    while True:
        await asyncio.sleep(DOWNLOAD_CLEANUP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(cleanup_stale_downloads)
        except OSError:
            log.exception("Falha ao verificar pastas temporárias antigas")


def download_media(url: str, mode: str, quality: str):
    if not validate_url(url):
        raise ValueError("O endereço não aponta para um host público.")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with active_download_dirs_lock:
        job_dir = Path(mkdtemp(prefix="td-", dir=TEMP_DIR)).resolve()
        active_download_dirs.add(job_dir)

    try:
        opts = ydl_base()
        opts["outtmpl"] = str(job_dir / "%(id)s.%(ext)s")
        opts["noplaylist"] = True
        opts["overwrites"] = True

        if mode == "audio":
            opts.update(
                {
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ],
                }
            )
        else:
            if quality == "best":
                opts["format"] = "bestvideo+bestaudio/best"
            else:
                opts["format"] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
            opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = Path(ydl.prepare_filename(info))
            if mode == "audio":
                filepath = filepath.with_suffix(".mp3")
            elif filepath.suffix.lower() != ".mp4":
                candidate = filepath.with_suffix(".mp4")
                if candidate.exists():
                    filepath = candidate
            if not filepath.exists():
                requested = info.get("requested_downloads") or []
                if requested and requested[0].get("filepath"):
                    filepath = Path(requested[0]["filepath"])
            return filepath, info, job_dir
    except Exception:
        cleanup_download_directory(job_dir)
        raise


def cleanup_pending() -> None:
    expiry = time.monotonic() - PENDING_TTL_SECONDS
    for key, (_, created_at) in list(pending.items()):
        if created_at < expiry:
            pending.pop(key, None)


def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Melhor qualidade", callback_data="video:best"),
            InlineKeyboardButton(text="📺 720p", callback_data="video:720"),
        ],
        [
            InlineKeyboardButton(text="📺 480p", callback_data="video:480"),
            InlineKeyboardButton(text="🎵 MP3", callback_data="audio:mp3"),
        ],
    ])


@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not is_authorized(user_id):
        await message.answer("Acesso restrito. Peça ao administrador para liberar seu ID.")
        return
    commands = "Comandos: /id mostra seu ID; /help mostra esta mensagem."
    if user_id in ADMIN_USER_IDS:
        commands += " /reiniciar reinicia o bot."
    await message.answer(
        "Envie um link público de vídeo ou áudio.\n"
        f"Vou analisar o conteúdo e mostrar as opções de download.\n\n{commands}"
    )


@dp.message(Command("id"))
async def show_id(message: Message):
    uid = message.from_user.id if message.from_user else "desconhecido"
    await message.answer(f"Seu Telegram ID: {uid}")


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await start(message)


@dp.message(Command("reiniciar"))
async def restart_bot(message: Message):
    global restart_requested
    user_id = message.from_user.id if message.from_user else None
    if user_id not in ADMIN_USER_IDS:
        await message.answer("Você não tem permissão para reiniciar o bot.")
        return
    if restart_requested:
        await message.answer("O bot já está reiniciando.")
        return

    restart_requested = True
    try:
        await message.answer("Reiniciando o bot...")
    except Exception:
        restart_requested = False
        raise
    asyncio.create_task(dp.stop_polling())


@dp.message(F.text)
async def receive_link(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not is_authorized(uid):
        await message.answer("Acesso restrito. Peça ao administrador para liberar seu ID.")
        return

    match = URL_RE.search(message.text or "")
    if not match:
        await message.answer("Não encontrei um link. Envie uma URL começando com http:// ou https://.")
        return

    url = match.group(0).rstrip(").,!?]}>")
    if not await asyncio.to_thread(validate_url, url):
        await message.answer("Esse endereço não parece ser uma URL pública HTTP(S válida.")
        return

    status = await message.answer("🔎 Analisando o link...")
    try:
        async with download_slots:
            info = await asyncio.to_thread(inspect_media, url)
        title = info.get("title") or "Mídia sem título"
        duration = info.get("duration")
        duration_text = f"\nDuração: {int(duration // 60)}:{int(duration % 60):02d}" if duration else ""
        cleanup_pending()
        await status.edit_text(
            f"{title[:700]}{duration_text}\n\nEscolha como deseja baixar:",
            reply_markup=keyboard(),
        )
        pending[(uid, status.message_id)] = (url, time.monotonic())
    except Exception:
        log.exception("Falha ao analisar URL")
        await status.edit_text("Não consegui analisar esse link. Confira a URL e tente novamente.")


@dp.callback_query(F.data.startswith(("video:", "audio:")))
async def choose_download(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_authorized(uid):
        await callback.answer("Acesso não autorizado.", show_alert=True)
        return

    if callback.message is None or callback.data is None:
        await callback.answer("Esse pedido expirou. Envie o link novamente.", show_alert=True)
        return

    key = (uid, callback.message.message_id)
    request = pending.pop(key, None)
    if request is None or time.monotonic() - request[1] > PENDING_TTL_SECONDS:
        await callback.answer("Esse pedido expirou. Envie o link novamente.", show_alert=True)
        return

    mode, quality = callback.data.split(":", 1)
    if (mode, quality) not in {
        ("audio", "mp3"),
        ("video", "best"),
        ("video", "720"),
        ("video", "480"),
    }:
        await callback.answer("Opção inválida. Envie o link novamente.", show_alert=True)
        return

    url = request[0]
    await callback.answer("Download iniciado.")
    await callback.message.edit_text("⏬ Baixando e preparando o arquivo...")
    job_dir = None
    preserve_download = False
    try:
        try:
            async with download_slots:
                path, info, job_dir = await asyncio.to_thread(download_media, url, mode, quality)
        except DownloadTooLarge as exc:
            await callback.message.answer(str(exc))
            return
        except yt_dlp.utils.PostProcessingError:
            log.exception("Falha no processamento da mídia pelo FFmpeg/yt-dlp")
            await callback.message.answer("A mídia foi baixada, mas não consegui processar o arquivo.")
            return
        except Exception:
            log.exception("Falha no download da mídia")
            await callback.message.answer("Não consegui baixar o arquivo. Confira o link e tente novamente.")
            return

        try:
            if not path.is_file():
                raise FileNotFoundError("O download terminou, mas o arquivo final não foi localizado.")
            size = path.stat().st_size
            if size > DOWNLOAD_LIMIT_BYTES:
                check_download_size(
                    {"status": "downloading", "downloaded_bytes": size}
                )
            title = (info.get("title") or path.name)[:900]
        except DownloadTooLarge as exc:
            await callback.message.answer(str(exc))
            return
        except Exception:
            log.exception("Falha ao preparar o arquivo baixado")
            await callback.message.answer("O download terminou, mas não consegui preparar o arquivo para envio.")
            return

        try:
            try:
                await bot.send_chat_action(
                    callback.message.chat.id,
                    ChatAction.UPLOAD_DOCUMENT,
                )
            except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError):
                log.warning("Falha ao notificar início do envio; vou tentar enviar a mídia mesmo assim", exc_info=True)
            await send_media_with_retries(callback.message, path, mode, title)
        except asyncio.CancelledError:
            preserve_download = True
            log.warning("Envio cancelado; arquivo temporário preservado em %s", path)
            raise
        except Exception:
            preserve_download = True
            log.exception("Falha no envio ao Telegram; download preservado em %s", path)
            await callback.message.answer(
                "O arquivo foi baixado, mas não consegui enviá-lo ao Telegram. "
                "Mantive o arquivo temporariamente no servidor."
            )
            return
        await callback.message.answer("Pronto. Pode mandar outro link.")
    finally:
        if job_dir is not None:
            if preserve_download:
                release_download_directory(job_dir)
            else:
                await asyncio.to_thread(cleanup_download_directory, job_dir)
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            log.warning("Não foi possível remover a mensagem de status do download")


async def main():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg não encontrado no PATH. Instale-o antes de iniciar o bot.")
    if not ADMIN_USER_IDS:
        log.warning("ADMIN_USER_IDS não configurado; o comando /reiniciar ficará desabilitado")
    cleanup_stale_downloads()
    log.info("Bot iniciado. Quantidade de usuários autorizados: %d", len(ALLOWED_USER_IDS))
    cleanup_task = asyncio.create_task(cleanup_stale_downloads_periodically())
    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
    if restart_requested:
        raise SystemExit(RESTART_EXIT_CODE)
