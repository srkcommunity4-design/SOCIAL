#!/usr/bin/env python3
# ======================================================================================
#  SR King — Social Media Downloader Bot
#  YouTube • Instagram • Facebook  |  Shorts/Reels + Songs (Audio)
#  Single-file build for Railway.com
# ======================================================================================

import os
import re
import time
import asyncio
import logging
import tempfile
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    InputFile,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError, BadRequest, Forbidden, TimedOut, NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import yt_dlp

# ======================================================================================
#  CONFIG  (all secrets come from Railway environment variables — never hardcode them)
# ======================================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# Your 3 force-join channels (public usernames, WITHOUT the @ for API calls)
FORCE_JOIN_CHANNELS = [
    {"username": "SRK_ERA", "url": "https://t.me/SRK_ERA"},
    {"username": "SRKING000001", "url": "https://t.me/SRKING000001"},
    {"username": "SRK_IMP1", "url": "https://t.me/SRK_IMP1"},
]

BOT_NAME = "SR King"
DOWNLOAD_ROOT = os.environ.get("DOWNLOAD_ROOT", "/tmp/srking_downloads")
MAX_TG_FILE_BYTES = 49 * 1024 * 1024  # Telegram Bot API hard upload limit ≈ 50MB
TARGET_FILE_BYTES = int(MAX_TG_FILE_BYTES * 0.94)  # aim a bit under the cap so real output never overshoots
JOIN_CACHE_TTL = 300  # seconds — how long we trust a "joined" result before re-checking

os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

# ------------------------------------------------------------------------------------
#  COOKIES — YouTube / Instagram / Facebook all increasingly require a logged-in
#  session to serve media (anti-bot walls). You can provide cookies TWO ways per
#  platform, either works:
#    1) Paste the raw cookies.txt (Netscape format) content into YT_COOKIES /
#       IG_COOKIES / FB_COOKIES as a Railway env var — the bot writes it to a file
#       for you at startup (no persistent volume needed).
#    2) Or point YT_COOKIES_FILE / IG_COOKIES_FILE / FB_COOKIES_FILE at a file
#       path if you're using a mounted volume.
#  COOKIES_FILE (old single var) is kept as a fallback for all three platforms.
# ------------------------------------------------------------------------------------

