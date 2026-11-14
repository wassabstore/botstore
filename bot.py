import os
import sqlite3
import contextlib
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import asyncio
from datetime import datetime, timedelta

# ==================== الإعدادات الرئيسية ====================
TOKEN = "9157079266:AAHnLCeWZCFsdR5YCRLs9clVghaYzqYDpTE"
OWNER_ID = 8233150959  # معرف مالك البوت الأساسي المحمى (Wassab Store)
ADMIN_ID = OWNER_ID

WEBHOOK_URL = f"https://botstore-54tw.onrender.com/{TOKEN}"

flask_app = Flask(__name__)
ADMIN_STATES = {}

# ==================== إدارة قاعدة البيانات ====================

@contextlib.contextmanager
def get_db_connection():
    conn = sqlite3.connect("wassab_store.db")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        
        # جدول المستخدمين مع عمود تاريخ انتهاء الـ VIP
        cursor.execute("""
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
                invited_by INTEGER DEFAULT 0
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                file_type TEXT, 
                file_id TEXT,
                category TEXT,
                sub_category TEXT DEFAULT 'عام'
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS forced_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT UNIQUE,
                channel_link TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                max_uses INTEGER DEFAULT 1,
                uses_count INTEGER DEFAULT 0,
                expires_at TEXT,
                created_by INTEGER
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_invited_by ON users(invited_by);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_type_subcat ON products(file_type, sub_category);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);")
        
        conn.commit()

init_db()

# ==================== دوال المساعدة ====================

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

def check_banned(user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row["is_banned"] == 1 if row else False

def check_vip(user_id):
    """فحص حالة الـ VIP مع التحقق من انتهاء الصلاحية تلقائياً بعد 30 يوماً"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_vip, vip_expires_at FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if not row or row["is_vip"] == 0:
            return False
            
        # التحقق من تاريخ انتهاء الصلاحية
        if row["vip_expires_at"]:
            current_date = datetime.now().strftime("%Y-%m-%d")
            if current_date > row["vip_expires_at"]:
                # انتهت المدة، إلغاء الـ VIP تلقائياً
                cursor.execute("UPDATE users SET is_vip = 0, vip_expires_at = NULL WHERE user_id = ?", (user_id,))
                conn.commit()
                return False
                
        return True

def add_user_with_referral(user_id, full_name, username, referrer_id=0):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if row is None:
            actual_referrer = referrer_id if (referrer_id != 0 and referrer_id != user_id) else 0
            cursor.execute("""
                INSERT OR IGNORE INTO users (user_id, full_name, username, is_banned, is_vip, points, invited_by) 
                VALUES (?, ?, ?, 0, 0, 0, ?)
            """, (user_id, full_name, username, actual_referrer))
            
            if actual_referrer != 0:
                cursor.execute("UPDATE users SET points = points + 15 WHERE user_id = ?", (actual_referrer,))
            
            conn.commit()
            return True, actual_referrer
        return False, 0

def update_user_stats(user_id, f_type, category):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        b_col = "dl_books" if f_type == "book" else ("dl_photos" if f_type == "photo" else "dl_videos")
        c_col = "dl_paid" if category == "paid" else "dl_free"
        cursor.execute(f"UPDATE users SET {b_col} = {b_col} + 1, {c_col} = {c_col} + 1 WHERE user_id = ?", (user_id,))
        conn.commit()

async def get_unsubscribed_channels(user_id, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(user_id):
        return []

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT channel_username, channel_link FROM forced_channels")
        channels = cursor.fetchall()

    unsubscribed = []
    for ch in channels:
        ch_username, ch_link = ch["channel_username"], ch["channel_link"]
        try:
            member = await context.bot.get_chat_member(chat_id=ch_username, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                unsubscribed.append((ch_username, ch_link))
        except Exception as e:
            print(f"خطأ في التحقق من القناة {ch_username}: {e}")
            unsubscribed.append((ch_username, ch_link))
            
    return unsubscribed

# ==================== واجهات الأزرار ====================

def subscription_keyboard(unsubscribed_channels):
    keyboard = []
    for ch_username, ch_link in unsubscribed_channels:
        if not ch_link.startswith("http://") and not ch_link.startswith("https://"):
            ch_link = f"https://t.me/{ch_username.replace('@', '')}"
        keyboard.append([InlineKeyboardButton(f"📢 اشترك في القناة: {ch_username}", url=ch_link)])
    keyboard.append([InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="check_sub")])
    return InlineKeyboardMarkup(keyboard)

def user_menu_keyboard(is_vip=False):
    keyboard = [
        [
            InlineKeyboardButton("📚 قسم الكتب", callback_data="cat_books"),
            InlineKeyboardButton("🎬 قسم الفيديوهات", callback_data="cat_videos")
        ],
        [
            InlineKeyboardButton("🖼️ قسم الصور", callback_data="cat_photos"),
            InlineKeyboardButton("💎 القسم المدفوع VIP", callback_data="cat_paid")
        ],
        [
            InlineKeyboardButton("🎁 تجميع النقاط", callback_data="get_daily_points"),
            InlineKeyboardButton("🔗 دعوة أصدقاء", callback_data="my_referral_link")
        ],
        [
            InlineKeyboardButton("🎟️ إدخال كود VIP ترويجي", callback_data="enter_promo_code"),
            InlineKeyboardButton("📞 الدعم الفني", callback_data="contact_support")
        ]
    ]
    if is_vip:
        keyboard.append([InlineKeyboardButton("🌟 أنت عضو مميز VIP لديك صلاحيات خاصة", callback_data="vip_notice")])
    return InlineKeyboardMarkup(keyboard)

def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 رفع ملف جديد للمتجر", callback_data="admin_start_upload"),
            InlineKeyboardButton("📦 إدارة وحذف المنتجات", callback_data="admin_manage_products")
        ],
        [
            InlineKeyboardButton("📢 قنوات الإجباري", callback_data="admin_manage_channels"),
            InlineKeyboardButton("📢 إذاعة عامة للكل", callback_data="admin_broadcast_start")
        ],
        [
            InlineKeyboardButton("📊 إحصائيات المتجر", callback_data="admin_stats_dashboard"),
            InlineKeyboardButton("🎟️ توليد كود VIP", callback_data="admin_generate_code")
        ],
        [
            InlineKeyboardButton("📋 الأكواد النشطة", callback_data="admin_list_promo_codes"),
            InlineKeyboardButton("👥 إدارة المستخدمين", callback_data="admin_users")
        ],
        [
            InlineKeyboardButton("💾 نسخة احتياطية", callback_data="admin_backup_db"),
            InlineKeyboardButton("🛒 واجهة المتجر", callback_data="switch_to_user_view")
        ]
    ])

