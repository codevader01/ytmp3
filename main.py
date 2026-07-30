import os
import re
import uuid
import asyncio
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
load_dotenv()

TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("API_KEY")
ADSTERRA_SMARTLINK = "https://www.effectivecpmnetwork.com/krgymfijv?key=dfdfcea0f160083fa0280f51e6b2b362"

pending_downloads = {}

# Public Invidious API instances pool for downloading YouTube audio streams
INVIDIOUS_INSTANCES = [
    "https://invidious.nerdvpn.de",
    "https://inv.tux.pizza",
    "https://invidious.flokinet.to",
    "https://invidious.projectsegfau.lt",
]


def extract_youtube_id(url: str) -> str:
    """Extracts the video ID from standard or shortened YouTube URLs."""
    pattern = r"(?:v=|\/([0-9A-Za-z_-]{11})|youtu\.be\/)([0-9A-Za-z_-]{11})"
    match = re.search(pattern, url)
    if match:
        return match.group(1) or match.group(2)
    return None


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^\w\s-]", "", name).strip()[:100]


def log_download_click(user):
    username = f"@{user.username}" if user.username else "NoUsername"
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{user.id} | {username} | {user.full_name} | {date_str}\n"

    with open("downloads.txt", "a", encoding="utf-8") as f:
        f.write(line)


def remove_file(filepath: Path):
    try:
        if filepath and filepath.exists():
            filepath.unlink()
            logging.info(f"[Cleaned] Removed temporary file: {filepath}")
    except Exception as e:
        logging.error(f"Failed to delete {filepath}: {e}")


def get_audio_stream_invidious(video_id: str):
    """Fetches direct audio stream URL and video title via Invidious public instances."""
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for instance in INVIDIOUS_INSTANCES:
        try:
            api_url = f"{instance}/api/v1/videos/{video_id}"
            logging.info(f"Querying Invidious instance: {instance}")
            
            res = requests.get(api_url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                title = data.get("title", "audio")
                adaptive_formats = data.get("adaptiveFormats", [])
                
                # Filter for audio-only streams (e.g. m4a, webm, mp3)
                audio_streams = [
                    f for f in adaptive_formats 
                    if f.get("type", "").startswith("audio/")
                ]
                
                if audio_streams:
                    # Pick highest bitrate audio stream
                    best_audio = max(
                        audio_streams, 
                        key=lambda x: int(x.get("bitrate", 0))
                    )
                    return best_audio["url"], title, best_audio.get("container", "m4a")
        except Exception as e:
            logging.warning(f"Instance {instance} failed: {e}")
            continue

    raise Exception("All audio extraction servers are currently busy. Please try again shortly.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or "User"

    await update.message.reply_text(
        "Send me any valid YouTube video link to extract the audio.\n"
        "⚠️ *Limit:* Video duration must be under 30 minutes.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    video_id = extract_youtube_id(text)
    if not video_id:
        await update.message.reply_text("❌ Please send a valid YouTube link.")
        return

    status_message = await update.message.reply_text("⚡ Extracting audio stream...")

    file_id = str(uuid.uuid4())

    try:
        # Step 1: Get raw stream URL and info using Invidious API
        stream_url, title, ext = await asyncio.to_thread(
            get_audio_stream_invidious, video_id
        )
        downloaded_file = TEMP_DIR / f"{file_id}.{ext}"

        # Step 2: Download raw audio file directly
        audio_res = requests.get(stream_url, stream=True, timeout=60)
        with open(downloaded_file, "wb") as f:
            for chunk in audio_res.iter_content(chunk_size=16384):
                f.write(chunk)

        if not downloaded_file.exists() or downloaded_file.stat().st_size == 0:
            await status_message.edit_text("❌ Audio file download failed.")
            return

        # Store pending download details
        pending_downloads[user_id] = {
            "title": title,
            "file_path": downloaded_file,
            "ext": ext,
        }

        # Step 3: Show Sponsor Buttons
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎉 1. Click to Open Sponsor Link", url=ADSTERRA_SMARTLINK)],
            [InlineKeyboardButton("✅ 2. I clicked the link (Get Audio)", callback_data="confirm_ad_click")]
        ])

        await status_message.edit_text(
            "✅ Audio Ready! Click the sponsor link below, then press 'I clicked the link' to receive your file.",
            reply_markup=keyboard,
        )

    except Exception as e:
        logging.error(f"Error processing URL for Telegram ID {user_id}: {e}")
        await status_message.edit_text(f"❌ Processing failed: {str(e)[:100]}")


async def ad_click_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    log_download_click(query.from_user)
    user_id = query.from_user.id

    if user_id not in pending_downloads:
        await query.edit_message_text("❌ Download session expired. Please send the link again.")
        return

    download_data = pending_downloads.pop(user_id)
    downloaded_file_path = download_data["file_path"]
    title = download_data["title"]
    ext = download_data.get("ext", "m4a")

    for i in range(5, 0, -1):
        await query.edit_message_text(f"⏳ Processing sponsor verification... Sending file in {i} seconds.")
        await asyncio.sleep(1)

    await query.edit_message_text("📤 Uploading audio to Telegram...")

    try:
        safe_title = sanitize_filename(title) or "audio"

        with open(downloaded_file_path, "rb") as audio_file:
            await context.bot.send_audio(
                audio=audio_file,
                title=title,
                filename=f"{safe_title}.{ext}",
                caption="Downloaded via Audio Bot",
            )

        await query.delete_message()

    except Exception as e:
        logging.error(f"Upload error for Telegram ID {user_id}: {e}")
        await query.edit_message_text("❌ Failed to send file.")
    finally:
        remove_file(downloaded_file_path)


if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(CallbackQueryHandler(ad_click_callback, pattern="^confirm_ad_click$"))

    print("Telegram Bot is running")
    app.run_polling()
