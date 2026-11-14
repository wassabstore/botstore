import os
import sqlite3
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

# ==================== الإعدادات الرئيسية ====================
TOKEN = "9157079266:AAHnLCeWZCFsdR5YCRLs9clVghaYzqYDpTE"
OWNER_ID = 8233150959  # معرف مالك البوت الأساسي المحمى (Wassab Store)
ADMIN_ID = OWNER_ID

# رابط الويب هوك الخاص بـ Render
WEBHOOK_URL = f"https://botstore-54tw.onrender.com/{TOKEN}"

# إعداد تطبيق الـ Flask
flask_app = Flask(__name__)

# متغيرات لحفظ حالة المالك (الإذاعة، رفع الملفات، أو إضافة قناة اشتراك إجباري)
ADMIN_STATES = {}

# ==================== إعداد قاعدة البيانات (SQLite) ====================
def init_db():
    conn = sqlite3.connect("wassab_store.db")
    cursor = conn.cursor()
    
    # جدول المستخدمين
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            is_banned INTEGER DEFAULT 0,
            is_vip INTEGER DEFAULT 0,
            dl_books INTEGER DEFAULT 0,
            dl_photos INTEGER DEFAULT 0,
            dl_videos INTEGER DEFAULT 0,
            dl_free INTEGER DEFAULT 0,
            dl_paid INTEGER DEFAULT 0
        )
    """)
    
    # جدول المنتجات والأقسام
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

    # جدول قنوات الاشتراك الإجباري (الديناميكي)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS forced_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT UNIQUE,
            channel_link TEXT
        )
    """)
    
    conn.commit()
    conn.close()

init_db()

def is_owner(user_id: int) -> bool:
    """دالة للتحقق مما إذا كان المستخدم هو مالك البوت الأساسي"""
    return user_id == OWNER_ID