def back_to_home_keyboard(is_admin=False):
    target = "admin_panel" if is_admin else "home"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data=target)]])

# ==================== دوال المعالجة ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if check_banned(user.id):
        if update.message:
            await update.message.reply_text("🚫 عذراً، لقد تم حظرك من استخدام هذا البوت.")
        return

    referrer_id = 0
    if context.args:
        try:
            referrer_id = int(context.args[0])
        except ValueError:
            referrer_id = 0

    unsub_channels = await get_unsubscribed_channels(user.id, context)
    if unsub_channels:
        sub_text = (
            f"⚠️ **عذراً يا أخي {user.mention_html()}، لا يمكنك استخدام البوت حالياً.**\n\n"
            f"🔔 يشترط الاشتراك في قنوات المتجر التالية لتفعيل البوت:\n"
        )
        for ch_username, _ in unsub_channels:
            sub_text += f"👉 {ch_username}\n"
        sub_text += "\nقم بالاشتراك بالقنوات ثم اضغط على زر **(تحقق من الاشتراك)** بالأسفل 👇"

        if update.message:
            await update.message.reply_text(sub_text, reply_markup=subscription_keyboard(unsub_channels), parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.answer("❌ لم تقم بالاشتراك في جميع القنوات بعد!", show_alert=True)
            await update.callback_query.edit_message_text(sub_text, reply_markup=subscription_keyboard(unsub_channels), parse_mode="HTML")
        return

    is_new, actual_ref = add_user_with_referral(user.id, user.full_name, user.username or "لا يوجد", referrer_id)
    
    if is_new and actual_ref != 0:
        try:
            await context.bot.send_message(
                chat_id=actual_ref,
                text="🎁 **مبروك!** انضم مستخدم جديد عبر رابط إحالتك وحصلت على **15 نقطة** رصيد إضافي!"
            )
        except Exception:
            pass

    admin_status = is_owner(user.id)
    is_vip = check_vip(user.id)

    if admin_status:
        welcome_text = f"👑 **أهلاً بك يا مالك المتجر العظيم ({user.full_name})!**"
        reply_markup = admin_menu_keyboard()
    else:
        vip_badge = "💎 [عضو مميز VIP]" if is_vip else ""
        welcome_text = (
            f"🔥 **أهلاً بك يا أخي {user.mention_html()} {vip_badge} في متجرنا الرقمي المميز!**\n\n"
            "✨ إليك منصتك المتكاملة لتحميل أرقى الكتب، الصور، والفيديوهات."
        )
        reply_markup = user_menu_keyboard(is_vip)

    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode="HTML")

