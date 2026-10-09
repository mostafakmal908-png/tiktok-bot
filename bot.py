"""Telegram bot that downloads TikTok videos and shares their descriptions."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from html import escape
import logging
import os
import re
import threading
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask
from telegram import (
  TIKTOK_URL_REGEX = r'https?://(?:www\.|vt\.|vm\.|mobile\.)?tiktok\.com/[^\s]+'

def expand_short_url(url):
    try:
        response = requests.head(url, allow_redirects=True, timeout=10)
        return response.url
    except Exception:
        return url
  Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import InvalidToken
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


class SecretRedactionFilter(logging.Filter):
    """Prevent the configured Telegram token from appearing in logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not TOKEN:
            return True

        message = record.getMessage()
        if TOKEN in message:
            record.msg = message.replace(TOKEN, "[REDACTED]")
            record.args = ()

        if record.exc_info:
            exception_text = "".join(traceback.format_exception(*record.exc_info))
            if TOKEN in exception_text:
                record.exc_text = exception_text.replace(TOKEN, "[REDACTED]")
                record.exc_info = None

        if record.exc_text and TOKEN in record.exc_text:
            record.exc_text = record.exc_text.replace(TOKEN, "[REDACTED]")
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedactionFilter())

# HTTPX includes full request URLs in INFO logs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

MAX_VIDEO_CAPTION_LENGTH = 1024
MAX_MESSAGE_LENGTH = 4096
MAX_TELEGRAM_VIDEO_SIZE = 50 * 1024 * 1024
PROGRESS_UPDATE_INTERVAL = 1.2
TIKWM_API_URL = "https://www.tikwm.com/api/"
TIKTOK_URL_HOST_SUFFIXES = ("tiktok.com", "douyin.com")
VIDEO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TRUSTED_VIDEO_HOST_SUFFIXES = (
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokv.com",
    "akamaized.net",
    "ibytedtos.com",
    "byteoversea.com",
    "tikwm.com",
)
MAX_VIDEO_REDIRECTS = 5


def build_start_menu() -> InlineKeyboardMarkup:
    """Create the inline welcome menu."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⭐ ابدأ التحميل الآن ⭐",
                    callback_data="start_info",
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 مشاركة البوت",
                    switch_inline_query="",
                )
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome users and explain how to send a TikTok video."""
    del context
    message = update.effective_message
    if message:
        await message.reply_text(
            "⭐ <b>أهلاً بك في بوت تحميل فيديوهات TikTok!</b>\n\n"
            "🚀 <b>طريقة الاستخدام:</b>\n"
            "أرسل لي أي رابط فيديو من تيك توك، وسأقوم بتحميله لك بجودة "
            "عالية مع الوصف المكتوب ونسبة تقدم حية 📊.",
            parse_mode=ParseMode.HTML,
            reply_markup=build_start_menu(),
        )


async def button_click(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Acknowledge the inline start button and explain how to use the bot."""
    del context
    query = update.callback_query
    if not query:
        return

    await query.answer()
    if query.data not in {"start_info", "star_click", "start_help"}:
        return

    if not query.message:
        return

    await query.message.reply_text(
        "⭐ <b>بوت تحميل تيك توك يعمل بنجاح!</b>\n\n"
        "كل ما عليك فعله هو نسخ رابط أي فيديو من TikTok وإرساله هنا في "
        "المحادثة، وسيبدأ البوت بتنزيل الفيديو وإرسال الوصف.",
        parse_mode=ParseMode.HTML,
    )


def is_tiktok_url(url: str) -> bool:
    """Accept TikTok/Douyin links, including TikTok Lite and short-link hosts."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False

    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"http", "https"} and (
        any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in TIKTOK_URL_HOST_SUFFIXES
        )
    )


def extract_tiktok_url(text: str | None) -> str | None:
    """Return the first valid TikTok URL embedded anywhere in a message."""
    if not text:
        return None

    search_from = 0
    url_pattern = r"""https?://[^\s<>"']+"""
    while match := re.search(
        url_pattern, text[search_from:], re.IGNORECASE
    ):
        url = match.group(0).rstrip(".,!?;:)]}>")
        if is_tiktok_url(url):
            return url
        search_from += match.end()

    return None


def telegram_text_units(text: str) -> int:
    """Count UTF-16 units as used by Telegram message length limits."""
    return len(text.encode("utf-16-le")) // 2


