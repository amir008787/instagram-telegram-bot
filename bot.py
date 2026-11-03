
import os
import re
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

# ---------------- CONFIG ----------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

HIKER_URL = "https://api.hikerapi.com"
IGRAM_URL = "https://api.igramapi.com"

MAX_SIZE = 45 * 1024 * 1024
MAX_ITEMS = 20
WAITING_DOWNLOAD, WAITING_PROFILE = range(2)

MAIN_TEXT = "🌟 به ربات اینستاگرام خوش آمدید"
DOWNLOAD_PROMPT = "📥 لینک ریلز و پست مورد نظر را ارسال کنید"
PROFILE_PROMPT = "👤 لطفاً آیدی یا لینک پیج عمومی اینستاگرام را ارسال کنید."


# ---------------- MENUS ----------------

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


# ---------------- USERNAME ----------------

def normalize_username(value):
    value = value.strip()

    if "instagram.com" in value.lower():
        parsed = urlparse(
            value if value.startswith(("http://", "https://"))
            else "https://" + value
        )
        parts = [p for p in parsed.path.split("/") if p]

        if not parts:
            raise ValueError("لینک پیج معتبر نیست.")

        value = parts[0]

    value = value.strip().lstrip("@").strip("/")

    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", value):
        raise ValueError("آیدی پیج معتبر نیست.")

    return value


# ---------------- API REQUESTS ----------------

def hiker_get(endpoint, params=None):
    key = os.getenv("HIKER_API_KEY")

    if not key:
        raise RuntimeError("کلید HIKER_API_KEY در سرور تنظیم نشده است.")

    response = requests.get(
        HIKER_URL + endpoint,
        headers={
            "x-access-key": key,
            "accept": "application/json"
        },
        params=params or {},
        timeout=45
    )

    if response.status_code == 401:
        raise RuntimeError("کلید HikerAPI نامعتبر است.")
    if response.status_code == 402:
        raise RuntimeError("اعتبار HikerAPI تمام شده است.")
    if response.status_code == 403:
        raise RuntimeError("دسترسی به این اطلاعات مجاز نیست.")
    if response.status_code == 404:
        raise ValueError("پیج یا محتوای موردنظر پیدا نشد.")
    if response.status_code == 429:
        raise RuntimeError("محدودیت درخواست API؛ کمی بعد دوباره امتحان کنید.")

    response.raise_for_status()
    return response.json()


def get_items(data):
    """Extract lists from common HikerAPI response envelopes."""

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in (
        "items", "users", "stories", "medias",
        "tray", "highlights", "results"
    ):
        value = data.get(key)
        if isinstance(value, list):
            return value

    for key in ("response", "data", "reel"):
        value = data.get(key)
        if isinstance(value, (dict, list)):
            result = get_items(value)
            if result:
                return result

    return []


# ---------------- PROFILE ----------------

def get_profile(username):
    igram_key = os.getenv("IGRAM_API_KEY")

    # Try IGramAPI first
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
            logger.exception("IGramAPI profile lookup failed")

    # Fallback to HikerAPI
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


def get_public_profile(username):
    profile = get_profile(username)

    if profile.get("private"):
        raise ValueError("این پیج خصوصی است. فقط پیج‌های عمومی پشتیبانی می‌شوند.")

    if not profile.get("user_id"):
        raise RuntimeError("شناسه پیج از API دریافت نشد.")

    return profile


# ---------------- FOLLOWERS / FOLLOWING ----------------

def get_user_list(username, kind):
    profile = get_public_profile(username)

    endpoint = (
        "/v1/user/followers/chunk"
        if kind == "followers"
        else "/v1/user/following/chunk"
    )

    data = hiker_get(
        endpoint,
        {"user_id": profile["user_id"]}
    )

    return get_items(data)


# ---------------- STORIES ----------------

