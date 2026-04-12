"""
main.py — Ultra Coach entry point

Wires together:
  - Telegram bot (message handling)
  - Agent (Claude brain)
  - Scheduler (cron jobs)
  - State machine (flow routing)
  - Health webhook (receives weight + sleep from iPhone Shortcut)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from aiohttp import web
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
    handle_post_activity_reply,
)
from integrations.health import save_health_entry
from scheduler import create_scheduler
from state import (
    get_flow,
    set_flow,
    FLOW_FREEFORM,
    FLOW_CHECKIN_REPLY,
    FLOW_MISSED_WORKOUT_REPLY,
    FLOW_POST_ACTIVITY_REPLY,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HEALTH_WEBHOOK_SECRET = os.getenv("HEALTH_WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", 8080))


# ---------------------------------------------------------------------------
# Health webhook handlers
# ---------------------------------------------------------------------------

async def handle_health_webhook(request: web.Request) -> web.Response:
    """
    POST /health — receives weight + sleep data from iPhone Shortcut.

    Expected JSON body:
        {
            "date": "YYYY-MM-DD",       # optional, defaults to today
            "weight_lbs": 175.2,        # optional
            "sleep_hours": 7.5          # optional
        }

    Requires Authorization: Bearer <HEALTH_WEBHOOK_SECRET> header.
    """
    # Auth check
    if HEALTH_WEBHOOK_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {HEALTH_WEBHOOK_SECRET}":
            return web.Response(status=401, text="Unauthorized")

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    save_health_entry(data)
    return web.Response(status=200, text="ok")


async def handle_ping(request: web.Request) -> web.Response:
    """GET /ping — Railway health check."""
    return web.Response(text="ok")


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if CHAT_ID and str(update.message.chat_id) != str(CHAT_ID):
        logger.warning(f"Ignoring message from unknown chat: {update.message.chat_id}")
        return

    user_text = update.message.text
    logger.info(f"Incoming [{get_flow()}]: {user_text!r}")
    append_message(role="user", content=user_text)

    flow = get_flow()

    try:
        if flow == FLOW_CHECKIN_REPLY:
            reply = await handle_checkin_reply(user_text)
            set_flow(FLOW_FREEFORM)
        elif flow == FLOW_MISSED_WORKOUT_REPLY:
            reply = await handle_missed_workout_reply(user_text)
            set_flow(FLOW_FREEFORM)
        elif flow == FLOW_POST_ACTIVITY_REPLY:
            reply = await handle_post_activity_reply(user_text)
            set_flow(FLOW_FREEFORM)
        else:
            reply = await handle_message(user_text)
    except Exception as e:
        logger.exception(f"Agent error: {e}")
        reply = "Something went wrong on my end — try again in a minute."

    await update.message.reply_text(reply)


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = get_flow()
    scheduler = context.application.bot_data.get("scheduler")
    scheduler_status = "running" if scheduler and scheduler.running else "stopped"
    await update.message.reply_text(
        f"Ultra Coach running\nFlow: {flow}\nScheduler: {scheduler_status}"
    )


async def on_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_flow(FLOW_FREEFORM)
    await update.message.reply_text("Flow reset to freeform.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    # --- Telegram app ---
    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    telegram_app.add_handler(CommandHandler("status", on_status))
    telegram_app.add_handler(CommandHandler("reset", on_reset))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # --- aiohttp web server ---
    web_app = web.Application()
    web_app.router.add_get("/ping", handle_ping)
    web_app.router.add_post("/health", handle_health_webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health webhook listening on port {PORT}.")

    # --- Start everything ---
    async with telegram_app:
        scheduler = create_scheduler()
        scheduler.start()
        telegram_app.bot_data["scheduler"] = scheduler
        logger.info("Scheduler started.")

        await telegram_app.start()
        logger.info("Ultra Coach starting — polling for updates...")
        await telegram_app.updater.start_polling(drop_pending_updates=True)

        try:
            await asyncio.Event().wait()  # Run until interrupted
        finally:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            scheduler.shutdown(wait=False)
            await runner.cleanup()
            logger.info("Ultra Coach shut down.")


if __name__ == "__main__":
    asyncio.run(run())
