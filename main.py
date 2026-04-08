"""
main.py — Ultra Coach entry point

Wires together:
  - Telegram bot (message handling)
  - Agent (Claude brain)
  - Scheduler (cron jobs)
  - State machine (flow routing)
"""

import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import append_message
from agent import (
    handle_message,
    handle_checkin_reply,
    handle_missed_workout_reply,
)
from scheduler import create_scheduler
from state import (
    get_flow,
    set_flow,
    FLOW_FREEFORM,
    FLOW_CHECKIN_REPLY,
    FLOW_MISSED_WORKOUT_REPLY,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route every incoming message through the correct handler based on flow state.

    Flow states:
      freeform             → agent.handle_message()       (default)
      checkin_reply        → agent.handle_checkin_reply()  (parse wellness + respond)
      missed_workout_reply → agent.handle_missed_workout_reply() (adjust plan + respond)
    """
    # Ignore messages from anyone other than the configured chat ID
    if CHAT_ID and str(update.message.chat_id) != str(CHAT_ID):
        logger.warning(f"Ignoring message from unknown chat: {update.message.chat_id}")
        return

    user_text = update.message.text
    logger.info(f"Incoming [{get_flow()}]: {user_text!r}")

    # Log incoming message to conversation history
    append_message(role="user", content=user_text)

    flow = get_flow()

    try:
        if flow == FLOW_CHECKIN_REPLY:
            reply = await handle_checkin_reply(user_text)
            set_flow(FLOW_FREEFORM)

        elif flow == FLOW_MISSED_WORKOUT_REPLY:
            reply = await handle_missed_workout_reply(user_text)
            set_flow(FLOW_FREEFORM)

        else:
            reply = await handle_message(user_text)

    except Exception as e:
        logger.exception(f"Agent error: {e}")
        reply = "Something went wrong on my end — try again in a minute."

    await update.message.reply_text(reply)


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — health check."""
    flow = get_flow()
    jobs = context.application.bot_data.get("scheduler")
    scheduler_status = "running" if jobs and jobs.running else "stopped"
    await update.message.reply_text(
        f"Ultra Coach running\nFlow: {flow}\nScheduler: {scheduler_status}"
    )


async def on_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset — manually reset conversation flow to freeform."""
    set_flow(FLOW_FREEFORM)
    await update.message.reply_text("Flow reset to freeform.")


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    """Start the scheduler once the Application is initialized."""
    scheduler = create_scheduler()
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Scheduler started.")


async def post_shutdown(app: Application) -> None:
    """Gracefully shut down the scheduler."""
    scheduler = app.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("reset", on_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("Ultra Coach starting — polling for updates...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
