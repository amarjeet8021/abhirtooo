import io
import time
import sqlite3
import secrets
import string
import hashlib
import requests
import qrcode
import telebot
from telebot import types

# ==========================================
# CONFIGURATION
# ==========================================
API_TOKEN = "8860397117:AAFYrUlAyIKLo3htr-6NClM3GAH9SBXa2ZM"  # BotFather se milne wala token
ADMIN_ID = 8951058008                        # Apna Numeric Telegram ID yahan dalein
UPI_ID = "BHARATPE2Y0G0R6C3V57831@unitype"                     # Apna UPI ID (e.g. merchant@paytm / name@okicici)
PAYEE_NAME = "VehicleRto"          # QR Scanner par dikhne wala name

bot = telebot.TeleBot(API_TOKEN)

# Memory tracker for pending UTR input: { user_id: txn_id }
user_pending_utr = {}

# Available Plans Configuration
PLANS = {
    "basic": {"name": "Basic — ₹20", "credits": 50, "price": 20, "type": "credits"},
    "popular": {"name": "Popular — ₹100", "credits": 200, "price": 100, "type": "credits"},
    "good": {"name": "Good Deal — ₹225", "credits": 500, "price": 225, "type": "credits"},
    "great": {"name": "Great Value — ₹450", "credits": 1000, "price": 450, "type": "credits"},
    "1m": {"name": "1 Month Premium — ₹1999", "credits": "Unlimited", "price": 1999, "type": "premium_1m"},
    "life": {"name": "Lifetime Premium — ₹4999", "credits": "Unlimited", "price": 4999, "type": "premium_life"}
}

# Authorized service catalog (display/order workflow only)
SERVICES = {
    "vehicle_detail": {"name": "🚘 Vehicle to Detail", "price": 1000},
    "rc_pdf": {"name": "📄 RC to PDF", "price": 1500},
    "num_info": {"name": "📱 Num to Info", "price": 500},
    "aadhar_family": {"name": "👨‍👩‍👧 Aadhaar to Family", "price": 1200},
    "dl_pdf": {"name": "🪪 DL No to PDF", "price": 1000},
}

# ==========================================
# DATABASE SETUP (SQLite Persistent Storage)
# ==========================================
def get_db_connection():
    return sqlite3.connect("bot_database.db")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            credits INTEGER DEFAULT 10,
            is_premium INTEGER DEFAULT 0,
            searches INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0
        )
    """)
    # Transactions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            txn_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            utr_number TEXT DEFAULT NULL,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Redeem codes table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS redeem_codes (
            code TEXT PRIMARY KEY,
            credits INTEGER NOT NULL,
            used INTEGER DEFAULT 0,
            used_by INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used_at TIMESTAMP DEFAULT NULL
        )
    """)
    # Referral relationships / rewards
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER UNIQUE NOT NULL,
            reward_credits INTEGER DEFAULT 20,
            status TEXT DEFAULT 'COMPLETED',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Persistent user/admin history for payments and referrals
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # API search audit log. Stores API type and a one-way query fingerprint,
    # not the raw phone/Aadhaar/vehicle query value.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            api_name TEXT NOT NULL,
            query_fingerprint TEXT,
            status TEXT DEFAULT 'SUCCESS',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            service_key TEXT NOT NULL,
            service_name TEXT NOT NULL,
            amount REAL NOT NULL,
            payment_txn_id TEXT,
            status TEXT DEFAULT 'PAYMENT_PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrations for existing databases created by older bot versions
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Ensure new tables exist on older databases too
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER UNIQUE NOT NULL,
            reward_credits INTEGER DEFAULT 20,
            status TEXT DEFAULT 'COMPLETED',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def get_or_create_user(user_id, username="Unknown"):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, credits, is_premium, searches, is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        formatted_username = f"@{username}" if username else "N/A"
        cursor.execute(
            "INSERT INTO users (user_id, username, credits, is_premium, searches) VALUES (?, ?, 10, 0, 0)",
            (user_id, formatted_username)
        )
        conn.commit()
        user_data = {"user_id": user_id, "username": formatted_username, "credits": 10, "is_premium": False, "searches": 0, "is_banned": False}
    else:
        user_data = {
            "user_id": row[0],
            "username": row[1],
            "credits": row[2],
            "is_premium": bool(row[3]),
            "searches": row[4],
            "is_banned": bool(row[5])
        }
    conn.close()
    return user_data

def check_access(user_id, cost=10):
    u = get_or_create_user(user_id)
    if u["is_banned"]:
        return False
    if u["is_premium"]:
        return True
    return u["credits"] >= cost

def deduct_credit(user_id, cost=10):
    u = get_or_create_user(user_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if not u["is_premium"]:
        cursor.execute("UPDATE users SET credits = credits - ?, searches = searches + 1 WHERE user_id = ?", (cost, user_id))
    else:
        cursor.execute("UPDATE users SET searches = searches + 1 WHERE user_id = ?", (user_id,))
        
    conn.commit()
    conn.close()

def log_api_search(user_id, api_name, query, status="SUCCESS"):
    # Keep only a SHA-256 fingerprint of the submitted identifier.
    fingerprint = hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:16]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO api_search_history (user_id, api_name, query_fingerprint, status) VALUES (?, ?, ?, ?)",
        (user_id, api_name, fingerprint, status)
    )
    conn.commit()
    conn.close()


def add_history(user_id, action, details=""):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO activity_history (user_id, action, details) VALUES (?, ?, ?)",
        (user_id, action, details)
    )
    conn.commit()
    conn.close()

def get_referral_link(user_id):
    me = bot.get_me()
    return f"https://t.me/{me.username}?start=ref_{user_id}"

