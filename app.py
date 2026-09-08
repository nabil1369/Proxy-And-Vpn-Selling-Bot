import os
import sqlite3
import secrets
import asyncio
import threading
from decimal import Decimal, InvalidOperation
from functools import wraps
from datetime import datetime, timezone

from flask import Flask, request, redirect, url_for, session, render_template_string, flash
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)

# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-password")
ADMIN_PANEL_USERNAME = os.getenv("ADMIN_PANEL_USERNAME", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Payment settings (editable from Admin Panel after login)
DEFAULT_SETTINGS = {
    "bkash_number": "01609345459",
    "nagad_number": "01609345459",
    "binance_pay_id": "1016685666",
    "usd_rate": "120",
    "support_username": "@Gamer13683",
    "bot_username": "",
}

if not BOT_TOKEN:
    print("WARNING: BOT_TOKEN is not set. Set it in Render Environment Variables.")
if not ADMIN_ID:
    print("WARNING: ADMIN_ID is not set. Set it to your Telegram numeric user ID.")

# ============================================================
# DATABASE
# ============================================================
db_lock = threading.RLock()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            first_name TEXT NOT NULL DEFAULT '',
            username TEXT DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            total_orders INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL CHECK(category IN ('proxy','vpn')),
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            credential TEXT NOT NULL,
            sold_to INTEGER DEFAULT NULL,
            sold_at TEXT DEFAULT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            delivered TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            method TEXT NOT NULL,
            amount REAL NOT NULL,
            trxid TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT DEFAULT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

        # Seed the four requested proxy products only if there are no products yet.
        count = c.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
        if count == 0:
            now = utcnow()
            seed = [
                ("proxy", "Owl Proxy", "200 MB", 10),
                ("proxy", "H143 Lite", "200 MB", 10),
                ("proxy", "9 Proxy", "200 MB", 25),
                ("proxy", "Node Maven", "120 MB", 50),
                ("vpn", "NordVPN", "VPN account", 0),
                ("vpn", "ProtonVPN", "VPN account", 0),
            ]
            c.executemany(
                "INSERT INTO products(category,name,description,price,created_at) VALUES(?,?,?,?,?)",
                [(a,b,c,d,now) for a,b,c,d in seed]
            )
        c.commit()

def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def get_setting(key):
    with db_lock, db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

def set_setting(key, value):
    with db_lock, db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))
        c.commit()

def ensure_user(tg_user, referred_by=None):
    uid = tg_user.id
    with db_lock, db() as c:
        row = c.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            c.execute("UPDATE users SET first_name=?, username=? WHERE id=?",
                      (tg_user.first_name or "", tg_user.username or "", uid))
        else:
            c.execute(
                "INSERT INTO users(id,first_name,username,balance,total_orders,referred_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (uid, tg_user.first_name or "", tg_user.username or "", 0, 0, referred_by, utcnow())
            )
        c.commit()

def money(v):
    return f"{float(v):.2f}"

def usd_from_bdt(bdt):
    try:
        rate = float(get_setting("usd_rate") or 120)
        return float(bdt) / rate if rate else 0
    except Exception:
        return 0

# ============================================================
# TELEGRAM BOT UI
# ============================================================
MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🛒 Buy Products", callback_data="menu_buy")],
    [InlineKeyboardButton("👤 My Profile", callback_data="menu_profile")],
    [InlineKeyboardButton("💰 Deposit Money", callback_data="menu_deposit")],
    [InlineKeyboardButton("📞 Support", callback_data="menu_support")],
])

def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_main")]])

def products_keyboard(category):
    with db_lock, db() as c:
        rows = c.execute(
            "SELECT * FROM products WHERE category=? AND active=1 ORDER BY id", (category,)
        ).fetchall()
    buttons = []
    for p in rows:
        price = money(p["price"])
        desc = f" | {p['description']}" if p["description"] else ""
        buttons.append([InlineKeyboardButton(
            f"{p['name']}{desc} | {price}৳",
            callback_data=f"product:{p['id']}"
        )])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu_buy")])
    return InlineKeyboardMarkup(buttons)

