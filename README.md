# 🎵 SpotiGram Bot

A Telegram inline bot that lets you share your currently playing Spotify track in any chat.

---

## Deploy to Railway (recommended)

### 1. Create a Telegram Bot

1. Open [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy your **Bot Token**
3. Send `/setinline` → select your bot → set placeholder e.g. `"Sharing now playing..."`

### 2. Push your code to GitHub

Put all these files in a GitHub repo:
- `bot.py`
- `requirements.txt`
- `railway.toml`

### 3. Deploy on Railway

1. Go to [railway.app](https://railway.app) and sign up (free)
2. Click **New Project** → **Deploy from GitHub repo** → select your repo
3. Once deployed, go to your service → **Settings** → **Networking** → **Generate Domain**
4. Copy your domain, e.g. `https://mybot.up.railway.app`

### 4. Create a Spotify App

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. **Create App**
3. Set **Redirect URI** to `https://mybot.up.railway.app/callback`
4. Copy **Client ID** and **Client Secret**

### 5. Set environment variables on Railway

In your Railway service → **Variables**, add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | your Telegram bot token |
| `SPOTIFY_CLIENT_ID` | from Spotify dashboard |
| `SPOTIFY_CLIENT_SECRET` | from Spotify dashboard |
| `SPOTIFY_REDIRECT_URI` | `https://mybot.up.railway.app/callback` |

Railway sets `PORT` automatically — don't add it manually.

Railway will redeploy automatically after you save the variables.

---

## User Flow

1. User opens private chat with the bot → `/login`
2. Bot sends an OAuth link → user clicks it → authorizes on Spotify
3. Spotify redirects to your Railway app → user sees ✅ page
4. Bot sends a confirmation message on Telegram automatically
5. In **any chat**, user types `@YourBot` → taps the card → shares now-playing ✅

---

## Commands

| Command | Description |
|---|---|
| `/start` | Welcome & instructions |
| `/login` | Connect Spotify account |
| `/status` | Check connection |
| `/logout` | Disconnect Spotify |

---

## Running locally

```bash
pip install -r requirements.txt

export BOT_TOKEN=...
export SPOTIFY_CLIENT_ID=...
export SPOTIFY_CLIENT_SECRET=...
export SPOTIFY_REDIRECT_URI=http://localhost:8080/callback

python bot.py
```

For local use set the redirect URI to `http://localhost:8080/callback` and add it to your Spotify app dashboard.
