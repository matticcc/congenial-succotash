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
    LinkPreviewOptions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN             = os.environ["BOT_TOKEN"]
SPOTIFY_CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
SPOTIFY_REDIRECT_URI  = os.environ["SPOTIFY_REDIRECT_URI"]
PORT = int(os.environ.get("PORT", 8080))

# Added user-modify-playback-state for playback controls
SCOPE = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-modify-playback-state "
    "user-library-read "
    "user-library-modify "
    "playlist-read-private"
)

TOKENS_FILE = "tokens.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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
    # The invisible link trick: Telegram renders the first URL as a link preview image.
    # Zero-width space (\u200b) as link text makes it invisible in the message.
    art = data.get("album_art") or ""
    art_link = f'<a href="{art}">​</a>' if art else ""
    return (
        f"{art_link}"
        f"{status}: <b>{data['track']}</b>\n"
        f"<i>{data['artists']}</i>\n"
        f"💿 {data['album']}\n\n"
        f"<code>{data['bar']}</code>"
    )


def make_keyboard(data: dict, user_id: int) -> InlineKeyboardMarkup:
    """
    Callback formats:
      Playback controls : action###owner_id
      Play on Spotify   : play###owner_id###spotify:track:track_id
    Pressing "Play on Spotify" plays the track on the PRESSER's active device,
    regardless of who shared the message.
    """
    uid = str(user_id)
    track_uri = f"spotify:track:{data['track_id']}"
    pause_play = "⏸" if data["is_playing"] else "▶️"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏮", callback_data=f"prev###{uid}"),
            InlineKeyboardButton("⏪", callback_data=f"seek_back###{uid}"),
            InlineKeyboardButton(pause_play, callback_data=f"pause_play###{uid}"),
            InlineKeyboardButton("⏩", callback_data=f"seek_fwd###{uid}"),
            InlineKeyboardButton("⏭", callback_data=f"next###{uid}"),
        ],
        [
            InlineKeyboardButton("🎵 Play on Spotify", callback_data=f"play###{uid}###{track_uri}"),
        ],
    ])


# ── OAuth callback web handler ────────────────────────────────────────────────
async def spotify_callback(request: web.Request) -> web.Response:
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
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
            [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="❌ Not connected to Spotify",
                description="Open a private chat with this bot and use /login",
                input_message_content=InputTextMessageContent(
                    "I need to connect my Spotify account first!"
                ),
            )],
            cache_time=0,
        )
        return

    data = get_now_playing(sp)

    if data is None:
        await update.inline_query.answer(
            [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="🎵 Nothing playing right now",
                description="Start playing something on Spotify first",
                input_message_content=InputTextMessageContent(
                    "🎵 Not listening to anything right now."
                ),
            )],
            cache_time=0,
        )
        return

    caption  = format_caption(data)
    keyboard = make_keyboard(data, user_id)

    # We embed the user_id in the result id so chosen_inline_result can read it
    result_id = f"{user_id}:{uuid.uuid4()}"

    results = [InlineQueryResultArticle(
            id=result_id,
            title=f"{data['track']} — {data['artists']}",
            description=data["album"],
            thumbnail_url=data.get("album_art") or None,
            input_message_content=InputTextMessageContent(
                caption,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(
                    is_disabled=False,
                    prefer_large_media=True,
                    show_above_text=False,  # image shows above text naturally via invisible link
                ),
            ),
            reply_markup=keyboard,
        )]

    await update.inline_query.answer(results, cache_time=0)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No longer needed for ownership — user_id lives in callback_data.
    # Keep the handler registered so Telegram still sends us these updates
    # (required for inline feedback to work).
    pass