async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except Exception:
        try:
            await query.message.reply_text(text=text, reply_markup=reply_markup)
        except Exception:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref = None
    if context.args:
        try:
            candidate = int(context.args[0])
            if candidate != update.effective_user.id:
                ref = candidate
        except ValueError:
            pass
    ensure_user(update.effective_user, ref)
    await update.message.reply_text(
        "👋 Welcome to Premium Store!\n\nChoose an option below:",
        reply_markup=MAIN_KB
    )

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    ensure_user(q.from_user)

    if q.data == "menu_main":
        await safe_edit(q, "🏠 Main Menu\n\nChoose an option:", MAIN_KB)

    elif q.data == "menu_buy":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Proxy", callback_data="cat:proxy")],
            [InlineKeyboardButton("🔐 VPN", callback_data="cat:vpn")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_main")],
        ])
        await safe_edit(q, "🛒 Buy Products\n\nSelect a category:", kb)

    elif q.data.startswith("cat:"):
        cat = q.data.split(":", 1)[1]
        title = "🌐 Proxy Products" if cat == "proxy" else "🔐 VPN Products"
        await safe_edit(q, title, products_keyboard(cat))

    elif q.data == "menu_profile":
        with db_lock, db() as c:
            u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        username = f"@{u['username']}" if u["username"] else "Not set"
        bot_username = get_setting("bot_username")
        if not bot_username and context.bot.username:
            bot_username = context.bot.username
        ref_link = f"https://t.me/{bot_username}?start={uid}" if bot_username else f"start={uid}"
        text = (
            "👤 My Profile\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"✨ Name: {u['first_name'] or 'User'}\n"
            f"⚙️ User ID: {u['id']}\n"
            f"🔗 Username: {username}\n"
            f"💰 Balance: {money(u['balance'])} BDT ({usd_from_bdt(u['balance']):.2f} USD)\n"
            f"📦 Total Orders: {u['total_orders']}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📊 Referral Link:\n{ref_link}"
        )
        await safe_edit(q, text, back_button())

    elif q.data == "menu_deposit":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 BKASH", callback_data="deposit:bKash")],
            [InlineKeyboardButton("💳 NAGAD", callback_data="deposit:Nagad")],
            [InlineKeyboardButton("💰 BINANCE", callback_data="deposit:Binance")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_main")],
        ])
        await safe_edit(q, "💰 Deposit Money\n\nSelect payment method:", kb)

    elif q.data == "menu_support":
        support = get_setting("support_username")
        await safe_edit(q, f"📞 Support:\n\nContact: {support}", back_button())

    elif q.data.startswith("deposit:"):
        method = q.data.split(":", 1)[1]
        context.user_data["deposit_method"] = method
        context.user_data["awaiting_deposit_amount"] = True
        await safe_edit(
            q,
            f"💳 {method}\n\nকত টাকা ডিপোজিট করবেন লিখুন:\n\nExample: 20",
            back_button()
        )

    elif q.data == "payment_done":
        context.user_data["awaiting_trxid"] = True
        await safe_edit(q, "⚙️ Transaction ID (TrxID) দিন:\n\nExample: DI576SYTU5", back_button())

    elif q.data.startswith("product:"):
        try:
            pid = int(q.data.split(":", 1)[1])
        except ValueError:
            return
        await purchase_product(q, context, pid)

