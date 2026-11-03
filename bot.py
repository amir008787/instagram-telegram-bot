
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
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# =============== SETTINGS ===============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

BASE = "https://api.hikerapi.com"
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("HIKER_API_KEY")

WAIT_DOWNLOAD, WAIT_PROFILE = range(2)

MAX_MEDIA = 30
MAX_BYTES = 45 * 1024 * 1024
TIMEOUT = 45

# =============== MENUS ===============

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 دانلود ریلز و پست", callback_data="download")],
        [InlineKeyboardButton("👤 اطلاعات پیج", callback_data="profile")]
    ])


def profile_menu():
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


# =============== API ===============

def api_get(path, params=None):
    if not API_KEY:
        raise RuntimeError("HIKER_API_KEY در تنظیمات سرور وجود ندارد.")

    response = requests.get(
        BASE + path,
        headers={
            "x-access-key": API_KEY,
            "Accept": "application/json"
        },
        params=params or {},
        timeout=TIMEOUT
    )

    if response.status_code == 401:
        raise RuntimeError("کلید HikerAPI نامعتبر است.")
    if response.status_code == 403:
        raise RuntimeError("دسترسی API رد شد.")
    if response.status_code == 404:
        raise RuntimeError("پیج یا محتوا پیدا نشد.")
    if response.status_code == 429:
        raise RuntimeError("محدودیت درخواست API؛ بعداً تلاش کن.")

    response.raise_for_status()
    return response.json()


def find_user_object(data):
    if not isinstance(data, dict):
        return None

    # HikerAPI v2 profile response
    response = data.get("response")
    if isinstance(response, dict):
        for key in ("user", "graphql"):
            if isinstance(response.get(key), dict):
                found = find_user_object(response[key])
                if found:
                    return found

        if response.get("pk") or response.get("id"):
            return response

    for key in ("user", "graphql"):
        value = data.get(key)
        if isinstance(value, dict):
            found = find_user_object(value)
            if found:
                return found

    if data.get("pk") or data.get("id"):
        return data

    return None


def get_profile(username):
    data = api_get(
        "/v2/user/by/username",
        {"username": username}
    )

    user = find_user_object(data)

    if not user:
        raise RuntimeError(
            "اطلاعات کاربر در پاسخ API پیدا نشد."
        )

    user_id = user.get("pk") or user.get("id")

    if not user_id:
        raise RuntimeError(
            "شناسه پیج در پاسخ API وجود ندارد."
        )

    return {
        "id": str(user_id),
        "username": user.get("username") or username,
        "name": user.get("full_name") or username,
        "followers": user.get("follower_count", 0),
        "following": user.get("following_count", 0),
        "posts": user.get("media_count", 0),
        "photo": user.get("profile_pic_url") or user.get("profile_pic_url_hd"),
        "private": user.get("is_private", False)
    }


def get_public_profile(username):
    profile = get_profile(username)

    if profile["private"]:
        raise ValueError(
            "این پیج خصوصی است. فقط پیج‌های عمومی پشتیبانی می‌شوند."
        )

    return profile


# =============== RESPONSE PARSING ===============

def get_list(data):
    if isinstance(data, list):
        # Some endpoints return [items, cursor]
        if len(data) == 2 and isinstance(data[0], list):
            return data[0]
        return data

    if not isinstance(data, dict):
        return []

    reel = data.get("reel")
    if isinstance(reel, dict):
        items = reel.get("items")
        if isinstance(items, list):
            return items

    response = data.get("response")
    if isinstance(response, dict):
        for key in ("items", "users", "tray", "medias"):
            if isinstance(response.get(key), list):
                return response[key]

    for key in ("items", "users", "stories", "medias", "tray", "highlights"):
        if isinstance(data.get(key), list):
            return data[key]

    return []


def get_cursor(data):
    if isinstance(data, list):
        if len(data) == 2 and isinstance(data[1], str):
            return data[1]
        return None

    if not isinstance(data, dict):
        return None

    for key in ("next_page_id", "max_id", "end_cursor"):
        if data.get(key):
            return data[key]

    response = data.get("response")
    if isinstance(response, dict):
        paging = response.get("paging_info") or {}
        return paging.get("max_id")

    return None


# =============== FOLLOWERS / FOLLOWING ===============

def get_user_list(username, kind):
    profile = get_public_profile(username)

    endpoint = (
        "/v1/user/followers/chunk"
        if kind == "followers"
        else "/v1/user/following/chunk"
    )

    result = []
    cursor = None

    # Maximum 3 pages to control API usage
    for _ in range(3):
        params = {"user_id": profile["id"]}
        if cursor:
            params["max_id"] = cursor

        data = api_get(endpoint, params)
        result.extend(get_list(data))

        new_cursor = get_cursor(data)
        if not new_cursor or new_cursor == cursor:
            break

        cursor = new_cursor

    return result


