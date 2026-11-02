
import asyncio
import logging
import os
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
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

loader = instaloader.Instaloader(quiet=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! آیدی پیج عمومی اینستاگرام را بفرست."
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip().lstrip("@")

    if not username or " " in username:
        await update.message.reply_text("آیدی معتبر وارد کن.")
        return

    msg = await update.message.reply_text("در حال بررسی...")

    try:
        profile = await asyncio.to_thread(
            instaloader.Profile.from_username,
            loader.context,
            username
        )

        result = (
            f"پیج: @{profile.username}\n"
            f"فالوورها: {profile.followers}\n"
            f"فالووینگ‌ها: {profile.followees}"
        )

        await msg.edit_text(result)

    except Exception:
        logging.exception("Instagram profile retrieval failed")
        await msg.edit_text(
            "دریافت اطلاعات ناموفق بود. لطفاً بعداً دوباره تلاش کن."
        )


def main():
    token = os.environ["BOT_TOKEN"]

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle
    ))

    logging.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
