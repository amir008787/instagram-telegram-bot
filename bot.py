import asyncio
import instaloader

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

loader = instaloader.Instaloader(quiet=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! آیدی پیج عمومی اینستاگرام را بفرست.")

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip().lstrip("@")
    msg = await update.message.reply_text("در حال بررسی...")

    try:
        profile = await asyncio.to_thread(
            instaloader.Profile.from_username,
            loader.context,
            username
        )
        result = (
            "پیج: @" + profile.username + "\n"
            + "فالوورها: " + str(profile.followers) + "\n"
            + "فالووینگ‌ها: " + str(profile.followees)
        )
        await msg.edit_text(result)
    except Exception:
        await msg.edit_text("دریافت اطلاعات ناموفق بود.")

def main():
    import os
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
