import os
import re
import asyncio
import mimetypes
import pathlib
import tempfile
import logging
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

MAX_DIRECT_DOWNLOAD_MB = 80
MAX_DIRECT_DOWNLOAD_BYTES = MAX_DIRECT_DOWNLOAD_MB * 1024 * 1024
CHUNK_SIZE = 1024 * 256
HTTP_TIMEOUT = 60
MAX_RETRIES = 2

CREATOR = "Islomzhon Ibragimov"
FOOTER = f"✨ *Created by {CREATOR}* ✨"
SEPARATOR = "━" * 30

MESSAGES = {
    "start": (
        "👋 *Welcome to Video Downloader Bot!*\n\n"
        "🎬 Send me a URL and I'll download it for you.\n\n"
        "✅ *Works best with:*\n"
        "• YouTube, TikTok, Instagram\n"
        "• Twitter/X, Reddit\n"
        "• Direct file links\n\n"
        "⚠️ _Direct downloads limited to ~80MB_\n\n"
        "Use /help for more info"
    ),
    "help": (
        "📖 *How to use:*\n\n"
        "1️⃣ Send any URL (http/https)\n"
        "2️⃣ Bot will try to download it\n"
        "3️⃣ Video or file sent directly to chat\n\n"
        "🎥 *Supported platforms:*\n"
        "YouTube • TikTok • Instagram • Twitter • Reddit • and more\n\n"
        "📝 *Tips:*\n"
        "• Use direct links for faster downloads\n"
        "• Large files may take longer\n"
        "• Some sites require public access\n\n"
        f"{SEPARATOR}\n"
        f"{FOOTER}\n"
        f"{SEPARATOR}"
    ),
    "send_url_prompt": "📝 *Send me a URL to download*\n\nExample: `https://youtube.com/watch?v=...`",
    "supported_sites": (
        "✨ *Supported Platforms:*\n\n"
        "🎥 *Video Platforms:*\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Twitter/X\n"
        "• Reddit\n"
        "• Facebook\n"
        "• Twitch\n\n"
        "🎵 *Audio:*\n"
        "• Spotify (with limitations)\n"
        "• SoundCloud\n"
        "• YouTube Music\n\n"
        "📁 *Other:*\n"
        "• Direct file links\n"
        "• Vimeo, DailyMotion\n"
        "• And 1000+ more sites!\n\n"
        "ℹ️ Some sites require public/unlisted content"
    ),
    "no_url": "❌ Please send a message with a *URL* (http/https)",
    "downloading": "⏳ *Downloading…*\n_Please wait, this may take a moment_",
    "uploading": "📤 *Uploading to Telegram…*",
    "failed": (
        "❌ *Download failed*\n\n"
        "Possible reasons:\n"
        "• Site requires login or DRM protection\n"
        "• Bot is blocked by the site\n"
        "• File is too large (>80MB)\n"
        "• Invalid or expired URL\n\n"
        "💡 Try a direct file link or different URL"
    ),
    "missing_token": "BOT_TOKEN is missing. Put it in .env",
    "message_closed": "✅ Message closed",
    "no_previous_url": "❌ No previous URL found. Please send a URL.",
    "about": (
        "🤖 *Video Downloader Bot*\n\n"
        "⚡ Fast and easy media downloads\n"
        "Supports 1000+ websites\n\n"
        "Powered by yt-dlp\n"
        "Built with python-telegram-bot\n\n"
        f"{SEPARATOR}\n"
        f"👨‍💻 *Created by:*\n"
        f"✨ {CREATOR} ✨\n"
        f"{SEPARATOR}\n\n"
        "🔗 [GitHub](https://github.com)"
    ),
    "goodbye": "👋 *Bot stopped*\n\nThanks for using Video Downloader Bot!\nSend /start to restart.",
}

def pick_filename_from_url(url: str) -> str:
    p = urlparse(url)
    name = pathlib.Path(p.path).name
    if not name:
        name = "download"
    name = name.split("?")[0].split("#")[0]
    return name or "download"


