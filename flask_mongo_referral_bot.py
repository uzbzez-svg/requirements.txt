import asyncio
import os
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from flask import Flask, jsonify, request
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


load_env_file(os.path.join(BASE_DIR, ".env"))


def clean_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def first_valid_env(*names: str, default: str = "") -> str:
    bad_parts = ("{", "}", "your_", "SHU_YERGA")
    for name in names:
        value = clean_env(name)
        if value and not any(part in value for part in bad_parts):
            return value
    return default

BOT_TOKEN = first_valid_env("8831278254:AAHdL4in2whlp76ZOGkw0tNimW5XeCQQOyc")
ADMIN_IDS_TEXT = os.getenv("ADMIN_IDS", "6968399046").strip()
MONGO_URI = first_valid_env(
    "MONGODB_URI",
    "MONGO_URI",
    default="mongodb+srv://nabijonmadaminov5_db_user:waD0CxXozOC75Odn@cluster0.ccjzdn4.mongodb.net/?appName=Cluster0",
)
MONGO_DB = first_valid_env("MONGO_DB", "MONGODB_DB", default="clckinobot")

# Hostingda WEBHOOK_URL public HTTPS bo'ladi. Localda bo'sh qoldiring, polling ishlaydi.
WEBHOOK_URL = first_valid_env("WEBHOOK_URL", "PUBLIC_BASE_URL")
WEBHOOK_SECRET = clean_env("WEBHOOK_SECRET")
PORT = int(os.getenv("PORT", "5000"))

REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "1000"))
UTC = timezone.utc
ADMIN_IDS = {int(item.strip()) for item in ADMIN_IDS_TEXT.split(",") if item.strip().isdigit()}

if not BOT_TOKEN or "SHU_YERGA" in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan. BOT_TOKEN env qiymatini yoki fayldagi joyni to'ldiring.")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI yoki MONGODB_URI kiritilmagan. .env ichiga MongoDB connection string yozing.")

mongo = MongoClient(MONGO_URI)
db = mongo[MONGO_DB]
users = db.users
referrals = db.referrals
channels = db.channels
withdrawals = db.withdrawals
broadcasts = db.broadcasts
admins = db.admins
promo_codes = db.promo_codes
promo_redemptions = db.promo_redemptions
coin_transfers = db.coin_transfers

telegram_app = Application.builder().token(BOT_TOKEN).build()
bot = Bot(BOT_TOKEN)
flask_app = Flask(__name__)
telegram_started = False
telegram_start_error: BaseException | None = None
telegram_loop = asyncio.new_event_loop()
telegram_ready = threading.Event()
telegram_thread: threading.Thread | None = None
webhook_configured = False


def now() -> datetime:
    return datetime.now(UTC)


def setup_indexes() -> None:
    users.create_index([("_id", ASCENDING)])
    users.create_index([("referral_count", DESCENDING)])
    users.create_index([("state", ASCENDING)])
    referrals.create_index([("referred_id", ASCENDING)], unique=True)
    referrals.create_index([("referrer_id", ASCENDING), ("created_at", DESCENDING)])
    channels.create_index([("chat_id", ASCENDING)], unique=True)
    withdrawals.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    admins.create_index([("_id", ASCENDING)])
    admins.create_index([("username", ASCENDING)])
    promo_codes.create_index([("code", ASCENDING)], unique=True)
    promo_codes.create_index([("active", ASCENDING), ("expires_at", ASCENDING)])
    promo_redemptions.create_index([("code", ASCENDING), ("user_id", ASCENDING)], unique=True)
    promo_redemptions.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    coin_transfers.create_index([("admin_id", ASCENDING), ("created_at", DESCENDING)])


def webhook_path() -> str:
    return f"/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/webhook"


def webhook_url(public_base_url: str | None = None) -> str:
    base_url = (public_base_url or WEBHOOK_URL).rstrip("/")
    if base_url.endswith("/webhook") or "/webhook/" in base_url:
        return base_url
    return f"{base_url}{webhook_path()}"


def request_public_base_url() -> str:
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    if forwarded_host:
        proto = forwarded_proto or "https"
        return f"{proto}://{forwarded_host}"
    return request.url_root.rstrip("/")


async def configure_webhook(public_base_url: str | None = None) -> None:
    if not (public_base_url or WEBHOOK_URL):
        return
    await bot.set_webhook(webhook_url(public_base_url))


def configure_webhook_once(public_base_url: str | None = None) -> None:
    global webhook_configured
    if webhook_configured or not (public_base_url or WEBHOOK_URL):
        return
    asyncio.run(configure_webhook(public_base_url))
    webhook_configured = True


def kb(text: str, style: str = "primary", icon_custom_emoji_id: str | None = None) -> KeyboardButton:
    return KeyboardButton(text, style=style, icon_custom_emoji_id=icon_custom_emoji_id)


def ib(
    text: str,
    style: str = "primary",
    icon_custom_emoji_id: str | None = None,
    **kwargs: Any,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text,
        style=style,
        icon_custom_emoji_id=icon_custom_emoji_id,
        **kwargs,
    )


USER_MENU = ReplyKeyboardMarkup(
    [
        [kb("🪙 Tekin coin olish", "success"), kb("💼 Mening hisobim", "primary")],
        [kb("🛒 Coinni yechish", "danger")],
    ],
    resize_keyboard=True,
)

