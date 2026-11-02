
import os
import asyncio
import logging
import instaloader

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

loader = instaloader.Instaloader(quiet=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام!\n"
        "آیدی یک پیج عمومی اینستاگرام را بفرست.\n"
        "مثال: instagram"
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip().lstrip("@")

    if not username or " " in username:
        await update.message.reply_text("آیدی معتبر وارد کن.")
        return

    msg = await update.message.reply_text("در حال بررسی استوری‌ها...")

    try:
        profile = await asyncio.to_thread(
            instaloader.Profile.from_username,
            loader.context,
            username
        )

        if profile.is_private:
            await msg.edit_text("این پیج خصوصی است.")
            return

        stories = await asyncio.to_thread(
            lambda: list(loader.get_stories(userids=[profile.userid]))
        )

        items = []
        for story in stories:
            items.extend(list(story.get_items()))

        if not items:
            await msg.edit_text("استوری فعالی پیدا نشد یا دسترسی ممکن نیست.")
            return

        await msg.edit_text(
            f"تعداد استوری‌های پیدا شده: {len(items)}\n"
            "در حال ارسال..."
        )

        for item in items:
            try:
                if item.is_video:
                    await update.message.reply_video(
                        video=item.video_url,
                        caption=f"استوری @{username}"
                    )
                else:
                    await update.message.reply_photo(
                        photo=item.url,
                        caption=f"استوری @{username}"
                    )
            except Exception:
                logging.exception("Story sending failed")

        await update.message.reply_text("ارسال استوری‌ها تمام شد.")

    except Exception:
        logging.exception("Story retrieval failed")
        await msg.edit_text(
            "دریافت استوری ناموفق بود. ممکن است اینستاگرام دسترسی را محدود کرده باشد."
        )


def main():
    token = os.environ["BOT_TOKEN"]

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle)
    )

    logging.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