def _materialize_cookiefile(env_var: str) -> str | None:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return None
    path = os.path.join(DOWNLOAD_ROOT, f"{env_var.lower()}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        return path
    except OSError:
        return None


_LEGACY_COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip() or None

YT_COOKIES_FILE = (
    _materialize_cookiefile("YT_COOKIES")
    or (os.environ.get("YT_COOKIES_FILE", "").strip() or None)
    or _LEGACY_COOKIES_FILE
)
IG_COOKIES_FILE = (
    _materialize_cookiefile("IG_COOKIES")
    or (os.environ.get("IG_COOKIES_FILE", "").strip() or None)
    or _LEGACY_COOKIES_FILE
)
FB_COOKIES_FILE = (
    _materialize_cookiefile("FB_COOKIES")
    or (os.environ.get("FB_COOKIES_FILE", "").strip() or None)
    or _LEGACY_COOKIES_FILE
)

COOKIES_BY_PLATFORM = {
    "YouTube": YT_COOKIES_FILE,
    "Instagram": IG_COOKIES_FILE,
    "Facebook": FB_COOKIES_FILE,
}

REFERER_BY_PLATFORM = {
    "YouTube": "https://www.youtube.com/",
    "Instagram": "https://www.instagram.com/",
    "Facebook": "https://www.facebook.com/",
}

# ======================================================================================
#  LOGGING
# ======================================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("SRKing")

# ======================================================================================
#  IN-MEMORY STATE  (kept simple & fast — no external DB needed to run this bot)
# ======================================================================================

KNOWN_USERS: set[int] = set()
JOIN_CACHE: dict[int, float] = {}          # user_id -> last verified timestamp
PENDING_LINKS: dict[str, dict] = {}        # short_id -> {url, platform, user_id}
EXECUTOR = ThreadPoolExecutor(max_workers=6)  # runs blocking yt-dlp jobs off the event loop

# ======================================================================================
#  PLATFORM DETECTION
# ======================================================================================

PLATFORM_PATTERNS = {
    "YouTube": re.compile(
        r"(https?://)?(www\.)?(youtube\.com/(shorts/|watch\?v=)|youtu\.be/)[\w\-?=&%.]+",
        re.IGNORECASE,
    ),
    "Instagram": re.compile(
        r"(https?://)?(www\.)?instagram\.com/(share/)?(reel|reels|p|tv|stories)/[\w\-]+",
        re.IGNORECASE,
    ),
    "Facebook": re.compile(
        r"(https?://)?(www\.|web\.|m\.)?(facebook\.com|fb\.watch)/[\w\-./?=&%]+",
        re.IGNORECASE,
    ),
}

# Instagram "sound/audio" pages (e.g. instagram.com/reels/audio/12345/) list every
# reel that uses a given audio track — they are not a single piece of media, and
# yt-dlp's Instagram extractor explicitly does not support them. We detect this
# shape separately so we can tell the user clearly instead of failing silently.
INSTAGRAM_AUDIO_PAGE = re.compile(
    r"(https?://)?(www\.)?instagram\.com/reels?/audio/[\w\-]+", re.IGNORECASE
)

PLATFORM_EMOJI = {"YouTube": "▶️", "Instagram": "📸", "Facebook": "📘"}


def detect_platform(text: str):
    """Return (platform_name, matched_url) or (None, None)."""
    for name, pattern in PLATFORM_PATTERNS.items():
        match = pattern.search(text)
        if match:
            return name, match.group(0)
    return None, None


def human_size(num_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}TB"


# ======================================================================================
#  FORCE-JOIN VERIFICATION  (fast: cached + parallel membership checks)
# ======================================================================================

async def check_membership(bot, user_id: int) -> bool:
    """True only if user has joined ALL force-join channels. Cached for speed."""
    now = time.time()
    if user_id in JOIN_CACHE and (now - JOIN_CACHE[user_id]) < JOIN_CACHE_TTL:
        return True

    async def _member_of(channel_username: str) -> bool:
        try:
            member = await bot.get_chat_member(f"@{channel_username}", user_id)
            return member.status in ("member", "administrator", "creator")
        except (BadRequest, Forbidden):
            return False
        except TelegramError:
            return False

    results = await asyncio.gather(
        *[_member_of(ch["username"]) for ch in FORCE_JOIN_CHANNELS]
    )
    joined_all = all(results)
    if joined_all:
        JOIN_CACHE[user_id] = now
    return joined_all


def join_keyboard(verify_callback: str = "verify_join") -> InlineKeyboardMarkup:
    rows = []
    for ch in FORCE_JOIN_CHANNELS:
        rows.append([InlineKeyboardButton(f"📢 Join Channel", url=ch["url"])])
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify Now", callback_data=verify_callback, style="success")])
    return InlineKeyboardMarkup(rows)


FORCE_JOIN_TEXT = (
    "🔐 <b>Access Locked</b>\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    f"To use <b>{BOT_NAME}</b>, please join our official channels below 👇\n\n"
    "After joining all of them, tap <b>✅ I've Joined</b> to unlock the bot instantly."
)


# ======================================================================================
#  DOWNLOAD ENGINE (yt-dlp)  — runs in a thread pool so the bot never freezes
# ======================================================================================

class DownloadError(Exception):
    """Friendly, user-facing download error."""


def _base_ydl_opts(out_template: str, platform: str | None = None) -> dict:
    opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 4,
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
        # Try several internal YouTube clients in order — some are blocked without
        # cookies while others (e.g. tv/ios) often still serve public videos.
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv", "web"]}},
    }

    cookiefile = COOKIES_BY_PLATFORM.get(platform or "")
    if cookiefile and os.path.isfile(cookiefile):
        opts["cookiefile"] = cookiefile

    referer = REFERER_BY_PLATFORM.get(platform or "")
    if referer:
        opts["http_headers"]["Referer"] = referer

    # Facebook frequently fails with "Cannot parse data" unless the request is
    # fingerprinted like a real browser (requires curl_cffi to be installed).
    if platform == "Facebook":
        opts["impersonate"] = yt_dlp.utils.ImpersonateTarget("chrome") if hasattr(yt_dlp.utils, "ImpersonateTarget") else "chrome"

    return opts


