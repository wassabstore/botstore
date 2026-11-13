import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================== الإعدادات الرئيسية ====================
TOKEN = "9157079266:AAHnLCeWZCFsdR5YCRLs9clVghaYzqYDpTE"
ADMIN_ID = 8233150959  # الآيدي الخاص بك (wassab store)

# إعدادات قناة الاشتراك الإجباري
CHANNEL_USERNAME = "@wassab_1" 
LINK_CHANNEL = "https://t.me/wassab_1"

# متغيرات لحفظ حالة المالك (الإذاعة أو خطوات رفع الملفات)
ADMIN_STATES = {}

# ==================== إعداد قاعدة البيانات (SQLite) ====================
def init_db():
    conn = sqlite3.connect("wassab_store.db")
    cursor = conn.cursor()
    
    # جدول المستخدمين مع إحصائيات التحميلات وحالة الـ VIP والحظر
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
    
    # جدول المنتجات (مع حقل القسم الفرعي للكتب)
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
    
    conn.commit()
    conn.close()

init_db()

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

# ==================== دالة التحقق من الاشتراك الإجباري ====================
async def check_subscription(user_id, context: ContextTypes.DEFAULT_TYPE):
    if user_id == ADMIN_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        print(f"خطأ في التحقق من الاشتراك: {e}")
        return False

# ==================== واجهات الأزرار الفخمة (Inline) ====================

def subscription_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 اشترك في القناة الآن", url=LINK_CHANNEL)],
        [InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="check_sub")]
    ])

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
    keyboard.append([InlineKeyboardButton("📞 التواصل مع الإدارة", url=LINK_CHANNEL)])
    return InlineKeyboardMarkup(keyboard)