def split_telegram_text(text: str, max_units: int) -> list[str]:
    """Split text within Telegram's limit while keeping Unicode characters intact."""
    chunks: list[str] = []
    start = 0

    while start < len(text):
        end = start
        units = 0
        last_space = -1

        while end < len(text):
            char_units = telegram_text_units(text[end])
            if units + char_units > max_units:
                break
            units += char_units
            if text[end].isspace():
                last_space = end + 1
            end += 1

        if end < len(text) and last_space > start:
            end = last_space
        if end == start:
            end += 1

        chunks.append(text[start:end])
        start = end

    return chunks


def download_video(
    url: str,
    output_dir: str,
    progress_callback: Callable[[int | None], None] | None = None,
) -> tuple[Path, str]:
    """Download one TikTok video and return its path and full description."""
    directory = Path(output_dir)

    def on_progress(progress: dict[str, Any]) -> None:
        if not progress_callback:
            return

        if progress.get("status") == "downloading":
            total_value = (
                progress.get("total_bytes")
                or progress.get("total_bytes_estimate")
                or 0
            )
            downloaded_value = progress.get("downloaded_bytes") or 0
            try:
                total = int(total_value)
                downloaded = int(downloaded_value)
            except (TypeError, ValueError):
                total = downloaded = 0
            percent = min(99, int(downloaded * 100 / total)) if total else None
            progress_callback(percent)
        elif progress.get("status") == "finished":
            progress_callback(100)

    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": VIDEO_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        },
        "socket_timeout": 30,
        "retries": 5,
        "extractor_retries": 3,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "progress_hooks": [on_progress],
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        video_path = Path(info.get("filepath") or downloader.prepare_filename(info))

    if not video_path.is_file():
        media_files = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
        ]
        if len(media_files) != 1:
            raise FileNotFoundError("yt-dlp did not produce a single video file.")
        video_path = media_files[0]

    description = info.get("description") or info.get("title") or "بدون وصف"
    return video_path, str(description)