async def admin_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⛔ عذراً، هذا الأمر مخصص لمالك المتجر فقط!")
        return
    await update.message.reply_text(
        "⚙️ **لوحة تحكم المسؤول الخارقة:**\nمرحباً بك يا قائد المتجر. اختر الإجراء المطلوب:",
        reply_markup=admin_menu_keyboard(),
        parse_mode="HTML"
    )

async def ban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        return
    try:
        target_user_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ يرجى تحديد معرف المستخدم (ID) المراد حظره.\nمثال: `/ban 123456789`", parse_mode="Markdown")
        return

    if is_owner(target_user_id):
        await update.message.reply_text("❌ لا يمكنك حظر مالك البوت الأساسي!")
        return

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_user_id,))
        conn.commit()

    await update.message.reply_text(f"✅ تم حظر المستخدم `{target_user_id}` بنجاح.", parse_mode="Markdown")

async def backup_database_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        return
    db_file = "wassab_store.db"
    if not os.path.exists(db_file):
        await update.message.reply_text("❌ ملف قاعدة البيانات غير موجود!")
        return
    try:
        with open(db_file, "rb") as f:
            await context.bot.send_document(
                chat_id=user.id,
                document=f,
                filename="wassab_store_backup.db",
                caption="💾 **نسخة احتياطية لقاعدة بيانات متجر وصاب:**",
                parse_mode="Markdown"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ حدث خطأ: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_id = user.id
    
    if check_banned(user_id):
        await query.answer("🚫 تم حظرك!", show_alert=True)
        return

    data = query.data

    if data == "check_sub":
        unsub = await get_unsubscribed_channels(user_id, context)
        if not unsub:
            await query.answer("✅ تم التحقق من اشتراكك بنجاح.", show_alert=True)
            await start(update, context)
        else:
            await query.answer("❌ لم تقم بالاشتراك في جميع القنوات بعد!", show_alert=True)
        return

    if data == "vip_notice":
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT vip_expires_at FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            expiry_str = row["vip_expires_at"] if row and row["vip_expires_at"] else "غير محدد"
        await query.answer(f"🌟 حسابك VIP ساري حتى تاريخ: {expiry_str}", show_alert=True)
        return

    admin_status = is_owner(user_id)

    if data == "home" or data == "switch_to_user_view":
        await query.answer()
        is_vip = check_vip(user_id)
        if admin_status and data == "switch_to_user_view":
            text = "🛒 **عرض واجهة المتجر كما يراها المستخدمون:**"
            markup_items = user_menu_keyboard(is_vip).inline_keyboard.copy()
            markup_items.append([InlineKeyboardButton("⚙️ العودة لوحة تحكم المالك", callback_data="admin_panel")])
            keyboard = InlineKeyboardMarkup(markup_items)
        else:
            text = "🏠 **القائمة الرئيسية للمتجر:**"
            keyboard = user_menu_keyboard(is_vip)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    elif data == "admin_panel":
        if not admin_status: return
        await query.answer()
        ADMIN_STATES.pop(user_id, None)
        await query.edit_message_text("⚙️ **لوحة تحكم المسؤول الخارقة:**", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
        return
    
    elif data == "admin_backup_db":
        if not admin_status: return
        await query.answer("💾 جاري الإرسال...")
        await backup_database_cmd(update, context)
        return

    elif data == "get_daily_points":
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET points = points + 10 WHERE user_id = ?", (user_id,))
            cursor.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
            new_pts = cursor.fetchone()["points"]
            conn.commit()
        await query.answer(f"🎁 حصلت على 10 نقاط. رصيدك: {new_pts}", show_alert=True)
        return

    elif data == "my_referral_link":
        await query.answer()
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS cnt FROM users WHERE invited_by = ?", (user_id,))
            invited_count = cursor.fetchone()["cnt"]
            cursor.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
            user_pts = cursor.fetchone()["points"]

        msg_text = (
            f"🔗 **نظام الإحالات والأرباح:**\n\n"
            f"👤 رصيدك: **{user_pts} نقطة**\n"
            f"👥 دعيت: **{invited_count} شخص**\n\n"
            f"🎁 **رابطك:**\n`{ref_link}`"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="home")]])
        await query.edit_message_text(msg_text, reply_markup=keyboard, parse_mode="Markdown")
        return

    elif data == "admin_stats_dashboard":
        if not admin_status: return
        await query.answer()
        current_date = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS cnt FROM users")
            total_users = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_vip = 1")
            total_vip = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_banned = 1")
            total_banned = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) AS cnt FROM products")
            total_products = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) AS cnt FROM promo_codes WHERE expires_at >= ? AND uses_count < max_uses", (current_date,))
            active_promo = cursor.fetchone()["cnt"]

        stats_text = (
            f"📊 **إحصائيات متجر وصاب:**\n\n"
            f"👥 المستخدمين: `{total_users}`\n"
            f"🌟 أعضاء VIP: `{total_vip}`\n"
            f"🚫 المحظورين: `{total_banned}`\n"
            f"📦 المنتجات: `{total_products}`\n"
            f"🎟️ الأكواد النشطة: `{active_promo}`"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")]])
        await query.edit_message_text(stats_text, reply_markup=keyboard, parse_mode="Markdown")
        return

    elif data == "admin_generate_code":
        if not admin_status: return
        import random, string
        rand_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        promo_code = f"VIP-{rand_str}"
        max_uses = 5
        expires_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO promo_codes (code, max_uses, uses_count, expires_at, created_by) VALUES (?, ?, 0, ?, ?)",
                (promo_code, max_uses, expires_date, user_id)
            )
            conn.commit()
            
        await query.answer("✅ تم توليد الكود!", show_alert=True)
        code_msg = f"🎟️ **كود VIP جديد:**\n`{promo_code}`\nاستخدامات: {max_uses} | ينتهي الكود: {expires_date}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")]])
        await query.edit_message_text(code_msg, reply_markup=keyboard, parse_mode="Markdown")
        return

    elif data == "admin_list_promo_codes":
        if not admin_status: return
        await query.answer()
        current_date = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT code, max_uses, uses_count, expires_at FROM promo_codes WHERE expires_at >= ? AND uses_count < max_uses", (current_date,))
            active_codes = cursor.fetchall()

        if not active_codes:
            msg = "📋 **الأكواد النشطة:** لا توجد أكواد حالياً."
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")]])
            await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
            return

        msg = "📋 **الأكواد النشطة:**\n\n"
        keyboard = []
        for c in active_codes:
            msg += f"🔑 `{c['code']}` | استخدام: `{c['uses_count']}/{c['max_uses']}` | ينتهي: `{c['expires_at']}`\n"
            keyboard.append([InlineKeyboardButton(f"🗑️ حذف {c['code']}", callback_data=f"del_promo_{c['code']}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
        await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        return

    elif data.startswith("del_promo_"):
        if not admin_status: return
        code_to_delete = data.split("_")[2]
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM promo_codes WHERE code = ?", (code_to_delete,))
            conn.commit()
        await query.answer("✅ تم الحذف!", show_alert=True)
        return await button_handler(update, context)

    elif data == "enter_promo_code":
        if check_vip(user_id):
            await query.answer("🌟 أنت VIP بالفعل!", show_alert=True)
            return
        await query.answer()
        ADMIN_STATES[user_id] = {"step": "waiting_promo_code"}
        await query.edit_message_text("🎟️ **أرسل الكود الترويجي الآن:**\n\n⚠️ للإلغاء أرسل `/cancel`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="home")]]))
        return

    elif data == "contact_support":
        await query.answer()
        ADMIN_STATES[user_id] = {"step": "waiting_support_message"}
        await query.edit_message_text("📞 **اكتب رسالتك للإدارة:**\n\n⚠️ للإلغاء أرسل `/cancel`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="home")]]))
        return

    elif data == "admin_manage_channels":
        if not admin_status: return
        await query.answer()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, channel_username, channel_link FROM forced_channels")
            channels = cursor.fetchall()
        keyboard = []
        for ch in channels:
            keyboard.append([
                InlineKeyboardButton(f"📢 {ch['channel_username']}", url=ch['channel_link']),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"del_ch_{ch['id']}")
            ])
        keyboard.append([InlineKeyboardButton("➕ إضافة قناة", callback_data="admin_add_channel")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
        await query.edit_message_text("📢 **قنوات الإجباري:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "admin_add_channel":
        if not admin_status: return
        await query.answer()
        ADMIN_STATES[user_id] = {"step": "waiting_channel_username"}
        await query.edit_message_text("➕ أرسل معرف القناة (مثل `@wassab_1`):\n\n⚠️ للإلغاء أرسل `/cancel`", reply_markup=back_to_home_keyboard(admin_status))
        return

    elif data.startswith("del_ch_"):
        if not admin_status: return
        ch_id = data.split("_")[2]
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM forced_channels WHERE id = ?", (ch_id,))
            conn.commit()
        await query.answer("✅ تم الحذف!", show_alert=True)
        return

    elif data == "admin_broadcast_start":
        if not admin_status: return
        await query.answer()
        ADMIN_STATES[user_id] = {"action": "broadcast"}
        await query.edit_message_text("📢 **أرسل رسالة الإذاعة للجميع:**\n\n⚠️ للإلغاء أرسل `/cancel`", reply_markup=back_to_home_keyboard(admin_status), parse_mode="Markdown")
        return

    elif data == "admin_start_upload":
        if not admin_status: return
        await query.answer()
        ADMIN_STATES[user_id] = {"step": "waiting_category"}
        cat_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 عادي", callback_data="set_cat_free"), InlineKeyboardButton("💎 VIP", callback_data="set_cat_paid")]
        ])
        await query.edit_message_text("📤 **اختر التصنيف:**", reply_markup=cat_keyboard)
        return

    elif data.startswith("set_cat_"):
        if not admin_status: return
        _, _, chosen_cat = data.split("_")
        if user_id in ADMIN_STATES:
            ADMIN_STATES[user_id]["category"] = chosen_cat
            type_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 كتب", callback_data="set_type_book"), InlineKeyboardButton("🖼️ صور", callback_data="set_type_photo"), InlineKeyboardButton("🎬 فيديو", callback_data="set_type_video")]
            ])
            await query.answer()
            await query.edit_message_text("📂 **اختر نوع المحتوى:**", reply_markup=type_keyboard)
        return

    elif data.startswith("set_type_"):
        if not admin_status: return
        _, _, chosen_type = data.split("_")
        if user_id in ADMIN_STATES and "category" in ADMIN_STATES[user_id]:
            ADMIN_STATES[user_id]["file_type"] = chosen_type
            ADMIN_STATES[user_id]["step"] = "waiting_subcat"
            await query.answer()
            await query.edit_message_text("📂 **اكتب اسم القسم الفرعي:**", reply_markup=back_to_home_keyboard(admin_status), parse_mode="Markdown")
        return

    elif data in ["cat_books", "cat_videos", "cat_photos"]:
        f_type_map = {"cat_books": ("book", "📚 الكتب"), "cat_videos": ("video", "🎬 الفيديوهات"), "cat_photos": ("photo", "🖼️ الصور")}
        f_type, title_text = f_type_map[data]
        await query.answer()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT sub_category FROM products WHERE file_type = ? AND category != 'paid'", (f_type,))
            sub_cats = cursor.fetchall()

        if not sub_cats:
            await query.edit_message_text(f"{title_text}\n\n⚠️ لا توجد محتويات.", reply_markup=back_to_home_keyboard(admin_status))
            return

        keyboard = [[InlineKeyboardButton(f"📁 {scat['sub_category']}", callback_data=f"view_sub_{f_type}_{scat['sub_category']}")] for scat in sub_cats]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="home" if not admin_status else "switch_to_user_view")])
        await query.edit_message_text(f"{title_text}\n\n👇 اختر القسم:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    elif data.startswith("view_sub_"):
        parts = data.split("_")
        f_type = parts[2]
        scat_name = "_".join(parts[3:])
        await query.answer()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM products WHERE file_type = ? AND sub_category = ? AND category != 'paid'", (f_type, scat_name))
            products = cursor.fetchall()

        keyboard = [[InlineKeyboardButton(f"📥 {p['title']}", callback_data=f"get_item_{p['id']}")] for p in products]
        back_cat_target = "cat_books" if f_type == "book" else ("cat_videos" if f_type == "video" else "cat_photos")
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=back_cat_target)])
        await query.edit_message_text(f"📁 **قسم: {scat_name}**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    elif data == "cat_paid":
        await query.answer()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM products WHERE category = 'paid'")
            products = cursor.fetchall()

        if not products:
            await query.edit_message_text("💎 **القسم المدفوع:** لا توجد محتويات.", reply_markup=back_to_home_keyboard(admin_status))
            return

        keyboard = [[InlineKeyboardButton(f"📥 {p['title']}", callback_data=f"get_item_{p['id']}")] for p in products]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="home" if not admin_status else "switch_to_user_view")])
        await query.edit_message_text("💎 **القسم المدفوع VIP:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    elif data.startswith("get_item_"):
        item_id = data.split("_")[2]
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT title, file_type, file_id, category FROM products WHERE id = ?", (item_id,))
            item = cursor.fetchone()

        if not item:
            await query.answer("❌ الملف غير متوفر!", show_alert=True)
            return

        title, f_type, file_id, category = item["title"], item["file_type"], item["file_id"], item["category"]
        is_vip = check_vip(user_id)
        
        if category == "paid" and not admin_status and not is_vip:
            await query.answer("💎 هذا المحتوى لـ VIP فقط!", show_alert=True)
            return

        update_user_stats(user_id, f_type, category)
        await query.answer("🚀 جاري إرسال الملف...")
        
        if f_type == "book":
            await context.bot.send_document(chat_id=user_id, document=file_id, caption=f"📖 {title}")
        elif f_type == "photo":
            await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=f"🖼️ {title}")
        elif f_type == "video":
            await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"🎬 {title}")
        return

    elif data == "admin_manage_products":
        if not admin_status: return
        await query.answer()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM products")
            products = cursor.fetchall()

        if not products:
            await query.edit_message_text("📦 لا توجد منتجات.", reply_markup=back_to_home_keyboard(admin_status))
            return

        keyboard = [[InlineKeyboardButton(f"🗑️ {p['title'][:15]}", callback_data=f"del_prod_{p['id']}")] for p in products]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
        await query.edit_message_text("🗑️ **إدارة المنتجات:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("del_prod_"):
        if not admin_status: return
        p_id = data.split("_")[2]
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM products WHERE id = ?", (p_id,))
            conn.commit()
        await query.answer("✅ تم الحذف!", show_alert=True)
        return

    elif data == "admin_users":
        if not admin_status: return
        await query.answer()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS cnt FROM users")
            total_users = cursor.fetchone()["cnt"]
            cursor.execute("SELECT user_id, full_name, is_banned, is_vip FROM users")
            all_users = cursor.fetchall()

        users_list_text = f"👥 **المستخدمين (الإجمالي: {total_users}):**\n\n"
        keyboard = []
        for u in all_users[:8]:
            status = "🔴 محظور" if u['is_banned'] else ("🌟 VIP" if u['is_vip'] else "🟢 عادي")
            b_data = f"unban_{u['user_id']}" if u['is_banned'] else f"ban_{u['user_id']}"
            b_text = "🔓 فتح" if u['is_banned'] else "⛔ حظر"
            keyboard.append([
                InlineKeyboardButton(f"👤 {u['full_name'][:10]} ({status})", callback_data="none"),
                InlineKeyboardButton(b_text, callback_data=b_data)
            ])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
        await query.edit_message_text(users_list_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    elif data.startswith("ban_") or data.startswith("unban_"):
        if not admin_status: return
        action, target_id = data.split("_")
        target_id = int(target_id)
        if is_owner(target_id):
            await query.answer("❌ لا يمكنك حظر المالك!", show_alert=True)
            return
        new_status = 1 if action == "ban" else 0
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (new_status, target_id))
            conn.commit()
        await query.answer("✅ تم!", show_alert=True)
        return

# ==================== معالجة الرسائل ====================

async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    if not message:
        return

    user_state = ADMIN_STATES.get(user.id, {})

    if message.text and message.text == "/cancel":
        ADMIN_STATES.pop(user.id, None)
        await message.reply_text("❌ تم الإلغاء.", reply_markup=user_menu_keyboard(check_vip(user.id)))
        return

    # معالجة كود الـ VIP مع تفعيل الصلاحية لمدة 30 يوماً
    if user_state.get("step") == "waiting_promo_code":
        entered_code = message.text.strip().upper()
        current_date = datetime.now().strftime("%Y-%m-%d")
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT code, max_uses, uses_count, expires_at FROM promo_codes WHERE code = ?", (entered_code,))
            promo = cursor.fetchone()
            
            if not promo:
                await message.reply_text("❌ كود خاطئ! تأكد من كتابته بدقة.")
                return
            if promo["expires_at"] and current_date > promo["expires_at"]:
                await message.reply_text("⏳ الكود منتهي الصلاحية!")
                return
            if promo["uses_count"] >= promo["max_uses"]:
                await message.reply_text("⚠️ الكود استنفد عدد مرات الاستخدام!")
                return
                
            # حساب تاريخ انتهاء الـ VIP بعد 30 يوماً من اليوم
            vip_expiry_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            
            cursor.execute("UPDATE promo_codes SET uses_count = uses_count + 1 WHERE code = ?", (entered_code,))
            cursor.execute("UPDATE users SET is_vip = 1, vip_expires_at = ? WHERE user_id = ?", (vip_expiry_date, user.id,))
            conn.commit()

        ADMIN_STATES.pop(user.id, None)
        await message.reply_text(
            f"🎉 **مبروك! تم تفعيل اشتراك VIP بنجاح لمدة 30 يوماً!**\n\n"
            f"⏳ ينتهي اشتراكك المميز في تاريخ: `{vip_expiry_date}`",
            reply_markup=user_menu_keyboard(True),
            parse_mode="Markdown"
        )
        return

    if user_state.get("step") == "waiting_support_message":
        if not message.text:
            await message.reply_text("⚠️ أرسل رسالة نصية.")
            return
        support_text = f"📩 **رسالة دعم فني:**\n👤 {user.full_name} (`{user.id}`)\n\n{message.text}"
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=support_text, parse_mode="Markdown")
            ADMIN_STATES.pop(user.id, None)
            await message.reply_text("✅ تم إرسال رسالتك للإدارة بنجاح!", reply_markup=user_menu_keyboard(check_vip(user.id)))
        except Exception:
            await message.reply_text("❌ فشل إرسال الرسالة.")
        return

    if not is_owner(user.id):
        return

    if user_state.get("step") == "waiting_channel_username" and message.text:
        ADMIN_STATES[user.id]["channel_username"] = message.text.strip()
        ADMIN_STATES[user.id]["step"] = "waiting_channel_link"
        await message.reply_text("🔗 أرسل رابط الدعوة الخاص بالقناة:")
        return

    if user_state.get("step") == "waiting_channel_link" and message.text:
        ch_link = message.text.strip()
        ch_uname = ADMIN_STATES[user.id].get("channel_username")
        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("INSERT INTO forced_channels (channel_username, channel_link) VALUES (?, ?)", (ch_uname, ch_link))
                conn.commit()
                success = True
            except sqlite3.IntegrityError:
                success = False
        ADMIN_STATES.pop(user.id, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ إدارة القنوات", callback_data="admin_manage_channels")]])
        await message.reply_text("✅ تمت الإضافة!" if success else "❌ القناة موجودة مسبقاً!", reply_markup=kb)
        return

    if user_state.get("action") == "broadcast":
        ADMIN_STATES.pop(user.id, None)
        await message.reply_text("🚀 جاري الإذاعة...")
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            all_users = cursor.fetchall()
        success_count, fail_count = 0, 0
        for u in all_users:
            if is_owner(u['user_id']): continue
            try:
                await context.bot.copy_message(chat_id=u['user_id'], from_chat_id=message.chat_id, message_id=message.message_id)
                success_count += 1
            except Exception:
                fail_count += 1
        await message.reply_text(f"✅ تمت الإذاعة.\nإرسال: `{success_count}` | فشل: `{fail_count}`", parse_mode="Markdown")
        return

    if user_state.get("step") == "waiting_subcat" and message.text:
        ADMIN_STATES[user.id]["sub_category"] = message.text.strip()
        ADMIN_STATES[user.id]["step"] = "waiting_file"
        await message.reply_text("📥 أرسل الملف الآن:")
        return

    if user_state.get("step") == "waiting_file":
        expected_type = user_state.get("file_type")
        file_id = message.document.file_id if message.document and expected_type == "book" else (
            message.photo[-1].file_id if message.photo and expected_type == "photo" else (
                message.video.file_id if message.video and expected_type == "video" else None
            )
        )
        if not file_id:
            await message.reply_text(f"❌ يجب إرسال ملف مطابق للنوع ({expected_type}).")
            return
        ADMIN_STATES[user.id]["file_id"] = file_id
        ADMIN_STATES[user.id]["step"] = "waiting_title"
        await message.reply_text("✍️ أرسل عنوان المنتج:")
        return

    if user_state.get("step") == "waiting_title" and message.text:
        title = message.text.strip()
        state = ADMIN_STATES[user.id]
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO products (title, file_type, file_id, category, sub_category) VALUES (?, ?, ?, ?, ?)",
                (title, state["file_type"], state["file_id"], state["category"], state.get("sub_category", "عام"))
            )
            conn.commit()
        ADMIN_STATES.pop(user.id, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ لوحة التحكم", callback_data="admin_panel")]])
        await message.reply_text("✅ تم رفع المنتج بنجاح!", reply_markup=kb)
        return

# ==================== بناء التطبيق وتشغيل الـ Webhook ====================

application = ApplicationBuilder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("admin", admin_panel_cmd))
application.add_handler(CommandHandler("ban", ban_user_cmd))
application.add_handler(CommandHandler("backup", backup_database_cmd))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_admin_messages))

@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook_listener():
    try:
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, application.bot)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def process():
            await application.initialize()
            await application.process_update(update)
            
        loop.run_until_complete(process())
        loop.close()
        
    except Exception as e:
        print(f"خطأ في الويب هوك: {e}")
        
    return "OK", 200

@flask_app.route("/")
def home():
    return "Wassab Store Bot is running live via Webhook! 🚀", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
