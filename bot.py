"""
bot.py — Main entry point for the PreBot Telegram running coach.

Handles incoming messages, logs them to conversation.json, and provides
a send_message() function for programmatic (unprompted) outbound messages.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # your personal chat ID for outbound messages

CONVERSATION_FILE = Path(__file__).parent / "data" / "conversation.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversation logging
# ---------------------------------------------------------------------------

def load_conversation() -> list[dict]:
    """Read the full conversation log from disk."""
    with open(CONVERSATION_FILE, "r") as f:
        return json.load(f)


def append_message(role: str, content: str) -> None:
    """Append a single message entry to conversation.json."""
    history = load_conversation()
    history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role": role,       # "user" | "assistant"
        "content": content,
    })
    with open(CONVERSATION_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every incoming user text message and send an acknowledgment."""
    user_text = update.message.text
    logger.info(f"Received message: {user_text!r}")

    # Persist to conversation log
    append_message(role="user", content=user_text)

    # Simple acknowledgment — will be replaced by AI response in later sessions
    await update.message.reply_text("Got it, coach is thinking...")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to /status with a health-check confirmation."""
    await update.message.reply_text("Bot is running ✓")


# ---------------------------------------------------------------------------
# Programmatic outbound messaging
# ---------------------------------------------------------------------------

async def send_message(text: str) -> None:
    """
    Send a message to CHAT_ID without any incoming update.
    Call this from scheduled jobs or external scripts to push messages to the user.
    """
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")

    app = Application.builder().token(BOT_TOKEN).build()
    async with app:
        await app.bot.send_message(chat_id=CHAT_ID, text=text)

    # Log the outbound message so the conversation history stays complete
    append_message(role="assistant", content=text)
    logger.info(f"Sent outbound message: {text!r}")


# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------

def main() -> None:
    """Build the Application, register handlers, and start polling."""
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("PreBot is starting — polling for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()