def is_trusted_video_url(url: str) -> bool:
    """Restrict API-provided video URLs and redirects to known TikTok/CDN hosts."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False

    return (
        parsed.scheme == "https"
        and bool(host)
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in TRUSTED_VIDEO_HOST_SUFFIXES
        )
    )


def open_video_stream(
    session: requests.Session, video_url: str
) -> requests.Response:
    """Fetch a TikWM video without following redirects to untrusted hosts."""
    current_url = video_url
    for _ in range(MAX_VIDEO_REDIRECTS + 1):
        if not is_trusted_video_url(current_url):
            raise ValueError("TikWM returned an untrusted video URL.")

        response = session.get(
            current_url,
            headers={"User-Agent": VIDEO_USER_AGENT},
            timeout=(10, 30),
            stream=True,
            allow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise requests.RequestException(
                    "Video server returned a redirect without a destination."
                )
            current_url = urljoin(current_url, location)
            continue

        try:
            response.raise_for_status()
            if not is_trusted_video_url(response.url):
                raise ValueError("Video server redirected to an untrusted host.")
        except Exception:
            response.close()
            raise
        return response

    raise requests.TooManyRedirects("Video server exceeded the redirect limit.")


def download_via_tikwm(
    url: str,
    output_dir: str,
    progress_callback: Callable[[int | None], None] | None = None,
) -> tuple[Path, str]:
    """Download a TikTok video through TikWM with bounded, validated requests."""
    api_response = requests.get(
        TIKWM_API_URL,
        params={"url": url},
        headers={
            "User-Agent": VIDEO_USER_AGENT,
            "Accept": "application/json",
        },
        timeout=(5, 20),
    )
    api_response.raise_for_status()
    payload = api_response.json()
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
        raise ValueError("TikWM could not resolve this TikTok link.")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("TikWM returned invalid video metadata.")
    video_url = data.get("play")
    if not isinstance(video_url, str) or not is_trusted_video_url(video_url):
        raise ValueError("TikWM returned an invalid video URL.")

    description = data.get("title")
    if not isinstance(description, str) or not description.strip():
        description = "بدون وصف"

    video_path = Path(output_dir) / "tikwm-video.mp4"
    downloaded = 0
    try:
        with requests.Session() as session:
            response = open_video_stream(session, video_url)
            with response:
                content_length = response.headers.get("Content-Length")
                try:
                    total = int(content_length) if content_length else 0
                except ValueError:
                    total = 0
                if total > MAX_TELEGRAM_VIDEO_SIZE:
                    raise ValueError("TikWM video exceeds Telegram's upload limit.")

                content_type = (
                    response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type and not (
                    content_type.startswith("video/")
                    or content_type
                    in {"application/octet-stream", "binary/octet-stream"}
                ):
                    raise ValueError("TikWM did not return a video stream.")

                if progress_callback:
                    progress_callback(0 if total else None)

                with video_path.open("wb") as video_file:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > MAX_TELEGRAM_VIDEO_SIZE:
                            raise ValueError(
                                "TikWM video exceeds Telegram's upload limit."
                            )
                        video_file.write(chunk)
                        if progress_callback:
                            percent = (
                                min(99, int(downloaded * 100 / total))
                                if total
                                else None
                            )
                            progress_callback(percent)

        if downloaded == 0:
            raise ValueError("TikWM returned an empty video.")
        if progress_callback:
            progress_callback(100)
        return video_path, description
    except Exception:
        video_path.unlink(missing_ok=True)
        raise


async def show_download_progress(
    status_message: Message, progress_queue: asyncio.Queue[int | None]
) -> None:
    """Render a rate-limited live progress bar in the status message."""
    loop = asyncio.get_running_loop()
    last_update = 0.0
    last_percent: int | None | object = object()

    while True:
        percent = await progress_queue.get()
        wait_for = PROGRESS_UPDATE_INTERVAL - (loop.time() - last_update)
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        while not progress_queue.empty():
            with suppress(asyncio.QueueEmpty):
                percent = progress_queue.get_nowait()

        if percent == last_percent:
            continue
        last_percent = percent

        if percent is None:
            bar = "░" * 10
            progress_label = "جارٍ حساب النسبة"
        else:
            percentage = min(100, max(0, percent))
            filled = percentage // 10
            bar = "█" * filled + "░" * (10 - filled)
            progress_label = f"{percentage}%"

        try:
            await status_message.edit_text(
                "⏳ <b>جاري تحميل الفيديو...</b>\n\n"
                f"<code>[{bar}] {progress_label}</code>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            logger.warning("Could not update the download progress message.")
            return
        last_update = loop.time()


async def run_download_with_progress(
    downloader: Callable[
        [str, str, Callable[[int | None], None] | None], tuple[Path, str]
    ],
    url: str,
    output_dir: str,
    status_message: Message,
) -> tuple[Path, str]:
    """Run a blocking downloader while keeping Telegram progress updates live."""
    loop = asyncio.get_running_loop()
    progress_queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=1)

    def enqueue_progress(percent: int | None) -> None:
        if progress_queue.full():
            with suppress(asyncio.QueueEmpty):
                progress_queue.get_nowait()
        with suppress(asyncio.QueueFull):
            progress_queue.put_nowait(percent)

    def report_progress(percent: int | None) -> None:
        loop.call_soon_threadsafe(enqueue_progress, percent)

    progress_task = asyncio.create_task(
        show_download_progress(status_message, progress_queue)
    )
    try:
        return await loop.run_in_executor(
            None, downloader, url, output_dir, report_progress
        )
    finally:
        progress_task.cancel()
        with suppress(asyncio.CancelledError):
            await progress_task


async def keep_upload_action(bot: Bot, chat_id: int) -> None:
    """Show Telegram's upload indicator while the video is being sent."""
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        except TelegramError:
            logger.debug("Could not send the video upload indicator.")
            return
        await asyncio.sleep(4)