async def purchase_product(q, context, pid):
    uid = q.from_user.id
    with db_lock, db() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            await safe_edit(q, "❌ Product is no longer available.", back_button())
            return

        stock = c.execute(
            "SELECT * FROM stock WHERE product_id=? AND sold_to IS NULL ORDER BY id LIMIT 1", (pid,)
        ).fetchone()

        if not stock:
            await safe_edit(q, "❌ Out of stock.\nPlease contact support.", back_button())
            return

        user = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not user or float(user["balance"]) < float(p["price"]):
            await safe_edit(
                q,
                f"❌ Insufficient balance.\n\nPrice: {money(p['price'])} BDT\n"
                f"Your balance: {money(user['balance'] if user else 0)} BDT",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Deposit Money", callback_data="menu_deposit")],
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_buy")]
                ])
            )
            return

        # Atomic transaction: deduct balance and mark one stock item sold.
        new_balance = float(user["balance"]) - float(p["price"])
        c.execute("UPDATE users SET balance=?, total_orders=total_orders+1 WHERE id=?",
                  (new_balance, uid))
        c.execute("UPDATE stock SET sold_to=?, sold_at=? WHERE id=? AND sold_to IS NULL",
                  (uid, utcnow(), stock["id"]))
        if c.execute("SELECT changes()").fetchone()[0] != 1:
            c.rollback()
            await safe_edit(q, "❌ Stock was just purchased by another user. Please try again.", back_button())
            return
        c.execute(
            "INSERT INTO orders(user_id,product_id,amount,delivered,created_at) VALUES(?,?,?,?,?)",
            (uid, pid, float(p["price"]), stock["credential"], utcnow())
        )
        c.commit()

    await safe_edit(
        q,
        "✅ Purchase Successful!\n\n"
        f"📦 Product: {p['name']}\n"
        f"💰 Paid: {money(p['price'])} BDT\n"
        f"💳 New Balance: {money(new_balance)} BDT\n\n"
        f"🔐 Your Product:\n{stock['credential']}",
        back_button()
    )

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    text = (update.message.text or "").strip()

    if context.user_data.get("awaiting_deposit_amount"):
        method = context.user_data.get("deposit_method")
        try:
            amount = float(Decimal(text))
            if amount <= 0 or amount > 1000000:
                raise ValueError
        except (InvalidOperation, ValueError):
            await update.message.reply_text("❌ Invalid amount. Please send a valid number, e.g. 20.")
            return

        context.user_data["deposit_amount"] = amount
        context.user_data["awaiting_deposit_amount"] = False

        if method == "bKash":
            destination = get_setting("bkash_number")
            label = "এই নাম্বারে টাকা পাঠান"
        elif method == "Nagad":
            destination = get_setting("nagad_number")
            label = "এই নাম্বারে টাকা পাঠান"
        else:
            destination = get_setting("binance_pay_id")
            label = "এই Pay ID-তে ডলার পাঠান"

        if method == "Binance":
            amount_text = f"{amount:.2f} USD ({amount * float(get_setting('usd_rate') or 120):.2f} BDT)"
        else:
            amount_text = f"{amount:.2f} BDT ({usd_from_bdt(amount):.2f} USD)"

        await update.message.reply_text(
            "💳 Deposit Request\n"
            f"Method: {method}\n"
            f"Amount: {amount_text}\n"
            f"{label}: {destination}\n\n"
            "পেমেন্ট সম্পন্ন হলে নিচের বাটনে ক্লিক করুন:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Payment Done", callback_data="payment_done")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_deposit")]
            ])
        )
        return

    if context.user_data.get("awaiting_trxid"):
        trxid = text[:100]
        method = context.user_data.get("deposit_method")
        amount = context.user_data.get("deposit_amount")
        context.user_data["awaiting_trxid"] = False

        if not method or not amount:
            await update.message.reply_text("❌ Deposit session expired. Please start again.", reply_markup=MAIN_KB)
            return

        with db_lock, db() as c:
            cur = c.execute(
                "INSERT INTO deposits(user_id,method,amount,trxid,status,created_at) VALUES(?,?,?,?,?,?)",
                (uid, method, float(amount), trxid, "pending", utcnow())
            )
            deposit_id = cur.lastrowid
            c.commit()

        await update.message.reply_text(
            f"✅ Deposit request submitted!\n\n"
            f"Method: {method}\nAmount: {amount}\nTrxID: {trxid}\n\n"
            "Admin will review your request.",
            reply_markup=MAIN_KB
        )

        if ADMIN_ID:
            if method == "Binance":
                bdt = float(amount) * float(get_setting("usd_rate") or 120)
                amount_line = f"{amount:.2f} USD ({bdt:.2f} BDT)"
            else:
                amount_line = f"{amount:.2f} BDT"
            admin_text = (
                "🔔 NEW DEPOSIT REQUEST\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Deposit ID: {deposit_id}\n"
                f"👤 User ID: {uid}\n"
                f"👤 Name: {update.effective_user.first_name or ''}\n"
                f"🔗 Username: @{update.effective_user.username if update.effective_user.username else 'none'}\n"
                f"💳 Method: {method}\n"
                f"💰 Amount: {amount_line}\n"
                f"⚙️ TrxID: {trxid}\n"
                "━━━━━━━━━━━━━━━━━━"
            )
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    admin_text,
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Approve", callback_data=f"adminapprove:{deposit_id}"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"adminreject:{deposit_id}")
                        ]
                    ])
                )
            except Exception as e:
                print("Could not notify admin:", e)
        return

    await update.message.reply_text("Please use the menu below:", reply_markup=MAIN_KB)

