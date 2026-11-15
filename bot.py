import os
import json
import html
import random
import string
import sqlite3
import asyncio
import logging
import threading
import contextlib
from datetime import datetime, timedelta

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================== الإعدادات الرئيسية ====================
# متغيرات البيئة في Render:
#   BOT_TOKEN  (إجباري)
#   OWNER_ID   (اختياري)
#   DB_PATH    (اختياري - ضعه على Disk دائم مثل /data/wassab_store.db)
TOKEN = os.environ["9157079266:AAHnLCeWZCFsdR5YCRLs9clVghaYzqYDpTE"]
OWNER_ID = int(os.environ.get("OWNER_ID", "8233150959"))
DB_PATH = os.environ.get("DB_PATH", "wassab_store.db")

BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://botstore-54tw.onrender.com").rstrip("/")
WEBHOOK_URL = f"{BASE_URL}/{TOKEN}"

VIP_DAYS = 30
DAILY_POINTS = 10
REFERRAL_POINTS = 15
MAX_LIST_BUTTONS = 40

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wassab")

flask_app = Flask(__name__)

# ==================== قاعدة البيانات ====================

@contextlib.contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                is_banned INTEGER DEFAULT 0,
                is_vip INTEGER DEFAULT 0,
                vip_expires_at TEXT,
                dl_books INTEGER DEFAULT 0,
                dl_photos INTEGER DEFAULT 0,
                dl_videos INTEGER DEFAULT 0,
                dl_free INTEGER DEFAULT 0,
                dl_paid INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                invited_by INTEGER DEFAULT 0,
                last_points_at TEXT
            )
        """)
        # ترقية قواعد البيانات القديمة
        for col_sql in (
            "ALTER TABLE users ADD COLUMN vip_expires_at TEXT",
            "ALTER TABLE users ADD COLUMN last_points_at TEXT",
        ):
            try:
                c.execute(col_sql)
            except sqlite3.OperationalError:
                pass

        c.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                file_type TEXT,
                file_id TEXT,
                category TEXT,
                sub_category TEXT DEFAULT 'عام'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sub_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE
            )
        """)
        c.execute("INSERT OR IGNORE INTO sub_categories (name) SELECT DISTINCT sub_category FROM products WHERE sub_category IS NOT NULL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS forced_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT UNIQUE,
                channel_link TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                max_uses INTEGER DEFAULT 1,
                uses_count INTEGER DEFAULT 0,
                expires_at TEXT,
                created_by INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS admin_states (
                user_id INTEGER PRIMARY KEY,
                data TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_invited_by ON users(invited_by);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_products_type_subcat ON products(file_type, sub_category);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);")


init_db()

# ==================== حالات المحادثة (محفوظة في القاعدة) ====================

def get_state(user_id):
    with get_db_connection() as conn:
        row = conn.execute("SELECT data FROM admin_states WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except Exception:
        return {}


def set_state(user_id, data: dict):
    with get_db_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_states (user_id, data) VALUES (?, ?)",
            (user_id, json.dumps(data, ensure_ascii=False)),
        )


def update_state(user_id, **kwargs):
    st = get_state(user_id)
    st.update(kwargs)
    set_state(user_id, st)
    return st


def clear_state(user_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM admin_states WHERE user_id = ?", (user_id,))

# ==================== دوال المساعدة ====================

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def check_banned(user_id):
    with get_db_connection() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["is_banned"] == 1)


def check_vip(user_id):
    """فحص حالة الـ VIP مع إلغائه تلقائياً عند انتهاء المدة"""
    with get_db_connection() as conn:
        row = conn.execute("SELECT is_vip, vip_expires_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row or row["is_vip"] == 0:
            return False
        if row["vip_expires_at"] and today_str() > row["vip_expires_at"]:
            conn.execute("UPDATE users SET is_vip = 0, vip_expires_at = NULL WHERE user_id = ?", (user_id,))
            return False
    return True


def add_user_with_referral(user_id, full_name, username, referrer_id=0):
    with get_db_connection() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None:
            return False, 0
        actual_ref = referrer_id if (referrer_id and referrer_id != user_id) else 0
        if actual_ref:
            ref_row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (actual_ref,)).fetchone()
            if not ref_row:
                actual_ref = 0
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, full_name, username, invited_by) VALUES (?, ?, ?, ?)",
            (user_id, full_name, username, actual_ref),
        )
        if actual_ref:
            conn.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (REFERRAL_POINTS, actual_ref))
        return True, actual_ref