def _run_ydl(url: str, opts: dict) -> dict:
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise DownloadError("No media found at this link.")
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            return info
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "private" in msg:
            raise DownloadError("This content is private or restricted.")
        if "unavailable" in msg or "not available" in msg:
            raise DownloadError("This content is unavailable (deleted, region-locked, or removed).")
        if "login" in msg or "rate-limit" in msg or "429" in msg:
            raise DownloadError("The platform is temporarily rate-limiting downloads. Please try again shortly.")
        raise DownloadError("Couldn't fetch this media. The link may be invalid or unsupported.")
    except Exception:
        log.error("yt-dlp failure:\n%s", traceback.format_exc())
        raise DownloadError("Something went wrong while downloading. Please try again.")


def _fmt_size(fmt: dict, duration: float | None) -> int | None:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def _probe_info(url: str, platform: str) -> dict:
    """Fast metadata-only pass (no download) so we can pick a format that fits Telegram's limit."""
    opts = _base_ydl_opts(os.path.join(DOWNLOAD_ROOT, "%(id)s.%(ext)s"), platform)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise DownloadError("No media found at this link.")
    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    return info


def _pick_video_format(info: dict, max_bytes: int) -> str:
    """Choose the highest-quality video+audio combo (or progressive stream) that fits max_bytes."""
    duration = info.get("duration") or 0
    formats = info.get("formats") or []
    video_only = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")]
    audio_only = [f for f in formats if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    progressive = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]

    best_audio = max(audio_only, key=lambda f: f.get("abr") or 0, default=None)
    audio_size = _fmt_size(best_audio, duration) if best_audio else 0

    combos = []
    for f in video_only:
        vsize = _fmt_size(f, duration)
        if vsize is None:
            continue
        combos.append((f.get("height") or 0, vsize + (audio_size or 0), f))
    combos.sort(key=lambda c: c[0], reverse=True)

    for height, total, vf in combos:
        if total <= max_bytes:
            return f"{vf['format_id']}+{best_audio['format_id']}" if best_audio else vf["format_id"]

    prog_sorted = sorted(progressive, key=lambda f: f.get("height") or 0, reverse=True)
    for f in prog_sorted:
        size = _fmt_size(f, duration)
        if size and size <= max_bytes:
            return f["format_id"]

    # Nothing confidently fits (e.g. missing size data, or source itself is huge) —
    # fall back to the smallest known combo so we at least attempt the download;
    # the final on-disk size check afterward is the real safety net.
    if combos:
        combos.sort(key=lambda c: c[1])
        height, total, vf = combos[0]
        return f"{vf['format_id']}+{best_audio['format_id']}" if best_audio else vf["format_id"]
    if prog_sorted:
        f = min(prog_sorted, key=lambda f: _fmt_size(f, duration) or float("inf"))
        return f["format_id"]

    return "bestvideo*+bestaudio/best"


def _pick_audio_bitrate_kbps(info: dict, max_bytes: int) -> str:
    duration = info.get("duration") or 0
    if not duration:
        return "192"
    target = int((max_bytes * 8 / 1000) / duration * 0.90)
    return str(max(48, min(320, target)))