def add_user(user_id, full_name, username):
    conn = sqlite3.connect("wassab_store.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute("""
            INSERT OR IGNORE INTO users (user_id, full_name, username, is_banned, is_vip, dl_books, dl_photos, dl_videos, dl_free, dl_paid) 
            VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0)
        """, (user_id, full_name, username))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def update_user_stats(user_id, f_type, category):
    conn = sqlite3.connect("wassab_store.db")
    cursor = conn.cursor()
    b_col = "dl_books" if f_type == "book" else ("dl_photos" if f_type == "photo" else "dl_videos")
    c_col = "dl_paid" if category == "paid" else "dl_free"
    cursor.execute(f"UPDATE users SET {b_col} = {b_col} + 1, {c_col} = {c_col} + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def check_banned(user_id):
    conn = sqlite3.connect("wassab_store.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] == 1 if row else False

def check_vip(user_id):
    conn = sqlite3.connect("wassab_store.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] == 1 if row else False

# ==================== التحقق الديناميكي من قنوات الاشتراك الإجباري ====================
async def get_unsubscribed_channels(user_id, context: ContextTypes.DEFAULT_TYPE):
    """التحقق من جميع القنوات الإجبارية وإرجاع القنوات التي لم يشترك فيها المستخدم بعد"""
    if is_owner(user_id):
        return []

    conn = sqlite3.connect("wassab_store.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_username, channel_link FROM forced_channels")
    channels = cursor.fetchall()
    conn.close()

    unsubscribed = []
    for ch_username, ch_link in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=ch_username, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                unsubscribed.append((ch_username, ch_link))
        except Exception as e:
            print(f"خطأ في التحقق من القناة {ch_username}: {e}")
            unsubscribed.append((ch_username, ch_link))
            
    return unsubscribed

# ==================== واجهات الأزرار الفخمة (Inline) ====================

def subscription_keyboard(unsubscribed_channels):
    keyboard = []
    for ch_username, ch_link in unsubscribed_channels:
        # التأكد من أن الرابط يبدأ بـ http أو https لكي لا يرفضه تيليجرام
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
        ]
    ]
    if is_vip:
        keyboard.append([InlineKeyboardButton("🌟 أنت عضو مميز VIP لديك صلاحيات خاصة", callback_data="vip_notice")])
    keyboard.append([InlineKeyboardButton("📞 التواصل مع الإدارة", url="https://t.me/wassab_1")])
    return InlineKeyboardMarkup(keyboard)

def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 رفع ملف جديد للمتجر", callback_data="admin_start_upload"),
            InlineKeyboardButton("📦 إدارة وحذف المنتجات", callback_data="admin_manage_products")
        ],
        [
            InlineKeyboardButton("📢 إدارة قنوات الإجباري", callback_data="admin_manage_channels"),
            InlineKeyboardButton("📢 إذاعة عامة للكل", callback_data="admin_broadcast_start")
        ],
        [
            InlineKeyboardButton("👥 إحصائيات وإدارة المستخدمين", callback_data="admin_users"),
            InlineKeyboardButton("🛒 الانتقال لواجهة المتجر", callback_data="switch_to_user_view")
        ]
    ])

def back_to_home_keyboard(is_admin=False):
    target = "admin_panel" if is_admin else "home"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data=target)]])

# ==================== دوال المعالجة (Handlers) ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if check_banned(user.id):
        if update.message:
            await update.message.reply_text("🚫 عذراً، لقد تم حظرك من استخدام هذا البوت.")
        return

    # فحص الاشتراك في القنوات الإجبارية المُضافة
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

    is_new = add_user(user.id, user.full_name, user.username or "لا يوجد")
    if is_new and not is_owner(user.id):
        alert_text = (
            f"🔔 **مستخدم جديد انضم إلى المتجر!**\n\n"
            f"👤 **الاسم:** {user.full_name}\n"
            f"🔗 **اليوزر:** @{user.username if user.username else 'لا يوجد'}\n"
            f"🆔 **الآيدي (ID):** `{user.id}`"
        )
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=alert_text, parse_mode="Markdown")
        except Exception as e:
            print(f"خطأ في إرسال التنبيه للمالك: {e}")

    admin_status = is_owner(user.id)
    is_vip = check_vip(user.id)

    if admin_status:
        welcome_text = (
            f"👑 **أهلاً بك يا مالك المتجر العظيم ({user.full_name})!**\n\n"
            "⚙️ تم فتح **لوحة التحكم السيادية** الخاصة بك بنجاح."
        )
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
        await update.message.reply_text("⛔ هذا الأمر للمشرفين فقط.")
        return

    try:
        target_user_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ يرجى تحديد معرف المستخدم (ID) المراد حظره بشكل صحيح.\nمثال: `/ban 123456789`", parse_mode="Markdown")
        return

    if is_owner(target_user_id):
        await update.message.reply_text("❌ تنبيه أمني: لا يمكنك حظر مالك البوت الأساسي (Wassab Store) أبداً!")
        return

    conn = sqlite3.connect("wassab_store.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_user_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ تم حظر المستخدم ذو المعرف `{target_user_id}` بنجاح.", parse_mode="Markdown")

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
            await query.answer("✅ شكراً لك! تم التحقق من اشتراكك بنجاح.", show_alert=True)
            await start(update, context)
        else:
            await query.answer("❌ عذراً، لم تقم بالاشتراك في جميع القنوات بعد!", show_alert=True)
        return

    if data == "vip_notice":
        await query.answer("🌟 أنت تمتلك حساباً مميزاً (VIP) يمنحك صلاحيات خاصة في المتجر!", show_alert=True)
        return

    admin_status = is_owner(user_id)

    if data == "home" or data == "switch_to_user_view":
        await query.answer()
        is_vip = check_vip(user_id)
        if admin_status and data == "switch_to_user_view":
            text = "🛒 **عرض واجهة المتجر كما يراها المستخدمون:**"
            keyboard = user_menu_keyboard(is_vip)
            keyboard.inline_keyboard.append([InlineKeyboardButton("⚙️ العودة لوحة تحكم المالك", callback_data="admin_panel")])
        else:
            text = "🏠 **القائمة الرئيسية للمتجر:**\nاختر ما يناسب اهتمامك من الأقسام المتاحة:"
            keyboard = user_menu_keyboard(is_vip)
            
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    elif data == "admin_panel":
        if not admin_status:
            await query.answer("❌ لست مخولاً بالدخول إلى هذه اللوحة!", show_alert=True)
            return
        await query.answer()
        ADMIN_STATES.pop(user_id, None)
        await query.edit_message_text(
            "⚙️ **لوحة تحكم المسؤول الخارقة:**\nمرحباً بك يا قائد المتجر. اختر الإجراء المطلوب:",
            reply_markup=admin_menu_keyboard(),
            parse_mode="HTML"
        )
        return
    
    # إدارة قنوات الاشتراك الإجباري من لوحة التحكم
    elif data == "admin_manage_channels":
        if not admin_status: return
        await query.answer()
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, channel_username, channel_link FROM forced_channels")
        channels = cursor.fetchall()
        conn.close()

        keyboard = []
        for ch_id, ch_uname, ch_link in channels:
            keyboard.append([
                InlineKeyboardButton(f"📢 {ch_uname}", url=ch_link),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"del_ch_{ch_id}")
            ])
        
        keyboard.append([InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="admin_add_channel")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع لوحة التحكم", callback_data="admin_panel")])

        await query.edit_message_text(
            "📢 **إدارة قنوات الاشتراك الإجباري:**\n\n"
            "هنا يمكنك استعراض القنوات المضافة حالياً أو حذفها أو إضافة قناة جديدة:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    elif data == "admin_add_channel":
        if not admin_status: return
        await query.answer()
        ADMIN_STATES[user_id] = {"step": "waiting_channel_username"}
        
        await query.edit_message_text(
            "➕ **إضافة قناة اشتراك إجباري جديدة:**\n\n"
            "الخطوة 1: أرسل معرف القناة (يوزر القناة مسبوقاً بـ @، مثلاً: `@wassab_1`):\n\n"
            "⚠️ للإلغاء أرسل `/cancel`",
            reply_markup=back_to_home_keyboard(admin_status)
        )
        return

    elif data.startswith("del_ch_"):
        if not admin_status: return
        ch_id = data.split("_")[2]
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM forced_channels WHERE id = ?", (ch_id,))
        conn.commit()
        conn.close()
        
        await query.answer("✅ تم حذف القناة بنجاح!", show_alert=True)
        
        # إعادة توجيه لوحة القنوات
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, channel_username, channel_link FROM forced_channels")
        channels = cursor.fetchall()
        conn.close()

        keyboard = []
        for cid, ch_uname, ch_link in channels:
            keyboard.append([
                InlineKeyboardButton(f"📢 {ch_uname}", url=ch_link),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"del_ch_{cid}")
            ])
        keyboard.append([InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="admin_add_channel")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع لوحة التحكم", callback_data="admin_panel")])

        await query.edit_message_text(
            "📢 **إدارة قنوات الاشتراك الإجباري:**\n\nتم الحذف بنجاح. القنوات الحالية:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    elif data == "admin_broadcast_start":
        if not admin_status: return
        await query.answer()
        ADMIN_STATES[user_id] = {"action": "broadcast"}
        
        guide_text = (
            "📢 **نظام الإذاعة العامة:**\n\n"
            "الآن، قم بإرسال الرسالة التي تريد إرسالها لجميع المشتركين (نص، صورة، فيديو، إلخ).\n\n"
            "⚠️ للإلغاء في أي وقت، أرسل الكلمة: `/cancel`"
        )
        await query.edit_message_text(guide_text, reply_markup=back_to_home_keyboard(admin_status), parse_mode="Markdown")
        return

    elif data == "admin_start_upload":
        if not admin_status: return
        await query.answer()
        ADMIN_STATES[user_id] = {"step": "waiting_category"}
        
        cat_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📂 قسم عادي", callback_data="set_cat_free"),
                InlineKeyboardButton("💎 القسم المدفوع (VIP)", callback_data="set_cat_paid")
            ]
        ])
        await query.edit_message_text(
            "📤 **خطوة 1: اختيار القسم الرئيسي**\n\n"
            "اختر ما إذا كان المحتوى عادياً أم مدفوعاً:",
            reply_markup=cat_keyboard
        )
        return

    elif data.startswith("set_cat_"):
        if not admin_status: return
        _, _, chosen_cat = data.split("_")
        if user_id in ADMIN_STATES:
            ADMIN_STATES[user_id]["category"] = chosen_cat
            
            type_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📚 كتب", callback_data="set_type_book"),
                    InlineKeyboardButton("🖼️ صور", callback_data="set_type_photo"),
                    InlineKeyboardButton("🎬 فيديوهات", callback_data="set_type_video")
                ]
            ])
            await query.answer()
            await query.edit_message_text(
                "📂 **خطوة 2: اختيار نوع المحتوى**\n\n"
                "اختر نوع الملف:",
                reply_markup=type_keyboard
            )
        return

    elif data.startswith("set_type_"):
        if not admin_status: return
        _, _, chosen_type = data.split("_")
        if user_id in ADMIN_STATES and "category" in ADMIN_STATES[user_id]:
            ADMIN_STATES[user_id]["file_type"] = chosen_type
            ADMIN_STATES[user_id]["step"] = "waiting_subcat"
            
            await query.answer()
            await query.edit_message_text(
                "📂 **خطوة 3: تحديد القسم الفرعي / التخصص**\n\n"
                "اكتب اسم القسم الفرعي (مثلاً: `برمجة`, `شبكات`, `قواعد البيانات`, `تصميم`) أو اكتب `عام`:",
                reply_markup=back_to_home_keyboard(admin_status),
                parse_mode="Markdown"
            )
        return

    elif data in ["cat_books", "cat_videos", "cat_photos"]:
        f_type_map = {
            "cat_books": ("book", "📚 قسم الكتب والمؤلفات"),
            "cat_videos": ("video", "🎬 قسم الفيديوهات التعليمية"),
            "cat_photos": ("photo", "🖼️ قسم الصور والتصاميم")
        }
        f_type, title_text = f_type_map[data]
        await query.answer()
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT sub_category FROM products WHERE file_type = ? AND category != 'paid'", (f_type,))
        sub_cats = cursor.fetchall()
        conn.close()

        if not sub_cats:
            await query.edit_message_text(f"{title_text}\n\n⚠️ عذراً، لا توجد محتويات مرفوعة في هذا القسم حالياً.", reply_markup=back_to_home_keyboard(admin_status))
            return

        keyboard = [[InlineKeyboardButton(f"📁 قسم: {scat}", callback_data=f"view_sub_{f_type}_{scat}")] for (scat,) in sub_cats]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="home" if not admin_status else "switch_to_user_view")])

        await query.edit_message_text(f"{title_text}\n\n👇 اختر القسم الفرعي المناسب:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    elif data.startswith("view_sub_"):
        parts = data.split("_")
        f_type = parts[2]
        scat_name = "_".join(parts[3:])
        await query.answer()
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, title FROM products WHERE file_type = ? AND sub_category = ? AND category != 'paid'", (f_type, scat_name))
        products = cursor.fetchall()
        conn.close()

        keyboard = [[InlineKeyboardButton(f"📥 {p_title}", callback_data=f"get_item_{p_id}")] for p_id, p_title in products]
        back_cat_target = "cat_books" if f_type == "book" else ("cat_videos" if f_type == "video" else "cat_photos")
        keyboard.append([InlineKeyboardButton("🔙 رجوع للأقسام", callback_data=back_cat_target)])

        await query.edit_message_text(f"📁 **قسم: {scat_name}**\n\n👇 اختر المحتوى الذي ترغب بتحميله:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    elif data == "cat_paid":
        await query.answer()
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, file_type FROM products WHERE category = 'paid'")
        products = cursor.fetchall()
        conn.close()

        if not products:
            text = "💎 **القسم المدفوع المميز (VIP)**\n\n⚠️ عذراً، لا توجد محتويات مدفوعة مرفوعة حالياً."
            await query.edit_message_text(text, reply_markup=back_to_home_keyboard(admin_status), parse_mode="HTML")
            return

        keyboard = [[InlineKeyboardButton(f"📥 {p_title}", callback_data=f"get_item_{p_id}")] for p_id, p_title, _ in products]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="home" if not admin_status else "switch_to_user_view")])
        
        await query.edit_message_text(
            "💎 **القسم المدفوع المميز (VIP)**\n\n👇 اختر المحتوى الحصري الذي ترغب في استعراضه:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    elif data.startswith("get_item_"):
        item_id = data.split("_")[2]
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT title, file_type, file_id, category FROM products WHERE id = ?", (item_id,))
        item = cursor.fetchone()
        conn.close()

        if not item:
            await query.answer("❌ هذا الملف لم يعد متوفراً!", show_alert=True)
            return

        title, f_type, file_id, category = item
        is_vip = check_vip(user_id)
        
        if category == "paid" and not admin_status and not is_vip:
            await query.answer("💎 هذا المحتوى خاص بأعضاء VIP فقط!", show_alert=True)
            return

        update_user_stats(user_id, f_type, category)

        await query.answer("🚀 جاري جلب الملف لك...")
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
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, title FROM products")
        products = cursor.fetchall()
        conn.close()

        if not products:
            await query.edit_message_text("📦 لا توجد منتجات مرفوعة حالياً.", reply_markup=back_to_home_keyboard(admin_status))
            return

        keyboard = [[InlineKeyboardButton(f"🗑️ حذف: {p_title[:15]}", callback_data=f"del_prod_{p_id}")] for p_id, p_title in products]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
        
        await query.edit_message_text("🗑️ **إدارة وحذف المحتوى:**\nاختر المنتج الذي تريد حذفه نهائياً:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("del_prod_"):
        if not admin_status: return
        p_id = data.split("_")[2]
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM products WHERE id = ?", (p_id,))
        conn.commit()
        
        cursor.execute("SELECT id, title FROM products")
        products = cursor.fetchall()
        conn.close()
        
        await query.answer("✅ تم حذف المنتج بنجاح!", show_alert=True)
        keyboard = [[InlineKeyboardButton(f"🗑️ حذف: {p_title[:15]}", callback_data=f"del_prod_{p_id}")] for p_id, p_title in products]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
        await query.edit_message_text("🗑️ **إدارة وحذف المحتوى:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "admin_users":
        if not admin_status: return
        await query.answer()
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        
        cursor.execute("SELECT user_id, full_name, username, is_banned, is_vip, dl_books, dl_photos, dl_videos, dl_free, dl_paid FROM users")
        all_users = cursor.fetchall()
        conn.close()

        users_list_text = f"👥 **إدارة وإحصائيات المستخدمين:**\nإجمالي المشتركين: `{total_users}`\n\n"
        
        keyboard = []
        for u in all_users[:8]:
            u_id, name, uname, banned, vip, b_cnt, p_cnt, v_cnt, free_cnt, paid_cnt = u
            
            status_text = "🔴 محظور" if banned == 1 else ("🌟 VIP" if vip == 1 else "🟢 عادي")
            stats_info = f"📚{b_cnt} 🖼️{p_cnt} 🎬{v_cnt} (مجاني:{free_cnt} مدفوع:{paid_cnt})"
            
            ban_btn_text = "🔓 فتح" if banned == 1 else "⛔ حظر"
            ban_data = f"unban_{u_id}" if banned == 1 else f"ban_{u_id}"
            
            vip_btn_text = "⭐ إزالة VIP" if vip == 1 else "💎 تفعيل VIP"
            vip_data = f"unvip_{u_id}" if vip == 1 else f"vip_{u_id}"

            users_list_text += f"👤 **{name}** | {status_text}\n📊 التحميلات: `{stats_info}`\n-------------------\n"

            keyboard.append([
                InlineKeyboardButton(f"👤 {name[:10]} ({status_text})", callback_data="none"),
                InlineKeyboardButton(vip_btn_text, callback_data=vip_data),
                InlineKeyboardButton(ban_btn_text, callback_data=ban_data)
            ])
        
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
        await query.edit_message_text(users_list_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    elif data.startswith("ban_") or data.startswith("unban_"):
        if not admin_status: return
        action, target_id_str = data.split("_")
        target_id = int(target_id_str)
        
        if is_owner(target_id):
            await query.answer("❌ تنبيه أمني: لا يمكنك حظر مالك البوت الأساسي أبداً!", show_alert=True)
            return

        new_status = 1 if action == "ban" else 0
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (new_status, target_id))
        conn.commit()
        conn.close()
        
        await query.answer("✅ تم تنفيذ الحظر/الفتح بنجاح!", show_alert=True)
        return

    elif data.startswith("vip_") or data.startswith("unvip_"):
        if not admin_status: return
        action, target_id = data.split("_")
        target_id = int(target_id)
        new_vip_status = 1 if action == "vip" else 0
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT is_vip FROM users WHERE user_id = ?", (target_id,))
        row = cursor.fetchone()
        current_vip = row[0] if row else 0
        
        cursor.execute("UPDATE users SET is_vip = ? WHERE user_id = ?", (new_vip_status, target_id))
        conn.commit()
        conn.close()
        
        action_msg = "تمت ترقية المستخدم إلى VIP بنجاح! 🌟" if new_vip_status == 1 else "تمت إزالة صلاحية VIP عن المستخدم."
        await query.answer(action_msg, show_alert=True)
        
        if new_vip_status == 1 and current_vip == 0:
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text="🌟 **مبارك لك!** لقد قامت الإدارة بترقية حسابك في المتجر إلى **اشتراك مميز (VIP)** وصار بإمكانك تصفح المحتوى الحصري."
                )
            except Exception:
                pass
        return

# ==================== معالجة رسائل ورفع ملفات / قنوات المسؤول ====================

async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        return

    message = update.message
    if not message:
        return

    user_state = ADMIN_STATES.get(user.id, {})

    if message.text and message.text == "/cancel":
        ADMIN_STATES.pop(user.id, None)
        await message.reply_text("❌ تم الإلغاء بنجاح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ لوحة التحكم", callback_data="admin_panel")]]))
        return

    # خطوة إضافة قناة اشتراك إجباري جديدة (اليوزر)
    if user_state.get("step") == "waiting_channel_username" and message.text:
        ch_username = message.text.strip()
        ADMIN_STATES[user.id]["channel_username"] = ch_username
        ADMIN_STATES[user.id]["step"] = "waiting_channel_link"
        
        await message.reply_text(
            f"🔗 **رابط القناة:**\n\n"
            f"تم حفظ معرف القناة: `{ch_username}`\n"
            f"الآن أرسل **رابط الدعوة الخاص بالقناة** (مثلاً: `https://t.me/wassab_1`):",
            parse_mode="Markdown"
        )
        return

    # الخطوة الثانية: حفظ رابط القناة في قاعدة البيانات
    if user_state.get("step") == "waiting_channel_link" and message.text:
        ch_link = message.text.strip()
        ch_username = ADMIN_STATES[user.id].get("channel_username")

        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO forced_channels (channel_username, channel_link) VALUES (?, ?)",
                (ch_username, ch_link)
            )
            conn.commit()
            success = True
        except sqlite3.IntegrityError:
            success = False
        conn.close()

        ADMIN_STATES.pop(user.id, None)

        success_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ إدارة القنوات", callback_data="admin_manage_channels")]])
        if success:
            await message.reply_text(
                f"✅ **تمت إضافة القناة بنجاح إلى نظام الاشتراك الإجباري!**\n\n"
                f"📢 **القناة:** `{ch_username}`\n"
                f"🔗 **الرابط:** {ch_link}",
                reply_markup=success_kb,
                parse_mode="Markdown"
            )
        else:
            await message.reply_text(
                "❌ عذراً، هذه القناة مضافة مسبقاً في القائمة!",
                reply_markup=success_kb
            )
        return

    if user_state.get("action") == "broadcast":
        ADMIN_STATES.pop(user.id, None)
        await message.reply_text("🚀 جاري إرسال الإذاعة لجميع المشتركين...")
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        all_users = cursor.fetchall()
        conn.close()

        success_count = 0
        fail_count = 0

        for (u_id,) in all_users:
            if is_owner(u_id):
                continue
            try:
                await context.bot.copy_message(
                    chat_id=u_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id
                )
                success_count += 1
            except Exception:
                fail_count += 1

        await message.reply_text(
            f"✅ **تمت الإذاعة بنجاح!**\n\n"
            f"📤 **تم الإرسال إلى:** `{success_count}` مستخدم\n"
            f"❌ **فشل (حظروا البوت):** `{fail_count}` مستخدم",
            parse_mode="Markdown"
        )
        return

    if user_state.get("step") == "waiting_subcat" and message.text:
        subcat = message.text.strip()
        ADMIN_STATES[user.id]["sub_category"] = subcat
        ADMIN_STATES[user.id]["step"] = "waiting_file"
        
        await message.reply_text(
            f"📥 **إرسال الملف المطلوب**\n\n"
            f"تم اعتماد القسم الفرعي: **{subcat}**\n"
            f"الآن أرسل الملف (كتاب، صورة، أو فيديو) في الشات مباشرة."
        )
        return

    if user_state.get("step") == "waiting_file":
        file_id = None
        expected_type = user_state.get("file_type")
        
        if message.document and expected_type == "book":
            file_id = message.document.file_id
        elif message.photo and expected_type == "photo":
            file_id = message.photo[-1].file_id
        elif message.video and expected_type == "video":
            file_id = message.video.file_id
        else:
            await message.reply_text(f"❌ عذراً، يجب إرسال ملف من نوع ({expected_type}) المطابق لاختيارك.")
            return

        ADMIN_STATES[user.id]["file_id"] = file_id
        ADMIN_STATES[user.id]["step"] = "waiting_title"

        await message.reply_text(
            "✍️ **خطوة أخيرة: عنوان الملف**\n\n"
            "اكتب الآن **عنوان المنتج أو الملف** وأرسله في رسالة نصية لحفظه نهائياً."
        )
        return

    if user_state.get("step") == "waiting_title" and message.text:
        title = message.text.strip()
        file_id = user_state.get("file_id")
        f_type = user_state.get("file_type")
        category = user_state.get("category")
        sub_category = user_state.get("sub_category", "عام")

        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO products (title, file_type, file_id, category, sub_category) VALUES (?, ?, ?, ?, ?)",
            (title, f_type, file_id, category, sub_category)
        )
        conn.commit()
        conn.close()

        ADMIN_STATES.pop(user.id, None)

        success_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ العودة لوحة التحكم", callback_data="admin_panel")]])
        await message.reply_text(
            f"✅ **تم رفع وتخزين المحتوى في المتجر بنجاح تام!**\n\n"
            f"📌 **العنوان:** {title}\n"
            f"📂 **النوع:** {f_type}\n"
            f"📁 **القسم الفرعي:** {sub_category}\n"
            f"💎 **التصنيف:** {category}",
            reply_markup=success_kb,
            parse_mode="Markdown"
        )
        return

# ==================== بناء تطبيق البوت وتسجيل الهاندلرز ====================

application = ApplicationBuilder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("admin", admin_panel_cmd))
application.add_handler(CommandHandler("ban", ban_user_cmd))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_admin_messages))

# ==================== مسارات الـ Webhook والـ Flask ====================

@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook_listener():
    json_data = request.get_json(force=True)
    update = Update.de_json(json_data, application.bot)
    
    async def process():
        await application.initialize()
        await application.process_update(update)
    
    asyncio.run(process())
    return "OK", 200

@flask_app.route("/")
def home():
    return "Wassab Store Bot is running live via Webhook! 🚀", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