ADMIN_MAIN_MENU = ReplyKeyboardMarkup(
    [
        [kb("🪙 Tekin coin olish", "success"), kb("💼 Mening hisobim", "primary")],
        [kb("🛒 Coinni yechish", "danger")],
        [kb("🛠 Admin panel", "primary")],
    ],
    resize_keyboard=True,
)

ADMIN_MENU = InlineKeyboardMarkup(
    [
        [
            ib("📊 Statistika", "primary", callback_data="admin:stats"),
            ib("➕ Majburiy obuna qo'shish", "success", callback_data="admin:add_channel"),
        ],
        [
            ib("➖ Majburiy obuna ayirish", "danger", callback_data="admin:remove_channel"),
            ib("📢 Hammaga xabar", "primary", callback_data="admin:broadcast"),
        ],
        [ib("📋 Majburiy obunalar ro'yxati", "primary", callback_data="admin:list_channels")],
        [
            ib("👑 Yangi admin qo'shish", "success", callback_data="admin:add_admin"),
            ib("🗑 Adminni olib tashlash", "danger", callback_data="admin:remove_admin"),
        ],
    ]
)

LIMITED_ADMIN_MENU = InlineKeyboardMarkup(
    [
        [ib("📊 Statistika", "primary", callback_data="admin:stats")],
        [ib("📢 Hammaga xabar", "primary", callback_data="admin:broadcast")],
        [ib("📋 Majburiy obunalar ro'yxati", "primary", callback_data="admin:list_channels")],
    ]
)

REQUIREMENT_TYPES = {
    "kanal": {
        "label": "📣 Oddiy kanal",
        "kind": "telegram",
        "prompt_name": "📌 Obuna nomini yuboring. Masalan: Kino kanal",
        "prompt_target": "👤 Username yuboring. Masalan: @kanal_username",
    },
    "chat": {
        "label": "💬 Public chat/guruh",
        "kind": "telegram",
        "prompt_name": "📌 Chat/guruh nomini yuboring. Masalan: Kino chat",
        "prompt_target": "👤 Username yuboring. Masalan: @chat_username",
    },
    "zayafka": {
        "label": "📝 Zayafka kanal",
        "kind": "telegram",
        "prompt_name": "📌 Zayafka kanal nomini yuboring. Masalan: VIP kanal",
        "prompt_target": "🔗 Invite link yoki -100 chat_id yuboring. Masalan: https://t.me/+abcDEF",
    },
    "instagram": {
        "label": "📸 Instagram",
        "kind": "manual",
        "prompt_name": "📌 Instagram sahifa nomini yuboring. Masalan: Kino sahifa",
        "prompt_target": "🔗 Instagram link yuboring. Masalan: https://instagram.com/username",
    },
}

REQUIREMENT_TYPE_KEYBOARD = InlineKeyboardMarkup(
    [
        [ib(REQUIREMENT_TYPES["kanal"]["label"], "primary", callback_data="req_type:kanal")],
        [ib(REQUIREMENT_TYPES["chat"]["label"], "success", callback_data="req_type:chat")],
        [ib(REQUIREMENT_TYPES["zayafka"]["label"], "success", callback_data="req_type:zayafka")],
        [ib(REQUIREMENT_TYPES["instagram"]["label"], "primary", callback_data="req_type:instagram")],
    ]
)

WITHDRAW_ITEMS = [
    ("king", "👑 King qilish", 3000),
    ("hp300", "🧰 300 HP", 4000),
    ("game_money", "💸 O'yin puli", 6000),
    ("coin30000", "🪙 30.000 coin", 10000),
    ("chrome", "🔷 Xrom qilish", 10000),
     ("id", "🆔 ID OZGARTIRISH", 7000),
]
WITHDRAW_LABELS = {code: label for code, label, _price in WITHDRAW_ITEMS}
WITHDRAW_PRICES = {code: price for code, _label, price in WITHDRAW_ITEMS}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or admins.find_one({"_id": user_id}) is not None


