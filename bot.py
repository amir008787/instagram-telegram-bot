import os
import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
import instaloader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_SIZE = 45 * 1024 * 1024
WAITING_DOWNLOAD, WAITING_PROFILE = range(2)

MAIN_TEXT = "🌟 به ربات اینستاگرام خوش آمدید"
DOWNLOAD_PROMPT = "📥 لینک ریلز و پست مورد نظر را ارسال کنید"
PROFILE_PROMPT = "👤 لطفاً آیدی یا لینک پیج عمومی اینستاگرام را ارسال کنید."


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 دانلود ریلز و پست", callback_data="download")],
        [InlineKeyboardButton("👤 اطلاعات پیج", callback_data="profile")],
    ])


def profile_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 لیست فالوورها", callback_data="followers"),
            InlineKeyboardButton("➡️ لیست فالووینگ", callback_data="following"),
        ],
        [
            InlineKeyboardButton("⭕ استوری‌ها", callback_data="stories"),
            InlineKeyboardButton("🖼 نمایش پست‌ها", callback_data="posts"),
        ],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
    ])


def normalize_username(value):
    value = value.strip()
    if "instagram.com" in value:
        parsed = urlparse(value if value.startswith(("http://", "https://")) else "https://" + value)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("شناسه پیج معتبر نیست.")
        value = parts[0]
    value = value.lstrip("@").strip("/")
    if not value or any(ch in value for ch in " ?#"):
        raise ValueError("شناسه پیج معتبر نیست.")
    return value


def get_profile(username):
    loader = instaloader.Instaloader(
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, download_geotags=False,
        download_comments=False, save_metadata=False, quiet=True
    )
    profile = instaloader.Profile.from_username(loader.context, username)
    return {
        "username": profile.username,
        "full_name": profile.full_name or profile.username,
        "followers": profile.followers,
        "following": profile.followees,
        "posts": profile.mediacount,
        "photo": profile.profile_pic_url,
        "private": profile.is_private,
    }


def download_media(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best[filesize<45M]/best",
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "max_filesize": MAX_SIZE, "socket_timeout": 30, "retries": 2,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(MAIN_TEXT, reply_markup=main_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "download":
        await query.message.reply_text(DOWNLOAD_PROMPT)
        return WAITING_DOWNLOAD

    if choice == "profile":
        await query.message.reply_text(PROFILE_PROMPT)
        return WAITING_PROFILE

    if choice == "home":
        context.user_data.clear()
        await query.message.reply_text(MAIN_TEXT, reply_markup=main_keyboard())
        return ConversationHandler.END

    if choice in ("followers", "following"):
        await query.message.reply_text(
            "⚠️ اینستاگرام دسترسی پایدار و عمومی برای دریافت فهرست کامل "
            "فالوورها و فالووینگِ پیج‌های دلخواه ارائه نمی‌کند؛ بنابراین "
            "نمایش فهرست کامل و قابل‌اعتماد ممکن نیست."
        )
    elif choice == "stories":
        await query.message.reply_text(
            "⚠️ قابلیت دریافت استوری در این نسخه فعال نیست. "
            "دسترسی به استوری‌ها به عمومی و قابل‌دسترسی بودن آن‌ها بستگی دارد."
        )
    elif choice == "posts":
        await query.message.reply_text(
            "🖼 برای دریافت پست یا ریلز، لینک عمومی آن را کپی کنید و از گزینه "
            "«دانلود ریلز و پست» بفرستید. نمایش خودکار گالری پست‌ها هنوز فعال نیست.",
            reply_markup=main_keyboard()
        )
    return ConversationHandler.END


async def receive_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        username = normalize_username(update.message.text or "")
        info = await asyncio.to_thread(get_profile, username)

        if info["private"]:
            await update.message.reply_text(
                "🔒 این پیج خصوصی است. فقط پیج‌های عمومی پشتیبانی می‌شوند.",
                reply_markup=main_keyboard()
            )
            return ConversationHandler.END

        caption = (
            f"👤 {info['full_name']}\n"
            f"🆔 @{info['username']}\n\n"
            f"👥 دنبال‌کنندگان: {info['followers']:,}\n"
            f"➡️ دنبال‌شوندگان: {info['following']:,}\n"
            f"🖼 تعداد پست‌ها: {info['posts']:,}"
        )
        await update.message.reply_photo(
            photo=info["photo"], caption=caption, reply_markup=profile_keyboard()
        )
    except Exception:
        logger.exception("Profile lookup failed")
        await update.message.reply_text(
            "❌ دریافت اطلاعات پیج ناموفق بود. آیدی را بررسی کنید؛ "
            "ممکن است اینستاگرام موقتاً دسترسی را محدود کرده باشد.",
            reply_markup=main_keyboard()
        )
    return ConversationHandler.END


async def receive_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    parsed = urlparse(url if url.startswith(("http://", "https://")) else "https://" + url)
    if parsed.netloc.lower() not in ("instagram.com", "www.instagram.com", "m.instagram.com"):
        await update.message.reply_text("لطفاً لینک معتبر اینستاگرام ارسال کنید.")
        return WAITING_DOWNLOAD

    status = await update.message.reply_text("⏳ در حال دریافت محتوا...")
    try:
        with tempfile.TemporaryDirectory() as folder:
            filename = await asyncio.to_thread(download_media, url, folder)
            path = Path(filename)
            if not path.exists():
                files = list(Path(folder).glob("*"))
                if not files:
                    raise RuntimeError("فایل دانلود نشد.")
                path = files[0]
            if path.stat().st_size > MAX_SIZE:
                raise RuntimeError("حجم فایل بیشتر از حد مجاز است.")

            with path.open("rb") as media:
                if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    await update.message.reply_photo(photo=media, caption="دریافت از اینستاگرام")
                else:
                    await update.message.reply_video(
                        video=media, caption="دریافت از اینستاگرام",
                        supports_streaming=True
                    )
        await status.delete()
    except Exception:
        logger.exception("Download failed")
        await status.edit_text(
            "❌ دانلود ناموفق بود. ممکن است لینک خصوصی، نامعتبر یا "
            "از طرف اینستاگرام غیرقابل‌دسترسی باشد."
        )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عملیات لغو شد.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("از منوی اصلی یکی از گزینه‌ها را انتخاب کنید.",
                                    reply_markup=main_keyboard())


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

    app = Application.builder().token(token).build()
    conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            menu_callback,
            pattern="^(download|profile|home|followers|following|stories|posts)$"
        )],
        states={
            WAITING_DOWNLOAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_download)],
            WAITING_PROFILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_profile)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conversation)
    logger.info("Bot is running")
    app.run_polling()


if __name__ == "__main__":
    main()