async def admin_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        await q.answer("Unauthorized", show_alert=True)
        return

    action, sid = q.data.split(":", 1)
    did = int(sid)

    with db_lock, db() as c:
        d = c.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if not d:
            await q.edit_message_text("❌ Deposit not found.")
            return
        if d["status"] != "pending":
            await q.edit_message_text(f"ℹ️ Already processed: {d['status']}")
            return

        if action == "adminapprove":
            if d["method"] == "Binance":
                bdt = float(d["amount"]) * float(get_setting("usd_rate") or 120)
            else:
                bdt = float(d["amount"])
            c.execute("UPDATE users SET balance=balance+? WHERE id=?", (bdt, d["user_id"]))
            c.execute("UPDATE deposits SET status='approved',reviewed_at=? WHERE id=?", (utcnow(), did))
            result = f"Approved. +{bdt:.2f} BDT"
        else:
            c.execute("UPDATE deposits SET status='rejected',reviewed_at=? WHERE id=?", (utcnow(), did))
            result = "Rejected. No balance added."
        c.commit()

    await q.edit_message_text(f"Deposit #{did}: {result}")
    try:
        if action == "adminapprove":
            await context.bot.send_message(
                d["user_id"],
                f"✅ Deposit Approved!\n\nYour balance has been credited.\nAmount added: {bdt:.2f} BDT",
                reply_markup=MAIN_KB
            )
        else:
            await context.bot.send_message(
                d["user_id"],
                "❌ Admin rejected your deposit request.\nNo balance was added.",
                reply_markup=MAIN_KB
            )
    except Exception as e:
        print("Could not notify user:", e)

# ============================================================
# FLASK ADMIN PANEL
# ============================================================
app = Flask(__name__)
app.secret_key = SECRET_KEY

BASE_CSS = """
<style>
body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;color:#17202a}
nav{background:#111827;padding:14px 20px;color:white;display:flex;gap:14px;flex-wrap:wrap}
nav a{color:white;text-decoration:none}
.container{max-width:1200px;margin:24px auto;padding:0 14px}
.card{background:white;padding:18px;border-radius:12px;margin-bottom:18px;box-shadow:0 2px 10px #0001}
table{width:100%;border-collapse:collapse;background:white}
th,td{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
input,select,textarea{padding:9px;border:1px solid #ccd3da;border-radius:7px;width:100%;box-sizing:border-box}
button,.btn{background:#111827;color:white;border:0;border-radius:7px;padding:9px 13px;text-decoration:none;cursor:pointer}
.danger{background:#b91c1c}.success{background:#047857}.warn{background:#b45309}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.stat{font-size:25px;font-weight:bold}.small{color:#667085;font-size:13px}
.alert{padding:10px;background:#ecfdf3;border-radius:8px;margin-bottom:12px}
.badge{padding:4px 8px;border-radius:12px;background:#eee}
form.inline{display:inline}
</style>
"""

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

