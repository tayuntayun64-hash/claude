import time
import json
import os
import requests
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ── CONFIG ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("MOTION_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "1038443084"))
KEYS_FILE       = "activation_keys.json"   # ← shared dengan NS Store Bot
HISTORY_CHANNEL_ID = os.getenv("MOTION_HISTORY_CHANNEL_ID") or os.getenv("HISTORY_CHANNEL_ID")

POLL_INTERVAL = 10
MAX_POLLS     = 60

# ── STATES ───────────────────────────────────────────────────────────────
(
    WAIT_IMAGE, WAIT_VIDEO, WAIT_PROMPT, WAIT_CFG,
    WAIT_APIKEY, WAIT_ACTIVATION
) = range(6)

# ── PRESETS ──────────────────────────────────────────────────────────────
PRESETS = {
    "🎬 Cinematic": "cinematic motion, natural lighting, smooth camera movement, realistic physics, high detail, film quality, stable face, smooth facial skin, no facial deformation",
    "🕺 Dance":     "smooth dance motion, energetic, precise body movement, natural rhythm, sharp detail, realistic, face preservation, stable facial features, no skin artifacts",
    "💼 Portrait":  "subtle natural movement, gentle breathing motion, realistic micro-movements, sharp focus, professional, smooth facial skin, face preservation, no wrinkles, stable face texture, minimal facial distortion",
    "🏃 Action":    "dynamic action motion, realistic physics, natural inertia, smooth transitions, high detail, stable face, no facial deformation, face preservation",
}

# Negative prompt default untuk semua generate — cegah kerutan & artifact wajah
DEFAULT_NEGATIVE_PROMPT = (
    "wrinkles, skin deformation, facial artifacts, distorted face, blurry face, "
    "aging effect, unnatural skin texture, face warping, skin creasing, "
    "face distortion, deformed features, ugly, low quality, artifacts"
)

# ── PLAN CONFIG ───────────────────────────────────────────────────────────
PLANS = {
    "free":      {"label": "Free",      "limit": 0},
    "1day":      {"label": "1 Hari",    "limit": 30},
    "3day":      {"label": "3 Hari",    "limit": 100},
    "7day":      {"label": "7 Hari",    "limit": 999999},
    "unlimited": {"label": "Unlimited", "limit": 999999},
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


# ── HISTORY AUDIT ─────────────────────────────────────────────────────────
def _user_label(user):
    if not user:
        return "-"
    username = f"@{user.username}" if user.username else "-"
    return f"{user.first_name or '-'} ({username}) | ID: {user.id}"

async def audit_event(ctx: ContextTypes.DEFAULT_TYPE, title: str, lines=None, user=None):
    if not HISTORY_CHANNEL_ID:
        return
    try:
        payload = [f"📋 Motion Bot — {title}", f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"]
        if user:
            payload.append(f"👤 {_user_label(user)}")
        if lines:
            payload.extend(str(line) for line in lines if line is not None)
        await ctx.bot.send_message(chat_id=HISTORY_CHANNEL_ID, text="\n".join(payload))
    except Exception as e:
        log.warning("Gagal kirim history Motion Bot: %s", e)


# ══════════════════════════════════════════════════════════════════════════
#  DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_user(user_id: int) -> dict:
    users = load_json("users.json", {})
    uid   = str(user_id)
    _is_new = uid not in users
    if _is_new:
        plan = "unlimited" if user_id == ADMIN_ID else "free"
        users[uid] = {
            "plan":      plan,
            "api_key":   "",
            "activated": True if user_id == ADMIN_ID else False,
            "used":      0,
            "joined":    datetime.now().isoformat(),
            "expire_ts": None,
        }
        save_json("users.json", users)
        result = dict(users[uid])
        result["_is_new"] = True
        return result
    else:
        if user_id == ADMIN_ID and users[uid].get("plan") != "unlimited":
            users[uid]["plan"]      = "unlimited"
            users[uid]["activated"] = True
            save_json("users.json", users)
    return users[uid]

def update_user(user_id: int, data: dict):
    users = load_json("users.json", {})
    uid   = str(user_id)
    if uid not in users:
        users[uid] = {}
    users[uid].update(data)
    save_json("users.json", users)

GENERATE_LOG_FILE = "generate_log.json"

def log_generate(user_id: int, username: str, resolusi: str):
    """Catat setiap generate ke log harian"""
    logs = load_json(GENERATE_LOG_FILE, [])
    logs.append({
        "user_id"  : str(user_id),
        "username" : username or "-",
        "resolusi" : resolusi,
        "waktu"    : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tanggal"  : datetime.now().strftime("%Y-%m-%d"),
    })
    save_json(GENERATE_LOG_FILE, logs)

def _esc(text: str) -> str:
    """Escape karakter Markdown spesial di konten dinamis."""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")

def get_laporan(tanggal: str = None) -> str:
    """Buat laporan generate per tanggal"""
    logs = load_json(GENERATE_LOG_FILE, [])
    if not logs:
        return "📊 Belum ada data generate."

    # Filter per tanggal kalau ada
    if tanggal:
        logs_filter = [l for l in logs if l.get("tanggal") == tanggal]
        judul = f"📊 *Laporan Generate — {tanggal}*"
    else:
        logs_filter = logs
        judul = "📊 *Laporan Generate — Semua Waktu*"

    if not logs_filter:
        return f"📊 Tidak ada data untuk tanggal {tanggal}."

    total     = len(logs_filter)
    total_720 = sum(1 for l in logs_filter if l.get("resolusi") == "720")
    total_1080= sum(1 for l in logs_filter if l.get("resolusi") == "1080")

    # Per user
    user_count = {}
    for l in logs_filter:
        key = f"{_esc(l.get('username', '-'))} ({l.get('user_id', '-')})"
        user_count[key] = user_count.get(key, 0) + 1

    per_user = "\n".join([f"  \u2022 {u}: {c}x" for u, c in sorted(user_count.items(), key=lambda x: -x[1])])

    # Per tanggal (kalau laporan semua)
    if not tanggal:
        date_count = {}
        for l in logs_filter:
            d = l.get("tanggal", "-")
            date_count[d] = date_count.get(d, 0) + 1
        per_tanggal = "\n".join([f"  • {d}: {c}x" for d, c in sorted(date_count.items(), reverse=True)[:10]])
        tanggal_section = f"\n\n📅 *Per tanggal (10 terakhir):*\n{per_tanggal}"
    else:
        tanggal_section = ""

    return (
        f"{judul}\n\n"
        f"🎬 Total generate: *{total}x*\n"
        f"📱 720p: {total_720}x\n"
        f"🖥 1080p: {total_1080}x\n"
        f"{tanggal_section}\n\n"
        f"👥 *Per user:*\n{per_user}"
    )



def get_keys() -> dict:
    return load_json(KEYS_FILE, {})

def save_keys(keys: dict):
    save_json(KEYS_FILE, keys)

def redeem_key(key: str):
    keys = get_keys()
    if key not in keys:
        return None, None
    entry = keys[key]
    if entry.get("used"):
        return None, None
    expire_ts = entry.get("expire_ts")
    if expire_ts and datetime.now().timestamp() > expire_ts:
        return None, None
    keys[key]["used"]    = True
    keys[key]["used_at"] = datetime.now().isoformat()
    save_keys(keys)
    return entry["plan"], expire_ts

def is_expired(user_data: dict) -> bool:
    if user_data.get("plan") in ("free", "unlimited"):
        return False
    expire_ts = user_data.get("expire_ts")
    if not expire_ts:
        return False
    return datetime.now().timestamp() > expire_ts

def check_quota(user_id: int):
    udata = get_user(user_id)
    if user_id == ADMIN_ID:
        return True, None
    if not udata.get("activated"):
        return False, (
            "⚠️ Akun kamu *belum diaktivasi*!\n\n"
            "Beli paket di *NS Store Bot* lalu masukkan kode aktivasi di menu *🎫 Aktivasi Plan*."
        )
    if is_expired(udata):
        return False, (
            "⏰ *Akses kamu sudah habis masa berlakunya!*\n\n"
            "Beli paket baru di *NS Store Bot* untuk perpanjang akses."
        )
    return True, None


# ══════════════════════════════════════════════════════════════════════════
#  FREEPIK HELPERS
# ══════════════════════════════════════════════════════════════════════════

def get_file_url(bot_token, file_id):
    r = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getFile",
        params={"file_id": file_id}, timeout=30
    )
    r.raise_for_status()
    path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{bot_token}/{path}"

def check_apikey(api_key: str):
    """Cek validitas dan kredit API key Freepik. Return (valid, kredit, pesan)"""
    # Daftar endpoint yang dicoba secara berurutan
    endpoints = [
        "https://api.freepik.com/v1/ai/balance",
        "https://api.freepik.com/v1/ai/account",
        "https://api.freepik.com/v1/profile",
    ]
    headers = {"x-freepik-api-key": api_key}

    for endpoint in endpoints:
        try:
            r = requests.get(endpoint, headers=headers, timeout=15)
            if r.status_code == 401:
                return False, 0, "❌ API Key tidak valid. Pastikan sudah copy dengan benar dari dashboard Freepik."
            if r.status_code == 429:
                return False, 0, "⚠️ Terlalu banyak request. Coba lagi sebentar."
            if r.status_code == 404:
                # Endpoint ini tidak ada, coba endpoint berikutnya
                continue
            if r.status_code != 200:
                continue  # Coba endpoint berikutnya
            data = r.json()
            # Ambil nilai kredit — struktur response bisa berbeda tiap endpoint
            remaining = (
                data.get("remaining")
                or data.get("credits")
                or data.get("balance")
                or data.get("data", {}).get("remaining")
                or data.get("data", {}).get("credits")
                or data.get("data", {}).get("balance")
                or 0
            )
            return True, remaining, ""
        except Exception as e:
            log.warning(f"check_apikey error on {endpoint}: {e}")
            continue

    # Semua endpoint gagal — coba validasi dengan hit endpoint generate (dry check)
    # Kalau dapat 401 berarti key salah, selain itu key kemungkinan valid
    try:
        r = requests.get(
            "https://api.freepik.com/v1/ai/video/kling-v2-6-motion-control-pro",
            headers=headers,
            timeout=15
        )
        if r.status_code == 401:
            return False, 0, "❌ API Key tidak valid. Pastikan sudah copy dengan benar dari dashboard Freepik."
        # Status lain (400, 405, 422, dll) berarti key valid tapi endpoint butuh parameter
        return True, "N/A", ""
    except Exception as e:
        return False, 0, f"❌ Gagal cek API Key: {str(e)}"


def submit_motion_task(image_url, video_url, prompt, cfg, api_key, resolusi="1080", negative_prompt=None):
    if resolusi == "720":
        endpoint = "https://api.freepik.com/v1/ai/video/kling-v2-6-motion-control-std"
    else:
        endpoint = "https://api.freepik.com/v1/ai/video/kling-v2-6-motion-control-pro"
    payload  = {
        "image_url": image_url,
        "video_url": video_url,
        "character_orientation": "video",
        "cfg_scale": cfg,
        "negative_prompt": negative_prompt or DEFAULT_NEGATIVE_PROMPT,
    }
    if prompt:
        payload["prompt"] = prompt[:2500]
    headers = {
        "x-freepik-api-key": api_key,
        "Content-Type": "application/json",
    }
    r = requests.post(endpoint, json=payload, headers=headers, timeout=60)
    print(f"Submit: {r.status_code} | {r.text[:300]}")
    if r.status_code == 401:
        raise Exception("API Key tidak valid atau sudah expired.")
    if r.status_code == 429:
        raise Exception("Limit API Key habis! Upgrade plan Freepik kamu.")
    r.raise_for_status()
    data = r.json()
    return data.get("task_id") or data.get("data", {}).get("task_id")

def get_status_endpoint(task_id: str, resolusi: str = "1080") -> str:
    """Tentukan endpoint status sesuai resolusi yang dipakai saat submit."""
    # Freepik motion-control: endpoint GET status ada di /v1/ai/video/{model}/{task_id}
    # Fallback: /v1/ai/image-to-video/kling-v2-6/{task_id} (endpoint universal)
    if resolusi == "720":
        return f"https://api.freepik.com/v1/ai/video/kling-v2-6-motion-control-std/{task_id}"
    return f"https://api.freepik.com/v1/ai/video/kling-v2-6-motion-control-pro/{task_id}"

STATUS_FALLBACK_ENDPOINTS = [
    "https://api.freepik.com/v1/ai/video/kling-v2-6-motion-control-pro/{task_id}",
    "https://api.freepik.com/v1/ai/video/kling-v2-6-motion-control-std/{task_id}",
    "https://api.freepik.com/v1/ai/image-to-video/kling-v2-6/{task_id}",
    "https://api.freepik.com/v1/ai/image-to-video/kling-v2/{task_id}",
]

def resolve_status_endpoint(task_id: str, api_key: str, resolusi: str = "1080") -> str:
    """Coba endpoint satu per satu, return endpoint pertama yang response 200/non-404."""
    primary = get_status_endpoint(task_id, resolusi)
    candidates = [primary] + [
        e.format(task_id=task_id) for e in STATUS_FALLBACK_ENDPOINTS
        if e.format(task_id=task_id) != primary
    ]
    headers = {"x-freepik-api-key": api_key}
    for ep in candidates:
        try:
            r = requests.get(ep, headers=headers, timeout=15)
            print(f"Trying endpoint {ep} -> {r.status_code}")
            if r.status_code == 404:
                continue
            return ep  # Endpoint valid (bisa 200, 401, 429, dll)
        except Exception as e:
            print(f"Endpoint probe error {ep}: {e}")
            continue
    return primary  # Fallback ke primary kalau semua gagal

def poll_until_done(task_id, api_key, resolusi="1080"):
    endpoint = resolve_status_endpoint(task_id, api_key, resolusi)
    log.info(f"Polling endpoint: {endpoint}")
    headers  = {"x-freepik-api-key": api_key}
    for i in range(MAX_POLLS):
        time.sleep(POLL_INTERVAL)
        try:
            r = requests.get(endpoint, headers=headers, timeout=30)
            print(f"[{i}] {r.status_code} | {r.text[:300]}")
            if r.status_code == 404:
                # Endpoint berubah? Coba resolve ulang
                endpoint = resolve_status_endpoint(task_id, api_key, resolusi)
                continue
            r.raise_for_status()
            result = r.json()
        except Exception as e:
            print(f"Polling error: {e}")
            continue
        data   = result.get("data", result)
        status = str(data.get("status", "")).upper()
        print(f"Status: {status}")
        if status == "COMPLETED":
            generated = data.get("generated", [])
            if generated:
                item = generated[0]
                if isinstance(item, dict):
                    return item.get("url") or item.get("video_url") or item.get("src")
                return item
            return None
        if status in ("FAILED", "ERROR", "CANCELLED"):
            return None
    return None

def check_task_status(task_id, api_key, resolusi="1080"):
    """Cek status task dan return (status, video_url, progress_pct, reason)"""
    endpoint = resolve_status_endpoint(task_id, api_key, resolusi)
    headers  = {"x-freepik-api-key": api_key}
    try:
        r = requests.get(endpoint, headers=headers, timeout=30)
        if r.status_code == 401:
            return "FAILED", None, 0, "API Key tidak valid atau sudah expired."
        if r.status_code == 429:
            return "FAILED", None, 0, "Limit API Key habis! Upgrade plan Freepik kamu."
        if r.status_code == 404:
            return "ERROR", None, 0, f"Endpoint tidak ditemukan ({endpoint}). Hubungi admin."
        r.raise_for_status()
        result = r.json()
        data   = result.get("data", result)
        status = str(data.get("status", "")).upper()
        progress = data.get("progress", 0) or 0
        reason = data.get("error_message") or data.get("message") or ""
        if status == "COMPLETED":
            generated = data.get("generated", [])
            if generated:
                item = generated[0]
                if isinstance(item, dict):
                    video_url = item.get("url") or item.get("video_url") or item.get("src")
                else:
                    video_url = item
                return "COMPLETED", video_url, 100, ""
            return "COMPLETED", None, 100, ""
        if status in ("FAILED", "ERROR", "CANCELLED"):
            if not reason:
                reason = "Proses gagal di server Freepik. Coba ulangi."
            return "FAILED", None, 0, reason
        return "PROCESSING", None, int(progress), ""
    except Exception as e:
        print(f"Check status error: {e}")
        return "ERROR", None, 0, str(e)

def parse_submit_error(r) -> str:
    try:
        j = r.json()
        return j.get("message") or j.get("error") or j.get("detail") or r.text[:200]
    except Exception:
        return r.text[:200]

def progress_bar(pct: int) -> str:
    filled = int(pct / 10)
    empty  = 10 - filled
    return "▓" * filled + "░" * empty


# ══════════════════════════════════════════════════════════════════════════
#  KEYBOARD & TEXT HELPERS
# ══════════════════════════════════════════════════════════════════════════

def main_menu_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔑 Setup API Key",        callback_data="setup"),
            InlineKeyboardButton("🎫 Aktivasi Plan",        callback_data="activate"),
        ],
        [InlineKeyboardButton("✨ Generate Motion Control", callback_data="generate")],
        [
            InlineKeyboardButton("📖 Cara Pakai",           callback_data="carapakai"),
            InlineKeyboardButton("ℹ️ Info Penting",         callback_data="info"),
        ],
        [
            InlineKeyboardButton("💰 Cek Kredit API",       callback_data="cekkredit"),
            InlineKeyboardButton("🔄 Refresh",              callback_data="refresh"),
        ],
    ])

