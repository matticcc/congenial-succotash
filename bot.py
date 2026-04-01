import os
import json
import logging
import asyncio
import uuid
from math import ceil

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from aiohttp import web
from telegram import (
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineQueryResultPhoto,
    Bot,
)
from telegram.ext import (
    Application,
    CommandHandler,
    InlineQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN             = os.environ["BOT_TOKEN"]
SPOTIFY_CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
# On Railway: https://yourapp.up.railway.app/callback
SPOTIFY_REDIRECT_URI  = os.environ["SPOTIFY_REDIRECT_URI"]
# Railway sets PORT automatically; default 8080 for local
PORT = int(os.environ.get("PORT", 8080))

SCOPE = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-library-read "
    "playlist-read-private"
)

TOKENS_FILE = "tokens.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Shared reference so the web callback can send Telegram messages
telegram_app: Application = None


# ── Token storage ─────────────────────────────────────────────────────────────
def load_tokens() -> dict:
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return {}


def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


# ── Spotify helpers ───────────────────────────────────────────────────────────
def make_oauth(state: str | None = None) -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SCOPE,
        state=state,
        cache_handler=None,
        open_browser=False,
    )


def get_spotify_for_user(user_id: int) -> spotipy.Spotify | None:
    tokens = load_tokens()
    token_info = tokens.get(str(user_id))
    if not token_info:
        return None
    oauth = make_oauth()
    if oauth.is_token_expired(token_info):
        try:
            token_info = oauth.refresh_access_token(token_info["refresh_token"])
            tokens[str(user_id)] = token_info
            save_tokens(tokens)
        except Exception as e:
            logger.error(f"Token refresh failed for {user_id}: {e}")
            return None
    return spotipy.Spotify(auth=token_info["access_token"])


def build_progress_bar(progress_ms: int, duration_ms: int) -> str:
    percentage = ceil(progress_ms / duration_ms * 100)
    filled = ceil(percentage / 10)
    empty = 10 - filled
    bar = "█" * filled + "░" * empty

    def ms_to_mmss(ms):
        total_sec = ms // 1000
        return f"{total_sec // 60:02d}:{total_sec % 60:02d}"

    return f"{bar} {ms_to_mmss(progress_ms)}/{ms_to_mmss(duration_ms)} ({percentage}%)"


def get_now_playing(sp: spotipy.Spotify) -> dict | None:
    try:
        pb = sp.current_playback()
    except Exception as e:
        logger.error(f"current_playback error: {e}")
        return None

    if not pb or not pb.get("item"):
        return None

    item     = pb["item"]
    album    = item["album"]
    images   = album.get("images", [])
    progress = pb.get("progress_ms", 0)
    duration = item.get("duration_ms", 1)

    return {
        "track":      item["name"],
        "artists":    ", ".join(a["name"] for a in item["artists"]),
        "album":      album["name"],
        "track_url":  item["external_urls"]["spotify"],
        "track_id":   item["id"],
        "album_art":  images[0]["url"] if images else None,
        "bar":        build_progress_bar(progress, duration),
        "is_playing": pb.get("is_playing", False),
    }


def format_caption(data: dict) -> str:
    status = "▶️ Listening to" if data["is_playing"] else "⏸ Paused on"
    return (
        f"{status}: <b>{data['track']}</b>\n"
        f"<i>{data['artists']}</i>\n"
        f"💿 {data['album']}\n\n"
        f"<code>{data['bar']}</code>\n\n"
        f'🔗 <a href="{data["track_url"]}">Open in Spotify</a>'
    )


# ── OAuth callback web handler ────────────────────────────────────────────────
async def spotify_callback(request: web.Request) -> web.Response:
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")   # Telegram user_id
    error = request.rel_url.query.get("error")

    if error or not code or not state:
        return web.Response(
            content_type="text/html",
            text="<h2>❌ Authorization failed or was cancelled.</h2>"
                 "<p>Go back to Telegram and try /login again.</p>",
        )

    try:
        user_id = int(state)
    except ValueError:
        return web.Response(content_type="text/html", text="<h2>❌ Invalid state.</h2>")

    oauth = make_oauth(state=state)
    try:
        token_info = oauth.get_access_token(code, as_dict=True, check_cache=False)
        tokens = load_tokens()
        tokens[str(user_id)] = token_info
        save_tokens(tokens)
        logger.info(f"Saved Spotify token for Telegram user {user_id}")
    except Exception as e:
        logger.error(f"Token exchange failed for {user_id}: {e}")
        return web.Response(
            content_type="text/html",
            text=f"<h2>❌ Token exchange failed.</h2><pre>{e}</pre>",
        )

    # Ping the user on Telegram
    try:
        await telegram_app.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ <b>Successfully connected to Spotify!</b>\n\n"
                "Now go to any chat, type <code>@YourBotUsername</code> "
                "and tap the result to share what you're listening to!"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Could not notify user {user_id}: {e}")

    return web.Response(
        content_type="text/html",
        text=(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h1>✅ Connected to Spotify!</h1>"
            "<p>You can close this tab and go back to Telegram.</p>"
            "</body></html>"
        ),
    )