async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses on inline messages."""
    query  = update.callback_query
    action = query.data
    inline_message_id = query.inline_message_id

    # Parse callback_data
    # Playback controls : action###owner_id
    # Play on Spotify   : play###owner_id###spotify:track:track_id
    parts = action.split("###")
    if len(parts) < 2:
        await query.answer("⚠️ Malformed callback data.", show_alert=True)
        return

    action = parts[0]
    try:
        owner_id = int(parts[1])
    except ValueError:
        await query.answer("⚠️ Malformed callback data.", show_alert=True)
        return

    presser_id = query.from_user.id

    # ── "Play on Spotify" — anyone who is logged in can press this ────────────
    if action == "play":
        track_uri = parts[2] if len(parts) > 2 else None
        if not track_uri:
            await query.answer("⚠️ Missing track URI.", show_alert=True)
            return

        sp_presser = get_spotify_for_user(presser_id)
        if sp_presser is None:
            await query.answer(
                "⚠️ You need to /login to the bot first to use this.",
                show_alert=True,
            )
            return

        try:
            pb = sp_presser.current_playback()
            if not pb or not pb.get("device"):
                await query.answer(
                    "⚠️ No active Spotify device found. Open Spotify on any device first.",
                    show_alert=True,
                )
                return
            sp_presser.start_playback(uris=[track_uri])
            await query.answer("▶️ Playing on your Spotify!")
        except Exception as e:
            logger.error(f"play callback error for {presser_id}: {e}")
            await query.answer("⚠️ Couldn't start playback. Is Spotify open?", show_alert=True)
        return  # no message refresh needed for play

    # ── Playback controls — owner only ────────────────────────────────────────
    if presser_id != owner_id:
        await query.answer("These controls only work for the person who shared this.", show_alert=True)
        return

    sp = get_spotify_for_user(owner_id)
    if sp is None:
        await query.answer("⚠️ Not connected to Spotify.", show_alert=True)
        return

    try:
        if action == "prev":
            sp.previous_track()
            await query.answer("⏮ Previous track")
        elif action == "next":
            sp.next_track()
            await query.answer("⏭ Next track")
        elif action == "pause_play":
            pb = sp.current_playback()
            if pb and pb.get("is_playing"):
                sp.pause_playback()
                await query.answer("⏸ Paused")
            else:
                sp.start_playback()
                await query.answer("▶️ Playing")
        elif action == "seek_back":
            pb = sp.current_playback()
            if pb:
                new_pos = max(0, pb["progress_ms"] - 10000)
                sp.seek_track(new_pos)
                await query.answer("⏪ -10s")
        elif action == "seek_fwd":
            pb = sp.current_playback()
            if pb:
                new_pos = pb["progress_ms"] + 10000
                sp.seek_track(new_pos)
                await query.answer("⏩ +10s")
        else:
            await query.answer()
            return
    except Exception as e:
        logger.error(f"Playback control error: {e}")
        await query.answer("⚠️ Playback error — is Spotify open?", show_alert=True)
        return

    # After any action, wait a moment then refresh the message with updated state
    await asyncio.sleep(0.5)
    try:
        data = get_now_playing(sp)
        if data:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=format_caption(data),
                parse_mode=ParseMode.HTML,
                reply_markup=make_keyboard(data, owner_id),
                link_preview_options=LinkPreviewOptions(
                    is_disabled=False,
                    prefer_large_media=True,
                ),
            )
    except Exception as e:
        logger.warning(f"Could not refresh inline message: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────
async def run():
    global telegram_app

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start",  cmd_start))
    telegram_app.add_handler(CommandHandler("login",  cmd_login))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("logout", cmd_logout))
    telegram_app.add_handler(InlineQueryHandler(inline_query))
    telegram_app.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    telegram_app.add_handler(CallbackQueryHandler(callback_query))

    web_app = web.Application()
    web_app.router.add_get("/",         healthcheck)
    web_app.router.add_get("/callback", spotify_callback)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Web server listening on port {PORT}")

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Telegram bot polling started")

    try:
        await asyncio.Event().wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN             = os.environ["BOT_TOKEN"]
SPOTIFY_CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
SPOTIFY_REDIRECT_URI  = os.environ["SPOTIFY_REDIRECT_URI"]
PORT = int(os.environ.get("PORT", 8080))

# Added user-modify-playback-state for playback controls
SCOPE = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-modify-playback-state "
    "user-library-read "
    "user-library-modify "
    "playlist-read-private"
)

TOKENS_FILE = "tokens.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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
    # The invisible link trick: Telegram renders the first URL as a link preview image.
    # Zero-width space (\u200b) as link text makes it invisible in the message.
    art = data.get("album_art") or ""
    art_link = f'<a href="{art}">​</a>' if art else ""
    return (
        f"{art_link}"
        f"{status}: <b>{data['track']}</b>\n"
        f"<i>{data['artists']}</i>\n"
        f"💿 {data['album']}\n\n"
        f"<code>{data['bar']}</code>"
    )


def make_keyboard(data: dict, user_id: int) -> InlineKeyboardMarkup:
    """
    Callback formats:
      Playback controls : action###owner_id
      Play on Spotify   : play###owner_id###spotify:track:track_id
    Pressing "Play on Spotify" plays the track on the PRESSER's active device,
    regardless of who shared the message.
    """
    uid = str(user_id)
    track_uri = f"spotify:track:{data['track_id']}"
    pause_play = "⏸" if data["is_playing"] else "▶️"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏮", callback_data=f"prev###{uid}"),
            InlineKeyboardButton("⏪", callback_data=f"seek_back###{uid}"),
            InlineKeyboardButton(pause_play, callback_data=f"pause_play###{uid}"),
            InlineKeyboardButton("⏩", callback_data=f"seek_fwd###{uid}"),
            InlineKeyboardButton("⏭", callback_data=f"next###{uid}"),
        ],
        [
            InlineKeyboardButton("🎵 Play on Spotify", callback_data=f"play###{uid}###{track_uri}"),
        ],
    ])


# ── OAuth callback web handler ────────────────────────────────────────────────
async def spotify_callback(request: web.Request) -> web.Response:
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
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
            [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="❌ Not connected to Spotify",
                description="Open a private chat with this bot and use /login",
                input_message_content=InputTextMessageContent(
                    "I need to connect my Spotify account first!"
                ),
            )],
            cache_time=0,
        )
        return

    data = get_now_playing(sp)

    if data is None:
        await update.inline_query.answer(
            [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="🎵 Nothing playing right now",
                description="Start playing something on Spotify first",
                input_message_content=InputTextMessageContent(
                    "🎵 Not listening to anything right now."
                ),
            )],
            cache_time=0,
        )
        return

    caption  = format_caption(data)
    keyboard = make_keyboard(data, user_id)

    # We embed the user_id in the result id so chosen_inline_result can read it
    result_id = f"{user_id}:{uuid.uuid4()}"

    results = [InlineQueryResultArticle(
            id=result_id,
            title=f"{data['track']} — {data['artists']}",
            description=data["album"],
            thumbnail_url=data.get("album_art") or None,
            input_message_content=InputTextMessageContent(
                caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            ),
            reply_markup=keyboard,
        )]

    await update.inline_query.answer(results, cache_time=0)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No longer needed for ownership — user_id lives in callback_data.
    # Keep the handler registered so Telegram still sends us these updates
    # (required for inline feedback to work).
    pass


async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses on inline messages."""
    query  = update.callback_query
    action = query.data
    inline_message_id = query.inline_message_id

    # Parse callback_data
    # Playback controls : action###owner_id
    # Play on Spotify   : play###owner_id###spotify:track:track_id
    parts = action.split("###")
    if len(parts) < 2:
        await query.answer("⚠️ Malformed callback data.", show_alert=True)
        return

    action = parts[0]
    try:
        owner_id = int(parts[1])
    except ValueError:
        await query.answer("⚠️ Malformed callback data.", show_alert=True)
        return

    presser_id = query.from_user.id

    # ── "Play on Spotify" — anyone who is logged in can press this ────────────
    if action == "play":
        track_uri = parts[2] if len(parts) > 2 else None
        if not track_uri:
            await query.answer("⚠️ Missing track URI.", show_alert=True)
            return

        sp_presser = get_spotify_for_user(presser_id)
        if sp_presser is None:
            await query.answer(
                "⚠️ You need to /login to the bot first to use this.",
                show_alert=True,
            )
            return

        try:
            pb = sp_presser.current_playback()
            if not pb or not pb.get("device"):
                await query.answer(
                    "⚠️ No active Spotify device found. Open Spotify on any device first.",
                    show_alert=True,
                )
                return
            sp_presser.start_playback(uris=[track_uri])
            await query.answer("▶️ Playing on your Spotify!")
        except Exception as e:
            logger.error(f"play callback error for {presser_id}: {e}")
            await query.answer("⚠️ Couldn't start playback. Is Spotify open?", show_alert=True)
        return  # no message refresh needed for play

    # ── Playback controls — owner only ────────────────────────────────────────
    if presser_id != owner_id:
        await query.answer("These controls only work for the person who shared this.", show_alert=True)
        return

    sp = get_spotify_for_user(owner_id)
    if sp is None:
        await query.answer("⚠️ Not connected to Spotify.", show_alert=True)
        return

    try:
        if action == "prev":
            sp.previous_track()
            await query.answer("⏮ Previous track")
        elif action == "next":
            sp.next_track()
            await query.answer("⏭ Next track")
        elif action == "pause_play":
            pb = sp.current_playback()
            if pb and pb.get("is_playing"):
                sp.pause_playback()
                await query.answer("⏸ Paused")
            else:
                sp.start_playback()
                await query.answer("▶️ Playing")
        elif action == "seek_back":
            pb = sp.current_playback()
            if pb:
                new_pos = max(0, pb["progress_ms"] - 10000)
                sp.seek_track(new_pos)
                await query.answer("⏪ -10s")
        elif action == "seek_fwd":
            pb = sp.current_playback()
            if pb:
                new_pos = pb["progress_ms"] + 10000
                sp.seek_track(new_pos)
                await query.answer("⏩ +10s")
        else:
            await query.answer()
            return
    except Exception as e:
        logger.error(f"Playback control error: {e}")
        await query.answer("⚠️ Playback error — is Spotify open?", show_alert=True)
        return

    # After any action, wait a moment then refresh the message with updated state
    await asyncio.sleep(0.5)
    try:
        data = get_now_playing(sp)
        if data:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=format_caption(data),
                parse_mode=ParseMode.HTML,
                reply_markup=make_keyboard(data, owner_id),
                disable_web_page_preview=False,
            )
    except Exception as e:
        logger.warning(f"Could not refresh inline message: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────
async def run():
    global telegram_app

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start",  cmd_start))
    telegram_app.add_handler(CommandHandler("login",  cmd_login))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("logout", cmd_logout))
    telegram_app.add_handler(InlineQueryHandler(inline_query))
    telegram_app.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    telegram_app.add_handler(CallbackQueryHandler(callback_query))

    web_app = web.Application()
    web_app.router.add_get("/",         healthcheck)
    web_app.router.add_get("/callback", spotify_callback)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Web server listening on port {PORT}")

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Telegram bot polling started")

    try:
        await asyncio.Event().wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN             = os.environ["BOT_TOKEN"]
