
import os
import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_SIZE = 45 * 1024 * 1024
WAITING_DOWNLOAD, WAITING_PROFILE = range(2)

HIKER_URL = "https://api.hikerapi.com"
IGRAM_URL = "https://api.igramapi.com"

MAIN_TEXT = "🌟 به ربات اینستاگرام خوش آمدید"
DOWNLOAD_PROMPT = "📥 لینک ریلز و پست مورد نظر را ارسال کنید"
PROFILE_PROMPT = "👤 لطفاً آیدی یا لینک پیج عمومی اینستاگرام را ارسال کنید."


# -------------------- KEYBOARDS --------------------

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 دانلود ریلز و پست", callback_data="download")],
        [InlineKeyboardButton("👤 اطلاعات پیج", callback_data="profile")]
    ])


def profile_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 لیست فالوورها", callback_data="followers"),
            InlineKeyboardButton("➡️ لیست فالووینگ", callback_data="following")
        ],
        [
            InlineKeyboardButton("⭕ استوری‌ها", callback_data="stories"),
            InlineKeyboardButton("🖼 نمایش پست‌ها", callback_data="posts")
        ],
        [InlineKeyboardButton("⭐ هایلایت‌ها", callback_data="highlights")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")]
    ])


# -------------------- HELPERS --------------------

def normalize_username(value):
    value = value.strip()

    if "instagram.com" in value:
        parsed = urlparse(
            value if value.startswith(("http://", "https://"))
            else "https://" + value
        )
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise ValueError("شناسه پیج معتبر نیست.")
        value = parts[0]

    value = value.lstrip("@").strip("/")

    if not value or any(ch in value for ch in " ?#"):
        raise ValueError("شناسه پیج معتبر نیست.")

    return value


def hiker_get(endpoint, params=None):
    key = os.environ.get("HIKER_API_KEY")
    if not key:
        raise RuntimeError("HIKER_API_KEY تنظیم نشده است.")

    response = requests.get(
        HIKER_URL + endpoint,
        headers={
            "x-access-key": key,
            "accept": "application/json"
        },
        params=params or {},
        timeout=40
    )

    if response.status_code == 401:
        raise RuntimeError("کلید HikerAPI نامعتبر است.")
    if response.status_code == 402:
        raise RuntimeError("اعتبار HikerAPI کافی نیست.")
    if response.status_code == 403:
        raise RuntimeError("دسترسی به این اطلاعات مجاز نیست.")
    if response.status_code == 404:
        raise ValueError("اطلاعات موردنظر پیدا نشد.")

    response.raise_for_status()
    return response.json()


def get_profile(username):
    # First try IGramAPI
    igram_key = os.environ.get("IGRAM_API_KEY")

    if igram_key:
        try:
            response = requests.get(
                f"{IGRAM_URL}/v1/profiles/{username}",
                headers={"Authorization": f"Bearer {igram_key}"},
                timeout=30
            )
            response.raise_for_status()

            data = response.json()
            p = data.get("profile", data)

            return {
                "username": p.get("username") or username,
                "full_name": p.get("full_name") or username,
                "followers": p.get("followers", 0),
                "following": p.get("following", 0),
                "posts": p.get("posts_count", 0),
                "photo": p.get("profile_picture_url"),
                "private": p.get("is_private", False),
                "user_id": p.get("id") or p.get("pk")
            }
        except Exception:
            logger.warning("IGramAPI failed; trying HikerAPI")

    # HikerAPI fallback
    p = hiker_get(
        "/v1/user/by/username",
        {"username": username}
    )

    return {
        "username": p.get("username") or username,
        "full_name": p.get("full_name") or username,
        "followers": p.get("follower_count", 0),
        "following": p.get("following_count", 0),
        "posts": p.get("media_count", 0),
        "photo": p.get("profile_pic_url"),
        "private": p.get("is_private", False),
        "user_id": p.get("pk") or p.get("id")
    }


def get_items(data):
    """Extract common list formats returned by HikerAPI."""
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in ("users", "items", "stories", "tray", "highlights", "medias"):
        value = data.get(key)
        if isinstance(value, list):
            return value

    response = data.get("response")
    if isinstance(response, dict):
        return get_items(response)

    reel = data.get("reel")
    if isinstance(reel, dict):
        return reel.get("items", [])

    return []


def get_user_list(username, kind):
    profile = get_profile(username)

    if profile["private"]:
        raise ValueError("این پیج خصوصی است.")

    user_id = profile.get("user_id")
    if not user_id:
        raise RuntimeError("شناسه پیج از API دریافت نشد.")

    endpoint = (
        "/v1/user/followers/chunk"
        if kind == "followers"
        else "/v1/user/following/chunk"
    )

    data = hiker_get(endpoint, {"user_id": user_id})
    return get_items(data)


def get_stories(username):
    data = hiker_get(
        "/v2/user/stories/by/username",
        {"username": username}
    )
    return get_items(data)


def get_highlights(username):
    data = hiker_get(
        "/v2/user/highlights/by/username",
        {"username": username}
    )
    return get_items(data)


def get_posts(username):
    profile = get_profile(username)

    if profile["private"]:
        raise ValueError("این پیج خصوصی است.")

    user_id = profile.get("user_id")
    if not user_id:
        raise RuntimeError("شناسه پیج دریافت نشد.")

    data = hiker_get(
        "/v1/user/medias/chunk",
        {"user_id": user_id}
    )
    return get_items(data)