def update_user_stats(user_id, f_type, category):
    b_col = "dl_books" if f_type == "book" else ("dl_photos" if f_type == "photo" else "dl_videos")
    c_col = "dl_paid" if category == "paid" else "dl_free"
    with get_db_connection() as conn:
        conn.execute(f"UPDATE users SET {b_col} = {b_col} + 1, {c_col} = {c_col} + 1 WHERE user_id = ?", (user_id,))


def normalize_link(link, username):
    if link and (link.startswith("http://") or link.startswith("https://")):
        return link
    return f"https://t.me/{username.replace('@', '')}"


async def get_unsubscribed_channels(user_id, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(user_id):
        return []
    with get_db_connection() as conn:
        channels = conn.execute("SELECT channel_username, channel_link FROM forced_channels").fetchall()

    unsubscribed = []
    for ch in channels:
        ch_username, ch_link = ch["channel_username"], ch["channel_link"]
        try:
            member = await context.bot.get_chat_member(chat_id=ch_username, user_id=user_id)
            if member.status not in ("member", "administrator", "creator", "owner"):
                unsubscribed.append((ch_username, ch_link))
        except Exception as e:
            logger.warning("خطأ في التحقق من القناة %s: %s", ch_username, e)
            unsubscribed.append((ch_username, ch_link))
    return unsubscribed


async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise

# ==================== الأزرار ====================

def subscription_keyboard(unsubscribed_channels):
    keyboard = []
    for ch_username, ch_link in unsubscribed_channels:
        keyboard.append([InlineKeyboardButton(f"📢 اشترك في القناة: {ch_username}", url=normalize_link(ch_link, ch_username))])
    keyboard.append([InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="check_sub")])
    return InlineKeyboardMarkup(keyboard)


def user_menu_keyboard(is_vip=False):
    keyboard = [
        [InlineKeyboardButton("📚 قسم الكتب", callback_data="cat_books"),
         InlineKeyboardButton("🎬 قسم الفيديوهات", callback_data="cat_videos")],
        [InlineKeyboardButton("🖼️ قسم الصور", callback_data="cat_photos"),
         InlineKeyboardButton("💎 القسم المدفوع VIP", callback_data="cat_paid")],
        [InlineKeyboardButton("🎁 نقاط اليوم", callback_data="get_daily_points"),
         InlineKeyboardButton("🔗 دعوة أصدقاء", callback_data="my_referral_link")],
        [InlineKeyboardButton("🎟️ إدخال كود VIP ترويجي", callback_data="enter_promo_code"),
         InlineKeyboardButton("📞 الدعم الفني", callback_data="contact_support")],
    ]
    if is_vip:
        keyboard.append([InlineKeyboardButton("🌟 أنت عضو مميز VIP", callback_data="vip_notice")])
    return InlineKeyboardMarkup(keyboard)


def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع ملف جديد للمتجر", callback_data="admin_start_upload"),
         InlineKeyboardButton("📦 إدارة وحذف المنتجات", callback_data="admin_manage_products")],
        [InlineKeyboardButton("📢 قنوات الإجباري", callback_data="admin_manage_channels"),
         InlineKeyboardButton("📣 إذاعة عامة للكل", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton("📊 إحصائيات المتجر", callback_data="admin_stats_dashboard"),
         InlineKeyboardButton("🎟️ توليد كود VIP", callback_data="admin_generate_code")],
        [InlineKeyboardButton("📋 الأكواد النشطة", callback_data="admin_list_promo_codes"),
         InlineKeyboardButton("👥 إدارة المستخدمين", callback_data="admin_users_0")],
        [InlineKeyboardButton("💾 نسخة احتياطية", callback_data="admin_backup_db"),
         InlineKeyboardButton("🛒 واجهة المتجر", callback_data="switch_to_user_view")],
    ])


def back_to_home_keyboard(is_admin=False):
    target = "admin_panel" if is_admin else "home"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data=target)]])


def back_button(target):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=target)]])

# ==================== عروض القوائم (تُحدَّث بعد كل عملية) ====================