def download_video_sync(url: str, work_dir: str, platform: str) -> tuple[str, dict]:
    """Probes available formats, then downloads the best video+audio combo that fits Telegram's limit."""
    info = _probe_info(url, platform)
    format_selector = _pick_video_format(info, TARGET_FILE_BYTES)

    out_template = os.path.join(work_dir, "%(id)s.%(ext)s")
    opts = _base_ydl_opts(out_template, platform)
    opts.update({
        "format": f"{format_selector}/bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    })
    info = _run_ydl(url, opts)
    filepath = _locate_downloaded_file(work_dir)
    return filepath, info


def download_audio_sync(url: str, work_dir: str, platform: str) -> tuple[str, dict]:
    """Probes duration, then extracts audio at the highest bitrate that still fits Telegram's limit."""
    info = _probe_info(url, platform)
    quality_kbps = _pick_audio_bitrate_kbps(info, TARGET_FILE_BYTES)

    out_template = os.path.join(work_dir, "%(id)s.%(ext)s")
    opts = _base_ydl_opts(out_template, platform)
    opts.update({
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality_kbps,
        }],
    })
    info = _run_ydl(url, opts)
    filepath = _locate_downloaded_file(work_dir)
    return filepath, info


def _locate_downloaded_file(work_dir: str) -> str:
    files = [os.path.join(work_dir, f) for f in os.listdir(work_dir)]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise DownloadError("Download finished but the file could not be located.")
    # Pick the largest file (avoids picking up leftover thumbnail/json files)
    return max(files, key=os.path.getsize)


async def run_download(kind: str, url: str, platform: str) -> tuple[str, dict]:
    loop = asyncio.get_running_loop()
    work_dir = tempfile.mkdtemp(prefix="srk_", dir=DOWNLOAD_ROOT)
    func = download_video_sync if kind == "video" else download_audio_sync
    try:
        filepath, info = await loop.run_in_executor(EXECUTOR, partial(func, url, work_dir, platform))
        return filepath, info
    except DownloadError:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.error("Unexpected download failure:\n%s", traceback.format_exc())
        raise DownloadError("Unexpected error during download. Please try again.")


# ======================================================================================
#  UI TEXT  (clean single-line dividers — renders identically on any DPI/screen size)
# ======================================================================================

DIVIDER = "━━━━━━━━━━━━━━━━━━━"

WELCOME_TEXT = (
    f"👑 <b>Welcome to {BOT_NAME}!</b>\n"
    f"{DIVIDER}\n"
    "⚡ Fast. Smart. High-Quality Downloads.\n\n"
    "Send me any <b>YouTube Shorts</b>, <b>Instagram Reel</b>, or <b>Facebook video</b> link "
    "and I'll fetch it in the best available quality — as a video or as audio (song), your choice.\n\n"
    "📥 <b>Supported:</b>\n"
    "▶️ YouTube Shorts\n"
    "📸 Instagram Reels\n"
    "📘 Facebook Reels/Videos\n\n"
    "Just paste a link below to get started 👇"
)

HELP_TEXT = (
    f"ℹ️ <b>How to use {BOT_NAME}</b>\n"
    f"{DIVIDER}\n"
    "1️⃣ Copy a short video link from YouTube, Instagram, or Facebook\n"
    "2️⃣ Paste it here in the chat\n"
    "3️⃣ Choose 🎬 Video or 🎵 Song\n"
    "4️⃣ Get your file in seconds, in the best quality available!\n\n"
    "That's it — no logins, no extra steps."
)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 How to Use", callback_data="show_help", style="primary")],
        [InlineKeyboardButton("📢 Our Channels", callback_data="show_channels", style="primary")],
    ])


def channel_list_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📢 {ch['username']}", url=ch["url"])] for ch in FORCE_JOIN_CHANNELS]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_home", style="danger")])
    return InlineKeyboardMarkup(rows)