def get_stories(username):
    profile = get_public_profile(username)

    data = hiker_get(
        "/v1/user/stories/by/username",
        {"username": username}
    )

    return get_items(data)


# ---------------- HIGHLIGHTS ----------------

def get_highlights(username):
    profile = get_public_profile(username)

    data = hiker_get(
        "/v1/user/highlights",
        {
            "user_id": profile["user_id"],
            "amount": 10
        }
    )

    return get_items(data)


# ---------------- POSTS ----------------

def get_posts(username):
    profile = get_public_profile(username)

    data = hiker_get(
        "/v1/user/medias/chunk",
        {"user_id": profile["user_id"]}
    )

    return get_items(data)


# ---------------- MEDIA EXTRACTION ----------------

def extract_media(item):
    if not isinstance(item, dict):
        return None, None

    # Video
    videos = item.get("video_versions") or []

    if videos:
        video = videos[0].get("url")
        if video:
            return "video", video

    video_url = item.get("video_url")

    if video_url:
        return "video", video_url

    # Image
    image_data = item.get("image_versions2") or {}
    candidates = image_data.get("candidates") or []

    if candidates:
        image_url = candidates[0].get("url")
        if image_url:
            return "photo", image_url

    for key in ("image_url", "display_url", "thumbnail_url"):
        value = item.get(key)

        if isinstance(value, str) and value.startswith("http"):
            return "photo", value

    return None, None


# ---------------- MEDIA DOWNLOAD ----------------