SPOTIFY_CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
SPOTIFY_REDIRECT_URI  = os.environ["SPOTIFY_REDIRECT_URI"]
PORT = int(os.environ.get("PORT", 8080))

# Added user-modify-playback-state for playback controls
SCOPE = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-modify-playback-state "
    "user-library-read "
    "user-library-modify "
    "playlist-read-private"
)

TOKENS_FILE = "tokens.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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
        f"<code>{data['bar']}</code>"
    )


def make_keyboard(data: dict, user_id: int) -> InlineKeyboardMarkup:
    """
    Callback formats:
      Playback controls : action###owner_id
      Play on Spotify   : play###owner_id###spotify:track:track_id
    Pressing "Play on Spotify" plays the track on the PRESSER's active device,
    regardless of who shared the message.
    """
    uid = str(user_id)
    track_uri = f"spotify:track:{data['track_id']}"
    pause_play = "⏸" if data["is_playing"] else "▶️"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏮", callback_data=f"prev###{uid}"),
            InlineKeyboardButton("⏪", callback_data=f"seek_back###{uid}"),
            InlineKeyboardButton(pause_play, callback_data=f"pause_play###{uid}"),
            InlineKeyboardButton("⏩", callback_data=f"seek_fwd###{uid}"),
            InlineKeyboardButton("⏭", callback_data=f"next###{uid}"),
        ],
        [
            InlineKeyboardButton("🎵 Play on Spotify", callback_data=f"play###{uid}###{track_uri}"),
        ],
    ])


