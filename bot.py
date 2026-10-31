import asyncio
import logging
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).resolve()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "49"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media-bot")

if not BOT_TOKEN:
    raise RuntimeError("Configure BOT_TOKEN no arquivo .env.")
if not ALLOWED_USER_IDS:
    raise RuntimeError("Configure pelo menos um ID em ALLOWED_USER_IDS no arquivo .env.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
download_slots = asyncio.Semaphore(2)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def is_authorized(user_id: int | None) -> bool:
    return user_id is not None and user_id in ALLOWED_USER_IDS


def validate_url(url: str) -> bool:
    """Aceita apenas URLs HTTP(S) públicas; bloqueia localhost e IPs privados."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            addresses = socket.getaddrinfo(host, None)
            for item in addresses:
                ip = item[4][0].split("%")[0]
                if not __import__("ipaddress").ip_address(ip).is_global:
                    return False
        except socket.gaierror:
            return False
        return True
    except Exception:
        return False


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
    }


def inspect_media(url: str):
    opts = ydl_base()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_media(url: str, mode: str, quality: str):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    opts = ydl_base()
    opts["outtmpl"] = str(DOWNLOAD_DIR / "%(title).120B-%(id)s.%(ext)s")
    opts["noplaylist"] = True
    opts["overwrites"] = True

    if mode == "audio":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
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
            # Algumas fontes retornam o caminho final em requested_downloads.
            requested = info.get("requested_downloads") or []
            if requested and requested[0].get("filepath"):
                filepath = Path(requested[0]["filepath"])
        return filepath, info


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
    if not is_authorized(message.from_user.id if message.from_user else None):
        await message.answer("Acesso restrito. Peça ao administrador para liberar seu ID.")
        return
    await message.answer(
        "Envie um link público de vídeo ou áudio.\n"
        "Vou analisar o conteúdo e mostrar as opções de download.\n\n"
        "Comandos: /id mostra seu ID; /help mostra esta mensagem."
    )


@dp.message(Command("id"))
async def show_id(message: Message):
    uid = message.from_user.id if message.from_user else "desconhecido"
    await message.answer(f"Seu Telegram ID: `{uid}`", parse_mode="Markdown")


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await start(message)


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
    if not validate_url(url):
        await message.answer("Esse endereço não parece ser uma URL pública HTTP(S válida.")
        return

    status = await message.answer("🔎 Analisando o link...")
    try:
        async with download_slots:
            info = await asyncio.to_thread(inspect_media, url)
        title = info.get("title") or "Mídia sem título"
        duration = info.get("duration")
        duration_text = f"\nDuração: {int(duration // 60)}:{int(duration % 60):02d}" if duration else ""
        await status.edit_text(
            f"**{title[:700]}**{duration_text}\n\nEscolha como deseja baixar:",
            parse_mode="Markdown",
            reply_markup=keyboard(),
        )
        # Guarda a URL associada ao botão na memória do processo.
        pending[(uid, status.message_id)] = url
    except Exception as exc:
        log.exception("Falha ao analisar URL")
        await status.edit_text(f"Não consegui analisar esse link.\n\nDetalhe: {str(exc)[:700]}")


pending = {}


@dp.callback_query(F.data.startswith(("video:", "audio:")))
async def choose_download(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_authorized(uid):
        await callback.answer("Acesso não autorizado.", show_alert=True)
        return

    key = (uid, callback.message.message_id if callback.message else -1)
    url = pending.get(key)
    if not url:
        await callback.answer("Esse pedido expirou. Envie o link novamente.", show_alert=True)
        return

    mode, quality = callback.data.split(":", 1)
    await callback.answer("Download iniciado.")
    await callback.message.edit_text("⏬ Baixando e preparando o arquivo...")
    path = None
    try:
        async with download_slots:
            path, info = await asyncio.to_thread(download_media, url, mode, quality)

        if not path.exists():
            raise FileNotFoundError("O download terminou, mas o arquivo final não foi localizado.")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(
                f"O arquivo tem {size / (1024*1024):.1f} MB e ultrapassa o limite configurado "
                f"de {MAX_FILE_MB} MB. Reduza a qualidade ou use um servidor Bot API local."
            )

        title = (info.get("title") or path.name)[:900]
        await bot.send_chat_action(callback.message.chat.id, ChatAction.UPLOAD_DOCUMENT)
        if mode == "audio":
            await callback.message.answer_audio(
                audio=path, title=title[:64], caption=f"🎵 {title}"
            )
        else:
            # Envia como documento para preservar o arquivo sem recodificação do Telegram.
            await callback.message.answer_document(
                document=path, caption=f"🎬 {title}"
            )
        await callback.message.answer("Pronto. Pode mandar outro link.")
    except Exception as exc:
        log.exception("Falha no download")
        await callback.message.answer(f"Não consegui concluir o download.\n\n{str(exc)[:900]}")
    finally:
        pending.pop(key, None)
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass


async def main():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Bot iniciado. Usuários autorizados: %s", sorted(ALLOWED_USER_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
