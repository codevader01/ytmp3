import os
import re
from dotenv import load_dotenv
import uuid
from datetime import datetime
import asyncio
import logging
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
load_dotenv()

TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)

YOUTUBE_COOKIES_RAW = os.getenv("YOUTUBE_COOKIES")
COOKIE_PATH = Path("cookies.txt")

if YOUTUBE_COOKIES_RAW:
    with open(COOKIE_PATH, "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES_RAW)
    logging.info("YouTube cookies loaded from environment variable.")

# CONFIGURATION
TELEGRAM_BOT_TOKEN = os.getenv("API_KEY")
ADSTERRA_SMARTLINK = "https://www.effectivecpmnetwork.com/krgymfijv?key=dfdfcea0f160083fa0280f51e6b2b362"

# Temporary store for pending conversions: { user_id: { "title": str, "file_path": Path } }
pending_downloads = {}


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^\w\s-]", "", name).strip()[:100]


def log_download_click(user):
    """Appends the user's details and timestamp to downloads.txt when they click download."""
    username = f"@{user.username}" if user.username else "NoUsername"
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{user.id} | {username} | {user.full_name} | {date_str}\n"

    with open("downloads.txt", "a", encoding="utf-8") as f:
        f.write(line)


def remove_file(filepath: Path):
    """Deletes the temporary file instantly from local storage."""
    try:
        if filepath and filepath.exists():
            filepath.unlink()
            logging.info(f"[Cleaned] Removed temporary file: {filepath}")
    except Exception as e:
        logging.error(f"Failed to delete {filepath}: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or "User"

    await update.message.reply_text(
        f"🎵 *Welcome, @{username}! (ID: `{user_id}`)*\n\n"
        "Send me any valid video/audio link to extract the audio.\n"
        "⚠️ *Limit:* Video duration must be under 30 minutes.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    # Validate URL
    if not text.startswith("http://") and not text.startswith("https://"):
        await update.message.reply_text("❌ Please send a valid HTTP/HTTPS media link.")
        return

    status_message = await update.message.reply_text("🔍 Checking video details...")

    file_id = str(uuid.uuid4())
    output_template = str(TEMP_DIR / f"{file_id}.%(ext)s")

    try:
        # Step 1: Extract Info & Check 30-Minute Limit
        ydl_opts_info = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }

        # Load cookies if cookiefile exists
        if COOKIE_PATH.exists():
            ydl_opts_info["cookiefile"] = str(COOKIE_PATH)

        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(text, download=False)
            if not info:
                await status_message.edit_text("❌ Could not retrieve media information.")
                return

            duration = info.get("duration", 0)
            title = info.get("title", "audio")

            # 🛑 Hard Limit: 30 minutes (1800s)
            if duration and duration > 1800:
                mins = round(duration / 60)
                await status_message.edit_text(
                    f"🛑 Video exceeds the 30-minute limit ({mins} mins long)."
                )
                return

        # Step 2: Extract Best Audio Stream WITHOUT FFmpeg
        await status_message.edit_text("⚡ Extracting audio stream...")

        ydl_opts_download = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "quiet": True,
        }

        # Load cookies if cookiefile exists
        if COOKIE_PATH.exists():
            ydl_opts_download["cookiefile"] = str(COOKIE_PATH)

        with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
            download_info = ydl.extract_info(text, download=True)
            downloaded_file = Path(ydl.prepare_filename(download_info))

        if not downloaded_file.exists():
            await status_message.edit_text("❌ Audio extraction failed.")
            return

        # Store pending download details using Telegram User ID
        pending_downloads[user_id] = {
            "title": title,
            "file_path": downloaded_file,
        }

        # Step 3: Show Adsterra Smartlink & Verification Button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎉 1. Click to Open Sponsor Link", url=ADSTERRA_SMARTLINK)],
            [InlineKeyboardButton("✅ 2. I clicked the link (Get Audio)", callback_data="confirm_ad_click")]
        ])

        await status_message.edit_text(
            "✅ Audio Ready! Click the sponsor link below, then press 'I clicked the link' to receive your file.",
            reply_markup=keyboard,
        )

    except Exception as e:
        logging.error(f"Error handling URL for Telegram ID {user_id}: {e}")
        await status_message.edit_text(f"❌ Processing failed: {str(e)[:100]}")


async def ad_click_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback triggered when user presses 'I clicked the link'."""
    query = update.callback_query
    await query.answer()

    # Log user download click in downloads.txt
    log_download_click(query.from_user)

    user_id = query.from_user.id

    if user_id not in pending_downloads:
        await query.edit_message_text("❌ Download session expired. Please send the link again.")
        return

    download_data = pending_downloads.pop(user_id)
    downloaded_file_path = download_data["file_path"]
    title = download_data["title"]

    # Step 4: 5-Second Delay
    for i in range(5, 0, -1):
        await query.edit_message_text(f"⏳ Processing sponsor verification... Sending file in {i} seconds.")
        await asyncio.sleep(1)

    await query.edit_message_text("📤 Uploading audio to Telegram...")

    try:
        # Step 5: Send Native Audio File
        safe_title = sanitize_filename(title) or "audio"
        ext = downloaded_file_path.suffix

        with open(downloaded_file_path, "rb") as audio_file:
            await context.bot.send_audio(
                chat_id=query.message.chat_id,
                audio=audio_file,
                title=title,
                filename=f"{safe_title}{ext}",
                caption="Downloaded via Audio Bot",
            )

        # Remove status message
        await query.delete_message()

    except Exception as e:
        logging.error(f"Upload error for Telegram ID {user_id}: {e}")
        await query.edit_message_text("❌ Failed to send file.")
    finally:
        # Step 6: Delete file instantly from local storage
        remove_file(downloaded_file_path)


if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(CallbackQueryHandler(ad_click_callback, pattern="^confirm_ad_click$"))

    print("Telegram Bot is running")
    app.run_polling()