LOGIN_HTML = BASE_CSS + """
<div class="container" style="max-width:420px;margin-top:80px">
<div class="card">
<h2>👑 Premium Bot Admin</h2>
<form method="post">
<p>Username</p><input name="username" required>
<p>Password</p><input type="password" name="password" required>
<br><br><button>Login</button>
</form></div></div>
"""

PANEL_HTML = BASE_CSS + """
<nav>
<a href="{{url_for('dashboard')}}">📊 Dashboard</a>
<a href="{{url_for('products')}}">📦 Products</a>
<a href="{{url_for('stock')}}">📥 Stock</a>
<a href="{{url_for('deposits')}}">💳 Deposits</a>
<a href="{{url_for('users')}}">👥 Users</a>
<a href="{{url_for('settings_page')}}">⚙️ Settings</a>
<a href="{{url_for('logout')}}">Logout</a>
</nav>
<div class="container">
{% with messages=get_flashed_messages() %}
{% for m in messages %}<div class="alert">{{m}}</div>{% endfor %}
{% endwith %}
{{body|safe}}
</div>
"""

@app.route("/admin/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if request.form.get("username") == ADMIN_PANEL_USERNAME and request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("dashboard"))
        flash("Invalid login.")
    return render_template_string(LOGIN_HTML)

@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/health")
def health():
    return "OK", 200

@app.route("/admin")
@admin_required
def dashboard():
    with db_lock, db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
        pending = c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"]
        revenue = c.execute("SELECT COALESCE(SUM(amount),0) n FROM orders").fetchone()["n"]
        stock = c.execute("SELECT COUNT(*) n FROM stock WHERE sold_to IS NULL").fetchone()["n"]
    body = f"""
    <h1>📊 Dashboard</h1>
    <div class="grid">
      <div class="card"><div class="small">Users</div><div class="stat">{users}</div></div>
      <div class="card"><div class="small">Orders</div><div class="stat">{orders}</div></div>
      <div class="card"><div class="small">Pending Deposits</div><div class="stat">{pending}</div></div>
      <div class="card"><div class="small">Revenue (BDT)</div><div class="stat">{revenue:.2f}</div></div>
      <div class="card"><div class="small">Available Stock</div><div class="stat">{stock}</div></div>
    </div>
    <div class="card">
      <h3>Quick Actions</h3>
      <a class="btn" href="{url_for('products')}">Manage Products</a>
      <a class="btn" href="{url_for('stock')}">Add Stock</a>
      <a class="btn" href="{url_for('deposits')}">Review Deposits</a>
    </div>
    """
    return render_template_string(PANEL_HTML, body=body)

@app.route("/admin/products", methods=["GET","POST"])
@admin_required
def products():
    if request.method == "POST":
        category = request.form["category"]
        name = request.form["name"].strip()
        desc = request.form.get("description","").strip()
        price = float(request.form["price"])
        with db_lock, db() as c:
            c.execute("INSERT INTO products(category,name,description,price,active,created_at) VALUES(?,?,?,?,1,?)",
                      (category,name,desc,price,utcnow()))
            c.commit()
        flash("Product added.")
        return redirect(url_for("products"))

    with db_lock, db() as c:
        rows = c.execute("""
        SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id AND s.sold_to IS NULL) available
        FROM products p ORDER BY p.category,p.id
        """).fetchall()

    rows_html = "".join(
        f"""<tr>
        <td>{p['id']}</td><td>{p['category']}</td><td>{p['name']}</td><td>{p['description']}</td>
        <td>{p['price']:.2f}</td><td>{p['available']}</td>
        <td>{'ON' if p['active'] else 'OFF'}</td>
        <td>
          <form class="inline" method="post" action="{url_for('product_toggle',pid=p['id'])}">
          <button>{'Disable' if p['active'] else 'Enable'}</button></form>
          <a class="btn warn" href="{url_for('product_edit',pid=p['id'])}">Edit</a>
          <form class="inline" method="post" action="{url_for('product_delete',pid=p['id'])}" onsubmit="return confirm('Delete product?')">
          <button class="danger">Delete</button></form>
        </td></tr>""" for p in rows)

    body = f"""
    <h1>📦 Products</h1>
    <div class="card"><h3>Add Product</h3>
    <form method="post">
      <div class="grid">
      <div><label>Category</label><select name="category"><option value="proxy">Proxy</option><option value="vpn">VPN</option></select></div>
      <div><label>Name</label><input name="name" required></div>
      <div><label>Description / package</label><input name="description" placeholder="200 MB"></div>
      <div><label>Price (BDT)</label><input type="number" step="0.01" name="price" required></div>
      </div><br><button class="success">Add Product</button>
    </form></div>
    <div class="card"><table><tr><th>ID</th><th>Cat</th><th>Name</th><th>Description</th><th>Price</th><th>Stock</th><th>Status</th><th>Actions</th></tr>
    {rows_html}</table></div>
    """
    return render_template_string(PANEL_HTML, body=body)

@app.route("/admin/products/<int:pid>/edit", methods=["GET","POST"])
@admin_required
def product_edit(pid):
    with db_lock, db() as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        return "Not found",404
    if request.method == "POST":
        with db_lock, db() as c:
            c.execute("UPDATE products SET category=?,name=?,description=?,price=? WHERE id=?",
                      (request.form["category"],request.form["name"].strip(),request.form.get("description","").strip(),
                       float(request.form["price"]),pid))
            c.commit()
        flash("Product updated.")
        return redirect(url_for("products"))
    body = f"""
    <h1>✏️ Edit Product #{pid}</h1><div class="card"><form method="post">
    <label>Category</label><select name="category">
      <option value="proxy" {'selected' if p['category']=='proxy' else ''}>Proxy</option>
      <option value="vpn" {'selected' if p['category']=='vpn' else ''}>VPN</option>
    </select><br><br>
    <label>Name</label><input name="name" value="{p['name']}" required><br><br>
    <label>Description</label><input name="description" value="{p['description']}"><br><br>
    <label>Price (BDT)</label><input type="number" step="0.01" name="price" value="{p['price']}" required><br><br>
    <button class="success">Save</button> <a class="btn" href="{url_for('products')}">Cancel</a>
    </form></div>
    """
    return render_template_string(PANEL_HTML, body=body)

@app.route("/admin/products/<int:pid>/toggle", methods=["POST"])
@admin_required
def product_toggle(pid):
    with db_lock, db() as c:
        c.execute("UPDATE products SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (pid,))
        c.commit()
    return redirect(url_for("products"))

@app.route("/admin/products/<int:pid>/delete", methods=["POST"])
@admin_required
def product_delete(pid):
    with db_lock, db() as c:
        c.execute("DELETE FROM products WHERE id=?", (pid,))
        c.commit()
    flash("Product deleted.")
    return redirect(url_for("products"))

@app.route("/admin/stock", methods=["GET","POST"])
@admin_required
def stock():
    if request.method == "POST":
        pid = int(request.form["product_id"])
        credentials = request.form["credentials"].splitlines()
        credentials = [x.strip() for x in credentials if x.strip()]
        with db_lock, db() as c:
            for cred in credentials:
                c.execute("INSERT INTO stock(product_id,credential) VALUES(?,?)",(pid,cred))
            c.commit()
        flash(f"{len(credentials)} stock item(s) added.")
        return redirect(url_for("stock"))

    with db_lock, db() as c:
        ps = c.execute("SELECT * FROM products ORDER BY category,id").fetchall()
        rows = c.execute("""
        SELECT s.*,p.name,p.category,u.username
        FROM stock s JOIN products p ON p.id=s.product_id
        LEFT JOIN users u ON u.id=s.sold_to
        ORDER BY s.id DESC LIMIT 300
        """).fetchall()
    opts = "".join(f"<option value='{p['id']}'>{p['category']} — {p['name']}</option>" for p in ps)
    trs = "".join(
        f"<tr><td>{s['id']}</td><td>{s['category']} — {s['name']}</td><td><pre>{s['credential']}</pre></td>"
        f"<td>{'Available' if not s['sold_to'] else 'Sold to '+str(s['sold_to'])}</td><td>{s['sold_at'] or ''}</td></tr>"
        for s in rows)
    body = f"""
    <h1>📥 Stock Management</h1>
    <div class="card"><h3>Add Stock</h3>
    <p class="small">Put one proxy/account per line. For VPN you can use: username | password</p>
    <form method="post"><label>Product</label><select name="product_id">{opts}</select><br><br>
    <label>Credentials</label><textarea name="credentials" rows="10" required></textarea><br><br>
    <button class="success">Add Stock</button></form></div>
    <div class="card"><table><tr><th>ID</th><th>Product</th><th>Credential</th><th>Status</th><th>Sold At</th></tr>{trs}</table></div>
    """
    return render_template_string(PANEL_HTML, body=body)

@app.route("/admin/deposits")
@admin_required
def deposits():
    with db_lock, db() as c:
        rows = c.execute("""
        SELECT d.*,u.first_name,u.username,u.id uid
        FROM deposits d JOIN users u ON u.id=d.user_id ORDER BY d.id DESC LIMIT 300
        """).fetchall()
    trs = ""
    for d in rows:
        actions = ""
        if d["status"] == "pending":
            actions = f"""
            <form class="inline" method="post" action="{url_for('deposit_action',did=d['id'])}">
            <input type="hidden" name="action" value="approve"><button class="success">Approve</button></form>
            <form class="inline" method="post" action="{url_for('deposit_action',did=d['id'])}">
            <input type="hidden" name="action" value="reject"><button class="danger">Reject</button></form>
            """
        trs += f"<tr><td>{d['id']}</td><td>{d['uid']}<br>{d['first_name']} @{d['username'] or ''}</td>" \
               f"<td>{d['method']}</td><td>{d['amount']}</td><td>{d['trxid']}</td><td>{d['status']}</td><td>{actions}</td></tr>"
    body = f"""
    <h1>💳 Deposits</h1>
    <div class="card"><table><tr><th>ID</th><th>User</th><th>Method</th><th>Amount</th><th>TrxID</th><th>Status</th><th>Action</th></tr>{trs}</table></div>
    """
    return render_template_string(PANEL_HTML, body=body)

@app.route("/admin/deposits/<int:did>/action", methods=["POST"])
@admin_required
def deposit_action(did):
    action = request.form.get("action")
    with db_lock, db() as c:
        d = c.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if not d or d["status"] != "pending":
            flash("Deposit not found or already processed.")
            return redirect(url_for("deposits"))
        if action == "approve":
            bdt = float(d["amount"]) * float(get_setting("usd_rate") or 120) if d["method"] == "Binance" else float(d["amount"])
            c.execute("UPDATE users SET balance=balance+? WHERE id=?", (bdt,d["user_id"]))
            c.execute("UPDATE deposits SET status='approved',reviewed_at=? WHERE id=?", (utcnow(),did))
            msg = f"Approved. Added {bdt:.2f} BDT."
        else:
            c.execute("UPDATE deposits SET status='rejected',reviewed_at=? WHERE id=?", (utcnow(),did))
            msg = "Rejected."
        c.commit()
    flash(msg)
    return redirect(url_for("deposits"))

@app.route("/admin/users")
@admin_required
def users():
    with db_lock, db() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id DESC LIMIT 500").fetchall()
    trs = "".join(
        f"""<tr><td>{u['id']}</td><td>{u['first_name']}<br>@{u['username'] or ''}</td>
        <td>{u['balance']:.2f}</td><td>{u['total_orders']}</td><td>{u['created_at']}</td>
        <td><a class="btn" href="{url_for('user_edit',uid=u['id'])}">Balance</a></td></tr>""" for u in rows)
    body = f"""<h1>👥 Users</h1><div class="card"><table>
    <tr><th>ID</th><th>User</th><th>Balance</th><th>Orders</th><th>Created</th><th>Action</th></tr>{trs}</table></div>"""
    return render_template_string(PANEL_HTML, body=body)

@app.route("/admin/users/<int:uid>/edit", methods=["GET","POST"])
@admin_required
def user_edit(uid):
    with db_lock, db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u: return "Not found",404
    if request.method == "POST":
        balance = float(request.form["balance"])
        with db_lock, db() as c:
            c.execute("UPDATE users SET balance=? WHERE id=?", (balance,uid))
            c.commit()
        flash("Balance updated.")
        return redirect(url_for("users"))
    body = f"""<h1>👤 User #{uid}</h1><div class="card"><form method="post">
    <p>Name: {u['first_name']} | @{u['username'] or ''}</p>
    <label>Balance (BDT)</label><input type="number" step="0.01" name="balance" value="{u['balance']}" required><br><br>
    <button class="success">Save Balance</button> <a class="btn" href="{url_for('users')}">Back</a>
    </form></div>"""
    return render_template_string(PANEL_HTML, body=body)

@app.route("/admin/settings", methods=["GET","POST"])
@admin_required
def settings_page():
    if request.method == "POST":
        for key in DEFAULT_SETTINGS:
            if key in request.form:
                set_setting(key, request.form[key].strip())
        flash("Settings saved.")
    vals = {k:get_setting(k) for k in DEFAULT_SETTINGS}
    body = f"""
    <h1>⚙️ Settings</h1><div class="card"><form method="post">
    <label>BKash Number</label><input name="bkash_number" value="{vals['bkash_number']}"><br><br>
    <label>Nagad Number</label><input name="nagad_number" value="{vals['nagad_number']}"><br><br>
    <label>Binance Pay ID</label><input name="binance_pay_id" value="{vals['binance_pay_id']}"><br><br>
    <label>USD Rate (1 USD = BDT)</label><input type="number" step="0.01" name="usd_rate" value="{vals['usd_rate']}"><br><br>
    <label>Support Username</label><input name="support_username" value="{vals['support_username']}"><br><br>
    <label>Bot Username (without @)</label><input name="bot_username" value="{vals['bot_username']}"><br><br>
    <button class="success">Save Settings</button>
    </form></div>
    <div class="card"><b>Important:</b> Keep BOT_TOKEN and ADMIN_ID in Render Environment Variables, not inside the source code.</div>
    """
    return render_template_string(PANEL_HTML, body=body)

# ============================================================
# STARTUP
# ============================================================
def run_bot():
    async def runner():
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(admin_deposit_callback, pattern=r"^admin(approve|reject):"))
        application.add_handler(CallbackQueryHandler(menu_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        print("Telegram bot started.")
        try:
            await asyncio.Event().wait()
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()

    asyncio.run(runner())

init_db()

if __name__ == "__main__":
    # Render expects an HTTP server. Bot runs in a background thread.
    if BOT_TOKEN:
        t = threading.Thread(target=run_bot, daemon=True)
        t.start()
    app.run(host="0.0.0.0", port=PORT)