def back_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="back")
    ]])

def build_welcome_text(user) -> str:
    data      = get_user(user.id)
    plan      = data.get("plan", "free")
    used      = data.get("used", 0)
    lim       = PLANS[plan]["limit"]
    remaining = max(0, lim - used)
    act       = "✅ Aktif" if data.get("activated") else "❌ Belum aktivasi"
    api  = "✅ Tersimpan" if data.get("api_key") else "❌ Belum diset"
    name      = user.username or user.first_name or "User"

    expire_ts = data.get("expire_ts")
    if expire_ts and plan not in ("free", "unlimited"):
        expire_dt = datetime.fromtimestamp(expire_ts)
        if datetime.now().timestamp() < expire_ts:
            sisa = expire_dt - datetime.now()
            jam  = int(sisa.total_seconds() // 3600)
            expire_info = f"⏳ Expire: {expire_dt.strftime('%d/%m/%Y %H:%M')} (sisa {jam} jam)"
        else:
            expire_info = "⏰ Expire: *HABIS* — beli paket baru di NS Store Bot"
    else:
        expire_info = ""

    return (
        f"🎬 *NS MOTION CONTROL*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Halo, *{name}*! 👋\n\n"
        f"📋 Plan       → {PLANS[plan]['label']}\n"
        f"🔑 API Key    → {api}\n"
        f"🎞 Generate  → {used} video\n"
        + (f"⏳ Expire     → {expire_info}\n" if expire_info else f"⏳ Expire     → Tidak terbatas\n")
        + f"\n━━━━━━━━━━━━━━━━━━━━"
    )


# ══════════════════════════════════════════════════════════════════════════
#  /start  &  CALLBACK QUERY
# ══════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    udata = get_user(uid)

    # Blok jika ada task aktif
    active_task = udata.get("active_task")
    if active_task:
        refresh_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Cek Progress", callback_data=f"cekprogress_{active_task}")
        ]])
        await update.message.reply_text(
            "⏳ *Kamu masih punya task yang sedang diproses!*\n\n"
            "Tunggu sampai selesai dulu sebelum generate lagi.\n"
            "Tekan tombol di bawah untuk cek status:",
            parse_mode="Markdown",
            reply_markup=refresh_kb
        )
        return

    udata_fresh = get_user(uid)
    if udata_fresh.pop("_is_new", False):
        await audit_event(ctx, "👤 User Baru", [
            "Bergabung pertama kali ke Motion Bot."
        ], user=update.effective_user)

    await update.message.reply_text(
        build_welcome_text(update.effective_user),
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    user  = query.from_user

    if data.startswith("genkey_"):
        if user.id != ADMIN_ID:
            await query.answer("⛔ Bukan admin.", show_alert=True)
            return
        durasi = data.replace("genkey_", "")
        await query.answer()
        await _do_genkey(query.message, durasi)
        return

    if data.startswith("laporan_"):
        if user.id != ADMIN_ID:
            await query.answer("⛔ Bukan admin.", show_alert=True)
            return
        tgl = None if data == "laporan_all" else data.replace("laporan_", "")
        laporan = get_laporan(tgl)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Semua waktu", callback_data="laporan_all")],
            [InlineKeyboardButton(f"📆 Hari ini ({datetime.now().strftime('%d/%m/%Y')})", callback_data=f"laporan_{datetime.now().strftime('%Y-%m-%d')}")],
        ])
        await query.edit_message_text(laporan, parse_mode="Markdown", reply_markup=kb)
        return

    if data.startswith("cekprogress_"):
        task_id = data.replace("cekprogress_", "")
        udata   = get_user(user.id)
        api_key = udata.get("api_key", "")
        resolusi_task = udata.get("active_resolusi", "1080")
        status, video_url, pct, reason = check_task_status(task_id, api_key, resolusi_task)
        bar = progress_bar(pct)

        refresh_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Cek Progress", callback_data=f"cekprogress_{task_id}")
        ]])

        if status == "COMPLETED" and video_url:
            update_user(user.id, {"used": udata.get("used", 0) + 1, "active_task": None, "active_resolusi": None})
            log_generate(user.id, user.username or user.first_name, ctx.user_data.get("resolusi", "1080"))
            await audit_event(ctx, "✅ Generate Selesai", [
                f"🆔 Task ID : {task_id}",
                f"🎞 Resolusi: {udata.get('active_resolusi', '?')}p",
                f"📊 Total generate user: {udata.get('used', 0) + 1}x",
            ], user=user)
            await query.edit_message_text(
                f"✅ *Video selesai! Sedang mengirim...*\n\n"
                f"⏱ Progress: {bar} 100%",
                parse_mode="Markdown"
            )
            try:
                await query.message.reply_video(
                    video=video_url,
                    caption="✅ *Video selesai!*\n\nMau generate lagi? Ketik /start 🚀",
                    parse_mode="Markdown"
                )
            except Exception:
                await query.message.reply_text(
                    f"✅ *Video selesai!*\n\n"
                    f"🔗 [Klik untuk download]({video_url})\n\n"
                    f"Mau generate lagi? Ketik /start 🚀",
                    parse_mode="Markdown"
                )
        elif status in ("FAILED", "ERROR"):
            update_user(user.id, {"active_task": None})
            await audit_event(ctx, "❌ Generate Gagal", [
                f"🆔 Task ID  : {task_id}",
                f"⚠️ Penyebab : {reason or 'Tidak diketahui'}",
                f"📊 Progress : {pct}%",
            ], user=user)
            reason_text = f"\n\n⚠️ *Penyebab:* {reason}" if reason else ""
            await query.edit_message_text(
                f"❌ *Gagal generate video.*{reason_text}\n\n"
                f"⏱ Progress: {bar} {pct}%\n\n"
                f"Coba lagi dengan /start",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"⏳ *Masih memproses...*\n\n"
                f"⏱ Progress: {bar} {pct}%\n\n"
                f"Tekan tombol lagi untuk update:",
                parse_mode="Markdown",
                reply_markup=refresh_kb
            )
        return

    if data in ("back", "refresh"):
        await query.edit_message_text(
            build_welcome_text(user),
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )
        return

    if data == "cekkredit":
        udata   = get_user(user.id)
        api_key = udata.get("api_key", "")
        if not api_key:
            await query.edit_message_text(
                "⚠️ Kamu belum setup API Key!\n\nKlik *Setup API Key* terlebih dahulu.",
                parse_mode="Markdown",
                reply_markup=back_kb()
            )
            return
        await query.edit_message_text(
            "🔍 *Sedang cek kredit...*",
            parse_mode="Markdown"
        )
        valid, remaining, err_msg = check_apikey(api_key)
        if not valid:
            await query.edit_message_text(
                f"❌ *API Key bermasalah!*\n\n{err_msg}\n\nSilakan setup ulang API Key.",
                parse_mode="Markdown",
                reply_markup=back_kb()
            )
            return
        if float(remaining) == 0:
            status_kredit = "🔴 *Kredit HABIS!* Top up di https://www.freepik.com/api/pricing"
        elif float(remaining) < 1:
            status_kredit = "🟡 *Kredit hampir habis!* Segera top up."
        else:
            status_kredit = "🟢 *Kredit tersedia*"
        await query.edit_message_text(
            f"💰 *Info Kredit API Freepik*\n\n"
            f"{status_kredit}\n"
            f"💵 Sisa kredit: *{remaining} EUR*\n\n"
            f"📊 *Estimasi video yang bisa dibuat:*\n"
            f"• 720p 5 detik ≈ {round(float(remaining)/0.25)} video\n"
            f"• 1080p 5 detik ≈ {round(float(remaining)/0.375)} video\n\n"
            f"🔑 API Key: `{api_key[:10]}...{api_key[-5:]}`",
            parse_mode="Markdown",
            reply_markup=back_kb()
        )
        return

    if data == "setup":
        ctx.user_data["state"] = WAIT_APIKEY
        await query.edit_message_text(
            "🔑 *Setup API Key Freepik*\n\n"
            "Kirimkan API Key Freepik kamu.\n"
            "Dapatkan di: https://www.freepik.com/api\n\n"
            "_Ketik /cancel untuk batal_",
            parse_mode="Markdown",
            reply_markup=back_kb()
        )
        return

    if data == "activate":
        ctx.user_data["state"] = WAIT_ACTIVATION
        await query.edit_message_text(
            "🎫 *Aktivasi Plan*\n\n"
            "Kirimkan kode aktivasi dari *NS Store Bot*:\n\n"
            "_Ketik /cancel untuk batal_",
            parse_mode="Markdown",
            reply_markup=back_kb()
        )
        return

    if data == "generate":
        udata = get_user(user.id)
        if not udata.get("api_key"):
            await query.edit_message_text(
                "⚠️ Kamu belum setup API Key!\n\nKlik *Setup API Key* terlebih dahulu.",
                parse_mode="Markdown",
                reply_markup=back_kb()
            )
            return

        boleh, pesan = check_quota(user.id)
        if not boleh:
            await query.edit_message_text(
                pesan,
                parse_mode="Markdown",
                reply_markup=back_kb()
            )
            return

        ctx.user_data.clear()
        ctx.user_data["generating"] = True
        await query.message.reply_text(
            "🖼️ *Langkah 1/4* — Kirim gambar karakter.\n\n"
            "💡 *Tips penting:*\n"
            "• Kirim sebagai *file/dokumen* (bukan foto langsung) agar kualitas tidak turun\n"
            "• Gunakan foto wajah jelas, pencahayaan bagus\n"
            "• Format JPG atau PNG",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if data == "carapakai":
        await query.edit_message_text(
            "📖 *Cara Pakai*\n\n"
            "*1. Beli paket di NS Store Bot*\n"
            "• Pilih paket 1, 3, atau 7 hari\n"
            "• Bayar via QRIS\n"
            "• Dapat kode aktivasi\n\n"
            "*2. Setup API Key Freepik:*\n"
            "• Daftar di freepik.com\n"
            "• Ambil API Key dari dashboard\n"
            "• Klik Setup API Key & paste key-nya\n\n"
            "*3. Aktivasi Plan:*\n"
            "• Klik Aktivasi Plan\n"
            "• Masukkan kode dari NS Store Bot\n\n"
            "*4. Generate Video:*\n"
            "• Klik Generate Motion Control\n"
            "• Kirim gambar karakter\n"
            "• Kirim video referensi gerak\n"
            "• Pilih preset / tulis prompt\n"
            "• Pilih CFG Scale & tunggu hasil!",
            parse_mode="Markdown",
            reply_markup=back_kb()
        )
        return

    if data == "info":
        await query.edit_message_text(
            "ℹ️ *Info Penting*\n\n"
            "• Bot ini menggunakan API Freepik Kling v2.6\n"
            "• Setiap user menggunakan API Key sendiri\n"
            "• Proses generate ~3-5 menit\n"
            "• Hasil video resolusi 1080p\n\n"
            "📦 *Paket (beli di NS Store Bot):*\n"
            "• 1 Hari — 30 generate\n"
            "• 3 Hari — 100 generate\n"
            "• 7 Hari — Unlimited generate\n\n"
            "• Kode aktivasi hanya bisa dipakai 1x\n"
            "• Akses otomatis expire sesuai durasi paket",
            parse_mode="Markdown",
            reply_markup=back_kb()
        )
        return


# ══════════════════════════════════════════════════════════════════════════
#  CONVERSATION — Generate Motion (entry via ctx.user_data["generating"])
# ══════════════════════════════════════════════════════════════════════════

async def got_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Hanya proses jika user sedang dalam flow generate
    if not ctx.user_data.get("generating") and ctx.user_data.get("step") not in (None, "wait_image"):
        return

    # Kalau sudah di step lain, abaikan
    current_step = ctx.user_data.get("step", "wait_image")
    if current_step not in ("wait_image", None) and not ctx.user_data.get("generating"):
        return

    if not ctx.user_data.get("generating") and current_step != "wait_image":
        return

    photo    = update.message.photo
    document = update.message.document
    if photo:
        file_id = photo[-1].file_id
    elif document and document.mime_type and document.mime_type.startswith("image"):
        file_id = document.file_id
    else:
        await update.message.reply_text("⚠️ Kirim gambar ya! (JPG/PNG)")
        return

    ctx.user_data["image_url"] = get_file_url(TELEGRAM_TOKEN, file_id)
    ctx.user_data["step"]      = "wait_video"
    ctx.user_data["generating"] = True
    await update.message.reply_text(
        "🎥 *Langkah 2/4* — Kirim video referensi gerak.\n\n"
        "_Tips: Video MP4 maksimal 30 detik, gerakan jelas_",
        parse_mode="Markdown"
    )

async def got_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("generating") or ctx.user_data.get("step") != "wait_video":
        return

    video = update.message.video or update.message.document
    if not video:
        await update.message.reply_text("⚠️ Kirim file video MP4 ya!")
        return

    ctx.user_data["video_url"] = get_file_url(TELEGRAM_TOKEN, video.file_id)
    ctx.user_data["step"]      = "wait_prompt"

    from telegram import ReplyKeyboardMarkup as RKM
    keyboard = [[p] for p in PRESETS.keys()] + [["✏️ Tulis Sendiri"], ["⏭️ Skip"]]
    await update.message.reply_text(
        "✏️ *Langkah 3/4* — Pilih preset prompt atau tulis sendiri:",
        parse_mode="Markdown",
        reply_markup=RKM(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )

async def got_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("generating") or ctx.user_data.get("step") != "wait_prompt":
        # Tidak dalam flow generate, teruskan ke text_router
        await text_router(update, ctx)
        return

    text = update.message.text
    if text == "⏭️ Skip":
        ctx.user_data["prompt"] = ""
        await ask_cfg(update, ctx)
    elif text == "✏️ Tulis Sendiri":
        ctx.user_data["step"] = "wait_custom_prompt"
        await update.message.reply_text(
            "✏️ Ketik prompt kamu (dalam bahasa Inggris):",
            reply_markup=ReplyKeyboardRemove()
        )
    elif text in PRESETS:
        ctx.user_data["prompt"] = PRESETS[text]
        await ask_cfg(update, ctx)
    else:
        ctx.user_data["prompt"] = text
        await ask_cfg(update, ctx)

async def got_custom_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("generating") or ctx.user_data.get("step") != "wait_custom_prompt":
        await text_router(update, ctx)
        return
    ctx.user_data["prompt"] = update.message.text
    await ask_cfg(update, ctx)

async def ask_cfg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from telegram import ReplyKeyboardMarkup as RKM
    keyboard = [
        ["0.3 — Lebih Kreatif"],
        ["0.4 — Portrait/Wajah ⭐"],
        ["0.5 — Seimbang"],
        ["0.7 — Lebih Presisi"],
    ]
    ctx.user_data["step"] = "wait_cfg"
    await update.message.reply_text(
        "⚙️ *Langkah 4/5* — Pilih kekuatan prompt:\n\n"
        "• *0.3* — AI lebih bebas berkreasi\n"
        "• *0.4* — Optimal untuk wajah & portrait ⭐\n"
        "• *0.5* — Seimbang (umum)\n"
        "• *0.7* — Ikuti prompt lebih ketat",
        parse_mode="Markdown",
        reply_markup=RKM(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )

async def got_cfg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("generating") or ctx.user_data.get("step") != "wait_cfg":
        await text_router(update, ctx)
        return

    text = update.message.text
    if "0.3" in text:
        ctx.user_data["cfg"] = 0.3
    elif "0.4" in text:
        ctx.user_data["cfg"] = 0.4
    elif "0.7" in text:
        ctx.user_data["cfg"] = 0.7
    else:
        ctx.user_data["cfg"] = 0.5
    await ask_resolusi(update, ctx)

async def ask_resolusi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from telegram import ReplyKeyboardMarkup as RKM
    keyboard = [["🎬 1080p — Kualitas Pro"], ["📱 720p — Lebih Hemat Kredit"]]
    ctx.user_data["step"] = "wait_resolusi"
    await update.message.reply_text(
        "🎞 *Langkah 5/5* — Pilih resolusi video:\n\n"
        "• *1080p* — Kualitas tinggi, lebih banyak kredit\n"
        "• *720p* — Hemat kredit, kualitas bagus",
        parse_mode="Markdown",
        reply_markup=RKM(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )

async def got_resolusi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("generating") or ctx.user_data.get("step") != "wait_resolusi":
        await text_router(update, ctx)
        return
    text = update.message.text
    if "720" in text:
        ctx.user_data["resolusi"] = "720"
    else:
        ctx.user_data["resolusi"] = "1080"
    await do_generate(update, ctx)

async def do_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    udata    = get_user(uid)
    prompt   = ctx.user_data.get("prompt", "")
    cfg      = ctx.user_data.get("cfg", 0.5)
    resolusi = ctx.user_data.get("resolusi", "1080")
    api_key  = udata.get("api_key", "")

    boleh, pesan = check_quota(uid)
    if not boleh:
        await update.message.reply_text(pesan, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        ctx.user_data.clear()
        return

    msg = await update.message.reply_text(
        f"⏳ *Sedang submit task...*\n\n"
        f"📝 Prompt: `{prompt[:50] if prompt else 'Tidak ada'}`\n"
        f"⚙️ CFG Scale: `{cfg}`\n"
        f"🎞 Resolusi: `{resolusi}p`\n\n"
        f"Mohon tunggu sebentar...",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )

    try:
        task_id = submit_motion_task(
            ctx.user_data["image_url"],
            ctx.user_data["video_url"],
            prompt, cfg, api_key, resolusi,
            negative_prompt=DEFAULT_NEGATIVE_PROMPT,
        )
        if not task_id:
            await msg.edit_text("❌ Gagal submit task. Coba /start lagi.")
            ctx.user_data.clear()
            return

        # Simpan active_task di user data
        update_user(uid, {"active_task": task_id, "active_resolusi": resolusi})
        await audit_event(ctx, "🎬 Generate Dimulai", [
            f"🆔 Task ID  : {task_id}",
            f"📝 Prompt   : {prompt[:60] if prompt else 'Tidak ada'}",
            f"⚙️ CFG Scale: {cfg}",
            f"🎞 Resolusi : {resolusi}p",
        ], user=update.effective_user)

        refresh_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Cek Progress", callback_data=f"cekprogress_{task_id}")
        ]])

        teks = (
            f"✅ *Task berhasil dikirim!*\n\n"
            f"📝 Prompt: `{prompt[:50] if prompt else 'Tidak ada'}`\n"
            f"⚙️ CFG Scale: `{cfg}`\n\n"
            f"⏱ Progress: ░░░░░░░░░░ 0%\n\n"
            f"Proses ~3-5 menit. Tekan tombol di bawah untuk cek status:"
        )
        try:
            await msg.edit_text(teks, parse_mode="Markdown", reply_markup=refresh_kb)
        except Exception:
            await update.message.reply_text(teks, parse_mode="Markdown", reply_markup=refresh_kb)
        ctx.user_data.clear()

    except Exception as e:
        log.error(f"Error generate: {e}")
        teks_err = (
            f"❌ *Gagal submit task!*\n\n"
            f"⚠️ *Penyebab:* `{str(e)}`\n\n"
            f"Coba /start lagi."
        )
        try:
            await msg.edit_text(teks_err, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(teks_err, parse_mode="Markdown")
        ctx.user_data.clear()


# ══════════════════════════════════════════════════════════════════════════
#  TEXT ROUTER — API Key, Activation, default fallback
# ══════════════════════════════════════════════════════════════════════════

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get("state")
    text  = update.message.text.strip()

    if state == WAIT_APIKEY:
        ctx.user_data.pop("state", None)
        api_key = text

        # Kasih tahu sedang cek
        msg = await update.message.reply_text(
            "🔍 *Sedang memverifikasi API Key...*\n\nMohon tunggu sebentar.",
            parse_mode="Markdown"
        )

        valid, remaining, err_msg = check_apikey(api_key)

        if not valid:
            await msg.edit_text(
                f"*Verifikasi API Key Gagal*\n\n"
                f"{err_msg}\n\n"
                f"💡 *Cara mendapatkan API Key yang benar:*\n"
                f"1. Buka https://www.freepik.com/api\n"
                f"2. Login ke akun Freepik kamu\n"
                f"3. Buka menu *Dashboard → API Keys*\n"
                f"4. Copy API Key dan kirim ulang ke sini\n\n"
                f"_Ketik /start untuk kembali ke menu_",
                parse_mode="Markdown"
            )
            return

        # Valid — simpan dan tampilkan info kredit
        update_user(update.effective_user.id, {"api_key": api_key})
        await audit_event(ctx, "⚙️ Setup API Key", [
            f"💰 Kredit tersisa: {remaining} EUR",
        ], user=update.effective_user)

        # Format info kredit
        if isinstance(remaining, float) or isinstance(remaining, int):
            kredit_info = f"💰 *Kredit tersisa:* {remaining} EUR"
            if float(remaining) == 0:
                kredit_info += "\n⚠️ *Kredit kamu sudah habis!* Top up di https://www.freepik.com/api/pricing"
            elif float(remaining) < 1:
                kredit_info += "\n⚠️ Kredit hampir habis, segera top up!"
        else:
            kredit_info = f"💰 *Kredit tersisa:* {remaining}"

        await msg.edit_text(
            f"✅ *API Key Valid & Berhasil Disimpan!*\n\n"
            f"{kredit_info}\n\n"
            f"📊 *Info kredit Kling Motion Control v2.6:*\n"
            f"• 720p Standard = 50 kredit/detik\n"
            f"• 1080p Pro = 75 kredit/detik\n"
            f"• Video 5 detik 720p ≈ 0.25 EUR\n"
            f"• Video 5 detik 1080p ≈ 0.375 EUR\n\n"
            f"Ketik /start untuk mulai generate! 🚀",
            parse_mode="Markdown"
        )
        return

    if state == WAIT_ACTIVATION:
        uid = update.effective_user.id

        # ADMIN BYPASS
        if uid == ADMIN_ID:
            ctx.user_data.pop("state", None)
            update_user(uid, {
                "plan":      "unlimited",
                "activated": True,
                "used":      0,
                "expire_ts": None,
            })
            await update.message.reply_text(
                "👑 *Admin bypass aktivasi!*\n\n"
                "📦 Plan: *Unlimited* — Tidak expire\n\n"
                "Ketik /start untuk ke menu.",
                parse_mode="Markdown"
            )
            return

        # USER BIASA
        plan, expire_ts = redeem_key(text.upper())
        if plan:
            ctx.user_data.pop("state", None)
            update_user(uid, {
                "plan":      plan,
                "activated": True,
                "used":      0,
                "expire_ts": expire_ts,
            })
            expire_str = (
                datetime.fromtimestamp(expire_ts).strftime("%d/%m/%Y %H:%M")
                if expire_ts else "-"
            )
            await audit_event(ctx, "🔑 Aktivasi Key", [
                f"📦 Plan    : {plan}",
                f"📅 Expire  : {expire_str}",
                f"🎫 Kode    : {text.upper()}",
            ], user=update.effective_user)
            await update.message.reply_text(
                "🎉 *Aktivasi berhasil!*\n\n"
                f"📦 Paket aktif hingga: *{expire_str}*\n\n"
                "Sekarang masukkan API Key Freepik kamu untuk mulai generate video! 🚀",
                parse_mode="Markdown"
            )
            ctx.user_data["state"] = WAIT_APIKEY
            await update.message.reply_text(
                "🔑 *Setup API Key*\n\n"
                "Kirim API Key Freepik kamu sekarang:\n\n"
                "💡 *Cara dapat API Key:*\n"
                "1. Buka https://www.freepik.com/api\n"
                "2. Login ke akun Freepik kamu\n"
                "3. Buka menu *Dashboard → API Keys*\n"
                "4. Copy API Key dan kirim ke sini",
                parse_mode="Markdown"
            )
        else:
            # Jangan pop state — user bisa coba lagi
            await update.message.reply_text(
                "❌ *Kode tidak valid atau sudah digunakan.*\n\n"
                "Silakan coba masukkan kode yang benar:",
                parse_mode="Markdown"
            )
        return


async def universal_text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Router utama untuk semua pesan teks — dispatch ke step generate yang tepat."""
    step = ctx.user_data.get("step")

    if ctx.user_data.get("generating"):
        if step == "wait_prompt":
            await got_prompt(update, ctx)
        elif step == "wait_custom_prompt":
            await got_custom_prompt(update, ctx)
        elif step == "wait_cfg":
            await got_cfg(update, ctx)
        elif step == "wait_resolusi":
            await got_resolusi(update, ctx)
        else:
            # Mungkin user kirim teks saat nunggu gambar
            await update.message.reply_text(
                "⚠️ Kirim *gambar* (foto/JPG/PNG) ya, bukan teks.",
                parse_mode="Markdown"
            )
        return

    # Bukan dalam flow generate → cek state API Key / Aktivasi
    await text_router(update, ctx)


async def universal_image_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Router untuk foto/dokumen gambar."""
    step = ctx.user_data.get("step", "wait_image")

    if ctx.user_data.get("generating") and step == "wait_image":
        await got_image(update, ctx)
    elif ctx.user_data.get("generating"):
        await update.message.reply_text("⚠️ Sekarang kirim *video* referensi gerak ya, bukan gambar.")
    elif ctx.user_data.get("generating") is None:
        # Bisa saja user kirim gambar tanpa flow — abaikan saja
        pass


async def universal_video_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Router untuk video."""
    if ctx.user_data.get("generating") and ctx.user_data.get("step") == "wait_video":
        await got_video(update, ctx)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Dibatalkan. Ketik /start untuk kembali ke menu.",
        reply_markup=ReplyKeyboardRemove()
    )


# ══════════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Kamu bukan admin.")
        return

    users = load_json("users.json", {})
    if not users:
        await update.message.reply_text("Belum ada user.")
        return

    lines = [f"📊 *Total user: {len(users)}*\n"]
    for uid, u in list(users.items())[:20]:
        plan  = u.get("plan", "free")
        used  = u.get("used", 0)
        exp   = ""
        if u.get("expire_ts") and plan not in ("free", "unlimited"):
            exp_dt = datetime.fromtimestamp(u["expire_ts"])
            exp = f" | exp {exp_dt.strftime('%d/%m')}"
        lines.append(f"• `{uid}` | {PLANS[plan]['label']} | {used} video{exp}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_laporan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Kamu bukan admin.")
        return

    # Cek argumen tanggal: /laporan 2026-05-18
    args = ctx.args
    tanggal = args[0] if args else datetime.now().strftime("%Y-%m-%d")

    laporan = get_laporan(tanggal)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Semua waktu", callback_data="laporan_all")],
        [InlineKeyboardButton(f"📆 Hari ini ({datetime.now().strftime('%d/%m/%Y')})", callback_data=f"laporan_{datetime.now().strftime('%Y-%m-%d')}")],
    ])
    await update.message.reply_text(laporan, parse_mode="Markdown", reply_markup=kb)




async def cmd_genkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin generate kode aktivasi. Usage: /genkey 1 atau /genkey 3 atau /genkey 7"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Kamu bukan admin.")
        return

    args = ctx.args
    if not args or args[0] not in ("1", "3", "7"):
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1️⃣ 1 Hari",  callback_data="genkey_1"),
                InlineKeyboardButton("3️⃣ 3 Hari",  callback_data="genkey_3"),
                InlineKeyboardButton("7️⃣ 7 Hari",  callback_data="genkey_7"),
            ]
        ])
        await update.message.reply_text(
            "🔑 *Generate Kode Aktivasi*\n\nPilih durasi paket:",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    await _do_genkey(update.message, args[0])


async def _do_genkey(message, durasi: str):
    import random, string
    plan_map = {"1": "1day", "3": "3day", "7": "7day"}
    label_map = {"1": "1 Hari", "3": "3 Hari", "7": "7 Hari"}

    plan     = plan_map[durasi]
    label    = label_map[durasi]
    kode     = "NS-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
    expire_ts = (datetime.now().timestamp()) + (int(durasi) * 24 * 3600)
    expire_str = datetime.fromtimestamp(expire_ts).strftime("%d/%m/%Y %H:%M")

    keys = get_keys()
    keys[kode] = {
        "plan":      plan,
        "expire_ts": expire_ts,
        "used":      False,
        "created_at": datetime.now().isoformat(),
    }
    save_keys(keys)

    await message.reply_text(
        f"✅ *Kode Aktivasi Berhasil Dibuat!*\n\n"
        f"📦 Paket   : *{label}*\n"
        f"🔑 Kode    : `{kode}`\n"
        f"⏳ Expire  : {expire_str}\n\n"
        f"Kirim kode ini ke user.",
        parse_mode="Markdown"
    )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("laporan", cmd_laporan))
    app.add_handler(CommandHandler("genkey",  cmd_genkey))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Handler gambar & video — dicek lewat step di ctx.user_data
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, universal_image_handler))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, universal_video_handler))

    # Handler teks — semua teks non-command masuk ke sini
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, universal_text_handler))

    log.info("✅ Bot Motion Control berjalan...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
