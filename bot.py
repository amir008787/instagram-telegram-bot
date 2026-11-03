
import os
import asyncio
import logging
import tempfile
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_SIZE = 45 * 1024 * 1024


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! به ربات دانلودر اینستاگرام خوش آمدی.\n\n"
        "لینک پست یا Reels عمومی را بفرست تا بررسی کنم.\n"
        "فعلاً لینک‌های عمومی و قابل‌دسترسی پشتیبانی می‌شوند."
    )


def download_media(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best[filesize<45M]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_SIZE,
        "socket_timeout": 30,
        "retries": 2,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    return filename, info


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if "instagram.com/" not in url:
        await update.message.reply_text(
            "لطفاً لینک معتبر اینستاگرام بفرست."
        )
        return

    status = await update.message.reply_text("در حال دریافت محتوا...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            filename, info = await asyncio.to_thread(
                download_media, url, folder
            )

            path = Path(filename)

            if not path.exists():
                files = list(Path(folder).glob("*"))
                if not files:
                    raise RuntimeError("فایل دانلود نشد.")
                path = files[0]

            if path.stat().st_size > MAX_SIZE:
                raise RuntimeError("حجم فایل بیشتر از حد مجاز است.")

            caption = "دریافت از اینستاگرام"

            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                with path.open("rb") as media:
                    await update.message.reply_photo(
                        photo=media, caption=caption
                    )
            else:
                with path.open("rb") as media:
                    await update.message.reply_video(
                        video=media, caption=caption,
                        supports_streaming=True
                    )

            await status.delete()

    except Exception:
        logger.exception("Download failed")
        await status.edit_text(
            "دانلود ناموفق بود. ممکن است لینک خصوصی، نامعتبر "
            "یا غیرقابل‌دسترسی باشد."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "لینک پست یا Reels عمومی اینستاگرام را ارسال کن."
    )


def main():
    token = os.environ["BOT_TOKEN"]

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle)
    )

    logger.info("Bot is running")
    app.run_polling()


if __name__ == "__main__":
    main()