def process_referral_start(user_id, referrer_id):
    if referrer_id == user_id:
        return False, "self"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM referrals WHERE referred_id = ?", (user_id,))
    if cursor.fetchone():
        conn.close()
        return False, "already"
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
    if not cursor.fetchone():
        conn.close()
        return False, "missing"
    reward = 20
    cursor.execute(
        "INSERT INTO referrals (referrer_id, referred_id, reward_credits, status) VALUES (?, ?, ?, 'COMPLETED')",
        (referrer_id, user_id, reward)
    )
    cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (reward, referrer_id))
    conn.commit()
    conn.close()
    add_history(referrer_id, "REFERRAL_REWARD", f"Referred user {user_id}; +{reward} credits")
    add_history(user_id, "REFERRED_BY", f"Referrer {referrer_id}")
    return True, reward

def generate_upi_qr(upi_id, name, amount, txn_note):
    upi_uri = f"upi://pay?pa={upi_id}&pn={name}&am={amount}&cu=INR&tn={txn_note}"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    bio.name = 'qr.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

# ==========================================
# KEYBOARDS
# ==========================================
def main_menu_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    
    # 3 Main Search APIs
    markup.add("🚘 Vehicle to Num", "📱 Num to Info")
    markup.add("🪪 Aadhar to Info")
    
    # User Features & Payment System
    markup.add("🛒 Services", "💳 Buy Credits / Pay")
    markup.add("🎁 Redeem Code")
    markup.add("🔗 Refer & Earn", "👤 My Profile")
    markup.add("📜 My History")
    
    if user_id == ADMIN_ID:
        markup.add("🛡️ Admin Panel")
        
    return markup

def admin_menu_keyboard():
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add("➕ Add Credits", "➖ Cut Credits")
    markup.add("💎 Make Premium", "🚫 Remove Premium")
    markup.add("🎁 Global Gift", "🧨 Global Deduct")
    markup.add("🎟️ Gen Redeem Code", "🔍 User Search")
    markup.add("💳 Transaction History", "🤝 Referral History")
    markup.add("📦 Service Orders")
    markup.add("📅 Daily Collection", "📆 Collection Report")
    markup.add("🛑 Ban User", "♻️ Unban User")
    markup.add("📊 Bot Stats", "📈 API Stats")
    markup.add("👥 User Database", "📡 API Status")
    markup.add("🔎 API Search History")
    markup.add("📋 User List", "📢 Broadcast")
    markup.add("🏓 Ping", "🔙 Back to Main")
    return markup

# ==========================================
# START COMMAND
# ==========================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    u = get_or_create_user(user_id, message.from_user.username)
    # Telegram deep-link referral: /start ref_<user_id>
    if len(message.text.split()) > 1 and message.text.split()[1].startswith("ref_"):
        try:
            referrer_id = int(message.text.split()[1].replace("ref_", "", 1))
            ok, result = process_referral_start(user_id, referrer_id)
            if ok:
                bot.send_message(referrer_id, f"🎉 <b>Referral Successful!</b>\n\nUser <code>{user_id}</code> joined using your link.\n💳 <b>+{result} credits</b> added to your balance.", parse_mode="HTML")
        except Exception:
            pass
    if u["is_banned"] and user_id != ADMIN_ID:
        bot.send_message(message.chat.id, "🛑 <b>You are banned from using this bot.</b>", parse_mode="HTML")
        return
    welcome_text = (
        "<b>Welcome to Search Bot!</b>\n\n"
        "🎁 You got <b>10 Free Credits</b> on sign up!\n"
        "⚡ Every search costs: <b>10 Credits</b>."
    )
    bot.send_message(
        message.chat.id, 
        welcome_text, 
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(message.from_user.id)
    )

# ==========================================
# MAIN ROUTING & PAYMENT INITIATION
# ==========================================
def send_daily_collection_report(chat_id):
    """Show today's approved payment collection using India (IST) date."""
    conn = get_db_connection()
    cursor = conn.cursor()
    # transactions.created_at is stored in SQLite UTC CURRENT_TIMESTAMP.
    cursor.execute("""
        SELECT COUNT(*), COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE status = 'APPROVED'
          AND date(created_at, '+5 hours', '+30 minutes') = date('now', '+5 hours', '+30 minutes')
    """)
    approved_count, approved_total = cursor.fetchone()

    cursor.execute("""
        SELECT COUNT(*), COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE status = 'PENDING'
          AND date(created_at, '+5 hours', '+30 minutes') = date('now', '+5 hours', '+30 minutes')
    """)
    pending_count, pending_total = cursor.fetchone()

    cursor.execute("""
        SELECT COUNT(*), COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE status = 'REJECTED'
          AND date(created_at, '+5 hours', '+30 minutes') = date('now', '+5 hours', '+30 minutes')
    """)
    rejected_count, rejected_total = cursor.fetchone()
    conn.close()

    import datetime
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime('%d-%m-%Y')
    report = (
        f"📅 <b>TODAY'S COLLECTION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📆 Date: <b>{today}</b> (IST)\n\n"
        f"✅ Approved: <b>{approved_count}</b> payments\n"
        f"💰 Collected: <b>₹{approved_total:,.2f}</b>\n\n"
        f"⏳ Pending: <b>{pending_count}</b> (₹{pending_total:,.2f})\n"
        f"❌ Rejected: <b>{rejected_count}</b> (₹{rejected_total:,.2f})"
    )
    bot.send_message(chat_id, report, parse_mode="HTML")