# =============== STORIES ===============

def get_stories(username):
    get_public_profile(username)

    data = api_get(
        "/v2/user/stories/by/username",
        {"username": username}
    )

    return get_list(data)


# =============== HIGHLIGHTS ===============

def get_highlights(username):
    profile = get_public_profile(username)

    data = api_get(
        "/v1/user/highlights",
        {
            "user_id": profile["id"],
            "amount": 20
        }
    )

    return get_list(data)


# =============== POSTS ===============

def get_posts(username):
    profile = get_public_profile(username)

    data = api_get(
        "/gql/user/medias",
        {
            "user_id": profile["id"],
            "flat": True
        }
    )

    return get_list(data)


# =============== MEDIA URL ===============

def extract_media(item):
    if not isinstance(item, dict):
        return None, None

    # Video: use original video URL
    videos = item.get("video_versions") or []

    if videos:
        videos = sorted(
            videos,
            key=lambda x: (
                (x.get("width") or 0) *
                (x.get("height") or 0)
            ),
            reverse=True
        )

        for video in videos:
            if video.get("url"):
                return "video", video["url"]

    if item.get("video_url"):
        return "video", item["video_url"]

    # Image: use highest-resolution candidate
    image_data = item.get("image_versions2") or {}
    candidates = image_data.get("candidates") or []

    if candidates:
        candidates = sorted(
            candidates,
            key=lambda x: (
                (x.get("width") or 0) *
                (x.get("height") or 0)
            ),
            reverse=True
        )

        for candidate in candidates:
            if candidate.get("url"):
                return "photo", candidate["url"]

    # Do not treat thumbnail as a complete video
    return None, None


# =============== DOWNLOAD MEDIA ===============

def download_media(url, folder, number):
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        stream=True,
        timeout=TIMEOUT
    )
    response.raise_for_status()

    content_type = response.headers.get(
        "content-type", ""
    ).lower()

    if "video" in content_type:
        extension = ".mp4"
    elif "png" in content_type:
        extension = ".png"
    elif "webp" in content_type:
        extension = ".webp"
    else:
        extension = ".jpg"

    path = Path(folder) / f"media_{number}{extension}"
    total = 0

    with path.open("wb") as file:
        for chunk in response.iter_content(256 * 1024):
            if not chunk:
                continue

            total += len(chunk)
            if total > MAX_BYTES:
                raise RuntimeError("حجم رسانه بیشتر از حد مجاز است.")

            file.write(chunk)

    return path