def choice_keyboard(token: str, platform: str) -> InlineKeyboardMarkup:
    emoji = PLATFORM_EMOJI.get(platform, "🔗")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video", callback_data=f"dl_video:{token}", style="success"),
            InlineKeyboardButton("🎵 Song", callback_data=f"dl_audio:{token}", style="primary"),
        ],
        [InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{token}", style="danger")],
    ])


# ======================================================================================
#  SAFE SEND / EDIT HELPERS — never let a Telegram hiccup crash a handler
# ======================================================================================

async def safe_reply(message, text, **kwargs):
    try:
        return await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kwargs)
    except (BadRequest, TimedOut, NetworkError) as e:
        log.warning("safe_reply failed: %s", e)
        return None


async def safe_edit(query, text, **kwargs):
    try:
        return await query.edit_message_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            log.warning("safe_edit failed: %s", e)
    except (TimedOut, NetworkError) as e:
        log.warning("safe_edit failed: %s", e)


async def safe_popup(query, text, alert: bool = True):
    try:
        await query.answer(text=text, show_alert=alert)
    except TelegramError as e:
        log.warning("safe_popup failed: %s", e)


async def safe_react(bot, chat_id, message_id, emoji="🔥"):
    try:
        await bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji)])
    except TelegramError:
        pass  # reactions are a nice-to-have, never worth breaking the flow over