async def show_promo_codes(query):
    with get_db_connection() as conn:
        codes = conn.execute(
            "SELECT code, max_uses, uses_count, expires_at FROM promo_codes WHERE expires_at >= ? AND uses_count < max_uses",
            (today_str(),),
        ).fetchall()
    if not codes:
        await safe_edit(query, "📋 <b>الأكواد النشطة:</b> لا توجد أكواد حالياً.", back_button("admin_panel"))
        return
    msg = "📋 <b>الأكواد النشطة:</b>\n\n"
    keyboard = []
    for c in codes:
        msg += f"🔑 <code>{c['code']}</code> | استخدام: <code>{c['uses_count']}/{c['max_uses']}</code> | ينتهي: <code>{c['expires_at']}</code>\n"
        keyboard.append([InlineKeyboardButton(f"🗑️ حذف {c['code']}", callback_data=f"del_promo_{c['code']}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
    await safe_edit(query, msg, InlineKeyboardMarkup(keyboard))


async def show_channels(query):
    with get_db_connection() as conn:
        channels = conn.execute("SELECT id, channel_username, channel_link FROM forced_channels").fetchall()
    keyboard = []
    for ch in channels:
        keyboard.append([
            InlineKeyboardButton(f"📢 {ch['channel_username']}", url=normalize_link(ch["channel_link"], ch["channel_username"])),
            InlineKeyboardButton("🗑️ حذف", callback_data=f"del_ch_{ch['id']}"),
        ])
    keyboard.append([InlineKeyboardButton("➕ إضافة قناة", callback_data="admin_add_channel")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
    await safe_edit(query, "📢 <b>قنوات الإجباري:</b>", InlineKeyboardMarkup(keyboard))


async def show_products_admin(query):
    with get_db_connection() as conn:
        products = conn.execute("SELECT id, title FROM products ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)).fetchall()
    if not products:
        await safe_edit(query, "📦 لا توجد منتجات.", back_button("admin_panel"))
        return
    keyboard = [[InlineKeyboardButton(f"🗑️ {p['title'][:25]}", callback_data=f"del_prod_{p['id']}")] for p in products]
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
    await safe_edit(query, "🗑️ <b>إدارة المنتجات</b> (اضغط على المنتج لحذفه):", InlineKeyboardMarkup(keyboard))


USERS_PER_PAGE = 8


async def show_users(query, page=0):
    with get_db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
        users = conn.execute(
            "SELECT user_id, full_name, is_banned, is_vip FROM users ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (USERS_PER_PAGE, page * USERS_PER_PAGE),
        ).fetchall()
    text = f"👥 <b>المستخدمين (الإجمالي: {total}):</b>"
    keyboard = []
    for u in users:
        status = "🔴 محظور" if u["is_banned"] else ("🌟 VIP" if u["is_vip"] else "🟢 عادي")
        b_data = f"unban_{u['user_id']}_{page}" if u["is_banned"] else f"ban_{u['user_id']}_{page}"
        b_text = "🔓 فتح" if u["is_banned"] else "⛔ حظر"
        name = (u["full_name"] or "؟")[:10]
        keyboard.append([
            InlineKeyboardButton(f"👤 {name} ({status})", callback_data="none"),
            InlineKeyboardButton(b_text, callback_data=b_data),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"admin_users_{page - 1}"))
    if (page + 1) * USERS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"admin_users_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def send_backup(context, chat_id):
    if not os.path.exists(DB_PATH):
        await context.bot.send_message(chat_id=chat_id, text="❌ ملف قاعدة البيانات غير موجود!")
        return
    try:
        with open(DB_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename="wassab_store_backup.db",
                caption="💾 نسخة احتياطية لقاعدة بيانات متجر وصاب",
            )
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ حدث خطأ: {e}")


async def render_home(update: Update, user, edit=False):
    is_vip = check_vip(user.id)
    if is_owner(user.id):
        text = f"👑 <b>أهلاً بك يا مالك المتجر ({html.escape(user.full_name)})!</b>"
        markup = admin_menu_keyboard()
    else:
        badge = "💎 [عضو مميز VIP]" if is_vip else ""
        text = (
            f"🔥 <b>أهلاً بك يا أخي {user.mention_html()} {badge} في متجرنا الرقمي المميز!</b>\n\n"
            "✨ إليك منصتك المتكاملة لتحميل أرقى الكتب، الصور، والفيديوهات."
        )
        markup = user_menu_keyboard(is_vip)
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")

# ==================== الأوامر ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if check_banned(user.id):
        await update.effective_message.reply_text("🚫 عذراً، لقد تم حظرك من استخدام هذا البوت.")
        return

    referrer_id = 0
    if context.args:
        try:
            referrer_id = int(context.args[0])
        except ValueError:
            referrer_id = 0

    unsub = await get_unsubscribed_channels(user.id, context)
    if unsub:
        sub_text = (
            f"⚠️ <b>عذراً يا أخي {user.mention_html()}، لا يمكنك استخدام البوت حالياً.</b>\n\n"
            "🔔 يشترط الاشتراك في قنوات المتجر التالية لتفعيل البوت:\n"
        )
        for ch_username, _ in unsub:
            sub_text += f"👉 {html.escape(ch_username)}\n"
        sub_text += "\nقم بالاشتراك ثم اضغط على زر <b>(تحقق من الاشتراك)</b> بالأسفل 👇"
        await update.effective_message.reply_text(sub_text, reply_markup=subscription_keyboard(unsub), parse_mode="HTML")
        return

    is_new, actual_ref = add_user_with_referral(user.id, user.full_name, user.username or "لا يوجد", referrer_id)
    if is_new and actual_ref:
        try:
            await context.bot.send_message(
                chat_id=actual_ref,
                text=f"🎁 <b>مبروك!</b> انضم مستخدم جديد عبر رابطك وحصلت على <b>{REFERRAL_POINTS} نقطة</b>!",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await render_home(update, user)


async def admin_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ هذا الأمر مخصص لمالك المتجر فقط!")
        return
    await update.message.reply_text(
        "⚙️ <b>لوحة تحكم المسؤول:</b>\nاختر الإجراء المطلوب:",
        reply_markup=admin_menu_keyboard(),
        parse_mode="HTML",
    )


async def _set_ban(update, context, value):
    if not is_owner(update.effective_user.id):
        return
    try:
        target = int(context.args[0])
    except (IndexError, ValueError):
        cmd = "ban" if value else "unban"
        await update.message.reply_text(f"⚠️ مثال: <code>/{cmd} 123456789</code>", parse_mode="HTML")
        return
    if is_owner(target):
        await update.message.reply_text("❌ لا يمكنك حظر مالك البوت!")
        return
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (value, target))
    await update.message.reply_text(f"✅ تم {'حظر' if value else 'فك حظر'} المستخدم <code>{target}</code>.", parse_mode="HTML")


async def ban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_ban(update, context, 1)


async def unban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_ban(update, context, 0)


async def backup_database_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await send_backup(context, update.effective_user.id)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    clear_state(user.id)
    markup = admin_menu_keyboard() if is_owner(user.id) else user_menu_keyboard(check_vip(user.id))
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=markup)

# ==================== معالج الأزرار ====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_id = user.id

    if check_banned(user_id):
        await query.answer("🚫 تم حظرك!", show_alert=True)
        return

    data = query.data
    admin = is_owner(user_id)

    if data == "none":
        await query.answer()
        return

    # ---------- الاشتراك ----------
    if data == "check_sub":
        unsub = await get_unsubscribed_channels(user_id, context)
        if unsub:
            await query.answer("❌ لم تقم بالاشتراك في جميع القنوات بعد!", show_alert=True)
            return
        await query.answer("✅ تم التحقق من اشتراكك بنجاح.", show_alert=True)
        add_user_with_referral(user_id, user.full_name, user.username or "لا يوجد", 0)
        await render_home(update, user, edit=True)
        return

    if data == "vip_notice":
        with get_db_connection() as conn:
            row = conn.execute("SELECT vip_expires_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
        expiry = row["vip_expires_at"] if row and row["vip_expires_at"] else "غير محدد"
        await query.answer(f"🌟 حسابك VIP ساري حتى: {expiry}", show_alert=True)
        return

    # ---------- التنقل ----------
    if data in ("home", "switch_to_user_view"):
        await query.answer()
        is_vip = check_vip(user_id)
        if admin and data == "switch_to_user_view":
            text = "🛒 <b>عرض واجهة المتجر كما يراها المستخدمون:</b>"
            rows = [list(r) for r in user_menu_keyboard(is_vip).inline_keyboard]
            rows.append([InlineKeyboardButton("⚙️ العودة للوحة التحكم", callback_data="admin_panel")])
            markup = InlineKeyboardMarkup(rows)
        else:
            text = "🏠 <b>القائمة الرئيسية للمتجر:</b>"
            markup = user_menu_keyboard(is_vip)
        await safe_edit(query, text, markup)
        return

    if data == "admin_panel":
        if not admin:
            return
        await query.answer()
        clear_state(user_id)
        await safe_edit(query, "⚙️ <b>لوحة تحكم المسؤول:</b>", admin_menu_keyboard())
        return

    if data == "admin_backup_db":
        if not admin:
            return
        await query.answer("💾 جاري الإرسال...")
        await send_backup(context, user_id)
        return

    # ---------- النقاط والإحالة ----------
    if data == "get_daily_points":
        today = today_str()
        with get_db_connection() as conn:
            row = conn.execute("SELECT points, last_points_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                await query.answer("⚠️ أرسل /start أولاً.", show_alert=True)
                return
            if row["last_points_at"] == today:
                await query.answer(f"⏳ استلمت نقاط اليوم بالفعل. رصيدك: {row['points']}", show_alert=True)
                return
            conn.execute(
                "UPDATE users SET points = points + ?, last_points_at = ? WHERE user_id = ?",
                (DAILY_POINTS, today, user_id),
            )
            new_pts = row["points"] + DAILY_POINTS
        await query.answer(f"🎁 حصلت على {DAILY_POINTS} نقاط. رصيدك: {new_pts}", show_alert=True)
        return

    if data == "my_referral_link":
        await query.answer()
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        with get_db_connection() as conn:
            invited = conn.execute("SELECT COUNT(*) AS cnt FROM users WHERE invited_by = ?", (user_id,)).fetchone()["cnt"]
            row = conn.execute("SELECT points FROM users WHERE user_id = ?", (user_id,)).fetchone()
            pts = row["points"] if row else 0
        msg = (
            "🔗 <b>نظام الإحالات والأرباح:</b>\n\n"
            f"👤 رصيدك: <b>{pts} نقطة</b>\n"
            f"👥 دعوت: <b>{invited} شخص</b>\n\n"
            f"🎁 <b>رابطك:</b>\n<code>{ref_link}</code>"
        )
        await safe_edit(query, msg, back_button("home"))
        return

    # ---------- إحصائيات ----------
    if data == "admin_stats_dashboard":
        if not admin:
            return
        await query.answer()
        with get_db_connection() as conn:
            total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            total_vip = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_vip = 1").fetchone()["c"]
            total_banned = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned = 1").fetchone()["c"]
            total_products = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
            active_promo = conn.execute(
                "SELECT COUNT(*) AS c FROM promo_codes WHERE expires_at >= ? AND uses_count < max_uses",
                (today_str(),),
            ).fetchone()["c"]
        text = (
            "📊 <b>إحصائيات متجر وصاب:</b>\n\n"
            f"👥 المستخدمين: <code>{total_users}</code>\n"
            f"🌟 أعضاء VIP: <code>{total_vip}</code>\n"
            f"🚫 المحظورين: <code>{total_banned}</code>\n"
            f"📦 المنتجات: <code>{total_products}</code>\n"
            f"🎟️ الأكواد النشطة: <code>{active_promo}</code>"
        )
        await safe_edit(query, text, back_button("admin_panel"))
        return

    # ---------- أكواد VIP ----------
    if data == "admin_generate_code":
        if not admin:
            return
        code = "VIP-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        max_uses = 5
        expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO promo_codes (code, max_uses, uses_count, expires_at, created_by) VALUES (?, ?, 0, ?, ?)",
                (code, max_uses, expires, user_id),
            )
        await query.answer("✅ تم توليد الكود!", show_alert=True)
        msg = f"🎟️ <b>كود VIP جديد:</b>\n<code>{code}</code>\nاستخدامات: {max_uses} | ينتهي الكود: {expires}"
        await safe_edit(query, msg, back_button("admin_panel"))
        return

    if data == "admin_list_promo_codes":
        if not admin:
            return
        await query.answer()
        await show_promo_codes(query)
        return

    if data.startswith("del_promo_"):
        if not admin:
            return
        code = data[len("del_promo_"):]
        with get_db_connection() as conn:
            conn.execute("DELETE FROM promo_codes WHERE code = ?", (code,))
        await query.answer("✅ تم الحذف!")
        await show_promo_codes(query)
        return

    if data == "enter_promo_code":
        if check_vip(user_id):
            await query.answer("🌟 أنت VIP بالفعل!", show_alert=True)
            return
        await query.answer()
        set_state(user_id, {"step": "waiting_promo_code"})
        await safe_edit(query, "🎟️ <b>أرسل الكود الترويجي الآن:</b>\n\n⚠️ للإلغاء أرسل /cancel", back_button("home"))
        return

    if data == "contact_support":
        await query.answer()
        set_state(user_id, {"step": "waiting_support_message"})
        await safe_edit(query, "📞 <b>اكتب رسالتك للإدارة:</b>\n\n⚠️ للإلغاء أرسل /cancel", back_button("home"))
        return

    # ---------- قنوات الإجباري ----------
    if data == "admin_manage_channels":
        if not admin:
            return
        await query.answer()
        await show_channels(query)
        return

    if data == "admin_add_channel":
        if not admin:
            return
        await query.answer()
        set_state(user_id, {"step": "waiting_channel_username"})
        await safe_edit(
            query,
            "➕ أرسل معرف القناة (مثل <code>@wassab_1</code>):\n\n"
            "⚠️ يجب أن يكون البوت <b>أدمن</b> في القناة.\n⚠️ للإلغاء أرسل /cancel",
            back_to_home_keyboard(True),
        )
        return

    if data.startswith("del_ch_"):
        if not admin:
            return
        ch_id = data[len("del_ch_"):]
        with get_db_connection() as conn:
            conn.execute("DELETE FROM forced_channels WHERE id = ?", (ch_id,))
        await query.answer("✅ تم الحذف!")
        await show_channels(query)
        return

    # ---------- الإذاعة ----------
    if data == "admin_broadcast_start":
        if not admin:
            return
        await query.answer()
        set_state(user_id, {"action": "broadcast"})
        await safe_edit(query, "📣 <b>أرسل رسالة الإذاعة للجميع:</b>\n\n⚠️ للإلغاء أرسل /cancel", back_to_home_keyboard(True))
        return

    # ---------- رفع الملفات ----------
    if data == "admin_start_upload":
        if not admin:
            return
        await query.answer()
        set_state(user_id, {"step": "waiting_category"})
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📂 عادي", callback_data="set_cat_free"),
            InlineKeyboardButton("💎 VIP", callback_data="set_cat_paid"),
        ]])
        await safe_edit(query, "📤 <b>اختر التصنيف:</b>", kb)
        return

    if data.startswith("set_cat_"):
        if not admin:
            return
        st = get_state(user_id)
        if st.get("step") != "waiting_category":
            await query.answer("⚠️ ابدأ الرفع من جديد.", show_alert=True)
            return
        update_state(user_id, category=data.split("_")[2], step="waiting_type")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📚 كتب", callback_data="set_type_book"),
            InlineKeyboardButton("🖼️ صور", callback_data="set_type_photo"),
            InlineKeyboardButton("🎬 فيديو", callback_data="set_type_video"),
        ]])
        await query.answer()
        await safe_edit(query, "📂 <b>اختر نوع المحتوى:</b>", kb)
        return

    if data.startswith("set_type_"):
        if not admin:
            return
        st = get_state(user_id)
        if "category" not in st:
            await query.answer("⚠️ ابدأ الرفع من جديد.", show_alert=True)
            return
        update_state(user_id, file_type=data.split("_")[2], step="waiting_subcat")
        await query.answer()
        await safe_edit(query, "📂 <b>اكتب اسم القسم الفرعي:</b>", back_to_home_keyboard(True))
        return

    # ---------- تصفح الأقسام ----------
    if data in ("cat_books", "cat_videos", "cat_photos"):
        f_type_map = {
            "cat_books": ("book", "📚 الكتب"),
            "cat_videos": ("video", "🎬 الفيديوهات"),
            "cat_photos": ("photo", "🖼️ الصور"),
        }
        f_type, title_text = f_type_map[data]
        await query.answer()
        back_target = "switch_to_user_view" if admin else "home"
        with get_db_connection() as conn:
            sub_cats = conn.execute(
                """SELECT DISTINCT s.id, s.name FROM sub_categories s
                   JOIN products p ON p.sub_category = s.name
                   WHERE p.file_type = ? AND p.category != 'paid'""",
                (f_type,),
            ).fetchall()
        if not sub_cats:
            await safe_edit(query, f"{title_text}\n\n⚠️ لا توجد محتويات.", back_button(back_target))
            return
        keyboard = [[InlineKeyboardButton(f"📁 {s['name']}", callback_data=f"vs_{f_type}_{s['id']}")] for s in sub_cats]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=back_target)])
        await safe_edit(query, f"{title_text}\n\n👇 اختر القسم:", InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("vs_"):
        _, f_type, sid = data.split("_")
        await query.answer()
        with get_db_connection() as conn:
            sc = conn.execute("SELECT name FROM sub_categories WHERE id = ?", (sid,)).fetchone()
            if not sc:
                await query.answer("❌ القسم غير موجود!", show_alert=True)
                return
            products = conn.execute(
                "SELECT id, title FROM products WHERE file_type = ? AND sub_category = ? AND category != 'paid' LIMIT ?",
                (f_type, sc["name"], MAX_LIST_BUTTONS),
            ).fetchall()
        keyboard = [[InlineKeyboardButton(f"📥 {p['title']}", callback_data=f"get_item_{p['id']}")] for p in products]
        back = {"book": "cat_books", "video": "cat_videos", "photo": "cat_photos"}.get(f_type, "home")
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=back)])
        await safe_edit(query, f"📁 <b>قسم: {html.escape(sc['name'])}</b>", InlineKeyboardMarkup(keyboard))
        return

    if data == "cat_paid":
        await query.answer()
        back_target = "switch_to_user_view" if admin else "home"
        with get_db_connection() as conn:
            products = conn.execute(
                "SELECT id, title FROM products WHERE category = 'paid' LIMIT ?", (MAX_LIST_BUTTONS,)
            ).fetchall()
        if not products:
            await safe_edit(query, "💎 <b>القسم المدفوع:</b> لا توجد محتويات.", back_button(back_target))
            return
        keyboard = [[InlineKeyboardButton(f"📥 {p['title']}", callback_data=f"get_item_{p['id']}")] for p in products]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=back_target)])
        await safe_edit(query, "💎 <b>القسم المدفوع VIP:</b>", InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("get_item_"):
        item_id = data[len("get_item_"):]
        with get_db_connection() as conn:
            item = conn.execute(
                "SELECT title, file_type, file_id, category FROM products WHERE id = ?", (item_id,)
            ).fetchone()
        if not item:
            await query.answer("❌ الملف غير متوفر!", show_alert=True)
            return
        title, f_type, file_id, category = item["title"], item["file_type"], item["file_id"], item["category"]
        if category == "paid" and not admin and not check_vip(user_id):
            await query.answer("💎 هذا المحتوى لـ VIP فقط!", show_alert=True)
            return
        await query.answer("🚀 جاري إرسال الملف...")
        try:
            if f_type == "book":
                await context.bot.send_document(chat_id=user_id, document=file_id, caption=f"📖 {title}")
            elif f_type == "photo":
                await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=f"🖼️ {title}")
            elif f_type == "video":
                await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"🎬 {title}")
            update_user_stats(user_id, f_type, category)
        except Exception as e:
            logger.warning("فشل إرسال الملف: %s", e)
            await context.bot.send_message(chat_id=user_id, text="❌ تعذر إرسال الملف، حاول لاحقاً.")
        return

    # ---------- إدارة المنتجات ----------
    if data == "admin_manage_products":
        if not admin:
            return
        await query.answer()
        await show_products_admin(query)
        return

    if data.startswith("del_prod_"):
        if not admin:
            return
        p_id = data[len("del_prod_"):]
        with get_db_connection() as conn:
            conn.execute("DELETE FROM products WHERE id = ?", (p_id,))
        await query.answer("✅ تم الحذف!")
        await show_products_admin(query)
        return

    # ---------- إدارة المستخدمين ----------
    if data.startswith("admin_users_"):
        if not admin:
            return
        await query.answer()
        await show_users(query, int(data.split("_")[2]))
        return

    if data.startswith("ban_") or data.startswith("unban_"):
        if not admin:
            return
        action, target, page = data.split("_")
        target = int(target)
        if is_owner(target):
            await query.answer("❌ لا يمكنك حظر المالك!", show_alert=True)
            return
        with get_db_connection() as conn:
            conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if action == "ban" else 0, target))
        await query.answer("✅ تم!")
        await show_users(query, int(page))
        return

    await query.answer()