async def send_item(message, item, number):
    kind, url = extract_media(item)

    if not url:
        return False

    try:
        with tempfile.TemporaryDirectory() as folder:
            path = await asyncio.to_thread(
                download_media,
                url,
                folder,
                number
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
        log.exception("Media sending error")
        return False


# =============== INSTAGRAM DOWNLOAD ===============

def download_post(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "socket_timeout": TIMEOUT,
        "retries": 2
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


# =============== TELEGRAM HANDLERS ===============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🌟 به ربات اینستاگرام خوش آمدید",
        reply_markup=main_menu()
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "عملیات لغو شد.",
        reply_markup=main_menu()
    )
    return ConversationHandler.END


async def receive_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = (update.message.text or "").strip()

    if "instagram.com" in value.lower():
        parsed = urlparse(
            value if value.startswith(("http://", "https://"))
            else "https://" + value
        )
        parts = [p for p in parsed.path.split("/") if p]
        value = parts[0] if parts else ""

    username = value.lstrip("@").strip("/")

    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        await update.message.reply_text("آیدی پیج معتبر نیست.")
        return WAIT_PROFILE

    status = await update.message.reply_text("⏳ در حال دریافت اطلاعات پیج...")

    try:
        p = await asyncio.to_thread(get_public_profile, username)

        context.user_data["profile_username"] = p["username"]

        caption = (
            f"👤 {p['name']}\n"
            f"🆔 @{p['username']}\n\n"
            f"👥 فالوورها: {int(p['followers'] or 0):,}\n"
            f"➡️ فالووینگ: {int(p['following'] or 0):,}\n"
            f"🖼 پست‌ها: {int(p['posts'] or 0):,}"
        )

        await status.delete()

        if p["photo"]:
            await update.message.reply_photo(
                photo=p["photo"],
                caption=caption,
                reply_markup=profile_menu()
            )
        else:
            await update.message.reply_text(
                caption,
                reply_markup=profile_menu()
            )

    except Exception as e:
        log.exception("Profile error")
        await status.edit_text(f"❌ خطا: {str(e)[:250]}")

    return ConversationHandler.END


async def receive_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    parsed = urlparse(
        url if url.startswith(("http://", "https://"))
        else "https://" + url
    )

    if parsed.hostname not in (
        "instagram.com", "www.instagram.com", "m.instagram.com"
    ):
        await update.message.reply_text("لینک معتبر اینستاگرام ارسال کن.")
        return WAIT_DOWNLOAD

    status = await update.message.reply_text("⏳ در حال دانلود...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            filename = await asyncio.to_thread(
                download_post, url, folder
            )
            path = Path(filename)

            if not path.exists():
                files = list(Path(folder).glob("*"))
                if not files:
                    raise RuntimeError("فایل دانلود نشد.")
                path = files[0]

            if path.stat().st_size > MAX_BYTES:
                raise RuntimeError("حجم فایل بیشتر از حد مجاز است.")

            with path.open("rb") as media:
                if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    await update.message.reply_photo(photo=media)
                else:
                    await update.message.reply_video(
                        video=media,
                        supports_streaming=True
                    )

        await status.delete()

    except Exception:
        log.exception("Download error")
        await status.edit_text(
            "❌ دانلود ناموفق بود. ممکن است لینک خصوصی یا غیرقابل‌دسترسی باشد."
        )

    return ConversationHandler.END


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    choice = query.data

    if choice == "home":
        context.user_data.clear()
        await query.message.reply_text(
            "🌟 به ربات اینستاگرام خوش آمدید",
            reply_markup=main_menu()
        )
        return ConversationHandler.END

    if choice == "download":
        await query.message.reply_text(
            "📥 لینک ریلز و پست مورد نظر را ارسال کنید"
        )
        return WAIT_DOWNLOAD

    if choice == "profile":
        await query.message.reply_text(
            "👤 لطفاً آیدی یا لینک پیج عمومی اینستاگرام را ارسال کنید."
        )
        return WAIT_PROFILE

    username = context.user_data.get("profile_username")

    if not username:
        await query.message.reply_text("ابتدا یک پیج را انتخاب کن.")
        return ConversationHandler.END

    status = await query.message.reply_text("⏳ در حال دریافت...")

    try:
        if choice in ("followers", "following"):
            items = await asyncio.to_thread(
                get_user_list, username, choice
            )

            title = "فالوورها" if choice == "followers" else "فالووینگ"
            lines = []

            for user in items[:100]:
                if isinstance(user, dict):
                    name = user.get("username", "نامشخص")
                    full = user.get("full_name", "")
                    lines.append(f"👤 @{name} {full}")

            text = "\n".join(lines) or "موردی دریافت نشد."

            await status.edit_text(
                f"📋 {title} پیج @{username}\n\n{text}"
            )
            return ConversationHandler.END

        if choice == "stories":
            items = await asyncio.to_thread(get_stories, username)
            title = "استوری‌ها"

        elif choice == "highlights":
            items = await asyncio.to_thread(get_highlights, username)
            title = "هایلایت‌ها"

        elif choice == "posts":
            items = await asyncio.to_thread(get_posts, username)
            title = "پست‌ها و ریلزها"

        else:
            await status.edit_text("گزینه نامعتبر است.")
            return ConversationHandler.END

        if not items:
            await status.edit_text(f"هیچ {title} فعالی پیدا نشد.")
            return ConversationHandler.END

        await status.edit_text(
            f"📥 تعداد {title} پیدا شده: {len(items)}\n"
            "⏳ ارسال رسانه‌ها شروع شد..."
        )

        success = 0
        failed = 0

        for index, item in enumerate(items[:MAX_MEDIA], 1):
            if await send_item(query.message, item, index):
                success += 1
            else:
                failed += 1

        await query.message.reply_text(
            f"✅ ارسال تمام شد.\n"
            f"موفق: {success}\n"
            f"ناموفق: {failed}"
        )

    except Exception as e:
        log.exception("Menu/API error")
        await status.edit_text(f"❌ خطا: {str(e)[:250]}")

    return ConversationHandler.END


# =============== MAIN ===============

def main():
    if not TOKEN:
        raise RuntimeError("توکن تلگرام تنظیم نشده است.")

    if not API_KEY:
        raise RuntimeError("HIKER_API_KEY تنظیم نشده است.")

    app = Application.builder().token(TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                menu_callback,
                pattern="^(download|profile|home|followers|following|stories|posts|highlights)$"
            )
        ],
        states={
            WAIT_DOWNLOAD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_download)
            ],
            WAIT_PROFILE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_profile)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conversation)

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()