# ======================================================================================
#  COMMAND HANDLERS
# ======================================================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    KNOWN_USERS.add(user.id)

    joined = await check_membership(context.bot, user.id)
    if not joined:
        await safe_reply(update.message, FORCE_JOIN_TEXT, reply_markup=join_keyboard())
        return

    await safe_reply(update.message, WELCOME_TEXT, reply_markup=main_menu_keyboard())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await safe_reply(
        update.message,
        f"📊 <b>{BOT_NAME} — Live Stats</b>\n{DIVIDER}\n"
        f"👥 Users seen this session: <b>{len(KNOWN_USERS)}</b>\n"
        f"✅ Cached verified joins: <b>{len(JOIN_CACHE)}</b>",
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await safe_reply(update.message, "Usage: <code>/broadcast your message here</code>")
        return
    text = " ".join(context.args)
    sent, failed = 0, 0
    status = await safe_reply(update.message, f"📣 Broadcasting to {len(KNOWN_USERS)} users…")
    for uid in list(KNOWN_USERS):
        try:
            await context.bot.send_message(uid, f"📣 <b>Announcement</b>\n{DIVIDER}\n{text}", parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)  # gentle pacing to avoid hitting flood limits
    if status:
        await safe_edit_plain(status, f"✅ Broadcast done — sent: {sent}, failed: {failed}")


async def safe_edit_plain(message, text):
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


# ======================================================================================
#  LINK HANDLER — user pastes a YouTube / Instagram / Facebook link
# ======================================================================================

_token_counter = 0


def _new_token() -> str:
    global _token_counter
    _token_counter += 1
    return f"{int(time.time())}{_token_counter}"


async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    KNOWN_USERS.add(user.id)

    joined = await check_membership(context.bot, user.id)
    if not joined:
        await safe_reply(message, FORCE_JOIN_TEXT, reply_markup=join_keyboard())
        return

    text = message.text or ""

    if INSTAGRAM_AUDIO_PAGE.search(text):
        await safe_reply(
            message,
            "🎧 <b>That's a sound/audio page, not a single reel.</b>\n"
            f"{DIVIDER}\n"
            "Links shaped like <code>instagram.com/reels/audio/…</code> list every reel "
            "that uses that sound — they aren't one downloadable video, so this format "
            "isn't supported.\n\n"
            "Please open one specific reel that uses this audio and send <b>that</b> "
            "reel's link (looks like <code>instagram.com/reel/XXXXXXXXX</code>) instead.",
        )
        return

    platform, url = detect_platform(text)
    if not platform:
        await safe_reply(
            message,
            "🤔 <b>Link not recognized.</b>\n"
            f"{DIVIDER}\n"
            "I currently support short-form links from:\n"
            "▶️ YouTube Shorts\n📸 Instagram Reels\n📘 Facebook Reels/Videos\n\n"
            "Please send a valid link from one of these platforms.",
        )
        return

    # Quick reaction so the user instantly sees the bot registered their message
    await safe_react(context.bot, message.chat_id, message.message_id, "🔥")

    token = _new_token()
    PENDING_LINKS[token] = {"url": url, "platform": platform, "user_id": user.id}

    emoji = PLATFORM_EMOJI.get(platform, "🔗")
    await safe_reply(
        message,
        f"{emoji} <b>{platform} link detected!</b>\n"
        f"{DIVIDER}\n"
        "What would you like to download?",
        reply_markup=choice_keyboard(token, platform),
    )


# ======================================================================================
#  CALLBACK HANDLER — buttons: verify_join, show_help, show_channels, back_home,
#  dl_video:<token>, dl_audio:<token>, cancel:<token>
# ======================================================================================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Never act — not even a popup with real content — outside a private DM.
    # This covers groups, supergroups, and channels the bot may be an admin in.
    chat = query.message.chat if query.message else None
    if chat is not None and chat.type != "private":
        try:
            await query.answer()
        except TelegramError:
            pass
        return

    data = query.data or ""
    user = update.effective_user
    KNOWN_USERS.add(user.id)

    try:
        if data == "verify_join":
            await handle_verify_join(query, context)
        elif data == "show_help":
            await query.answer()
            await safe_edit(query, HELP_TEXT, reply_markup=main_menu_keyboard())
        elif data == "show_channels":
            await query.answer()
            await safe_edit(
                query,
                f"📢 <b>Our Official Channels</b>\n{DIVIDER}\nStay updated & get support 👇",
                reply_markup=channel_list_keyboard(),
            )
        elif data == "back_home":
            await query.answer()
            await safe_edit(query, WELCOME_TEXT, reply_markup=main_menu_keyboard())
        elif data.startswith("dl_video:") or data.startswith("dl_audio:"):
            await handle_download(query, context, data)
        elif data.startswith("cancel:"):
            token = data.split(":", 1)[1]
            PENDING_LINKS.pop(token, None)
            await safe_popup(query, "✖️ Cancelled", alert=False)
            await safe_edit(query, "✖️ <b>Cancelled.</b> Send me another link anytime!")
        else:
            await safe_popup(query, "⚠️ This action expired. Please try again.", alert=True)
    except Exception:
        log.error("callback_router crashed:\n%s", traceback.format_exc())
        await safe_popup(query, "⚠️ Something went wrong. Please try again.", alert=True)


async def handle_verify_join(query, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    JOIN_CACHE.pop(user_id, None)  # force a fresh check, not the cache
    joined = await check_membership(context.bot, user_id)
    if joined:
        await safe_popup(query, "✅ Verified! Welcome aboard 🎉", alert=True)
        await safe_edit(query, WELCOME_TEXT, reply_markup=main_menu_keyboard())
    else:
        await safe_popup(query, "❌ You haven't joined all channels yet. Please join & try again.", alert=True)


async def handle_download(query, context: ContextTypes.DEFAULT_TYPE, data: str):
    kind, token = data.split(":", 1)
    kind = "video" if kind == "dl_video" else "audio"

    entry = PENDING_LINKS.get(token)
    if not entry:
        await safe_popup(query, "⚠️ This link expired. Please send it again.", alert=True)
        return

    user_id = query.from_user.id
    joined = await check_membership(context.bot, user_id)
    if not joined:
        await safe_popup(query, "🔒 Please join our channels first.", alert=True)
        await safe_edit(query, FORCE_JOIN_TEXT, reply_markup=join_keyboard())
        return

    await safe_popup(query, "⚡ Starting download — best quality selected!", alert=False)

    url, platform = entry["url"], entry["platform"]
    label = "🎬 Video" if kind == "video" else "🎵 Song"
    emoji = PLATFORM_EMOJI.get(platform, "🔗")
    await safe_edit(
        query,
        f"{emoji} <b>{platform}</b> — {label}\n"
        f"{DIVIDER}\n"
        "⏳ Fetching your file at the best available quality…\n"
        "<i>This usually takes just a few seconds.</i>",
    )

    chat_id = query.message.chat_id
    action = ChatAction.UPLOAD_VIDEO if kind == "video" else ChatAction.UPLOAD_VOICE
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, action))

    filepath = None
    try:
        filepath, info = await run_download(kind, url, platform)

        size = os.path.getsize(filepath)
        if size > MAX_TG_FILE_BYTES:
            await safe_edit(
                query,
                f"⚠️ <b>File too large to send</b> ({human_size(size)}).\n"
                f"{DIVIDER}\nTelegram bots can only send files up to 50MB. "
                "Try a shorter clip or the audio-only option.",
            )
            return

        title = (info.get("title") or "Media")[:150]
        caption = (
            f"{emoji} <b>{title}</b>\n"
            f"{DIVIDER}\n"
            f"📦 {human_size(size)} • via <b>{BOT_NAME}</b> 👑"
        )

        with open(filepath, "rb") as f:
            if kind == "video":
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=InputFile(f, filename=os.path.basename(filepath)),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    write_timeout=120,
                    read_timeout=120,
                )
            else:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=InputFile(f, filename=os.path.basename(filepath)),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    title=title,
                    write_timeout=120,
                    read_timeout=120,
                )

        await safe_edit(
            query,
            f"✅ <b>Delivered!</b>\n{DIVIDER}\nEnjoy your {label.lower()} from {platform} 🎉\n"
            "Send another link anytime.",
        )

    except DownloadError as e:
        await safe_edit(
            query,
            f"❌ <b>Download failed</b>\n{DIVIDER}\n{str(e)}\n\nPlease try a different link.",
        )
    except (TimedOut, NetworkError):
        await safe_edit(
            query,
            f"⌛ <b>Upload timed out.</b>\n{DIVIDER}\nThe file may be large or your connection slow. Please try again.",
        )
    except Exception:
        log.error("handle_download crashed:\n%s", traceback.format_exc())
        await safe_edit(query, "⚠️ <b>Unexpected error.</b> Please try again in a moment.")
    finally:
        typing_task.cancel()
        PENDING_LINKS.pop(token, None)
        if filepath:
            shutil.rmtree(os.path.dirname(filepath), ignore_errors=True)