def is_owner(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def all_admin_ids() -> set[int]:
    saved_admins = {admin["_id"] for admin in admins.find({}, {"_id": 1})}
    return ADMIN_IDS | saved_admins


def admin_menu_for(user_id: int) -> InlineKeyboardMarkup:
    return ADMIN_MENU if is_owner(user_id) else LIMITED_ADMIN_MENU


def main_menu_for(user_id: int) -> ReplyKeyboardMarkup:
    return ADMIN_MAIN_MENU if is_admin(user_id) else USER_MENU


class AdminStateFilter(filters.MessageFilter):
    def filter(self, message) -> bool:
        tg_user = message.from_user
        if not tg_user or not is_admin(tg_user.id):
            return False
        user = get_user(tg_user.id)
        return bool(user and user.get("state"))


def get_user(user_id: int) -> dict[str, Any] | None:
    return users.find_one({"_id": user_id})


def user_name(user: dict[str, Any] | None) -> str:
    if not user:
        return "Noma'lum"
    if user.get("username"):
        return f"@{user['username']}"
    full_name = " ".join(
        item for item in [user.get("first_name"), user.get("last_name")] if item
    ).strip()
    return full_name or str(user["_id"])


def find_user_by_username(username: str) -> dict[str, Any] | None:
    clean = username.strip().lstrip("@")
    if not clean:
        return None
    return users.find_one({"username": {"$regex": f"^{re.escape(clean)}$", "$options": "i"}})


def profile_url(user_id: int) -> str:
    return f"tg://user?id={user_id}"


def upsert_user(tg_user, referrer_id: int | None = None) -> dict[str, Any]:
    old_user = get_user(tg_user.id)
    update_doc = {
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
        "updated_at": now(),
    }

    if old_user:
        users.update_one({"_id": tg_user.id}, {"$set": update_doc})
        return get_user(tg_user.id)

    pending_referrer_id = None
    if referrer_id and referrer_id != tg_user.id and get_user(referrer_id):
        pending_referrer_id = referrer_id

    new_user = {
        "_id": tg_user.id,
        **update_doc,
        "coins": 0,
        "withdrawn_coins": 0,
        "referral_count": 0,
        "pending_referrer_id": pending_referrer_id,
        "referrer_id": None,
        "is_referral_counted": False,
        "state": None,
        "created_at": now(),
    }
    users.insert_one(new_user)
    return get_user(tg_user.id)


def requirement_type(requirement: dict[str, Any]) -> str:
    return requirement.get("type") or "kanal"


def requirement_key(requirement: dict[str, Any]) -> str:
    return str(requirement.get("_id"))


def can_auto_check_requirement(requirement: dict[str, Any]) -> bool:
    req_type = requirement_type(requirement)
    chat_id = str(requirement.get("chat_id", "")).strip()
    return req_type in {"kanal", "chat", "zayafka"} and (
        chat_id.startswith("@") or chat_id.startswith("-100")
    )


def member_is_active(member) -> bool:
    if member.status in {"creator", "administrator", "member"}:
        return True
    if member.status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


def normalize_bot_username(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/").split("?", 1)[0].split("/", 1)[0]
    return value.lstrip("@")


def clean_username(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/").split("?", 1)[0].split("/", 1)[0]
    return value if value.startswith("@") or value.startswith("-100") else f"@{value.lstrip('@')}"


def manual_requirement_done_text(requirement: dict[str, Any]) -> str:
    req_type = requirement_type(requirement)
    title = requirement.get("title", "Topshiriq")
    if req_type == "instagram":
        return f"✅ {title} sahifasiga obuna bo'ldim"
    if req_type == "zayafka":
        return f"✅ {title} kanaliga zayafka yubordim"
    if req_type == "bot":
        return f"✅ {title} botiga start bosdim"
    return f"✅ {title} bajarildi"


def make_invite_link(req_type: str, target: str) -> str:
    target = target.strip()
    if target.startswith("http://") or target.startswith("https://"):
        return target
    if req_type == "instagram":
        return f"https://instagram.com/{target.lstrip('@')}"
    if req_type == "bot":
        return f"https://t.me/{normalize_bot_username(target)}?start=required"
    if target.startswith("@"):
        return f"https://t.me/{target.lstrip('@')}"
    return ""


def save_requirement(req_type: str, title: str, target: str, invite_link_override: str | None = None) -> None:
    req_type = req_type.strip().lower()
    title = title.strip()
    target = target.strip()
    if req_type == "zayafka":
        chat_id = target if target.startswith("-100") else target
        invite_link = invite_link_override or make_invite_link(req_type, target)
    elif req_type in {"kanal", "chat"}:
        chat_id = clean_username(target)
        invite_link = invite_link_override or make_invite_link(req_type, chat_id)
    elif req_type == "instagram":
        chat_id = f"instagram:{target.lower().rstrip('/')}"
        invite_link = invite_link_override or make_invite_link(req_type, target)
    elif req_type == "bot":
        bot_username = normalize_bot_username(target)
        chat_id = f"bot:{bot_username.lower()}"
        invite_link = invite_link_override or make_invite_link(req_type, bot_username)
    else:
        raise ValueError("Noto'g'ri majburiy obuna turi.")

    channels.update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "chat_id": chat_id,
                "target": target,
                "type": req_type,
                "title": title,
                "invite_link": invite_link,
                "updated_at": now(),
            },
            "$setOnInsert": {"created_at": now()},
        },
        upsert=True,
    )


def requirement_icon(requirement: dict[str, Any]) -> str:
    req_type = requirement_type(requirement)
    if req_type == "chat":
        return "💬"
    if req_type in {"bot", "manual"}:
        return "🤖"
    if req_type == "instagram":
        return "📸"
    if req_type == "zayafka":
        return "📝"
    return "📺"


async def is_member(user_id: int, channel: dict[str, Any]) -> bool:
    if not can_auto_check_requirement(channel):
        user = get_user(user_id)
        completed = user.get("completed_manual_requirements", []) if user else []
        return requirement_key(channel) in completed

    try:
        member = await bot.get_chat_member(channel["chat_id"], user_id)
        return member_is_active(member)
    except TelegramError:
        return False


async def missing_channels(user_id: int) -> list[dict[str, Any]]:
    missing = []
    for channel in channels.find({}).sort("created_at", ASCENDING):
        if not await is_member(user_id, channel):
            missing.append(channel)
    return missing