# ==================== معالجة الرسائل ====================

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    if not message or not user:
        return
    if check_banned(user.id):
        return

    state = get_state(user.id)
    step = state.get("step")

    # ---- كود VIP ----
    if step == "waiting_promo_code":
        if not message.text:
            await message.reply_text("⚠️ أرسل الكود كنص.")
            return
        code = message.text.strip().upper()
        add_user_with_referral(user.id, user.full_name, user.username or "لا يوجد", 0)
        with get_db_connection() as conn:
            promo = conn.execute(
                "SELECT code, max_uses, uses_count, expires_at FROM promo_codes WHERE code = ?", (code,)
            ).fetchone()
            if not promo:
                await message.reply_text("❌ كود خاطئ! تأكد من كتابته بدقة.")
                return
            if promo["expires_at"] and today_str() > promo["expires_at"]:
                await message.reply_text("⏳ الكود منتهي الصلاحية!")
                return
            cur = conn.execute(
                "UPDATE promo_codes SET uses_count = uses_count + 1 WHERE code = ? AND uses_count < max_uses", (code,)
            )
            if cur.rowcount == 0:
                await message.reply_text("⚠️ الكود استنفد عدد مرات الاستخدام!")
                return
            expiry = (datetime.now() + timedelta(days=VIP_DAYS)).strftime("%Y-%m-%d")
            conn.execute("UPDATE users SET is_vip = 1, vip_expires_at = ? WHERE user_id = ?", (expiry, user.id))
        clear_state(user.id)
        await message.reply_text(
            f"🎉 <b>مبروك! تم تفعيل VIP لمدة {VIP_DAYS} يوماً!</b>\n\n⏳ ينتهي في: <code>{expiry}</code>",
            reply_markup=user_menu_keyboard(True),
            parse_mode="HTML",
        )
        return

    # ---- رسالة دعم ----
    if step == "waiting_support_message":
        if not message.text:
            await message.reply_text("⚠️ أرسل رسالة نصية.")
            return
        support_text = (
            "📩 <b>رسالة دعم فني:</b>\n"
            f"👤 {html.escape(user.full_name)} (<code>{user.id}</code>)\n\n{html.escape(message.text)}"
        )
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=support_text, parse_mode="HTML")
            clear_state(user.id)
            await message.reply_text("✅ تم إرسال رسالتك للإدارة!", reply_markup=user_menu_keyboard(check_vip(user.id)))
        except Exception:
            await message.reply_text("❌ فشل إرسال الرسالة.")
        return

    # ---- ما بعد هذا للمالك فقط ----
    if not is_owner(user.id):
        return

    if step == "waiting_channel_username" and message.text:
        uname = message.text.strip()
        if not uname.startswith("@"):
            uname = "@" + uname
        update_state(user.id, channel_username=uname, step="waiting_channel_link")
        await message.reply_text("🔗 أرسل رابط الدعوة الخاص بالقناة:")
        return

    if step == "waiting_channel_link" and message.text:
        ch_link = message.text.strip()
        ch_uname = state.get("channel_username")
        try:
            with get_db_connection() as conn:
                conn.execute(
                    "INSERT INTO forced_channels (channel_username, channel_link) VALUES (?, ?)", (ch_uname, ch_link)
                )
            ok = True
        except sqlite3.IntegrityError:
            ok = False
        clear_state(user.id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ إدارة القنوات", callback_data="admin_manage_channels")]])
        await message.reply_text("✅ تمت الإضافة!" if ok else "❌ القناة موجودة مسبقاً!", reply_markup=kb)
        return

    if state.get("action") == "broadcast":
        clear_state(user.id)
        await message.reply_text("🚀 جاري الإذاعة...")
        with get_db_connection() as conn:
            all_users = conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()
        ok_count, fail_count = 0, 0
        for u in all_users:
            if is_owner(u["user_id"]):
                continue
            try:
                await context.bot.copy_message(
                    chat_id=u["user_id"], from_chat_id=message.chat_id, message_id=message.message_id
                )
                ok_count += 1
            except Exception:
                fail_count += 1
            await asyncio.sleep(0.05)
        await message.reply_text(
            f"✅ تمت الإذاعة.\nإرسال: <code>{ok_count}</code> | فشل: <code>{fail_count}</code>", parse_mode="HTML"
        )
        return

    if step == "waiting_subcat" and message.text:
        name = message.text.strip()[:40]
        with get_db_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO sub_categories (name) VALUES (?)", (name,))
        update_state(user.id, sub_category=name, step="waiting_file")
        await message.reply_text("📥 أرسل الملف الآن:")
        return

    if step == "waiting_file":
        expected = state.get("file_type")
        file_id = None
        if expected == "book" and message.document:
            file_id = message.document.file_id
        elif expected == "photo" and message.photo:
            file_id = message.photo[-1].file_id
        elif expected == "video" and message.video:
            file_id = message.video.file_id
        if not file_id:
            await message.reply_text(f"❌ يجب إرسال ملف مطابق للنوع ({expected}).")
            return
        update_state(user.id, file_id=file_id, step="waiting_title")
        await message.reply_text("✍️ أرسل عنوان المنتج:")
        return

    if step == "waiting_title" and message.text:
        title = message.text.strip()
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO products (title, file_type, file_id, category, sub_category) VALUES (?, ?, ?, ?, ?)",
                (title, state["file_type"], state["file_id"], state["category"], state.get("sub_category", "عام")),
            )
        clear_state(user.id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ لوحة التحكم", callback_data="admin_panel")]])
        await message.reply_text("✅ تم رفع المنتج بنجاح!", reply_markup=kb)
        return


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطأ أثناء معالجة التحديث", exc_info=context.error)

# ==================== بناء التطبيق ====================

application = ApplicationBuilder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("admin", admin_panel_cmd))
application.add_handler(CommandHandler("ban", ban_user_cmd))
application.add_handler(CommandHandler("unban", unban_user_cmd))
application.add_handler(CommandHandler("backup", backup_database_cmd))
application.add_handler(CommandHandler("cancel", cancel_cmd))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_messages))
application.add_error_handler(error_handler)

# ==================== الويب هوك (loop واحد دائم) ====================

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()


async def _setup():
    await application.initialize()
    await application.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info("تم ضبط الويب هوك: %s", BASE_URL)


asyncio.run_coroutine_threadsafe(_setup(), loop).result(timeout=60)


@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook_listener():
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
    except Exception:
        logger.exception("خطأ في الويب هوك")
    return "OK", 200


@flask_app.route("/")
def home():
    return "Wassab Store Bot is running live via Webhook! 🚀", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