def get_help_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔗 Send URL", callback_data="send_url"),
            InlineKeyboardButton("📖 Documentation", url="https://github.com"),
        ],
        [
            InlineKeyboardButton("✨ Supported Sites", callback_data="sites"),
            InlineKeyboardButton("❌ Close", callback_data="close"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_retry_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔄 Try Again", callback_data="retry"),
            InlineKeyboardButton("📖 Help", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("📚 Help"), KeyboardButton("ℹ️ About")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_back_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def run_yt_dlp(url: str, out_dir: str) -> str | None:
    outtmpl = os.path.join(out_dir, "%(title).200s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-o", outtmpl,
        "-f", "bv*+ba/best",
        url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"yt-dlp failed for {url}: {stderr.decode()}")
            return None

        files = sorted(
            (os.path.join(out_dir, f) for f in os.listdir(out_dir)),
            key=lambda x: os.path.getmtime(x),
            reverse=True,
        )
        return files[0] if files else None
    except FileNotFoundError:
        logger.error("yt-dlp not found. Install it with: pip install yt-dlp")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in yt-dlp: {e}")
        return None


async def direct_download(url: str, out_dir: str) -> str | None:
    filename = pick_filename_from_url(url)
    out_path = os.path.join(out_dir, filename)

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TelegramDownloaderBot/1.0)"
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT) as client:
            head = None
            try:
                head = await client.head(url, headers=headers)
            except Exception as e:
                logger.debug(f"HEAD request failed for {url}: {e}")
                head = None

            if head is not None and head.status_code < 400:
                cl = head.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > MAX_DIRECT_DOWNLOAD_BYTES:
                    logger.warning(f"File too large: {cl} bytes for {url}")
                    return None

            async with client.stream("GET", url, headers=headers) as r:
                if r.status_code >= 400:
                    logger.warning(f"HTTP {r.status_code} for {url}")
                    return None

                total = 0
                ct = r.headers.get("content-type", "").split(";")[0].strip()

                if "." not in os.path.basename(out_path) and ct:
                    ext = mimetypes.guess_extension(ct) or ""
                    if ext and not out_path.endswith(ext):
                        out_path += ext

                with open(out_path, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DIRECT_DOWNLOAD_BYTES:
                            logger.warning(f"Download exceeded limit for {url}")
                            if os.path.exists(out_path):
                                os.remove(out_path)
                            return None
                        f.write(chunk)

        return out_path if os.path.exists(out_path) else None
    except Exception as e:
        logger.error(f"Direct download failed for {url}: {e}")
        if os.path.exists(out_path):
            os.remove(out_path)
        return None


async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str) -> None:
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)

    ext = pathlib.Path(file_name).suffix.lower()
    video_exts = {".mp4", ".mkv", ".mov", ".webm"}

    with open(file_path, "rb") as f:
        if ext in video_exts and size <= 49 * 1024 * 1024:
            await update.message.reply_video(video=f, caption=f"✅ `{file_name}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_document(document=f, caption=f"✅ `{file_name}`", parse_mode=ParseMode.MARKDOWN)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        MESSAGES["start"],
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_help_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        MESSAGES["help"],
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_help_keyboard(),
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        MESSAGES["about"],
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_help_keyboard(),
    )


async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, status_msg=None) -> None:
    """Helper function to process URL download"""
    if status_msg is None:
        status_msg = await update.message.reply_text(MESSAGES["downloading"], parse_mode=ParseMode.MARKDOWN)
    
    # Store URL in context for retry
    context.user_data["last_url"] = url
    
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = await run_yt_dlp(url, tmpdir)

        if not file_path:
            file_path = await direct_download(url, tmpdir)

        if not file_path:
            await status_msg.edit_text(
                MESSAGES["failed"],
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_retry_keyboard(),
            )
            return

        await status_msg.edit_text(MESSAGES["uploading"], parse_mode=ParseMode.MARKDOWN)
        await send_file(update, context, file_path)
        await status_msg.delete()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (update.message.text or "").strip()
    
    if msg == "📚 Help":
        await help_command(update, context)
        return
    
    if msg == "ℹ️ About":
        await about_command(update, context)
        return
    
    m = URL_RE.search(msg)
    if not m:
        await update.message.reply_text(
            MESSAGES["no_url"],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_help_keyboard(),
        )
        return

    url = m.group(1).rstrip(").,]}")
    await process_download(update, context, url)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button callbacks"""
    query = update.callback_query
    await query.answer()  # Remove loading animation

    if query.data == "send_url":
        await query.edit_message_text(
            MESSAGES["send_url_prompt"],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_back_keyboard(),
        )

    elif query.data == "sites":
        await query.edit_message_text(
            MESSAGES["supported_sites"],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_back_keyboard(),
        )

    elif query.data == "close":
        await query.edit_message_text(
            MESSAGES["goodbye"],
            parse_mode=ParseMode.MARKDOWN,
        )
        # Clear user data
        context.user_data.clear()

    elif query.data == "back_to_help":
        await query.edit_message_text(
            MESSAGES["help"],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_help_keyboard(),
        )

    elif query.data == "help":
        await query.edit_message_text(
            MESSAGES["help"],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_help_keyboard(),
        )

    elif query.data == "retry":
        last_url = context.user_data.get("last_url")
        if not last_url:
            await query.edit_message_text(
                MESSAGES["no_previous_url"],
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_help_keyboard(),
            )
            return

        # Delete the old message
        await query.delete_message()
        
        # Create new download status message
        status_msg = await query.message.reply_text(MESSAGES["downloading"], parse_mode=ParseMode.MARKDOWN)
        
        # Simulate update object for process_download
        class FakeUpdate:
            def __init__(self, message):
                self.message = message
        
        fake_update = FakeUpdate(status_msg)
        await process_download(fake_update, context, last_url, status_msg)


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(MESSAGES["missing_token"])

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