def send_collection_report(chat_id):
    """Show approved collection totals for the last 7 IST calendar days."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT date(created_at, '+5 hours', '+30 minutes') AS day,
               COUNT(*), COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE status = 'APPROVED'
          AND datetime(created_at, '+5 hours', '+30 minutes') >= datetime('now', '+5 hours', '+30 minutes', '-6 days')
        GROUP BY day
        ORDER BY day DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        bot.send_message(chat_id, "📆 <b>COLLECTION REPORT</b>\n\nNo approved payments found in the last 7 days.", parse_mode="HTML")
        return

    total = sum(float(r[2] or 0) for r in rows)
    count = sum(int(r[1] or 0) for r in rows)
    out = "📆 <b>7-DAY COLLECTION REPORT</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for day, cnt, amount in rows:
        out += f"📅 <b>{day}</b> — {cnt} payments — <b>₹{float(amount):,.2f}</b>\n"
    out += f"\n💰 <b>Total: ₹{total:,.2f}</b>\n🧾 <b>Payments: {count}</b>"
    bot.send_message(chat_id, out, parse_mode="HTML")


@bot.message_handler(func=lambda message: True)
def handle_menu(message):
    user_id = message.from_user.id
    text = message.text
    u = get_or_create_user(user_id, message.from_user.username)

    if u["is_banned"] and user_id != ADMIN_ID and text != "♻️ Unban User":
        bot.send_message(message.chat.id, "🛑 <b>You are banned from using this bot.</b>", parse_mode="HTML")
        return

    if text == "🔙 Back to Main":
        bot.send_message(message.chat.id, "Main Menu:", reply_markup=main_menu_keyboard(user_id))

    elif text == "🛒 Services":
        show_services(message.chat.id)

    elif text == "👤 My Profile":
        status = "💎 Premium User" if u["is_premium"] else "🆓 Free User"
        profile_text = (
            f"🔍 <b>USER REPORT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
            f"👤 <b>Username:</b> {u['username']}\n"
            f"💳 <b>Credits:</b> {u['credits']}\n"
            f"🏷️ <b>Status:</b> {status}\n"
            f"📈 <b>Total API Calls:</b> {u['searches']}\n\n"
        )
        bot.send_message(message.chat.id, profile_text, parse_mode="HTML")

    elif text == "🔗 Refer & Earn":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(reward_credits), 0) FROM referrals WHERE referrer_id = ?", (user_id,))
        total_refs, total_rewards = cursor.fetchone()
        conn.close()
        link = get_referral_link(user_id)
        refer_text = (
            "🔗 <b>REFER & EARN</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>Reward:</b> 20 Credits per successful referral\n"
            f"📊 <b>Total Referrals:</b> {total_refs}\n"
            f"💳 <b>Total Earned:</b> {total_rewards} Credits\n\n"
            "📨 Your Referral Link:\n"
            f"<code>{link}</code>\n\n"
            "Share this link. When a new user starts the bot through your link, the reward is added automatically."
        )
        bot.send_message(message.chat.id, refer_text, parse_mode="HTML")

    elif text == "📜 My History":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT action, details, created_at FROM activity_history WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,))
        rows = cursor.fetchall()
        cursor.execute("SELECT txn_id, amount, status, created_at FROM transactions WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,))
        txns = cursor.fetchall()
        conn.close()
        out = "📜 <b>MY HISTORY</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        if rows:
            out += "\n<b>Activity:</b>\n"
            for action, details, created_at in rows:
                out += f"• {created_at} — <b>{action}</b>\n  {details}\n"
        else:
            out += "\nNo activity history yet.\n"
        if txns:
            out += "\n<b>Payments:</b>\n"
            for txn_id, amount, status, created_at in txns:
                out += f"• <code>{txn_id}</code> | ₹{amount:.2f} | {status} | {created_at}\n"
        bot.send_message(message.chat.id, out[:4000], parse_mode="HTML")

    elif text == "💳 Buy Credits / Pay":
        plan_msg = "<b>💳 AVAILABLE PLANS</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for key, p in PLANS.items():
            plan_msg += f"• <b>{p['name']}</b>\n  Type: {p['type']}\n\n"
        
        bot.send_message(message.chat.id, plan_msg, parse_mode="HTML")
        msg = bot.send_message(message.chat.id, "💵 Enter the amount you want to add (in ₹):")
        bot.register_next_step_handler(msg, process_payment_amount)

    # API 1: Vehicle to Num
    elif text == "🚘 Vehicle to Num":
        msg = bot.send_message(message.chat.id, "Enter Vehicle Number (e.g., KL41V3504):")
        bot.register_next_step_handler(msg, process_vehicle_to_num)

    # API 2: Num to Info
    elif text == "📱 Num to Info":
        msg = bot.send_message(message.chat.id, "Enter Mobile Number (e.g., 9876543210):")
        bot.register_next_step_handler(msg, process_num_to_info)

    # API 3: Aadhar to Info
    elif text == "🪪 Aadhar to Info":
        msg = bot.send_message(message.chat.id, "Enter 12-digit Aadhar Number:")
        bot.register_next_step_handler(msg, process_aadhar_to_info)

    # Admin Commands
    elif text == "📦 Service Orders" and user_id == ADMIN_ID:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT order_id,user_id,service_name,amount,status,created_at FROM service_orders ORDER BY created_at DESC LIMIT 30")
        rows = cur.fetchall(); conn.close()
        if not rows:
            bot.send_message(message.chat.id, "📦 No service orders yet.")
        else:
            out = "📦 <b>RECENT SERVICE ORDERS</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            for oid,uid,name,amt,status,created in rows:
                out += f"\n<code>{oid}</code> | <code>{uid}</code>\n{name} — ₹{amt:.0f}\nStatus: <b>{status}</b>\n{created}\n"
            bot.send_message(message.chat.id, out, parse_mode="HTML")

    elif text == "🛡️ Admin Panel" and user_id == ADMIN_ID:
        bot.send_message(message.chat.id, "🛡️ <b>Admin Panel Unlocked</b>", parse_mode="HTML", reply_markup=admin_menu_keyboard())

    elif text == "💳 Transaction History" and user_id == ADMIN_ID:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT txn_id, user_id, amount, utr_number, status, created_at FROM transactions ORDER BY created_at DESC LIMIT 30")
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            bot.send_message(message.chat.id, "📜 No transactions found.")
        else:
            out = "💳 <b>TRANSACTION HISTORY</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            for txn_id, uid, amount, utr, status, created_at in rows:
                out += f"<code>{txn_id}</code> | User: <code>{uid}</code> | ₹{amount:.2f} | {status}\nUTR: <code>{utr or '-'} </code> | {created_at}\n\n"
            bot.send_message(message.chat.id, out[:4000], parse_mode="HTML")

    elif text == "📅 Daily Collection" and user_id == ADMIN_ID:
        send_daily_collection_report(message.chat.id)

    elif text == "📆 Collection Report" and user_id == ADMIN_ID:
        send_collection_report(message.chat.id)

    elif text == "🤝 Referral History" and user_id == ADMIN_ID:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT referrer_id, referred_id, reward_credits, status, created_at FROM referrals ORDER BY created_at DESC LIMIT 30")
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            bot.send_message(message.chat.id, "🤝 No referral history found.")
        else:
            out = "🤝 <b>REFERRAL HISTORY</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            for referrer, referred, reward, status, created_at in rows:
                out += f"👤 Referrer: <code>{referrer}</code> → User: <code>{referred}</code>\n💳 Reward: {reward} Credits | {status}\n🕒 {created_at}\n\n"
            bot.send_message(message.chat.id, out[:4000], parse_mode="HTML")

    elif text == "📊 Bot Stats" and user_id == ADMIN_ID:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        conn.close()
        bot.send_message(message.chat.id, f"📊 <b>Total Registered Users:</b> {total_users}", parse_mode="HTML")

    elif text == "👥 User Database" and user_id == ADMIN_ID:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(credits),0), SUM(is_premium), SUM(is_banned), COALESCE(SUM(searches),0) FROM users")
        total_users, total_credits, premium_users, banned_users, total_searches = cursor.fetchone()
        cursor.execute("SELECT user_id, username, credits, is_premium, searches, is_banned FROM users ORDER BY user_id DESC LIMIT 25")
        rows = cursor.fetchall()
        conn.close()
        out = (f"👥 <b>USER DATABASE</b>\n━━━━━━━━━━━━━━━━━━━━\n"
               f"👤 Total Users: <b>{total_users}</b>\n"
               f"💳 Total Credits: <b>{total_credits}</b>\n"
               f"💎 Premium: <b>{premium_users or 0}</b>\n"
               f"🛑 Banned: <b>{banned_users or 0}</b>\n"
               f"📈 Total Searches: <b>{total_searches}</b>\n\n<b>Latest 25 Users:</b>\n")
        for uid, uname, credits, premium, searches, banned in rows:
            status = "💎" if premium else "🆓"
            if banned:
                status = "🛑"
            out += f"{status} <code>{uid}</code> | {uname or 'N/A'} | C:{credits} | S:{searches}\n"
        bot.send_message(message.chat.id, out[:4000], parse_mode="HTML")

    elif text == "🔎 API Search History" and user_id == ADMIN_ID:
        msg = bot.send_message(message.chat.id, "Enter User ID to view API search history:")
        bot.register_next_step_handler(msg, process_api_search_history)

    elif text == "📡 API Status" and user_id == ADMIN_ID:
        api_list = [
            ("Vehicle to Num", "https://sarkariupdate.online/darkhosting/DarkCore/DarkApiX.php?api=vehicle_to_num&q=KL41V3504"),
            ("Num to Info", "https://sarkariupdate.online/darkhosting/DarkCore/DarkApiX.php?api=num_to_info&q=9876543210"),
            ("Aadhar to Info", "https://sarkariupdate.online/darkhosting/DarkCore/DarkApiX.php?api=aadhar_to_info&q=000000000000"),
        ]
        out = "📡 <b>API STATUS</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for name, url in api_list:
            try:
                started = time.time()
                r = requests.get(url, timeout=8)
                ms = round((time.time() - started) * 1000)
                if r.status_code == 200:
                    out += f"🟢 <b>{name}</b> — ONLINE ({ms}ms)\n"
                else:
                    out += f"🟠 <b>{name}</b> — HTTP {r.status_code} ({ms}ms)\n"
            except Exception as e:
                out += f"🔴 <b>{name}</b> — OFFLINE\n"
        bot.send_message(message.chat.id, out, parse_mode="HTML")

    elif text == "🏓 Ping":
        start_time = time.time()
        msg = bot.send_message(message.chat.id, "Pinging...")
        end_time = time.time()
        ping_ms = round((end_time - start_time) * 1000)
        bot.edit_message_text(f"🏓 <b>Pong!</b> {ping_ms}ms", message.chat.id, msg.message_id, parse_mode="HTML")

    elif text == "🔍 User Search" and user_id == ADMIN_ID:
        msg = bot.send_message(message.chat.id, "Enter User ID to Search:")
        bot.register_next_step_handler(msg, process_admin_user_search)

    # Admin: Add Credits
    elif text == "➕ Add Credits" and user_id == ADMIN_ID:
        msg = bot.send_message(
            message.chat.id,
            "➕ Enter User ID and credits:\n\n<code>123456789 100</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_add_credits)

    # Admin: Cut Credits
    elif text == "➖ Cut Credits" and user_id == ADMIN_ID:
        msg = bot.send_message(
            message.chat.id,
            "➖ Enter User ID and credits to cut:\n\n<code>123456789 50</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_cut_credits)

    # Admin: Make Premium
    elif text == "💎 Make Premium" and user_id == ADMIN_ID:
        msg = bot.send_message(
            message.chat.id,
            "💎 Enter User ID:\n\n<code>123456789</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_make_premium)

    # Admin: Remove Premium
    elif text == "🚫 Remove Premium" and user_id == ADMIN_ID:
        msg = bot.send_message(
            message.chat.id,
            "🚫 Enter User ID:\n\n<code>123456789</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_remove_premium)

    # Admin: Ban User
    elif text == "🛑 Ban User" and user_id == ADMIN_ID:
        msg = bot.send_message(
            message.chat.id,
            "🛑 Enter User ID to ban:\n\n<code>123456789</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_ban_user)

    # Admin: Unban User
    elif text == "♻️ Unban User" and user_id == ADMIN_ID:
        msg = bot.send_message(
            message.chat.id,
            "♻️ Enter User ID to unban:\n\n<code>123456789</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_unban_user)

    # Admin: Generate Redeem Code
    elif text == "🎟️ Gen Redeem Code" and user_id == ADMIN_ID:
        msg = bot.send_message(
            message.chat.id,
            "🎟️ Enter credits for the redeem code:\n\n<code>100</code>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_generate_redeem_code)

    # User: Redeem Code
    elif text == "🎁 Redeem Code":
        msg = bot.send_message(message.chat.id, "🎁 Enter your redeem code:")
        bot.register_next_step_handler(msg, process_redeem_code)

    # Admin: Global Gift
    elif text == "🎁 Global Gift" and user_id == ADMIN_ID:
        msg = bot.send_message(message.chat.id, "🎁 Enter credits to gift every registered user:\n\n<code>10</code>", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_global_gift)

    # Admin Broadcast
    elif text == "📢 Broadcast" and user_id == ADMIN_ID:
        msg = bot.send_message(
            message.chat.id,
            "📢 <b>Broadcast Mode</b>\\n\\n"
            "Ab jo message/photo/video/document/sticker bhejoge, "
            "woh sabhi registered users ko send hoga.\\n\\n"
            "❌ Cancel karne ke liye /cancel bhejo.",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_broadcast)

# ==========================================
# ADMIN CREDIT / PREMIUM / REDEEM FUNCTIONS
# ==========================================

def _admin_user_amount(message):
    parts = message.text.strip().split()
    if len(parts) != 2:
        raise ValueError
    user_id = int(parts[0])
    amount = int(parts[1])
    if user_id <= 0 or amount <= 0:
        raise ValueError
    return user_id, amount


def process_add_credits(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id, amount = _admin_user_amount(message)
        get_or_create_user(target_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, target_id))
        cursor.execute("SELECT credits FROM users WHERE user_id = ?", (target_id,))
        balance = cursor.fetchone()[0]
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"✅ <b>Credits Added</b>\n\n👤 ID: <code>{target_id}</code>\n➕ Added: <b>{amount}</b>\n💳 Balance: <b>{balance}</b>",
            parse_mode="HTML", reply_markup=admin_menu_keyboard()
        )
        try:
            bot.send_message(target_id, f"🎁 Admin ne <b>{amount}</b> credits add kiye.\n💳 Balance: <b>{balance}</b>", parse_mode="HTML")
        except Exception:
            pass
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Format: <code>USER_ID AMOUNT</code>", parse_mode="HTML")


def process_cut_credits(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id, amount = _admin_user_amount(message)
        get_or_create_user(target_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT credits FROM users WHERE user_id = ?", (target_id,))
        old = cursor.fetchone()[0]
        cut = min(amount, old)
        cursor.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (cut, target_id))
        cursor.execute("SELECT credits FROM users WHERE user_id = ?", (target_id,))
        balance = cursor.fetchone()[0]
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"✅ <b>Credits Cut</b>\n\n👤 ID: <code>{target_id}</code>\n➖ Cut: <b>{cut}</b>\n💳 Balance: <b>{balance}</b>",
            parse_mode="HTML", reply_markup=admin_menu_keyboard()
        )
        try:
            bot.send_message(target_id, f"⚠️ Admin ne <b>{cut}</b> credits cut kiye.\n💳 Balance: <b>{balance}</b>", parse_mode="HTML")
        except Exception:
            pass
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Format: <code>USER_ID AMOUNT</code>", parse_mode="HTML")


def process_make_premium(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text.strip())
        get_or_create_user(target_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"💎 <b>Premium Activated</b>\n\n👤 ID: <code>{target_id}</code>",
            parse_mode="HTML", reply_markup=admin_menu_keyboard()
        )
        try:
            bot.send_message(target_id, "🎉 <b>Premium Activated!</b>\nAdmin ne aapka account Premium kar diya.", parse_mode="HTML")
        except Exception:
            pass
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Invalid User ID.")


def process_remove_premium(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text.strip())
        get_or_create_user(target_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_premium = 0 WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"🚫 <b>Premium Removed</b>\n\n👤 ID: <code>{target_id}</code>",
            parse_mode="HTML", reply_markup=admin_menu_keyboard()
        )
        try:
            bot.send_message(target_id, "ℹ️ Aapka Premium status remove kar diya gaya hai.", parse_mode="HTML")
        except Exception:
            pass
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Invalid User ID.")


def process_ban_user(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text.strip())
        if target_id == ADMIN_ID:
            bot.send_message(message.chat.id, "❌ Admin account cannot be banned.", reply_markup=admin_menu_keyboard())
            return
        get_or_create_user(target_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"🛑 <b>User Banned</b>\n\n👤 ID: <code>{target_id}</code>", parse_mode="HTML", reply_markup=admin_menu_keyboard())
        try:
            bot.send_message(target_id, "🛑 <b>Your access has been banned by the admin.</b>", parse_mode="HTML")
        except Exception:
            pass
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Invalid User ID.", reply_markup=admin_menu_keyboard())


def process_unban_user(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text.strip())
        get_or_create_user(target_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"♻️ <b>User Unbanned</b>\n\n👤 ID: <code>{target_id}</code>", parse_mode="HTML", reply_markup=admin_menu_keyboard())
        try:
            bot.send_message(target_id, "♻️ <b>Your bot access has been restored.</b>", parse_mode="HTML")
        except Exception:
            pass
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Invalid User ID.", reply_markup=admin_menu_keyboard())


def generate_redeem_code():
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "VEH-" + "".join(secrets.choice(chars) for _ in range(10))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM redeem_codes WHERE code = ?", (code,))
        exists = cursor.fetchone()
        conn.close()
        if not exists:
            return code


def process_generate_redeem_code(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        credits = int(message.text.strip())
        if credits <= 0:
            raise ValueError
        code = generate_redeem_code()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO redeem_codes (code, credits) VALUES (?, ?)", (code, credits))
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"🎟️ <b>Redeem Code Generated</b>\n━━━━━━━━━━━━━━━━\n"
            f"🔑 Code: <code>{code}</code>\n💳 Credits: <b>{credits}</b>\n\n"
            f"Ye code kisi bhi user ko de sakte ho. Code <b>1 baar</b> hi redeem hoga.",
            parse_mode="HTML", reply_markup=admin_menu_keyboard()
        )
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Example: <code>100</code>", parse_mode="HTML")


def process_redeem_code(message):
    user_id = message.from_user.id
    code = message.text.strip().upper()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT credits, used FROM redeem_codes WHERE code = ?", (code,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        bot.send_message(message.chat.id, "❌ Redeem code not found.")
        return
    credits, used = row
    if used:
        conn.close()
        bot.send_message(message.chat.id, "❌ This code has already been used.")
        return

    get_or_create_user(user_id, message.from_user.username)
    cursor.execute(
        "UPDATE redeem_codes SET used=1, used_by=?, used_at=CURRENT_TIMESTAMP WHERE code=? AND used=0",
        (user_id, code)
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        bot.send_message(message.chat.id, "❌ This code has already been used.")
        return

    cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (credits, user_id))
    cursor.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    balance = cursor.fetchone()[0]
    conn.commit()
    conn.close()

    bot.send_message(
        message.chat.id,
        f"🎉 <b>Redeem Successful!</b>\n\n"
        f"🎟️ Code: <code>{code}</code>\n➕ Added: <b>{credits}</b> credits\n💳 Balance: <b>{balance}</b>",
        parse_mode="HTML"
    )


# ==========================================
# GLOBAL GIFT
# ==========================================
def process_global_gift(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET credits = credits + ?", (amount,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"🎁 <b>GLOBAL GIFT COMPLETED</b>\n\n👥 Users: <b>{affected}</b>\n➕ Added per user: <b>{amount} Credits</b>\n💳 Total credits distributed: <b>{affected * amount}</b>",
            parse_mode="HTML", reply_markup=admin_menu_keyboard()
        )
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Enter a valid positive credit amount.", reply_markup=admin_menu_keyboard())

# ==========================================
# BROADCAST SYSTEM
# ==========================================
def process_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text and message.text.strip().lower() == "/cancel":
        bot.send_message(
            message.chat.id,
            "❌ Broadcast cancelled.",
            reply_markup=admin_menu_keyboard()
        )
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    user_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    sent = 0
    failed = 0

    for target_id in user_ids:
        try:
            # Copies the exact message type (text/photo/video/document/sticker, etc.)
            bot.copy_message(
                chat_id=target_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1

    bot.send_message(
        message.chat.id,
        f"📢 <b>Broadcast Completed</b>\\n\\n"
        f"👥 Total Users: <b>{len(user_ids)}</b>\\n"
        f"✅ Sent: <b>{sent}</b>\\n"
        f"❌ Failed: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard()
    )

# ==========================================
# SERVICE STORE / ORDER FLOW
# ==========================================
def services_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, service in SERVICES.items():
        markup.add(types.InlineKeyboardButton(
            f"{service['name']} — ₹{service['price']}",
            callback_data=f"svc_{key}"
        ))
    return markup

def show_services(chat_id):
    text = "🛒 <b>SERVICES</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    text += "Select a service below to see its price and create an order.\n\n"
    for service in SERVICES.values():
        text += f"• {service['name']} — <b>₹{service['price']}</b>\n"
    text += "\n⚠️ Orders are fulfilled only for lawful/authorized services."
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=services_keyboard())

@bot.callback_query_handler(func=lambda call: call.data.startswith("svc_"))
def service_selected(call):
    key = call.data.replace("svc_", "", 1)
    if key not in SERVICES:
        bot.answer_callback_query(call.id, "Service not found.", show_alert=True)
        return
    service = SERVICES[key]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        f"💳 Pay ₹{service['price']}", callback_data=f"buyservice_{key}"
    ))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="services_back"))
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"<b>{service['name']}</b>\n\n💰 Price: <b>₹{service['price']}</b>\n\n"
        "Press Pay to generate a UPI QR and create your order.",
        parse_mode="HTML", reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "services_back")
def services_back(call):
    bot.answer_callback_query(call.id)
    show_services(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("buyservice_"))
def buy_service(call):
    key = call.data.replace("buyservice_", "", 1)
    user_id = call.from_user.id
    if key not in SERVICES:
        bot.answer_callback_query(call.id, "Service not found.", show_alert=True)
        return
    service = SERVICES[key]
    order_id = f"ORD{int(time.time())}{user_id % 1000}"
    txn_id = f"TXN{int(time.time())}{user_id % 1000}"
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO service_orders (order_id,user_id,service_key,service_name,amount,payment_txn_id,status) VALUES (?,?,?,?,?,?,?)",
                (order_id,user_id,key,service["name"],service["price"],txn_id,"PAYMENT_PENDING"))
    cur.execute("INSERT INTO transactions (txn_id,user_id,amount,status) VALUES (?,?,?,'PENDING')",
                (txn_id,user_id,service["price"]))
    conn.commit(); conn.close()
    add_history(user_id, "SERVICE_ORDER_CREATED", f"{order_id}; {service['name']}; ₹{service['price']}")
    qr_bytes = generate_upi_qr(UPI_ID, PAYEE_NAME, service["price"], f"Order_{order_id}")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📥 Submit UTR / Ref No.", callback_data=f"submit_utr_{txn_id}"))
    caption = (f"🛒 <b>Service Order</b>\n━━━━━━━━━━━━━━━━━━━━\n"
               f"📦 {service['name']}\n🆔 Order: <code>{order_id}</code>\n"
               f"💵 Amount: <b>₹{service['price']}</b>\n📌 UPI: <code>{UPI_ID}</code>\n\n"
               "Pay the exact amount, then submit your UTR. Payment is verified before fulfillment.")
    bot.answer_callback_query(call.id)
    bot.send_photo(call.message.chat.id, qr_bytes, caption=caption, parse_mode="HTML", reply_markup=markup)

# Admin: view recent service orders
# ==========================================
# PAYMENT QR & UTR SUBMISSION ENGINE
# ==========================================
def process_payment_amount(message):
    user_id = message.from_user.id
    try:
        amount = float(message.text.strip())
        if amount < 1:
            bot.send_message(message.chat.id, "❌ Minimum deposit amount is ₹1.")
            return
            
        txn_id = f"TXN{int(time.time())}{user_id % 1000}"
        
        # Save transaction in SQLite DB
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO transactions (txn_id, user_id, amount, status) VALUES (?, ?, ?, 'PENDING')",
                       (txn_id, user_id, amount))
        conn.commit()
        conn.close()
        add_history(user_id, "PAYMENT_CREATED", f"Txn {txn_id}; amount ₹{amount:.2f}")
        
        # Generate Dynamic UPI QR Code Image
        qr_bytes = generate_upi_qr(UPI_ID, PAYEE_NAME, amount, f"Ref_{txn_id}")
        
        caption = (
            f"✅ <b>Payment Request Generated</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Txn Reference:</b> <code>{txn_id}</code>\n"
            f"💵 <b>Amount:</b> ₹{amount:.2f}\n"
            f"📌 <b>UPI ID:</b> <code>{UPI_ID}</code>\n\n"
            f"1️⃣ QR Code scan karke exact <b>₹{amount:.2f}</b> pay karein.\n"
            f"2️⃣ Payment ke baad niche button par click karke 12-digit UTR/Ref number enter karein."
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📥 Submit UTR / Ref No.", callback_data=f"submit_utr_{txn_id}"))
        
        bot.send_photo(message.chat.id, photo=qr_bytes, caption=caption, parse_mode="HTML", reply_markup=markup)
        
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid input. Please enter a numerical amount.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("submit_utr_"))
def ask_for_utr(call):
    txn_id = call.data.replace("submit_utr_", "")
    user_id = call.from_user.id
    
    user_pending_utr[user_id] = txn_id
    bot.answer_callback_query(call.id)
    
    msg = bot.send_message(
        call.message.chat.id,
        f"📝 Please reply with the <b>12-digit UTR/Transaction Ref Number</b> for Txn <code>{txn_id}</code>:",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, process_utr_input)

def process_utr_input(message):
    user_id = message.from_user.id
    utr = message.text.strip()
    
    if user_id not in user_pending_utr:
        bot.send_message(message.chat.id, "❌ Session expired or invalid transaction request. Please initiate payment again.")
        return
        
    txn_id = user_pending_utr.pop(user_id)
    
    if len(utr) < 6 or not utr.isalnum():
        bot.send_message(message.chat.id, "❌ Invalid UTR format. Please make sure you enter a valid transaction reference.")
        return

    # Update UTR in database
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE transactions SET utr_number = ? WHERE txn_id = ?", (utr, txn_id))
    cursor.execute("SELECT amount FROM transactions WHERE txn_id = ?", (txn_id,))
    row = cursor.fetchone()
    conn.close()
    
    amount = row[0] if row else 0.0
    add_history(user_id, "UTR_SUBMITTED", f"Txn {txn_id}; UTR {utr}")
    
    bot.send_message(
        message.chat.id,
        f"⏳ <b>UTR Submitted Successfully!</b>\n\nUTR: <code>{utr}</code>\nYour payment is pending admin verification.",
        parse_mode="HTML"
    )
    
    # Admin Approval Request Message
    admin_markup = types.InlineKeyboardMarkup()
    btn_approve = types.InlineKeyboardButton("✅ Approve", callback_data=f"app_{txn_id}")
    btn_reject = types.InlineKeyboardButton("❌ Reject", callback_data=f"rej_{txn_id}")
    admin_markup.add(btn_approve, btn_reject)
    
    admin_msg = (
        f"🔔 <b>New Payment Verification Request</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
        f"🆔 <b>Txn ID:</b> <code>{txn_id}</code>\n"
        f"💵 <b>Amount:</b> ₹{amount:.2f}\n"
        f"🔢 <b>Submitted UTR:</b> <code>{utr}</code>"
    )
    
    bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML", reply_markup=admin_markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith(("app_", "rej_")))
def handle_admin_decision(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Unauthorized", show_alert=True)
        return

    action, txn_id = call.data.split("_", 1)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, amount, utr_number, status FROM transactions WHERE txn_id = ?", (txn_id,))
    txn = cursor.fetchone()
    
    if not txn:
        bot.answer_callback_query(call.id, "Transaction not found.")
        conn.close()
        return
        
    user_id, amount, utr, current_status = txn
    
    if current_status != "PENDING":
        bot.answer_callback_query(call.id, f"Already processed as {current_status}.")
        conn.close()
        return
        
    if action == "app":
        cursor.execute("UPDATE transactions SET status = 'APPROVED' WHERE txn_id = ?", (txn_id,))
        cursor.execute("UPDATE service_orders SET status='PAID', updated_at=CURRENT_TIMESTAMP WHERE payment_txn_id=?", (txn_id,))
        cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (int(amount), user_id))
        conn.commit()
        add_history(user_id, "PAYMENT_APPROVED", f"Txn {txn_id}; +₹{amount:.2f} credits")
        
        bot.edit_message_text(
            f"✅ <b>Approved Txn</b> <code>{txn_id}</code>\nAmount: ₹{amount:.2f} | UTR: <code>{utr}</code>",
            ADMIN_ID,
            call.message.message_id,
            parse_mode="HTML"
        )
        cursor.execute("SELECT order_id, service_name FROM service_orders WHERE payment_txn_id=?", (txn_id,))
        service_order = cursor.fetchone()
        bot.send_message(
            user_id,
            f"🎉 <b>Payment Approved!</b>\n\n₹{amount:.2f} payment approved.",
            parse_mode="HTML"
        )
        if service_order:
            bot.send_message(user_id, f"📦 <b>Order Paid</b>\nOrder: <code>{service_order[0]}</code>\nService: {service_order[1]}\n\nYour authorized service order is now marked <b>PAID</b> and ready for fulfillment.", parse_mode="HTML")
    else:
        cursor.execute("UPDATE transactions SET status = 'REJECTED' WHERE txn_id = ?", (txn_id,))
        cursor.execute("UPDATE service_orders SET status='PAYMENT_REJECTED', updated_at=CURRENT_TIMESTAMP WHERE payment_txn_id=?", (txn_id,))
        conn.commit()
        add_history(user_id, "PAYMENT_REJECTED", f"Txn {txn_id}; ₹{amount:.2f}")
        
        bot.edit_message_text(
            f"❌ <b>Rejected Txn</b> <code>{txn_id}</code>",
            ADMIN_ID,
            call.message.message_id,
            parse_mode="HTML"
        )
        bot.send_message(
            user_id,
            f"❌ <b>Payment Rejected</b>\n\nTransaction ID <code>{txn_id}</code> verify nahi ho paaya.",
            parse_mode="HTML"
        )
        
    conn.close()

# ==========================================
# 3 SEARCH API HANDLERS
# ==========================================

# 1. Vehicle to Num
def process_vehicle_to_num(message):
    user_id = message.from_user.id
    if not check_access(user_id, cost=10):
        bot.send_message(message.chat.id, "❌ Minimum 10 Credits required to search! Please buy credits.")
        return

    query = message.text.strip()
    bot.send_message(message.chat.id, "🔍 Searching Vehicle Details...")
    
    url = f"https://sarkariupdate.online/darkhosting/DarkCore/DarkApiX.php?api=api_8b46c1&q={query}"
    try:
        res = requests.get(url, timeout=15)
        deduct_credit(user_id, cost=10)
        log_api_search(user_id, "Vehicle to Num", query, "SUCCESS")
        bot.send_message(message.chat.id, f"<b>Result:</b>\n<code>{res.text}</code>", parse_mode="HTML")
    except Exception as e:
        log_api_search(user_id, "Vehicle to Num", query, "ERROR")
        bot.send_message(message.chat.id, f"❌ Error fetching data: {str(e)}")

# 2. Num to Info
def process_num_to_info(message):
    user_id = message.from_user.id
    if not check_access(user_id, cost=10):
        bot.send_message(message.chat.id, "❌ Minimum 10 Credits required to search! Please buy credits.")
        return

    query = message.text.strip()
    bot.send_message(message.chat.id, "🔍 Searching Number Info...")
    
    url = f"https://sarkariupdate.online/darkhosting/DarkCore/DarkApiX.php?api=num_to_info&q={query}"
    try:
        res = requests.get(url, timeout=15)
        deduct_credit(user_id, cost=10)
        log_api_search(user_id, "Num to Info", query, "SUCCESS")
        bot.send_message(message.chat.id, f"<b>Result:</b>\n<code>{res.text}</code>", parse_mode="HTML")
    except Exception as e:
        log_api_search(user_id, "Num to Info", query, "ERROR")
        bot.send_message(message.chat.id, f"❌ Error fetching data: {str(e)}")

# 3. Aadhar to Info
def process_aadhar_to_info(message):
    user_id = message.from_user.id
    if not check_access(user_id, cost=10):
        bot.send_message(message.chat.id, "❌ Minimum 10 Credits required to search! Please buy credits.")
        return

    query = message.text.strip()
    bot.send_message(message.chat.id, "🔍 Fetching Details...")
    
    url = f"https://sarkariupdate.online/darkhosting/DarkCore/DarkApiX.php?api=aadhar_to_info&q={query}"
    try:
        res = requests.get(url, timeout=15)
        deduct_credit(user_id, cost=10)
        log_api_search(user_id, "Aadhar to Info", query, "SUCCESS")
        
        # Display response safely
        response_text = res.text
        bot.send_message(message.chat.id, f"<b>Result:</b>\n<code>{response_text}</code>", parse_mode="HTML")
    except Exception as e:
        log_api_search(user_id, "Aadhar to Info", query, "ERROR")
        bot.send_message(message.chat.id, f"❌ Error fetching data: {str(e)}")

# ==========================================
# ADMIN API SEARCH HISTORY
# ==========================================
def process_api_search_history(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid User ID.")
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT api_name, query_fingerprint, status, created_at
        FROM api_search_history WHERE user_id = ?
        ORDER BY id DESC LIMIT 50
    """, (target_id,))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        bot.send_message(message.chat.id, "📭 Is user ki API search history nahi mili.")
        return
    out = f"🔎 <b>API SEARCH HISTORY</b>\n🆔 <code>{target_id}</code>\n━━━━━━━━━━━━━━━━━━━━\n"
    for api_name, fp, status, created_at in rows:
        out += f"• <b>{api_name}</b> | {status} | {created_at}\n  🔐 Query fingerprint: <code>{fp}</code>\n"
    bot.send_message(message.chat.id, out[:4090], parse_mode="HTML")

# ==========================================
# ADMIN USER SEARCH HANDLER
# ==========================================
def process_admin_user_search(message):
    try:
        target_id = int(message.text.strip())
        u = get_or_create_user(target_id)
        
        if u:
            uname = u["username"]
            credits = u["credits"]
            status = "💎 Premium" if u["is_premium"] else "🆓 Free"
            total_searches = u["searches"]

            ans = (
                f"🔍 <b>USER REPORT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>ID:</b> <code>{target_id}</code>\n"
                f"👤 <b>Username:</b> {uname}\n"
                f"💳 <b>Credits:</b> {credits}\n"
                f"🏷️ <b>Status:</b> {status}\n"
                f"📈 <b>Total API Calls:</b> {total_searches}\n\n"
            )
            bot.send_message(message.chat.id, ans, parse_mode="HTML")
        else:
            bot.send_message(message.chat.id, "❌ User ID not found in database.")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid User ID.")

# ==========================================
# BOT RUNNER
# ==========================================
if __name__ == "__main__":
    print("Bot is running...")
    bot.infinity_polling()