def subscription_keyboard(missing: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for channel in missing:
        title = channel.get("title") or str(channel["chat_id"])
        invite_link = channel.get("invite_link")
        icon = requirement_icon(channel)
        if invite_link:
            rows.append([ib(f"{icon} {title}", "primary", url=invite_link)])
        else:
            rows.append([ib(f"{icon} {title}", "primary", callback_data="noop")])
        if not can_auto_check_requirement(channel):
            rows.append([ib(manual_requirement_done_text(channel), "success", callback_data=f"req_done:{requirement_key(channel)}")])
    rows.append([ib("✅ Obunani tekshirish", "success", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


async def maybe_count_referral(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(user_id)
    if not user:
        return

    referrer_id = user.get("pending_referrer_id")
    if not referrer_id or user.get("is_referral_counted") or user.get("referrer_id"):
        return
    if referrer_id == user_id or not get_user(referrer_id):
        return

    try:
        referrals.insert_one(
            {
                "referrer_id": referrer_id,
                "referred_id": user_id,
                "referred_username": user.get("username"),
                "created_at": now(),
            }
        )
    except DuplicateKeyError:
        users.update_one(
            {"_id": user_id},
            {
                "$set": {
                    "is_referral_counted": True,
                    "pending_referrer_id": None,
                    "updated_at": now(),
                }
            },
        )
        return
    except PyMongoError:
        return

    users.update_one(
        {"_id": user_id, "is_referral_counted": False, "referrer_id": None},
        {
            "$set": {
                "referrer_id": referrer_id,
                "is_referral_counted": True,
                "pending_referrer_id": None,
                "updated_at": now(),
            }
        },
    )
    users.update_one(
        {"_id": referrer_id},
        {
            "$inc": {"coins": REFERRAL_REWARD, "referral_count": 1},
            "$set": {"updated_at": now()},
        },
    )

    try:
        await context.bot.send_message(
            referrer_id,
            f"✅ Sizga {REFERRAL_REWARD} coin qo'shildi!\nYangi referal: {user_name(user)}",
        )
    except TelegramError:
        pass


async def require_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    tg_user = update.effective_user
    if not tg_user:
        return False

    missing = await missing_channels(tg_user.id)
    if not missing:
        await maybe_count_referral(tg_user.id, context)
        return True

    text = (
        "⚠️ Botdan foydalanish uchun majburiy obuna/topshiriqlarni bajaring.\n"
        "Obuna bo'lgach, ✅ Obunani tekshirish tugmasini bosing."
    )
    target_message = update.callback_query.message if update.callback_query else update.effective_message
    await target_message.reply_text(text, reply_markup=subscription_keyboard(missing))
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    referrer_id = int(context.args[0]) if context.args and context.args[0].isdigit() else None
    upsert_user(update.effective_user, referrer_id)

    if not await require_subscription(update, context):
        return

    await update.message.reply_text(
        "Assalomu alaykum! Kerakli bo'limni tanlang.",
        reply_markup=main_menu_for(update.effective_user.id),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    upsert_user(update.effective_user)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Bu bo'lim faqat admin uchun.")
        return
    await update.message.reply_text("🛠 Admin panel:", reply_markup=admin_menu_for(update.effective_user.id))


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    upsert_user(query.from_user)

    missing = await missing_channels(query.from_user.id)
    if missing:
        await query.message.reply_text(
            "Hali barcha majburiy obuna/topshiriqlar bajarilmagan.",
            reply_markup=subscription_keyboard(missing),
        )
        return

    await maybe_count_referral(query.from_user.id, context)
    await query.message.reply_text("✅ Obuna tasdiqlandi. Botdan foydalanishingiz mumkin.", reply_markup=main_menu_for(query.from_user.id))


async def requirement_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    upsert_user(query.from_user)

    requirement_id = query.data.split(":", 1)[1]
    requirement = channels.find_one({"_id": ObjectId(requirement_id)})
    if not requirement:
        await query.message.reply_text("Bu majburiy obuna topilmadi yoki olib tashlangan.")
        return
    if requirement_type(requirement) not in {"bot", "instagram", "zayafka", "manual"}:
        await query.message.reply_text("Bu tur avtomatik tekshiriladi. Obunani tekshirish tugmasini bosing.")
        return

    users.update_one(
        {"_id": query.from_user.id},
        {
            "$addToSet": {"completed_manual_requirements": requirement_id},
            "$set": {"updated_at": now()},
        },
    )
    await query.message.reply_text(
        f"✅ {requirement.get('title', 'Topshiriq')} tasdiqlandi. Endi obunani tekshiring.",
        reply_markup=subscription_keyboard(await missing_channels(query.from_user.id)),
    )


async def free_coin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={update.effective_user.id}"
    await update.message.reply_text(
        "🪙 Tekin coin olish uchun referral linkingiz:\n\n"
        f"{link}\n\n"
        f"Har bir yangi va majburiy obuna/topshiriqlarni bajargan referral uchun {REFERRAL_REWARD} coin beriladi."
    )


def top_referrers_text(days: int, title: str) -> str:
    since = now() - timedelta(days=days)
    pipeline = [
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": "$referrer_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
        {
            "$lookup": {
                "from": "users",
                "localField": "_id",
                "foreignField": "_id",
                "as": "user",
            }
        },
        {"$unwind": {"path": "$user", "preserveNullAndEmptyArrays": True}},
    ]
    rows = list(referrals.aggregate(pipeline))
    if not rows:
        return f"{title}\n\nHozircha referral yo'q."

    lines = [title, ""]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {user_name(row.get('user'))} - {row['count']} ta referral")
    return "\n".join(lines)


async def my_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(update.effective_user.id) or upsert_user(update.effective_user)
    keyboard = InlineKeyboardMarkup(
        [
            [ib("🔝 1 haftalik eng ko'p taklif qilganlar", "primary", callback_data="top:7")],
            [ib("🔝 20 kunlik eng ko'p taklif qilganlar", "primary", callback_data="top:20")],
            [ib("🔝 1 yillik eng ko'p taklif qilganlar", "primary", callback_data="top:365")],
        ]
    )
    await update.message.reply_text(
        "💼 Mening hisobim\n\n"
        f"Balans 💼: {user.get('coins', 0)} coin\n"
        f"Yechgan coinlar 🪙: {user.get('withdrawn_coins', 0)} coin\n"
        f"Taklif qilgan referallar 🎎: {user.get('referral_count', 0)} ta",
        reply_markup=keyboard,
    )


async def top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await require_subscription(update, context):
        return

    days = int(query.data.split(":")[1])
    titles = {
        7: "🔝 1 haftalik eng ko'p taklif qilgan odamlar",
        20: "🔝 20 kunlik eng ko'p taklif qilgan odamlar",
        365: "🔝 1 yillik eng ko'p taklif qilgan odamlar",
    }
    await query.message.reply_text(top_referrers_text(days, titles[days]))


async def withdraw_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = [
        [ib(f"{label} - {price} coin", "danger", callback_data=f"withdraw:{code}")]
        for code, label, price in WITHDRAW_ITEMS
    ]
    await update.message.reply_text("🛒 Coinni yechish turini tanlang:", reply_markup=InlineKeyboardMarkup(rows))


async def withdraw_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await require_subscription(update, context):
        return

    code = query.data.split(":")[1]
    price = WITHDRAW_PRICES[code]
    label = WITHDRAW_LABELS[code]
    user = get_user(query.from_user.id)
    if not user or user.get("coins", 0) < price:
        await query.message.reply_text(f"Balansingiz yetarli emas. Kerak: {price} coin.")
        return

    keyboard = InlineKeyboardMarkup([[ib("✅ Tasdiqlash", "success", callback_data=f"confirm:{code}")]])
    await query.message.reply_text(
        f"Siz ushbu haridni tasdiqlaysizmi?\n\n{label}\nNarxi: {price} coin",
        reply_markup=keyboard,
    )


async def withdraw_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await require_subscription(update, context):
        return

    code = query.data.split(":")[1]
    price = WITHDRAW_PRICES[code]
    label = WITHDRAW_LABELS[code]
    user_id = query.from_user.id

    result = users.update_one(
        {"_id": user_id, "coins": {"$gte": price}},
        {
            "$inc": {"coins": -price, "withdrawn_coins": price},
            "$set": {"updated_at": now()},
        },
    )
    if result.modified_count != 1:
        await query.message.reply_text("Balansingiz yetarli emas.")
        return

    request_doc = {
        "user_id": user_id,
        "username": query.from_user.username,
        "item_code": code,
        "item_label": label,
        "price": price,
        "status": "new",
        "created_at": now(),
    }
    insert_result = withdrawals.insert_one(request_doc)
    user = get_user(user_id)

    admin_keyboard = InlineKeyboardMarkup([[ib("👤 Foydalanuvchi", "primary", url=profile_url(user_id))]])
    admin_text = (
        "🔔 Yangi coin yechish so'rovi\n\n"
        f"Foydalanuvchi: {user_name(user)}\n"
        f"User ID: {user_id}\n"
        f"Harid: {label}\n"
        f"Yechilgan coin: {price}\n"
        f"So'rov ID: {insert_result.inserted_id}"
    )
    for admin_id in all_admin_ids():
        try:
            await context.bot.send_message(admin_id, admin_text, reply_markup=admin_keyboard)
        except TelegramError:
            pass

    await query.message.reply_text(f"✅ So'rov qabul qilindi. {label} uchun {price} coin yechildi.")


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    upsert_user(update.effective_user)

    if text == "🛠 Admin panel":
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("Bu bo'lim faqat admin uchun.")
            return
        await update.message.reply_text("🛠 Admin panel:", reply_markup=admin_menu_for(update.effective_user.id))
        return

    if not await require_subscription(update, context):
        return

    if text == "🪙 Tekin coin olish":
        await free_coin(update, context)
    elif text == "💼 Mening hisobim":
        await my_account(update, context)
    elif text == "🛒 Coinni yechish":
        await withdraw_menu(update, context)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.message.reply_text("Bu bo'lim faqat admin uchun.")
        return

    action = query.data.split(":")[1]
    owner_only_actions = {"add_channel", "remove_channel", "add_admin", "remove_admin"}
    if action in owner_only_actions and not is_owner(query.from_user.id):
        await query.message.reply_text("Bu amal faqat bot egasi uchun.")
        return

    if action == "stats":
        total_users = users.count_documents({})
        total_channels = channels.count_documents({})
        total_referrals = referrals.count_documents({})
        total_withdrawn_row = list(users.aggregate([{"$group": {"_id": None, "sum": {"$sum": "$withdrawn_coins"}}}]))
        total_withdrawn = total_withdrawn_row[0]["sum"] if total_withdrawn_row else 0
        await query.message.reply_text(
            "📊 Statistika\n\n"
            f"Foydalanuvchilar: {total_users}\n"
            f"Majburiy obunalar: {total_channels}\n"
            f"Jami referallar: {total_referrals}\n"
            f"Jami yechilgan coin: {total_withdrawn}"
        )
        return

    if action == "add_channel":
        users.update_one(
            {"_id": query.from_user.id},
            {"$set": {"state": None}, "$unset": {"admin_add_requirement": ""}},
        )
        await query.message.reply_text(
            "Qanday majburiy obuna qo'shasiz?",
            reply_markup=REQUIREMENT_TYPE_KEYBOARD,
        )
        return

    if action == "remove_channel":
        all_channels = list(channels.find({}).sort("created_at", ASCENDING))
        if not all_channels:
            await query.message.reply_text("Majburiy obuna yo'q.")
            return
        rows = [
            [ib(f"➖ {requirement_icon(channel)} {channel.get('title', channel['chat_id'])}", "danger", callback_data=f"remove_channel:{channel['_id']}")]
            for channel in all_channels
        ]
        await query.message.reply_text("O'chiriladigan majburiy obunani tanlang:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "broadcast":
        users.update_one({"_id": query.from_user.id}, {"$set": {"state": "admin_broadcast"}})
        await query.message.reply_text("Yuboriladigan xabarni tashlang. Matn, rasm, video va boshqa turdagi xabarlar ishlaydi.")
        return

    if action == "add_admin":
        users.update_one(
            {"_id": query.from_user.id},
            {"$set": {"state": "admin_add_admin_username"}, "$unset": {"pending_admin_add": ""}},
        )
        await query.message.reply_text("👤 Qo'shmoqchi bo'lgan admin username'ni tashlang. Masalan: @username")
        return

    if action == "remove_admin":
        saved_admins = list(admins.find({}).sort("created_at", ASCENDING))
        if not saved_admins:
            await query.message.reply_text("Panel orqali qo'shilgan admin yo'q.")
            return
        rows = []
        for admin in saved_admins:
            label = f"🗑 {user_name(admin)}"
            if admin.get("first_name"):
                label = f"🗑 {admin.get('first_name')} {user_name(admin)}"
            rows.append([ib(label, "danger", callback_data=f"admin_remove:{admin['_id']}")])
        await query.message.reply_text("Olib tashlanadigan adminni tanlang:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "list_channels":
        all_channels = list(channels.find({}).sort("created_at", ASCENDING))
        if not all_channels:
            await query.message.reply_text("Majburiy obuna yo'q.")
            return
        lines = ["📋 Majburiy obunalar", ""]
        for index, channel in enumerate(all_channels, 1):
            req_type = requirement_type(channel)
            lines.append(
                f"{index}. {requirement_icon(channel)} {channel.get('title')} - {req_type} - {channel.get('chat_id')}"
            )
        await query.message.reply_text("\n".join(lines))


async def remove_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.message.reply_text("Majburiy obunani faqat bot egasi olib tashlay oladi.")
        return
    requirement_id = query.data.split(":", 1)[1]
    channels.delete_one({"_id": ObjectId(requirement_id)})
    await query.message.reply_text("✅ Majburiy obuna olib tashlandi.")


async def requirement_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.message.reply_text("Majburiy obunani faqat bot egasi qo'sha oladi.")
        return

    req_type = query.data.split(":", 1)[1]
    config = REQUIREMENT_TYPES.get(req_type)
    if not config:
        await query.message.reply_text("Bunday tur topilmadi.")
        return

    users.update_one(
        {"_id": query.from_user.id},
        {
            "$set": {
                "state": "admin_add_requirement_name",
                "admin_add_requirement": {"type": req_type},
                "updated_at": now(),
            }
        },
    )
    await query.message.reply_text(config["prompt_name"])


async def admin_add_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.message.reply_text("Adminni faqat bot egasi qo'sha oladi.")
        return

    new_admin_id = int(query.data.split(":", 1)[1])
    user = get_user(new_admin_id)
    if not user:
        await query.message.reply_text("Foydalanuvchi topilmadi. Avval botga /start bossin.")
        return

    admins.update_one(
        {"_id": new_admin_id},
        {
            "$set": {
                "username": user.get("username"),
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "added_by": query.from_user.id,
                "updated_at": now(),
            },
            "$setOnInsert": {"created_at": now()},
        },
        upsert=True,
    )
    users.update_one({"_id": query.from_user.id}, {"$set": {"state": None}, "$unset": {"pending_admin_add": ""}})
    await query.message.reply_text(f"✅ {user_name(user)} admin qilib qo'shildi.", reply_markup=admin_menu_for(query.from_user.id))


async def admin_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.message.reply_text("Adminni faqat bot egasi olib tashlay oladi.")
        return

    admin_id = int(query.data.split(":", 1)[1])
    if admin_id in ADMIN_IDS:
        await query.message.reply_text("Asosiy adminni paneldan olib tashlab bo'lmaydi.")
        return
    removed = admins.find_one_and_delete({"_id": admin_id})
    if not removed:
        await query.message.reply_text("Admin topilmadi yoki allaqachon olib tashlangan.")
        return
    await query.message.reply_text(f"✅ {user_name(removed)} adminlikdan olib tashlandi.", reply_markup=admin_menu_for(query.from_user.id))


async def admin_state_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user or not is_admin(tg_user.id):
        return

    admin = get_user(tg_user.id) or upsert_user(tg_user)
    state = admin.get("state")

    if state == "admin_add_admin_username":
        if not is_owner(tg_user.id):
            users.update_one({"_id": tg_user.id}, {"$set": {"state": None}, "$unset": {"pending_admin_add": ""}})
            await update.message.reply_text("Adminni faqat bot egasi qo'sha oladi.", reply_markup=admin_menu_for(tg_user.id))
            return
        if not update.message.text:
            await update.message.reply_text("Username matn ko'rinishida yuboring. Masalan: @username")
            return
        candidate = find_user_by_username(update.message.text)
        if not candidate:
            await update.message.reply_text(
                "Bu username bot foydalanuvchilari ichidan topilmadi.\n"
                "Admin qilinadigan odam avval botga /start bosishi kerak."
            )
            return
        if candidate["_id"] == tg_user.id:
            await update.message.reply_text("O'zingizni qayta admin qilib qo'shish shart emas.")
            return
        if is_admin(candidate["_id"]):
            await update.message.reply_text("Bu foydalanuvchi allaqachon admin.")
            return
        users.update_one(
            {"_id": tg_user.id},
            {
                "$set": {
                    "pending_admin_add": candidate["_id"],
                    "updated_at": now(),
                }
            },
        )
        keyboard = InlineKeyboardMarkup(
            [[ib("✅ Tasdiqlash", "success", callback_data=f"admin_add_confirm:{candidate['_id']}")]]
        )
        await update.message.reply_text(
            f"{user_name(candidate)} admin qilib qo'shishni tasdiqlaysizmi?",
            reply_markup=keyboard,
        )
        return

    if state == "admin_add_requirement_name":
        if not is_owner(tg_user.id):
            users.update_one({"_id": tg_user.id}, {"$set": {"state": None}, "$unset": {"admin_add_requirement": ""}})
            await update.message.reply_text("Majburiy obunani faqat bot egasi qo'sha oladi.", reply_markup=admin_menu_for(tg_user.id))
            return
        if not update.message.text:
            await update.message.reply_text("Matn yuboring.")
            return
        draft = admin.get("admin_add_requirement") or {}
        req_type = draft.get("type")
        config = REQUIREMENT_TYPES.get(req_type)
        if not config:
            users.update_one({"_id": tg_user.id}, {"$set": {"state": None}, "$unset": {"admin_add_requirement": ""}})
            await update.message.reply_text("Tur topilmadi. Qaytadan urinib ko'ring.", reply_markup=admin_menu_for(tg_user.id))
            return
        draft["title"] = update.message.text.strip()
        users.update_one(
            {"_id": tg_user.id},
            {
                "$set": {
                    "state": "admin_add_requirement_target",
                    "admin_add_requirement": draft,
                    "updated_at": now(),
                }
            },
        )
        await update.message.reply_text(config["prompt_target"])
        return

    if state == "admin_add_requirement_target":
        if not is_owner(tg_user.id):
            users.update_one({"_id": tg_user.id}, {"$set": {"state": None}, "$unset": {"admin_add_requirement": ""}})
            await update.message.reply_text("Majburiy obunani faqat bot egasi qo'sha oladi.", reply_markup=admin_menu_for(tg_user.id))
            return
        if not update.message.text:
            await update.message.reply_text("Matn yuboring.")
            return
        draft = admin.get("admin_add_requirement") or {}
        req_type = draft.get("type")
        title = draft.get("title")
        if not req_type or not title:
            users.update_one({"_id": tg_user.id}, {"$set": {"state": None}, "$unset": {"admin_add_requirement": ""}})
            await update.message.reply_text("Ma'lumot to'liq emas. Qaytadan urinib ko'ring.", reply_markup=admin_menu_for(tg_user.id))
            return
        try:
            save_requirement(req_type, title, update.message.text.strip())
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        users.update_one(
            {"_id": tg_user.id},
            {"$set": {"state": None, "updated_at": now()}, "$unset": {"admin_add_requirement": ""}},
        )
        await update.message.reply_text("✅ Majburiy obuna qo'shildi.", reply_markup=admin_menu_for(tg_user.id))
        return

    if state == "admin_add_channel":
        if not is_owner(tg_user.id):
            users.update_one({"_id": tg_user.id}, {"$set": {"state": None}})
            await update.message.reply_text("Majburiy obunani faqat bot egasi qo'sha oladi.", reply_markup=admin_menu_for(tg_user.id))
            return
        if not update.message.text:
            await update.message.reply_text("Matn yuboring: tur | chat_id yoki username | nomi | link")
            return
        parts = [part.strip() for part in update.message.text.split("|")]
        if len(parts) == 3:
            req_type = "kanal"
            target, title, invite_link = parts[0], parts[1], parts[2]
        elif len(parts) >= 4:
            req_type, target, title, invite_link = parts[0].lower(), parts[1], parts[2], parts[3]
        else:
            await update.message.reply_text(
                "Format noto'g'ri.\n\n"
                "Masalan: kanal | @kanal | Kanal nomi | https://t.me/kanal\n"
                "Yoki: bot | @BotUsername | Bot nomi | https://t.me/BotUsername?start=required"
            )
            return

        aliases = {
            "channel": "kanal",
            "guruh": "chat",
            "group": "chat",
            "supergroup": "chat",
            "robot": "bot",
        }
        req_type = aliases.get(req_type, req_type)
        if req_type not in {"kanal", "chat", "zayafka", "instagram", "bot"}:
            await update.message.reply_text("Tur noto'g'ri. Faqat kanal, chat, zayafka, instagram yoki bot yozing.")
            return
        save_requirement(req_type, title, target, invite_link)
        users.update_one({"_id": tg_user.id}, {"$set": {"state": None}})
        await update.message.reply_text("✅ Majburiy obuna qo'shildi.", reply_markup=main_menu_for(tg_user.id))
        return

    if state == "admin_broadcast":
        users.update_one({"_id": tg_user.id}, {"$set": {"state": None}})
        await update.message.reply_text("📢 Xabar yuborish boshlandi.")
        sent = 0
        failed = 0
        for user in users.find({}, {"_id": 1}):
            try:
                await context.bot.copy_message(
                    chat_id=user["_id"],
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.message_id,
                )
                sent += 1
            except (Forbidden, BadRequest, TelegramError):
                failed += 1
        broadcasts.insert_one(
            {
                "admin_id": tg_user.id,
                "message_id": update.message.message_id,
                "sent": sent,
                "failed": failed,
                "created_at": now(),
            }
        )
        await update.message.reply_text(f"✅ Yuborildi: {sent}\n❌ Yetib bormadi: {failed}", reply_markup=main_menu_for(tg_user.id))


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        upsert_user(update.effective_user)
    if not await require_subscription(update, context):
        return
    await update.effective_message.reply_text("Menyudan kerakli tugmani tanlang.", reply_markup=main_menu_for(update.effective_user.id))


def register_handlers() -> None:
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("admin", admin_command))
    telegram_app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    telegram_app.add_handler(CallbackQueryHandler(requirement_done_callback, pattern="^req_done:"))
    telegram_app.add_handler(CallbackQueryHandler(requirement_type_callback, pattern="^req_type:"))
    telegram_app.add_handler(CallbackQueryHandler(top_callback, pattern="^top:"))
    telegram_app.add_handler(CallbackQueryHandler(withdraw_select, pattern="^withdraw:"))
    telegram_app.add_handler(CallbackQueryHandler(withdraw_confirm, pattern="^confirm:"))
    telegram_app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin:"))
    telegram_app.add_handler(CallbackQueryHandler(admin_add_confirm_callback, pattern="^admin_add_confirm:"))
    telegram_app.add_handler(CallbackQueryHandler(admin_remove_callback, pattern="^admin_remove:"))
    telegram_app.add_handler(CallbackQueryHandler(remove_channel_callback, pattern="^remove_channel:"))
    telegram_app.add_handler(CallbackQueryHandler(lambda update, context: update.callback_query.answer(), pattern="^noop$"))
    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT
            & filters.Regex("^(🪙 Tekin coin olish|💼 Mening hisobim|🛒 Coinni yechish|🛠 Admin panel)$"),
            main_menu_handler,
        )
    )
    telegram_app.add_handler(MessageHandler(AdminStateFilter(), admin_state_messages))
    telegram_app.add_handler(MessageHandler(filters.ALL, unknown))


register_handlers()
setup_indexes()
configure_webhook_once()


def run_telegram_loop() -> None:
    asyncio.set_event_loop(telegram_loop)

    async def start_telegram_app() -> None:
        global telegram_started, telegram_start_error
        try:
            await telegram_app.initialize()
            await telegram_app.start()
            telegram_started = True
        except BaseException as exc:
            telegram_start_error = exc
        finally:
            telegram_ready.set()

    telegram_loop.create_task(start_telegram_app())
    telegram_loop.run_forever()


def ensure_telegram_started() -> None:
    global telegram_thread
    if telegram_started:
        return

    if telegram_thread is None or not telegram_thread.is_alive():
        telegram_ready.clear()
        telegram_thread = threading.Thread(target=run_telegram_loop, daemon=True)
        telegram_thread.start()

    telegram_ready.wait(timeout=20)
    if telegram_start_error:
        raise RuntimeError("Telegram app ishga tushmadi.") from telegram_start_error
    if not telegram_started:
        raise RuntimeError("Telegram app ishga tushishi 20 sekund ichida tugamadi.")


@flask_app.get("/")
def health():
    public_base_url = request_public_base_url()
    configure_webhook_once(public_base_url)
    return jsonify({"ok": True, "bot": "flask-mongo-referral-coin-bot", "webhook": webhook_url(public_base_url)})


def process_webhook_request():
    ensure_telegram_started()
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    future = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), telegram_loop)
    try:
        future.result(timeout=30)
    except FutureTimeoutError:
        return jsonify({"ok": False, "error": "telegram_update_timeout"}), 504
    return jsonify({"ok": True})


@flask_app.post("/webhook")
def webhook():
    return process_webhook_request()


@flask_app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook_with_secret():
    return process_webhook_request()


@flask_app.cli.command("set-webhook")
def set_webhook_command():
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL kiritilmagan.")

    asyncio.run(configure_webhook())
    print(f"Webhook o'rnatildi: {webhook_url()}")


if __name__ == "__main__":
    if WEBHOOK_URL:
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        print("Bot polling rejimida ishga tushdi. To'xtatish uchun CTRL+C bosing.")
        telegram_app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