async def healthcheck(request: web.Request) -> web.Response:
    """Railway pings GET / to check the service is alive."""
    return web.Response(text="OK")


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Welcome to SpotiGram Bot!</b>\n\n"
        "Share what you're listening to on Spotify as an inline message in any chat.\n\n"
        "<b>Getting started:</b>\n"
        "1️⃣ /login — connect your Spotify account\n"
        "2️⃣ In any chat, type <code>@YourBotUsername</code>\n"
        "3️⃣ Tap the result to share your now-playing track!\n\n"
        "<b>Other commands:</b>\n"
        "/status — check connection\n"
        "/logout — disconnect Spotify",
        parse_mode=ParseMode.HTML,
    )


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use /login in a private chat with me.")
        return

    user_id  = update.effective_user.id
    oauth    = make_oauth(state=str(user_id))
    auth_url = oauth.get_authorize_url()

    await update.message.reply_text(
        "🎵 <b>Connect your Spotify account</b>\n\n"
        f'<a href="{auth_url}">👉 Click here to authorize</a>\n\n'
        "After authorizing you'll see a ✅ page — then come back here!",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sp = get_spotify_for_user(user_id)
    if sp:
        try:
            me = sp.current_user()
            await update.message.reply_text(
                f"✅ Connected as <b>{me['display_name']}</b> (<code>{me['id']}</code>)",
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass
    await update.message.reply_text(
        "❌ Not connected. Use /login to connect your Spotify account."
    )


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tokens  = load_tokens()
    if str(user_id) in tokens:
        del tokens[str(user_id)]
        save_tokens(tokens)
        await update.message.reply_text("✅ Disconnected from Spotify.")
    else:
        await update.message.reply_text("You weren't connected.")


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.inline_query.from_user.id
    sp      = get_spotify_for_user(user_id)

    if sp is None:
        await update.inline_query.answer(
            [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="❌ Not connected to Spotify",
                    description="Open a private chat with this bot and use /login",
                    input_message_content=InputTextMessageContent(
                        "I need to connect my Spotify account first!"
                    ),
                )
            ],
            cache_time=0,
        )
        return

    data = get_now_playing(sp)

    if data is None:
        await update.inline_query.answer(
            [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="🎵 Nothing playing right now",
                    description="Start playing something on Spotify first",
                    input_message_content=InputTextMessageContent(
                        "🎵 Not listening to anything right now."
                    ),
                )
            ],
            cache_time=0,
        )
        return

    caption = format_caption(data)

    if data["album_art"]:
        results = [
            InlineQueryResultPhoto(
                id=str(uuid.uuid4()),
                photo_url=data["album_art"],
                thumbnail_url=data["album_art"],
                title=f"{data['track']} — {data['artists']}",
                description=data["album"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        ]
    else:
        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title=f"🎵 {data['track']}",
                description=f"{data['artists']} · {data['album']}",
                input_message_content=InputTextMessageContent(
                    caption, parse_mode=ParseMode.HTML
                ),
            )
        ]

    await update.inline_query.answer(results, cache_time=0)


# ── Entry point ───────────────────────────────────────────────────────────────
async def run():
    global telegram_app

    # Telegram bot
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start",  cmd_start))
    telegram_app.add_handler(CommandHandler("login",  cmd_login))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("logout", cmd_logout))
    telegram_app.add_handler(InlineQueryHandler(inline_query))

    # aiohttp web server for the OAuth callback
    web_app = web.Application()
    web_app.router.add_get("/",         healthcheck)
    web_app.router.add_get("/callback", spotify_callback)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Web server listening on port {PORT}")

    # Start polling
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Telegram bot polling started")

    # Block forever until killed
    try:
        await asyncio.Event().wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