def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 رفع ملف جديد للمتجر", callback_data="admin_start_upload"),
            InlineKeyboardButton("📦 إدارة وحذف المنتجات", callback_data="admin_manage_products")
        ],
        [
            InlineKeyboardButton("👥 إحصائيات وإدارة المستخدمين", callback_data="admin_users"),
            InlineKeyboardButton("📢 إذاعة عامة للكل", callback_data="admin_broadcast_start")
        ],
        [
            InlineKeyboardButton("🛒 الانتقال لواجهة المتجر العادية", callback_data="switch_to_user_view")
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

    is_subscribed = await check_subscription(user.id, context)
    if not is_subscribed:
        sub_text = (
            f"⚠️ **عذراً يا أخي {user.mention_html()}، لا يمكنك استخدام البوت حالياً.**\n\n"
            f"🔔 يشترط الاشتراك في قناة المتجر لتفعيل البوت واستخدام خدماته:\n"
            f"👉 {CHANNEL_USERNAME}\n\n"
            "قم بالاشتراك بالقناة ثم اضغط على زر **(تحقق من الاشتراك)** بالأسفل 👇"
        )
        if update.message:
            await update.message.reply_text(sub_text, reply_markup=subscription_keyboard(), parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.answer("❌ لم تقم بالاشتراك بعد!", show_alert=True)
            await update.callback_query.edit_message_text(sub_text, reply_markup=subscription_keyboard(), parse_mode="HTML")
        return

    is_new = add_user(user.id, user.full_name, user.username or "لا يوجد")
    if is_new and user.id != ADMIN_ID:
        alert_text = (
            f"🔔 **مستخدم جديد انضم إلى المتجر!**\n\n"
            f"👤 **الاسم:** {user.full_name}\n"
            f"🔗 **اليوزر:** @{user.username if user.username else 'لا يوجد'}\n"
            f"🆔 **الآيدي (ID):** `{user.id}`"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=alert_text, parse_mode="Markdown")
        except Exception as e:
            print(f"خطأ في إرسال التنبيه للمالك: {e}")

    is_admin = (user.id == ADMIN_ID)
    is_vip = check_vip(user.id)

    if is_admin:
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user  # تم تعريف user لتجنب خطأ NameError
    user_id = user.id
    
    if check_banned(user_id):
        await query.answer("🚫 تم حظرك!", show_alert=True)
        return

    data = query.data

    if data == "check_sub":
        if await check_subscription(user_id, context):
            await query.answer("✅ شكراً لك! تم تفعيل البوت بنجاح.", show_alert=True)
            await start(update, context)
        else:
            await query.answer("❌ عذراً، لم تقم بالاشتراك في القناة بعد!", show_alert=True)
        return

    if data == "vip_notice":
        await query.answer("🌟 أنت تمتلك حساباً مميزاً (VIP) يمنحك صلاحيات خاصة في المتجر!", show_alert=True)
        return

    is_admin = (user_id == ADMIN_ID)

    if data == "home" or data == "switch_to_user_view":
        await query.answer()
        is_vip = check_vip(user_id)
        if is_admin and data == "switch_to_user_view":
            text = "🛒 **عرض واجهة المتجر كما يراها المستخدمون:**"
            keyboard = user_menu_keyboard(is_vip)
            keyboard.inline_keyboard.append([InlineKeyboardButton("⚙️ العودة لوحة تحكم المالك", callback_data="admin_panel")])
        else:
            text = "🏠 **القائمة الرئيسية للمتجر:**\nاختر ما يناسب اهتمامك من الأقسام المتاحة:"
            keyboard = user_menu_keyboard(is_vip)
            
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    elif data == "admin_panel":
        if not is_admin:
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
    
    elif data == "admin_broadcast_start":
        if not is_admin: return
        await query.answer()
        ADMIN_STATES[user_id] = {"action": "broadcast"}
        
        guide_text = (
            "📢 **نظام الإذاعة العامة:**\n\n"
            "الآن، قم بإرسال الرسالة التي تريد إرسالها لجميع المشتركين (نص، صورة، فيديو، إلخ).\n\n"
            "⚠️ للإلغاء في أي وقت، أرسل الكلمة: `/cancel`"
        )
        await query.edit_message_text(guide_text, reply_markup=back_to_home_keyboard(is_admin), parse_mode="Markdown")
        return

    elif data == "admin_start_upload":
        if not is_admin: return
        await query.answer()
        ADMIN_STATES[user_id] = {"step": "waiting_category"}
        
        cat_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📂 قسم عادي", callback_data="set_cat_free"),
                InlineKeyboardButton("💎 القسم المدفوع (VIP)", callback_data="set_cat_paid")
            ]
        ])
        await query.edit_message_text(
            "📤 **خطوة 1: اختيار القسم**\n\n"
            "اختر ما إذا كان المحتوى عادياً أم مدفوعاً:",
            reply_markup=cat_keyboard
        )
        return

    elif data.startswith("set_cat_"):
        if not is_admin: return
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
        if not is_admin: return
        _, _, chosen_type = data.split("_")
        if user_id in ADMIN_STATES and "category" in ADMIN_STATES[user_id]:
            ADMIN_STATES[user_id]["file_type"] = chosen_type
            
            if chosen_type == "book":
                ADMIN_STATES[user_id]["step"] = "waiting_subcat"
                await query.answer()
                await query.edit_message_text(
                    "📚 **خطوة 3: قسم الكتب الفرعي**\n\n"
                    "اكتب اسم القسم الخاص بهذا الكتاب (مثلاً: `برمجة`, `روايات`) وأرسله في رسالة نصية.",
                    reply_markup=back_to_home_keyboard(is_admin),
                    parse_mode="Markdown"
                )
            else:
                ADMIN_STATES[user_id]["sub_category"] = "عام"
                ADMIN_STATES[user_id]["step"] = "waiting_file"
                await query.answer()
                await query.edit_message_text(
                    "📥 **خطوة 3: إرسال الملف**\n\n"
                    "أرسل الآن الملف المطلوب في الشات مباشرة.\n\n"
                    "⚠️ للإلغاء أرسل `/cancel`",
                    reply_markup=back_to_home_keyboard(is_admin),
                    parse_mode="Markdown"
                )
        return

    elif data == "cat_books":
        await query.answer()
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT sub_category FROM products WHERE file_type = 'book' AND category != 'paid'")
        sub_cats = cursor.fetchall()
        conn.close()

        if not sub_cats:
            await query.edit_message_text("📚 **قسم الكتب والمؤلفات**\n\n⚠️ عذراً، لا توجد كتب مرفوعة حالياً.", reply_markup=back_to_home_keyboard(is_admin))
            return

        keyboard = [[InlineKeyboardButton(f"📁 قسم: {scat}", callback_data=f"book_subcat_{scat}")] for (scat,) in sub_cats]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="home" if not is_admin else "switch_to_user_view")])

        await query.edit_message_text("📚 **قسم الكتب والمؤلفات**\n\n👇 اختر القسم المناسب:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    elif data.startswith("book_subcat_"):
        scat_name = data.replace("book_subcat_", "")
        await query.answer()
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, title FROM products WHERE file_type = 'book' AND sub_category = ? AND category != 'paid'", (scat_name,))
        books = cursor.fetchall()
        conn.close()

        keyboard = [[InlineKeyboardButton(f"📖 {b_title}", callback_data=f"get_item_{b_id}")] for b_id, b_title in books]
        keyboard.append([InlineKeyboardButton("🔙 رجوع للأقسام", callback_data="cat_books")])

        await query.edit_message_text(f"📁 **قسم: {scat_name}**\n\n👇 اختر الكتاب الذي ترغب بتحميله:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    elif data.startswith("cat_"):
        cat_map = {
            "cat_videos": ("🎬 قسم الفيديوهات التعليمية", "video"),
            "cat_photos": ("🖼️ قسم الصور والتصاميم", "photo"),
            "cat_paid": ("💎 القسم المدفوع المميز (VIP)", "paid")
        }
        title, f_type = cat_map[data]
        await query.answer()
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        if f_type == "paid":
            cursor.execute("SELECT id, title, file_type FROM products WHERE category = 'paid'")
        else:
            cursor.execute("SELECT id, title, file_type FROM products WHERE file_type = ? AND category != 'paid'", (f_type,))
        
        products = cursor.fetchall()
        conn.close()

        if not products:
            text = f"📂 **{title}**\n\n⚠️ عذراً، لا توجد محتويات مرفوعة في هذا القسم حالياً."
            await query.edit_message_text(text, reply_markup=back_to_home_keyboard(is_admin), parse_mode="HTML")
            return

        keyboard = [[InlineKeyboardButton(f"📥 {p_title}", callback_data=f"get_item_{p_id}")] for p_id, p_title, _ in products]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="home" if not is_admin else "switch_to_user_view")])
        
        await query.edit_message_text(
            f"📂 **{title}**\n\n👇 اختر المحتوى الذي ترغب في استعراضه:",
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
        
        if category == "paid" and not is_admin and not is_vip:
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
        if not is_admin: return
        await query.answer()
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, title FROM products")
        products = cursor.fetchall()
        conn.close()

        if not products:
            await query.edit_message_text("📦 لا توجد منتجات مرفوعة حالياً.", reply_markup=back_to_home_keyboard(is_admin))
            return

        keyboard = [[InlineKeyboardButton(f"🗑️ حذف: {p_title[:15]}", callback_data=f"del_prod_{p_id}")] for p_id, p_title in products]
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
        
        await query.edit_message_text("🗑️ **إدارة وحذف المحتوى:**\nاختر المنتج الذي تريد حذفه نهائياً:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("del_prod_"):
        if not is_admin: return
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
        if not is_admin: return
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
        if not is_admin: return
        action, target_id = data.split("_")
        target_id = int(target_id)
        new_status = 1 if action == "ban" else 0
        
        conn = sqlite3.connect("wassab_store.db")
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (new_status, target_id))
        conn.commit()
        conn.close()
        
        await query.answer("✅ تم تنفيذ الحظر/الفتح بنجاح!", show_alert=True)
        return

    elif data.startswith("vip_") or data.startswith("unvip_"):
        if not is_admin: return
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

# ==================== معالجة رسائل ورفع ملفات المسؤول ====================

async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    message = update.message
    user_state = ADMIN_STATES.get(user.id, {})

    if message.text and message.text == "/cancel":
        ADMIN_STATES.pop(user.id, None)
        await message.reply_text("❌ تم الإلغاء بنجاح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ لوحة التحكم", callback_data="admin_panel")]]))
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
            if u_id == ADMIN_ID:
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
            f"📥 **إرسال الملف**\n\n"
            f"تم اعتماد القسم الفرعي: **{subcat}**\n"
            f"الآن أرسل مستند الكتاب في الشات مباشرة."
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
            "اكتب الآن **عنوان الكتاب أو المنتج** وأرسله في رسالة نصية لحفظه نهائياً."
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

import os
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters

flask_app = Flask(__name__)

TOKEN = "9157079266:AAHnLCeWZCFsdR5YCRLs9clVghaYzqYDpTE"
WEBHOOK_URL = f"https://botstore-54tw.onrender.com/{TOKEN}"

# بناء تطبيق البوت
application = ApplicationBuilder().token(TOKEN).build()

# إضافة الهاندلرز الخاصة بك
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_admin_messages))

@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook_listener():
    json_data = request.get_json(force=True)
    update = Update.de_json(json_data, application.bot)
    application.update_queue.put_nowait(update)
    return "OK", 200

@flask_app.route("/")
def home():
    return "Wassab Store Bot is running live via Webhook! 🚀", 200

# (تمت إزالة set_webhook من هنا لأنه تم ربطه مسبقاً وتيليجرام يحفظه تلقائياً)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