def extract_media_url(item):
    if not isinstance(item, dict):
        return None

    for key in (
        "video_url", "image_url", "display_url",
        "thumbnail_url", "url"
    ):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value

    candidates = item.get("image_versions2", {}).get("candidates", [])
    if candidates:
        return candidates[0].get("url")

    versions = item.get("video_versions", [])
    if versions:
        return versions[0].get("url")

    return None


def download_media(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_SIZE,
        "socket_timeout": 30,
        "retries": 2
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


# -------------------- TELEGRAM COMMANDS --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        MAIN_TEXT,
        reply_markup=main_keyboard()
    )


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
        await query.message.reply_text(
            MAIN_TEXT,
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END

    username = context.user_data.get("profile_username")

    if not username:
        await query.message.reply_text(
            "ابتدا از منوی اصلی اطلاعات یک پیج را دریافت کنید.",
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END

    status = await query.message.reply_text("⏳ در حال دریافت اطلاعات...")

    try:
        if choice in ("followers", "following"):
            items = await asyncio.to_thread(
                get_user_list, username, choice
            )

            title = "فالوورها" if choice == "followers" else "فالووینگ"

            if not items:
                result = "فهرستی دریافت نشد."
            else:
                lines = []
                for item in items[:30]:
                    if isinstance(item, dict):
                        name = item.get("username", "نامشخص")
                        full = item.get("full_name", "")
                        lines.append(f"👤 @{name} {full}")

                result = "\n".join(lines) or "اطلاعاتی دریافت نشد."

            await status.edit_text(
                f"📋 {title} پیج @{username}\n\n{result}\n\n"
                "نمایش حداکثر ۳۰ مورد از صفحه اول."
            )

        elif choice in ("stories", "highlights", "posts"):
            if choice == "stories":
                items = await asyncio.to_thread(get_stories, username)
                title = "استوری‌ها"
            elif choice == "highlights":
                items = await asyncio.to_thread(get_highlights, username)
                title = "هایلایت‌ها"
            else:
                items = await asyncio.to_thread(get_posts, username)
                title = "پست‌ها و ریلزها"

            if not items:
                await status.edit_text(
                    f"محتوایی برای نمایش در بخش {title} پیدا نشد."
                )
                return ConversationHandler.END

            await status.edit_text(
                f"📥 {title} پیج @{username}\n"
                f"تعداد موارد دریافت‌شده: {len(items)}"
            )

            for item in items[:5]:
                media_url = extract_media_url(item)

                if not media_url:
                    continue

                try:
                    await query.message.reply_photo(
                        photo=media_url
                    )
                except Exception:
                    try:
                        await query.message.reply_video(
                            video=media_url
                        )
                    except Exception:
                        logger.warning("Could not send media")

        return ConversationHandler.END

    except Exception as exc:
        logger.exception("API request failed")
        await status.edit_text(f"❌ خطا: {str(exc)[:250]}")
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

        context.user_data["profile_username"] = info["username"]

        caption = (
            f"👤 {info['full_name']}\n"
            f"🆔 @{info['username']}\n\n"
            f"👥 دنبال‌کنندگان: {int(info['followers'] or 0):,}\n"
            f"➡️ دنبال‌شوندگان: {int(info['following'] or 0):,}\n"
            f"🖼 تعداد پست‌ها: {int(info['posts'] or 0):,}"
        )

        if info["photo"]:
            await update.message.reply_photo(
                photo=info["photo"],
                caption=caption,
                reply_markup=profile_keyboard()
            )
        else:
            await update.message.reply_text(
                caption,
                reply_markup=profile_keyboard()
            )

    except Exception:
        logger.exception("Profile lookup failed")
        await update.message.reply_text(
            "❌ دریافت اطلاعات پیج ناموفق بود. آیدی را بررسی کنید.",
            reply_markup=main_keyboard()
        )

    return ConversationHandler.END


async def receive_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()

    parsed = urlparse(
        url if url.startswith(("http://", "https://"))
        else "https://" + url
    )

    if parsed.netloc.lower() not in (
        "instagram.com", "www.instagram.com", "m.instagram.com"
    ):
        await update.message.reply_text(
            "لطفاً لینک معتبر اینستاگرام ارسال کنید."
        )
        return WAITING_DOWNLOAD

    status = await update.message.reply_text("⏳ در حال دریافت محتوا...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            filename = await asyncio.to_thread(
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

            with path.open("rb") as media:
                if path.suffix.lower() in (
                    ".jpg", ".jpeg", ".png", ".webp"
                ):
                    await update.message.reply_photo(
                        photo=media,
                        caption="دریافت از اینستاگرام"
                    )
                else:
                    await update.message.reply_video(
                        video=media,
                        caption="دریافت از اینستاگرام",
                        supports_streaming=True
                    )

        await status.delete()

    except Exception:
        logger.exception("Download failed")
        await status.edit_text(
            "❌ دانلود ناموفق بود. ممکن است لینک خصوصی یا غیرقابل‌دسترسی باشد."
        )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "عملیات لغو شد.",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "از منوی اصلی یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=main_keyboard()
    )


# -------------------- MAIN --------------------

def main():
    token = (
        os.environ.get("BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
    )

    if not token:
        raise RuntimeError("توکن ربات تنظیم نشده است.")

    app = Application.builder().token(token).build()

    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                menu_callback,
                pattern="^(download|profile|home|followers|following|stories|posts|highlights)$"
            )
        ],
        states={
            WAITING_DOWNLOAD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_download
                )
            ],
            WAITING_PROFILE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_profile
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conversation)

    logger.info("Bot is running")
    app.run_polling()


if __name__ == "__main__":
    main()