async def _keep_typing(bot, chat_id, action):
    try:
        while True:
            await bot.send_chat_action(chat_id, action)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except TelegramError:
        pass


# ======================================================================================
#  GLOBAL ERROR HANDLER — catches ANYTHING that slips past local try/excepts,
#  so the bot NEVER crashes and NEVER gets stuck for any user.
# ======================================================================================

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception: %s", context.error, exc_info=context.error)
    try:
        if isinstance(update, Update):
            if update.callback_query:
                await safe_popup(update.callback_query, "⚠️ Something went wrong. Please try again.", alert=True)
            elif update.effective_message:
                await safe_reply(update.effective_message, "⚠️ Something went wrong. Please try again.")
    except Exception:
        log.error("Error handler itself failed:\n%s", traceback.format_exc())


# ======================================================================================
#  ENTRY POINT
# ======================================================================================

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ BOT_TOKEN environment variable is missing. "
            "Set it in Railway → Variables before deploying."
        )

    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)   # handle many users' requests in parallel — fast under load
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(20)
        .build()
    )

    PRIVATE_ONLY = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", start_cmd, filters=PRIVATE_ONLY))
    app.add_handler(CommandHandler("stats", stats_cmd, filters=PRIVATE_ONLY))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd, filters=PRIVATE_ONLY))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & PRIVATE_ONLY, link_handler))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(global_error_handler)

    log.info("👑 %s is starting up…", BOT_NAME)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