# ── OAuth callback web handler ────────────────────────────────────────────────
async def spotify_callback(request: web.Request) -> web.Response:
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
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
            [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="❌ Not connected to Spotify",
                description="Open a private chat with this bot and use /login",
                input_message_content=InputTextMessageContent(
                    "I need to connect my Spotify account first!"
                ),
            )],
            cache_time=0,
        )
        return

    data = get_now_playing(sp)

    if data is None:
        await update.inline_query.answer(
            [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="🎵 Nothing playing right now",
                description="Start playing something on Spotify first",
                input_message_content=InputTextMessageContent(
                    "🎵 Not listening to anything right now."
                ),
            )],
            cache_time=0,
        )
        return

    caption  = format_caption(data)
    keyboard = make_keyboard(data, user_id)

    # We embed the user_id in the result id so chosen_inline_result can read it
    result_id = f"{user_id}:{uuid.uuid4()}"

    if data["album_art"]:
        results = [InlineQueryResultPhoto(
            id=result_id,
            photo_url=data["album_art"],
            thumbnail_url=data["album_art"],
            title=f"{data['track']} — {data['artists']}",
            description=data["album"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )]
    else:
        results = [InlineQueryResultArticle(
            id=result_id,
            title=f"🎵 {data['track']}",
            description=f"{data['artists']} · {data['album']}",
            input_message_content=InputTextMessageContent(
                caption, parse_mode=ParseMode.HTML
            ),
            reply_markup=keyboard,
        )]

    await update.inline_query.answer(results, cache_time=0)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No longer needed for ownership — user_id lives in callback_data.
    # Keep the handler registered so Telegram still sends us these updates
    # (required for inline feedback to work).
    pass


async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses on inline messages."""
    query  = update.callback_query
    action = query.data
    inline_message_id = query.inline_message_id

    # Parse callback_data
    # Playback controls : action###owner_id
    # Play on Spotify   : play###owner_id###spotify:track:track_id
    parts = action.split("###")
    if len(parts) < 2:
        await query.answer("⚠️ Malformed callback data.", show_alert=True)
        return

    action = parts[0]
    try:
        owner_id = int(parts[1])
    except ValueError:
        await query.answer("⚠️ Malformed callback data.", show_alert=True)
        return

    presser_id = query.from_user.id

    # ── "Play on Spotify" — anyone who is logged in can press this ────────────
    if action == "play":
        track_uri = parts[2] if len(parts) > 2 else None
        if not track_uri:
            await query.answer("⚠️ Missing track URI.", show_alert=True)
            return

        sp_presser = get_spotify_for_user(presser_id)
        if sp_presser is None:
            await query.answer(
                "⚠️ You need to /login to the bot first to use this.",
                show_alert=True,
            )
            return

        try:
            pb = sp_presser.current_playback()
            if not pb or not pb.get("device"):
                await query.answer(
                    "⚠️ No active Spotify device found. Open Spotify on any device first.",
                    show_alert=True,
                )
                return
            sp_presser.start_playback(uris=[track_uri])
            await query.answer("▶️ Playing on your Spotify!")
        except Exception as e:
            logger.error(f"play callback error for {presser_id}: {e}")
            await query.answer("⚠️ Couldn't start playback. Is Spotify open?", show_alert=True)
        return  # no message refresh needed for play

    # ── Playback controls — owner only ────────────────────────────────────────
    if presser_id != owner_id:
        await query.answer("These controls only work for the person who shared this.", show_alert=True)
        return

    sp = get_spotify_for_user(owner_id)
    if sp is None:
        await query.answer("⚠️ Not connected to Spotify.", show_alert=True)
        return

    try:
        if action == "prev":
            sp.previous_track()
            await query.answer("⏮ Previous track")
        elif action == "next":
            sp.next_track()
            await query.answer("⏭ Next track")
        elif action == "pause_play":
            pb = sp.current_playback()
            if pb and pb.get("is_playing"):
                sp.pause_playback()
                await query.answer("⏸ Paused")
            else:
                sp.start_playback()
                await query.answer("▶️ Playing")
        elif action == "seek_back":
            pb = sp.current_playback()
            if pb:
                new_pos = max(0, pb["progress_ms"] - 10000)
                sp.seek_track(new_pos)
                await query.answer("⏪ -10s")
        elif action == "seek_fwd":
            pb = sp.current_playback()
            if pb:
                new_pos = pb["progress_ms"] + 10000
                sp.seek_track(new_pos)
                await query.answer("⏩ +10s")
        else:
            await query.answer()
            return
    except Exception as e:
        logger.error(f"Playback control error: {e}")
        await query.answer("⚠️ Playback error — is Spotify open?", show_alert=True)
        return

    # After any action, wait a moment then refresh the message with updated state
    await asyncio.sleep(0.5)
    try:
        data = get_now_playing(sp)
        if data:
            await context.bot.edit_message_caption(
                inline_message_id=inline_message_id,
                caption=format_caption(data),
                parse_mode=ParseMode.HTML,
                reply_markup=make_keyboard(data, owner_id),
            )
    except Exception as e:
        logger.warning(f"Could not refresh inline message: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────
async def run():
    global telegram_app

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start",  cmd_start))
    telegram_app.add_handler(CommandHandler("login",  cmd_login))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("logout", cmd_logout))
    telegram_app.add_handler(InlineQueryHandler(inline_query))
    telegram_app.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    telegram_app.add_handler(CallbackQueryHandler(callback_query))

    web_app = web.Application()
    web_app.router.add_get("/",         healthcheck)
    web_app.router.add_get("/callback", spotify_callback)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Web server listening on port {PORT}")

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Telegram bot polling started")

    try:
        await asyncio.Event().wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