def download_url(url, folder, filename):
    response = requests.get(
        url,
        stream=True,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()

    if "video" in content_type:
        extension = ".mp4"
    elif "webp" in content_type:
        extension = ".webp"
    elif "png" in content_type:
        extension = ".png"
    else:
        extension = ".jpg"

    path = Path(folder) / (filename + extension)
    total = 0

    with path.open("wb") as file:
        for chunk in response.iter_content(256 * 1024):
            if not chunk:
                continue

            total += len(chunk)

            if total > MAX_SIZE:
                raise RuntimeError("حجم رسانه بیشتر از حد مجاز است.")

            file.write(chunk)

    return path


async def send_media_item(message, item, index):
    kind, url = extract_media(item)

    if not url:
        return False

    try:
        with tempfile.TemporaryDirectory() as folder:
            path = await asyncio.to_thread(
                download_url,
                url,
                folder,
                f"media_{index}"
            )

            with path.open("rb") as media:
                if kind == "video":
                    await message.reply_video(
                        video=media,
                        supports_streaming=True
                    )
                else:
                    await message.reply_photo(photo=media)

        return True

    except Exception:
        logger.exception("Media sending failed")
        return False


# ---------------- POST / REEL DOWNLOAD ----------------

def download_instagram_post(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_SIZE,
        "socket_timeout": 40,
        "retries": 2
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


# ---------------- TELEGRAM START ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    await update.message.reply_text(
        MAIN_TEXT,
        reply_markup=main_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "از منوی اصلی یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=main_keyboard()
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "عملیات لغو شد.",
        reply_markup=main_keyboard()
    )

    return ConversationHandler.END


# ---------------- PROFILE MENU CALLBACK ----------------

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
            "ابتدا اطلاعات یک پیج را دریافت کنید.",
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END

    status = await query.message.reply_text("⏳ در حال دریافت اطلاعات...")

    try:
        # Followers / following
        if choice in ("followers", "following"):
            items = await asyncio.to_thread(
                get_user_list,
                username,
                choice
            )

            title = "فالوورها" if choice == "followers" else "فالووینگ"

            lines = []

            for item in items[:30]:
                if not isinstance(item, dict):
                    continue

                name = item.get("username") or "نامشخص"
                full_name = item.get("full_name") or ""

                lines.append(f"👤 @{name} {full_name}")

            result = "\n".join(lines) or "فهرستی دریافت نشد."

            await status.edit_text(
                f"📋 {title} پیج @{username}\n\n"
                f"{result}\n\n"
                "نمایش حداکثر ۳۰ مورد از صفحه اول."
            )

            return ConversationHandler.END

        # Stories
        if choice == "stories":
            items = await asyncio.to_thread(get_stories, username)
            title = "استوری‌ها"

        # Highlights
        elif choice == "highlights":
            items = await asyncio.to_thread(get_highlights, username)
            title = "هایلایت‌ها"

        # Posts
        elif choice == "posts":
            items = await asyncio.to_thread(get_posts, username)
            title = "پست‌ها و ریلزها"

        else:
            return ConversationHandler.END

        if not items:
            await status.edit_text(
                f"محتوایی در بخش {title} پیدا نشد."
            )
            return ConversationHandler.END

        await status.edit_text(
            f"📥 {title} پیج @{username}\n"
            f"تعداد موارد دریافت‌شده: {len(items)}\n"
            "⏳ در حال ارسال..."
        )

        sent = 0
        failed = 0

        for index, item in enumerate(items[:MAX_ITEMS], start=1):
            success = await send_media_item(
                query.message,
                item,
                index
            )

            if success:
                sent += 1
            else:
                failed += 1

        await query.message.reply_text(
            f"✅ ارسال {title} تمام شد.\n"
            f"موفق: {sent}\n"
            f"ناموفق: {failed}"
        )

    except Exception as exc:
        logger.exception("Menu API error")

        await status.edit_text(
            f"❌ خطا در دریافت اطلاعات:\n{str(exc)[:250]}"
        )

    return ConversationHandler.END


# ---------------- RECEIVE PROFILE ----------------

async def receive_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        username = normalize_username(update.message.text or "")

        info = await asyncio.to_thread(
            get_public_profile,
            username
        )

        context.user_data["profile_username"] = info["username"]

        caption = (
            f"👤 {info['full_name']}\n"
            f"🆔 @{info['username']}\n\n"
            f"👥 دنبال‌کنندگان: {int(info['followers'] or 0):,}\n"
            f"➡️ دنبال‌شوندگان: {int(info['following'] or 0):,}\n"
            f"🖼 تعداد پست‌ها: {int(info['posts'] or 0):,}"
        )

        if info.get("photo"):
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

    except Exception as exc:
        logger.exception("Profile lookup failed")

        await update.message.reply_text(
            f"❌ دریافت اطلاعات پیج ناموفق بود:\n{str(exc)[:200]}",
            reply_markup=main_keyboard()
        )

    return ConversationHandler.END


# ---------------- RECEIVE DOWNLOAD LINK ----------------

async def receive_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()

    parsed = urlparse(
        url if url.startswith(("http://", "https://"))
        else "https://" + url
    )

    if parsed.hostname not in (
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com"
    ):
        await update.message.reply_text(
            "لطفاً لینک معتبر اینستاگرام ارسال کنید."
        )
        return WAITING_DOWNLOAD

    status = await update.message.reply_text("⏳ در حال دریافت محتوا...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            filename = await asyncio.to_thread(
                download_instagram_post,
                url,
                folder
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
        logger.exception("Instagram download failed")

        await status.edit_text(
            "❌ دانلود ناموفق بود. ممکن است لینک خصوصی، نامعتبر "
            "یا از طرف اینستاگرام غیرقابل‌دسترسی باشد."
        )

    return ConversationHandler.END


# ---------------- MAIN ----------------

def main():
    token = (
        os.getenv("BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
    )

    if not token:
        raise RuntimeError("توکن ربات تنظیم نشده است.")

    app = Application.builder().token(token).build()

    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                menu_callback,
                pattern=(
                    "^(download|profile|home|followers|following|"
                    "stories|posts|highlights)$"
                )
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
        fallbacks=[
            CommandHandler("cancel", cancel)
        ],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conversation)

    logger.info("Instagram Telegram bot is running")
    app.run_polling()


if __name__ == "__main__":
    main()