async def download_tiktok(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download a TikTok link and send the video with its description."""
    message = update.effective_message
    if not message:
        return

    message_body = message.text or message.caption
    url = extract_tiktok_url(message_body)
    if not url:
        return

    status = await message.reply_text(
        "⏳ <b>جاري جلب الفيديو والوصف...</b>",
        parse_mode=ParseMode.HTML,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="telegram-tiktok-") as temp_dir:
            try:
                video_path, description = await run_download_with_progress(
                    download_via_tikwm, url, temp_dir, status
                )
            except Exception as tikwm_error:
                logger.warning(
                    "TikWM failed (%s); trying yt-dlp fallback.",
                    type(tikwm_error).__name__,
                )
                for partial_file in Path(temp_dir).iterdir():
                    if partial_file.is_file():
                        partial_file.unlink(missing_ok=True)

                await status.edit_text(
                    "🔄 <b>تعذر جلب الفيديو من المصدر الأول، جارٍ المحاولة عبر yt-dlp...</b>",
                    parse_mode=ParseMode.HTML,
                )
                try:
                    video_path, description = await run_download_with_progress(
                        download_video, url, temp_dir, status
                    )
                except Exception as yt_dlp_error:
                    logger.error(
                        "Both download methods failed (TikWM=%s, yt-dlp=%s).",
                        type(tikwm_error).__name__,
                        type(yt_dlp_error).__name__,
                    )
                    raise

            if video_path.stat().st_size > MAX_TELEGRAM_VIDEO_SIZE:
                await status.edit_text(
                    "حجم الفيديو أكبر من الحد الذي يسمح تيليجرام برفعه."
                )
                return

            caption_heading = "🎬 الفيديو من تيك توك\n\n📝 الوصف:\n"
            caption_description_limit = (
                MAX_VIDEO_CAPTION_LENGTH - telegram_text_units(caption_heading)
            )
            caption_parts = split_telegram_text(
                description, caption_description_limit
            )
            video_description = caption_parts[0] if caption_parts else "بدون وصف"
            video_caption = (
                "<b>🎬 الفيديو من تيك توك</b>\n\n"
                "<b>📝 الوصف:</b>\n"
                f"{escape(video_description)}"
            )
            remaining_description = "".join(caption_parts[1:])

            await status.edit_text(
                "✅ <b>اكتمل التحميل، جاري إرسال الفيديو والوصف...</b>",
                parse_mode=ParseMode.HTML,
            )
            upload_task = asyncio.create_task(
                keep_upload_action(context.bot, message.chat_id)
            )
            with video_path.open("rb") as video_file:
                try:
                    await message.reply_video(
                        video=video_file,
                        caption=video_caption,
                        parse_mode=ParseMode.HTML,
                    )
                finally:
                    upload_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await upload_task

            if remaining_description:
                continuation_heading = "📝 <b>متابعة الوصف:</b>\n\n"
                continuation_limit = (
                    MAX_MESSAGE_LENGTH - telegram_text_units("📝 متابعة الوصف:\n\n")
                )
                continuation_parts = split_telegram_text(
                    remaining_description, continuation_limit
                )
                for index, part in enumerate(continuation_parts):
                    await message.reply_text(
                        f"{continuation_heading if index == 0 else ''}"
                        f"{escape(part)}",
                        parse_mode=ParseMode.HTML,
                    )

        await status.delete()
    except Exception as error:
        logger.error(
            "Failed to download or send TikTok video (%s).",
            type(error).__name__,
        )
        await status.edit_text(
            "❌ <b>حدث خطأ أثناء تحميل الفيديو.</b>\n"
            "تأكد أن الرابط صحيح وأن الفيديو غير خاص، ثم حاول مرة أخرى.",
            parse_mode=ParseMode.HTML,
        )


app_web = Flask(__name__)


@app_web.get("/")
def home() -> str:
    """Return a simple response for external keep-alive checks."""
    return "Bot is alive!"


@app_web.get("/api/healthz")
def api_health() -> dict[str, str]:
    """Preserve the API server's health response on the shared Flask service."""
    return {"status": "ok"}


def run_web() -> None:
    """Serve the keep-alive route on the configured or default port."""
    app_web.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        debug=False,
        use_reloader=False,
    )


def keep_alive() -> None:
    """Start the keep-alive web server without blocking Telegram polling."""
    web_thread = threading.Thread(
        target=run_web,
        name="telegram-keep-alive",
        daemon=True,
    )
    web_thread.start()


def main() -> None:
    """Start the bot with long polling."""
    if not TOKEN:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN. Add the regenerated token to Replit "
            "Secrets or telegram_bot/.env; see telegram_bot/README.md."
        )

    keep_alive()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(
            button_click,
            pattern="^(start_info|star_click|start_help)$",
        )
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_tiktok)
    )
    logger.info("Telegram TikTok bot is starting.")
    try:
        app.run_polling()
    except InvalidToken:
        logger.error(
            "Telegram rejected TELEGRAM_BOT_TOKEN. Replace the secret and restart."
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
