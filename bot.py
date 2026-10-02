from datetime import date, datetime, timedelta
import json
import logging
import os
import shutil
import sqlite3
from zoneinfo import ZoneInfo

import jdatetime
import openpyxl
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# تنظیمات لاگ‌برداری پیشرفته
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_activity.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================
# تنظیمات
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "bashmaq_reports.db")
TOKEN_FILE = os.path.join(BASE_DIR, "token.txt")
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TOKEN and os.path.exists(TOKEN_FILE):
  with open(TOKEN_FILE, "r", encoding="utf-8") as f:
    TOKEN = f.read().strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "1439302328"))
FIXED_FOOTER = "آکو کهنه پوشی نماینده شرکت ایران مقصد (مرزباشماق)"
try:
  TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:
  from datetime import timezone

  TEHRAN = timezone(timedelta(hours=3, minutes=30))

# مراحل ثبت/ویرایش گزارش
(
    DATE,
    COUNT,
    NAME,
    ISSUED,
    START,
    END,
    CANCELLED,
    CANCELLED_SERIAL,
    TRANSIT,
    EXPORT,
) = range(10)
SEARCH_DATE = 10
RANGE_START, RANGE_END = 11, 12
MONTH = 13
EDIT_DATE = 14
DELETE_DATE = 15
SEND_DATE, SEND_USER = 16, 17


# ============================================================
# دیتابیس
# ============================================================
def db():
  return sqlite3.connect(DB_FILE)


def init_db():
  conn = db()
  conn.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        allowed INTEGER DEFAULT 1,
        is_admin INTEGER DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT ''
    )""")
  conn.execute("""CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    )""")

  user_columns = {
      row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
  }
  if "is_admin" not in user_columns:
    conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
  if "allowed" not in user_columns:
    conn.execute("ALTER TABLE users ADD COLUMN allowed INTEGER DEFAULT 1")
  if "username" not in user_columns:
    conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
  if "first_name" not in user_columns:
    conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
  if "created_at" not in user_columns:
    conn.execute(
        "ALTER TABLE users ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
    )

  now_text = datetime.now(TEHRAN).isoformat(timespec="seconds")
  conn.execute(
      "UPDATE users SET created_at=? WHERE created_at IS NULL OR created_at=''",
      (now_text,),
  )

  report_table_exists = conn.execute(
      "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reports'"
  ).fetchone()

  if not report_table_exists:
    conn.execute("""CREATE TABLE reports (
            date TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
  else:
    report_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(reports)").fetchall()
    }
    if "data" not in report_columns:
      if "date_g" in report_columns and "branch_data" in report_columns:
        legacy_name = "reports_legacy_v1"
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (legacy_name,),
        ).fetchone():
          legacy_name = (
              "reports_legacy_v1_" + datetime.now().strftime("%Y%m%d%H%M%S")
          )

        conn.execute(f'ALTER TABLE reports RENAME TO "{legacy_name}"')
        conn.execute("""CREATE TABLE reports (
                    date TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")

        legacy_rows = conn.execute(
            f"""SELECT date_g, date_s, branch_data,
                       transit, export, created_at
                FROM "{legacy_name}"
                ORDER BY id"""
        ).fetchall()

        for row in legacy_rows:
          date_g, date_s, branch_data, transit, export, created_at = row
          try:
            branches = json.loads(branch_data or "[]")
            if not isinstance(branches, list):
              branches = []
          except Exception:
            branches = []

          normalized_branches = []
          for b in branches:
            if not isinstance(b, dict):
              continue
            normalized_branches.append({
                "name": b.get("name", "بدون نام"),
                "issued": int(b.get("issued", 0) or 0),
                "start": str(b.get("start", b.get("start_serial", "0"))),
                "end": str(b.get("end", b.get("end_serial", "0"))),
                "cancelled": int(b.get("cancelled", 0) or 0),
                "cancelled_serial": str(
                    b.get("cancelled_serial", "0") or "0"
                ),
            })

          data = {
              "date": date_g,
              "shamsi_date": date_s,
              "branches": normalized_branches,
              "transit": int(transit or 0),
              "export": int(export or 0),
          }

          conn.execute(
              """INSERT OR REPLACE INTO reports(date, data, created_at)
                   VALUES (?, ?, ?)""",
              (
                  date_g,
                  json.dumps(data, ensure_ascii=False),
                  created_at or now_text,
              ),
          )

  conn.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (ADMIN_ID,))
  conn.execute(
      "UPDATE users SET is_admin=1, allowed=1 WHERE user_id=?", (ADMIN_ID,)
  )

  conn.commit()
  conn.close()
  logger.info("Database initialized successfully.")


def save_user(user):
  if not user:
    return
  conn = db()
  conn.execute(
      """INSERT INTO users(user_id, username, first_name, allowed, is_admin, created_at)
         VALUES (?, ?, ?, 1, ?, ?)
         ON CONFLICT(user_id) DO UPDATE SET
           username=excluded.username,
           first_name=excluded.first_name""",
      (
          user.id,
          user.username or "",
          user.first_name or "",
          1 if is_admin_id(user.id) else 0,
          datetime.now(TEHRAN).isoformat(timespec="seconds"),
      ),
  )
  conn.commit()
  conn.close()


def is_admin_id(uid):
  conn = db()
  row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
  conn.close()
  return bool(row) or uid == ADMIN_ID


def is_allowed(uid):
  if is_admin_id(uid):
    return True
  conn = db()
  row = conn.execute(
      "SELECT allowed FROM users WHERE user_id=?", (uid,)
  ).fetchone()
  conn.close()
  return True if row is None else bool(row[0])


def save_report(data):
  conn = db()
  conn.execute(
      "INSERT OR REPLACE INTO reports(date, data, created_at) VALUES (?, ?, ?)",
      (
          data["date"],
          json.dumps(data, ensure_ascii=False),
          datetime.now().isoformat(timespec="seconds"),
      ),
  )
  conn.commit()
  conn.close()


def load_report(date_text):
  conn = db()
  row = conn.execute(
      "SELECT data FROM reports WHERE date=?", (date_text,)
  ).fetchone()
  conn.close()
  return json.loads(row[0]) if row else None


def delete_report(date_text):
  conn = db()
  cur = conn.execute("DELETE FROM reports WHERE date=?", (date_text,))
  conn.commit()
  conn.close()
  return cur.rowcount > 0


def list_dates():
  conn = db()
  rows = conn.execute("SELECT date FROM reports ORDER BY date DESC").fetchall()
  conn.close()
  return [r[0] for r in rows]


def all_reports():
  conn = db()
  rows = conn.execute("SELECT data FROM reports ORDER BY date").fetchall()
  conn.close()
  return [json.loads(r[0]) for r in rows]


def add_admin(uid):
  conn = db()
  conn.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (uid,))
  conn.execute(
      "INSERT OR IGNORE INTO users(user_id, allowed, is_admin) VALUES (?,1,1)",
      (uid,),
  )
  conn.execute(
      "UPDATE users SET is_admin=1, allowed=1 WHERE user_id=?", (uid,)
  )
  conn.commit()
  conn.close()


def remove_admin(uid):
  if uid == ADMIN_ID:
    return False
  conn = db()
  cur = conn.execute("DELETE FROM admins WHERE user_id=?", (uid,))
  conn.execute("UPDATE users SET is_admin=0 WHERE user_id=?", (uid,))
  conn.commit()
  conn.close()
  return cur.rowcount > 0


def set_allowed(uid, allowed):
  conn = db()
  conn.execute(
      "INSERT OR IGNORE INTO users(user_id, allowed) VALUES (?,?)",
      (uid, 1 if allowed else 0),
  )
  conn.execute(
      "UPDATE users SET allowed=? WHERE user_id=?", (1 if allowed else 0, uid)
  )
  conn.commit()
  conn.close()


def users_rows():
  conn = db()
  rows = conn.execute(
      "SELECT user_id, username, first_name, allowed, is_admin FROM users"
      " ORDER BY user_id"
  ).fetchall()
  conn.close()
  return rows


# ============================================================
# تاریخ
# ============================================================
def parse_gregorian(s):
  return datetime.strptime(s.strip(), "%d/%m/%Y").date()


def norm_date(s):
  return parse_gregorian(s).strftime("%d/%m/%Y")


def shamsi(d):
  return jdatetime.date.fromgregorian(date=d).strftime("%d/%m/%Y")


def iran_today():
  return datetime.now(TEHRAN).date()


def is_admin(update):
  return bool(
      update.effective_user and is_admin_id(update.effective_user.id)
  )


def can_use(update):
  return bool(update.effective_user and is_allowed(update.effective_user.id))


# ============================================================
# منوها
# ============================================================
def admin_menu():
  return ReplyKeyboardMarkup(
      [
          ["📝 ثبت گزارش جدید", "🔎 جستجوی گزارش"],
          ["📊 گزارش بازه‌ای", "📅 گزارش امروز"],
          ["📈 آمار ماهانه", "⚙️ مدیریت"],
          ["📤 ارسال گزارش", "❌ لغو"],
      ],
      resize_keyboard=True,
  )


def user_menu():
  return ReplyKeyboardMarkup(
      [
          ["🔎 جستجوی گزارش", "📊 گزارش بازه‌ای"],
          ["📅 گزارش امروز", "📈 آمار ماهانه"],
          ["❌ لغو"],
      ],
      resize_keyboard=True,
  )


def management_menu():
  return ReplyKeyboardMarkup(
      [
          ["✏️ ویرایش گزارش", "🗑 حذف گزارش"],
          ["📋 لیست گزارش‌ها", "👥 مدیریت کاربران"],
          ["💾 پشتیبان‌گیری"],
          ["🔙 بازگشت"],
      ],
      resize_keyboard=True,
  )


def get_menu(update):
  return admin_menu() if is_admin(update) else user_menu()


# ============================================================
# توابع کمکی امن و اعتبارسنجی
# ============================================================
def safe_int(s):
  try:
    n = int(str(s).strip())
    return n if n >= 0 else None
  except Exception:
    return None


def serial_check(branch):
  try:
    a, b = int(branch["start"]), int(branch["end"])
    expected = b - a + 1
    return expected == int(branch["issued"]), expected
  except Exception:
    return True, None


def total_issued(data):
  return sum(int(b.get("issued", 0)) for b in data.get("branches", []))


# ============================================================
# ایجاد خروجی اکسل
# ============================================================
def create_excel_report(start, end):
  rows, issued, transit, export, cancelled, branch_totals = range_summary(
      start, end
  )
  wb = openpyxl.Workbook()
  ws = wb.active
  ws.title = "گزارش بازه‌ای باشماق"

  ws.append(["تاریخ میلادی", "تاریخ شمسی", "ترانزیت", "صادرات", "مجموع صادره"])
  for item in rows:
    ws.append([
        item.get("date"),
        item.get("shamsi_date"),
        item.get("transit", 0),
        item.get("export", 0),
        total_issued(item),
    ])

  filename = f"bashmaq_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
  path = os.path.join(BASE_DIR, filename)
  wb.save(path)
  return path


# ============================================================
# متن گزارش‌ها
# ============================================================
def create_branch_report(data):
  r = "باسلام\n\n(سه شعبه مرز باشماق)\n\n"
  r += f"در روز {data['date']}\nتاریخ شمسی: {data['shamsi_date']}\n\n"
  for i, b in enumerate(data["branches"], 1):
    r += (
        f"شعبه {i} ({b['name']}) مرز باشماق\n\n"
        f"تعداد صادره {b['issued']} فقره مانیفست از سریال {b['start']} الی"
        f" {b['end']} درمرز باشماق صادر گردید\n"
        f"تعداد ابطالی {b['cancelled']} فقره\n"
        f"سریال ابطالی: {b['cancelled_serial']}\n\n"
    )
  r += f"جمع کل صادرها : {total_issued(data)} فقره\n\n{FIXED_FOOTER}"
  return r


def create_trade_report(data):
  return (
      "شعبه مرز باشماق\n\n"
      f"{data['date']}\n{data['shamsi_date']}\n\n"
      "تعداد صادره\n"
      f"ترانزیت : {data['transit']}\n"
      f"صادرات : {data['export']}\n\n{FIXED_FOOTER}"
  )


# ============================================================
# آمار بازه‌ای و ماهانه
# ============================================================
def range_summary(start, end):
  rows = []
  for item in all_reports():
    try:
      gd = parse_gregorian(item["date"])
    except Exception:
      continue
    if start <= gd <= end:
      rows.append(item)
  rows.sort(key=lambda x: parse_gregorian(x["date"]))

  issued = sum(total_issued(x) for x in rows)
  transit = sum(int(x.get("transit", 0)) for x in rows)
  export = sum(int(x.get("export", 0)) for x in rows)
  cancelled = sum(
      int(b.get("cancelled", 0)) for x in rows for b in x.get("branches", [])
  )

  branch_totals = {}
  for x in rows:
    for b in x.get("branches", []):
      name = b.get("name", "بدون نام")
      branch_totals[name] = branch_totals.get(name, 0) + int(
          b.get("issued", 0)
      )

  return rows, issued, transit, export, cancelled, branch_totals


def range_text(start, end):
  rows, issued, transit, export, cancelled, branch_totals = range_summary(
      start, end
  )
  s = (
      "📊 جمع گزارش‌های بازه‌ای\n\n"
      f"از تاریخ: {start.strftime('%d/%m/%Y')}\n"
      f"تا تاریخ: {end.strftime('%d/%m/%Y')}\n"
      f"از شمسی: {shamsi(start)} تا {shamsi(end)}\n\n"
      f"تعداد روزهای دارای گزارش: {len(rows)}\n\n"
      f"جمع کل صادره: {issued} فقره\n"
      f"جمع کل ابطالی: {cancelled} فقره\n"
      f"جمع ترانزیت: {transit}\n"
      f"جمع صادرات: {export}"
  )
  if branch_totals:
    s += "\n\nتفکیک شعبه‌ها:\n" + "\n".join(
        f"• {name}: {value} فقره" for name, value in branch_totals.items()
    )
  if not rows:
    s += "\n\n❌ در این بازه گزارشی ثبت نشده است."
  s += f"\n\n{FIXED_FOOTER}"
  return s


# ============================================================
# ارسال گزارش (صرفاً متنی و فایل اکسل)
# ============================================================
async def send_full_report(update, data):
  await update.message.reply_text(create_branch_report(data))
  await update.message.reply_text(create_trade_report(data))


# ============================================================
# عمومی
# ============================================================
async def start(update, context):
  context.user_data.clear()
  save_user(update.effective_user)
  if not can_use(update):
    await update.message.reply_text("⛔ دسترسی شما توسط مدیر غیرفعال شده است.")
    return ConversationHandler.END
  await update.message.reply_text(
      "سلام 🌹\n\nبه ربات گزارش مرز باشماق خوش آمدید.",
      reply_markup=get_menu(update),
  )
  return ConversationHandler.END


async def my_id(update, context):
  save_user(update.effective_user)
  await update.message.reply_text(
      f"شناسه عددی تلگرام شما:\n\n{update.effective_user.id}"
  )


async def cancel(update, context):
  context.user_data.clear()
  await update.message.reply_text("❌ عملیات لغو شد.", reply_markup=get_menu(update))
  return ConversationHandler.END


# ============================================================
# ثبت گزارش جدید
# ============================================================
async def new_report(update, context):
  save_user(update.effective_user)
  if not is_admin(update):
    await update.message.reply_text(
        "⛔ فقط مدیر اجازه ثبت گزارش دارد.", reply_markup=get_menu(update)
    )
    return ConversationHandler.END
  context.user_data.clear()
  context.user_data["branches"] = []
  context.user_data["mode"] = "new"
  await update.message.reply_text(
      "📅 تاریخ را به صورت میلادی وارد کنید.\n\nمثال:\n25/09/2026",
      reply_markup=ReplyKeyboardRemove(),
  )
  return DATE


async def get_date(update, context):
  try:
    d = parse_gregorian(update.message.text)
  except ValueError:
    await update.message.reply_text("❌ تاریخ اشتباه است. مثال: 25/09/2026")
    return DATE

  ds = d.strftime("%d/%m/%Y")
  context.user_data["date"] = ds
  context.user_data["shamsi_date"] = shamsi(d)

  old = load_report(ds)
  if old:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ جایگزینی گزارش قبلی", callback_data="dup_replace")],
        [InlineKeyboardButton("❌ لغو", callback_data="dup_cancel")],
    ])
    await update.message.reply_text(
        f"⚠️ برای تاریخ {ds} قبلاً گزارش ثبت شده است.\n\nاگر ادامه بدهی گزارش"
        " قبلی جایگزین می‌شود.",
        reply_markup=kb,
    )
    return DATE

  context.user_data["count"] = 2
  context.user_data["current_branch"] = 1
  await update.message.reply_text("نام شعبه 1 را وارد کنید.")
  return NAME


async def duplicate_callback(update, context):
  q = update.callback_query
  await q.answer()
  if q.data == "dup_cancel":
    context.user_data.clear()
    await q.edit_message_text("❌ عملیات لغو شد.")
    await q.message.reply_text("🏠 منوی اصلی", reply_markup=admin_menu())
    return ConversationHandler.END
  await q.edit_message_text("✏️ گزارش قبلی در پایان این ثبت، جایگزین خواهد شد.")
  context.user_data["count"] = 2
  context.user_data["current_branch"] = 1
  await q.message.reply_text("نام شعبه 1 را وارد کنید.")
  return NAME


async def get_count(update, context):
  context.user_data["count"] = 2
  context.user_data["current_branch"] = 1
  await update.message.reply_text("نام شعبه 1 را وارد کنید.")
  return NAME


async def get_name(update, context):
  name = update.message.text.strip()
  if not name:
    await update.message.reply_text("❌ نام شعبه نمی‌تواند خالی باشد.")
    return NAME
  context.user_data["branch_name"] = name
  await update.message.reply_text("تعداد صادره این شعبه را وارد کنید.")
  return ISSUED


async def get_issued(update, context):
  n = safe_int(update.message.text)
  if n is None:
    await update.message.reply_text("❌ فقط عدد وارد کنید.")
    return ISSUED
  context.user_data["issued"] = n
  await update.message.reply_text("سریال شروع را وارد کنید.\nمثال: 25044")
  return START


async def get_start(update, context):
  s = update.message.text.strip()
  if not s:
    await update.message.reply_text("❌ سریال شروع را وارد کنید.")
    return START
  context.user_data["start_serial"] = s
  await update.message.reply_text("سریال پایان را وارد کنید.\nمثال: 25053")
  return END


async def get_end(update, context):
  s = update.message.text.strip()
  context.user_data["end_serial"] = s
  ok, expected = serial_check({
      "start": context.user_data["start_serial"],
      "end": s,
      "issued": context.user_data["issued"],
  })
  if not ok:
    await update.message.reply_text(
        f"⚠️ تعداد صادره با بازه سریال هماهنگ نیست.\nتعداد واردشده:"
        f" {context.user_data['issued']}\nتعداد سریال در بازه:"
        f" {expected}\n\nسریال پایان را دوباره وارد کنید."
    )
    return END
  await update.message.reply_text("تعداد ابطالی را وارد کنید. اگر ندارد 0 وارد کنید.")
  return CANCELLED


async def get_cancelled(update, context):
  n = safe_int(update.message.text)
  if n is None:
    await update.message.reply_text("❌ فقط عدد وارد کنید.")
    return CANCELLED
  context.user_data["cancelled"] = n
  if n:
    await update.message.reply_text("سریال ابطالی را وارد کنید.")
    return CANCELLED_SERIAL
  context.user_data["cancelled_serial"] = "0"
  return await save_branch(update, context)


async def get_cancelled_serial(update, context):
  s = update.message.text.strip()
  if not s:
    await update.message.reply_text("❌ سریال ابطالی را وارد کنید.")
    return CANCELLED_SERIAL
  context.user_data["cancelled_serial"] = s
  return await save_branch(update, context)


async def save_branch(update, context):
  b = {
      "name": context.user_data["branch_name"],
      "issued": context.user_data["issued"],
      "start": context.user_data["start_serial"],
      "end": context.user_data["end_serial"],
      "cancelled": context.user_data["cancelled"],
      "cancelled_serial": context.user_data["cancelled_serial"],
  }
  context.user_data["branches"].append(b)
  current = context.user_data["current_branch"]
  total = context.user_data["count"]
  if current < total:
    context.user_data["current_branch"] += 1
    await update.message.reply_text(f"نام شعبه {current + 1} را وارد کنید.")
    return NAME
  await update.message.reply_text(
      "اطلاعات تمام شعب ثبت شد ✅\n\nتعداد ترانزیت را وارد کنید."
  )
  return TRANSIT


async def get_transit(update, context):
  n = safe_int(update.message.text)
  if n is None:
    await update.message.reply_text("❌ فقط عدد وارد کنید.")
    return TRANSIT
  context.user_data["transit"] = n
  await update.message.reply_text("تعداد صادرات را وارد کنید.")
  return EXPORT


async def get_export(update, context):
  n = safe_int(update.message.text)
  if n is None:
    await update.message.reply_text("❌ فقط عدد وارد کنید.")
    return EXPORT
  context.user_data["export"] = n
  context.user_data["created_by"] = update.effective_user.id
  save_report(context.user_data)
  await send_full_report(update, context.user_data)
  context.user_data.clear()
  await update.message.reply_text(
      "✅ گزارش با موفقیت ذخیره شد.", reply_markup=admin_menu()
  )
  return ConversationHandler.END


# ============================================================
# جستجو
# ============================================================
async def search_report(update, context):
  if not can_use(update):
    await update.message.reply_text("⛔ دسترسی شما غیرفعال است.")
    return ConversationHandler.END
  await update.message.reply_text(
      "📅 تاریخ گزارش را به صورت میلادی وارد کنید.\nمثال: 25/09/2026",
      reply_markup=ReplyKeyboardRemove(),
  )
  return SEARCH_DATE


async def get_search_date(update, context):
  try:
    ds = norm_date(update.message.text)
  except ValueError:
    await update.message.reply_text("❌ تاریخ اشتباه است. مثال: 25/09/2026")
    return SEARCH_DATE
  data = load_report(ds)
  if not data:
    await update.message.reply_text(
        f"❌ برای {ds} گزارشی پیدا نشد.", reply_markup=get_menu(update)
    )
    return ConversationHandler.END
  await send_full_report(update, data)
  await update.message.reply_text(
      "✅ گزارش پیدا شد.", reply_markup=get_menu(update)
  )
  return ConversationHandler.END


async def today(update, context):
  d = iran_today()
  ds = d.strftime("%d/%m/%Y")
  data = load_report(ds)
  if not data:
    await update.message.reply_text(
        f"❌ برای امروز ({ds}) گزارشی ثبت نشده است.", reply_markup=get_menu(update)
    )
    return
  await send_full_report(update, data)
  await update.message.reply_text(
      "✅ گزارش امروز", reply_markup=get_menu(update)
  )


# ============================================================
# گزارش بازه‌ای (همراه با فایل اکسل)
# ============================================================
async def range_start(update, context):
  if not can_use(update):
    return ConversationHandler.END
  await update.message.reply_text(
      "📅 تاریخ شروع را وارد کنید.\nمثال: 01/09/2026",
      reply_markup=ReplyKeyboardRemove(),
  )
  return RANGE_START


async def get_range_start(update, context):
  try:
    context.user_data["range_start"] = parse_gregorian(update.message.text)
  except ValueError:
    await update.message.reply_text("❌ تاریخ اشتباه است. مثال: 01/09/2026")
    return RANGE_START
  await update.message.reply_text(
      "📅 تاریخ پایان را وارد کنید.\nمثال: 30/09/2026"
  )
  return RANGE_END


async def get_range_end(update, context):
  try:
    end = parse_gregorian(update.message.text)
  except ValueError:
    await update.message.reply_text("❌ تاریخ اشتباه است. مثال: 30/09/2026")
    return RANGE_END
  start = context.user_data.get("range_start")
  if not start:
    await update.message.reply_text(
        "❌ تاریخ شروع پیدا نشد. دوباره از منو شروع کنید.",
        reply_markup=get_menu(update),
    )
    context.user_data.clear()
    return ConversationHandler.END
  if end < start:
    await update.message.reply_text(
        "❌ تاریخ پایان نمی‌‌تواند قبل از تاریخ شروع باشد."
    )
    return RANGE_END

  await update.message.reply_text(
      range_text(start, end), reply_markup=get_menu(update)
  )

  # ارسال فایل اکسل خروجی
  excel_path = None
  try:
    excel_path = create_excel_report(start, end)
    with open(excel_path, "rb") as f:
      await update.message.reply_document(
          f, filename=os.path.basename(excel_path), caption="📊 فایل اکسل آمار"
      )
  except Exception as e:
    logger.error(f"Excel error: {e}")
  finally:
    if excel_path:
      try:
        os.remove(excel_path)
      except OSError:
        pass

  context.user_data.clear()
  return ConversationHandler.END


# ============================================================
# آمار ماهانه
# ============================================================
async def month_start(update, context):
  if not can_use(update):
    return ConversationHandler.END
  await update.message.reply_text(
      "📅 ماه را به صورت MM/YYYY وارد کنید.\nمثال: 09/2026",
      reply_markup=ReplyKeyboardRemove(),
  )
  return MONTH


async def get_month(update, context):
  try:
    m, y = map(int, update.message.text.strip().split("/"))
    if not 1 <= m <= 12 or y < 1900 or y > 2200:
      raise ValueError
    start = date(y, m, 1)
    end = (
        date(y + 1, 1, 1) - timedelta(days=1)
        if m == 12
        else date(y, m + 1, 1) - timedelta(days=1)
    )
  except Exception:
    await update.message.reply_text("❌ فرمت صحیح: 09/2026")
    return MONTH
  await update.message.reply_text(
      range_text(start, end), reply_markup=get_menu(update)
  )

  # خروجی اکسل ماهانه
  excel_path = create_excel_report(start, end)
  try:
    with open(excel_path, "rb") as f:
      await update.message.reply_document(
          f, filename=os.path.basename(excel_path), caption="📊 فایل اکسل ماهانه"
      )
  finally:
    if excel_path:
      try:
        os.remove(excel_path)
      except OSError:
        pass

  context.user_data.clear()
  return ConversationHandler.END


# ============================================================
# مدیریت
# ============================================================
async def management(update, context):
  if not is_admin(update):
    await update.message.reply_text(
        "⛔ فقط مدیر.", reply_markup=get_menu(update)
    )
    return
  await update.message.reply_text("⚙️ مدیریت", reply_markup=management_menu())


async def edit_start(update, context):
  if not is_admin(update):
    return ConversationHandler.END
  await update.message.reply_text(
      "📅 تاریخ گزارشی که می‌خواهی ویرایش کنی را وارد کن.",
      reply_markup=ReplyKeyboardRemove(),
  )
  return EDIT_DATE


async def get_edit_date(update, context):
  try:
    ds = norm_date(update.message.text)
  except ValueError:
    await update.message.reply_text("❌ تاریخ اشتباه است.")
    return EDIT_DATE
  old = load_report(ds)
  if not old:
    await update.message.reply_text(
        "❌ چنین گزارشی وجود ندارد.", reply_markup=management_menu()
    )
    return ConversationHandler.END

  context.user_data.clear()
  context.user_data.update(
      branches=[],
      count=0,
      current_branch=1,
      date=ds,
      shamsi_date=shamsi(parse_gregorian(ds)),
      editing=True,
  )
  context.user_data["count"] = 2
  context.user_data["current_branch"] = 1
  await update.message.reply_text("✏️ ویرایش شروع شد.\nنام شعبه 1 را وارد کنید.")
  return NAME


async def delete_start(update, context):
  if not is_admin(update):
    return ConversationHandler.END
  await update.message.reply_text(
      "📅 تاریخ گزارش برای حذف را وارد کنید.", reply_markup=ReplyKeyboardRemove()
  )
  return DELETE_DATE


async def get_delete_date(update, context):
  try:
    ds = norm_date(update.message.text)
  except ValueError:
    await update.message.reply_text("❌ تاریخ اشتباه است.")
    return DELETE_DATE
  if not load_report(ds):
    await update.message.reply_text(
        "❌ گزارشی برای این تاریخ نیست.", reply_markup=management_menu()
    )
    return ConversationHandler.END
  kb = InlineKeyboardMarkup([
      [InlineKeyboardButton("🗑 بله، حذف شود", callback_data=f"del_yes:{ds}")],
      [InlineKeyboardButton("❌ لغو", callback_data="del_no")],
  ])
  await update.message.reply_text(
      f"⚠ حذف گزارش {ds}؟ این عملیات قابل برگشت نیست.", reply_markup=kb
  )
  return DELETE_DATE


async def delete_callback(update, context):
  q = update.callback_query
  await q.answer()
  if q.data == "del_no":
    await q.edit_message_text("❌ حذف لغو شد.")
  else:
    ds = q.data.split(":", 1)[1]
    await q.edit_message_text(
        "🗑 گزارش حذف شد." if delete_report(ds) else "❌ گزارش پیدا نشد."
    )
  await q.message.reply_text("⚙️ مدیریت", reply_markup=management_menu())
  context.user_data.clear()
  return ConversationHandler.END


async def list_reports(update, context):
  if not is_admin(update):
    return
  dates = list_dates()
  if not dates:
    await update.message.reply_text(
        "📋 هنوز گزارشی ثبت نشده است.", reply_markup=management_menu()
    )
    return
  text = "📋 گزارش‌های ثبت‌شده:\n\n" + "\n".join(f"• {x}" for x in dates[:100])
  if len(dates) > 100:
    text += f"\n\n... و {len(dates) - 100} گزارش دیگر"
  await update.message.reply_text(text, reply_markup=management_menu())


async def user_management(update, context):
  if not is_admin(update):
    return
  rows = users_rows()
  if not rows:
    await update.message.reply_text(
        "هنوز کاربری ثبت نشده است.", reply_markup=management_menu()
    )
    return
  buttons = []
  for uid, username, first_name, allowed, adm in rows[:50]:
    label = f"{first_name or username or uid} | {'مدیر' if adm else ('فعال' if allowed else 'غیرفعال')}"
    buttons.append(
        [InlineKeyboardButton(label[:60], callback_data=f"user:{uid}")]
    )
  await update.message.reply_text(
      "👥 کاربر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons)
  )


async def user_callback(update, context):
  q = update.callback_query
  await q.answer()
  if not is_admin(update):
    return
  uid = int(q.data.split(":")[1])
  rows = {r[0]: r for r in users_rows()}
  row = rows.get(uid)
  if not row:
    await q.edit_message_text("کاربر پیدا نشد.")
    return
  _, username, first_name, allowed, adm = row

  status_text = "فعال" if allowed else "غیرفعال"
  role_text = "مدیر" if adm else "کاربر"
  name_text = first_name or username or uid

  kb = InlineKeyboardMarkup([
      [
          InlineKeyboardButton("🚫 غیرفعال کردن", callback_data=f"ua:{uid}:0"),
          InlineKeyboardButton("✅ فعال کردن", callback_data=f"ua:{uid}:1"),
      ],
      [
          InlineKeyboardButton("👑 مدیر کردن", callback_data=f"ua:{uid}:admin"),
          InlineKeyboardButton("👤 حذف مدیر", callback_data=f"ua:{uid}:unadmin"),
      ],
  ])

  text = (
      f"کاربر: {name_text}\nID: {uid}\nوضعیت: {status_text}\nدسترسی:"
      f" {role_text}"
  )
  await q.edit_message_text(text, reply_markup=kb)


async def user_action(update, context):
  q = update.callback_query
  await q.answer()
  if not is_admin(update):
    return
  _, uid_s, action = q.data.split(":")
  uid = int(uid_s)
  if action in ("0", "1"):
    if uid == ADMIN_ID and action == "0":
      await q.answer("مدیر اصلی قابل غیرفعال شدن نیست.", show_alert=True)
      return
    set_allowed(uid, action == "1")
  elif action == "admin":
    add_admin(uid)
  elif action == "unadmin":
    if uid == ADMIN_ID:
      await q.answer("مدیر اصلی قابل حذف نیست.", show_alert=True)
      return
    remove_admin(uid)
  await q.edit_message_text("✅ تغییر اعمال شد.")
  await q.message.reply_text("⚙️ مدیریت", reply_markup=management_menu())


async def backup(update, context):
  if not is_admin(update):
    return
  if not os.path.exists(DB_FILE):
    await update.message.reply_text(
        "❌ فایل دیتابیس هنوز ساخته نشده است.", reply_markup=management_menu()
    )
    return
  backup_path = os.path.join(
      BASE_DIR,
      f"bashmaq_reports_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
  )
  shutil.copy2(DB_FILE, backup_path)
  try:
    with open(backup_path, "rb") as f:
      await update.message.reply_document(
          f,
          filename=os.path.basename(backup_path),
          caption="💾 پشتیبان دیتابیس گزارش‌ها",
      )
  finally:
    try:
      os.remove(backup_path)
    except OSError:
      pass


# ============================================================
# ارسال گزارش به کاربر دیگر
# ============================================================
async def send_report_start(update, context):
  if not is_admin(update):
    return ConversationHandler.END
  await update.message.reply_text(
      "📅 تاریخ گزارشی که می‌‌خواهی ارسال کنی را وارد کن.",
      reply_markup=ReplyKeyboardRemove(),
  )
  return SEND_DATE


async def get_send_date(update, context):
  try:
    ds = norm_date(update.message.text)
  except ValueError:
    await update.message.reply_text("❌ تاریخ اشتباه است.")
    return SEND_DATE
  if not load_report(ds):
    await update.message.reply_text("❌ گزارشی برای این تاریخ پیدا نشد.")
    return SEND_DATE
  context.user_data["send_date"] = ds
  await update.message.reply_text(
      "👤 شناسه عددی تلگرام گیرنده را وارد کن.\nمثال: 123456789"
  )
  return SEND_USER


async def get_send_user(update, context):
  try:
    uid = int(update.message.text.strip())
    if uid <= 0:
      raise ValueError
  except ValueError:
    await update.message.reply_text("❌ شناسه عددی صحیح وارد کنید.")
    return SEND_USER

  ds = context.user_data.get("send_date")
  data = load_report(ds)
  if not data:
    await update.message.reply_text(
        "❌ گزارش دیگر پیدا نشد.", reply_markup=admin_menu()
    )
    context.user_data.clear()
    return ConversationHandler.END

  try:
    await context.bot.send_message(chat_id=uid, text=create_branch_report(data))
    await context.bot.send_message(chat_id=uid, text=create_trade_report(data))
    await update.message.reply_text(
        "✅ گزارش برای گیرنده ارسال شد.", reply_markup=admin_menu()
    )
  except Exception as e:
    await update.message.reply_text(
        f"❌ ارسال انجام نشد. جزئیات: {e}", reply_markup=admin_menu()
    )
  context.user_data.clear()
  return ConversationHandler.END


async def back(update, context):
  context.user_data.clear()
  await update.message.reply_text("🏠 منوی اصلی", reply_markup=get_menu(update))


async def unknown(update, context):
  if update.message and update.message.text:
    await update.message.reply_text(
        "لطفاً یکی از گزینه‌های منو را انتخاب کنید.", reply_markup=get_menu(update)
    )


# ============================================================
# اجرای ربات
# ============================================================
def main():
  if not TOKEN or TOKEN == "YOUR_BOT_TOKEN":
    raise RuntimeError("توکن ربات تنظیم نشده است.")

  init_db()
  app = Application.builder().token(TOKEN).build()

  report_conv = ConversationHandler(
      entry_points=[
          MessageHandler(filters.Regex(r"^📝 ثبت گزارش جدید$"), new_report),
          MessageHandler(filters.Regex(r"^✏️ ویرایش گزارش$"), edit_start),
      ],
      states={
          DATE: [
              CallbackQueryHandler(duplicate_callback, pattern=r"^dup_"),
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_date),
          ],
          EDIT_DATE: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_edit_date)
          ],
          COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_count)],
          NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
          ISSUED: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_issued)
          ],
          START: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_start)],
          END: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_end)],
          CANCELLED: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_cancelled)
          ],
          CANCELLED_SERIAL: [
              MessageHandler(
                  filters.TEXT & ~filters.COMMAND, get_cancelled_serial
              )
          ],
          TRANSIT: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_transit)
          ],
          EXPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_export)],
      },
      fallbacks=[
          CommandHandler("cancel", cancel),
          MessageHandler(filters.Regex(r"^❌ لغو$"), cancel),
      ],
      allow_reentry=True,
  )

  search_conv = ConversationHandler(
      entry_points=[
          MessageHandler(filters.Regex(r"^🔎 جستجوی گزارش$"), search_report)
      ],
      states={
          SEARCH_DATE: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_search_date)
          ]
      },
      fallbacks=[MessageHandler(filters.Regex(r"^❌ لغو$"), cancel)],
  )

  range_conv = ConversationHandler(
      entry_points=[
          MessageHandler(filters.Regex(r"^📊 گزارش بازه‌ای$"), range_start)
      ],
      states={
          RANGE_START: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_range_start)
          ],
          RANGE_END: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_range_end)
          ],
      },
      fallbacks=[MessageHandler(filters.Regex(r"^❌ لغو$"), cancel)],
  )

  month_conv = ConversationHandler(
      entry_points=[
          MessageHandler(filters.Regex(r"^📈 آمار ماهانه$"), month_start)
      ],
      states={
          MONTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_month)]
      },
      fallbacks=[MessageHandler(filters.Regex(r"^❌ لغو$"), cancel)],
  )

  delete_conv = ConversationHandler(
      entry_points=[
          MessageHandler(filters.Regex(r"^🗑 حذف گزارش$"), delete_start)
      ],
      states={
          DELETE_DATE: [
              CallbackQueryHandler(delete_callback, pattern=r"^del_"),
              MessageHandler(
                  filters.TEXT & ~filters.COMMAND, get_delete_date
              ),
          ]
      },
      fallbacks=[MessageHandler(filters.Regex(r"^❌ لغو$"), cancel)],
  )

  send_conv = ConversationHandler(
      entry_points=[
          MessageHandler(filters.Regex(r"^📤 ارسال گزارش$"), send_report_start)
      ],
      states={
          SEND_DATE: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_send_date)
          ],
          SEND_USER: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, get_send_user)
          ],
      },
      fallbacks=[MessageHandler(filters.Regex(r"^❌ لغو$"), cancel)],
  )

  app.add_handler(report_conv, group=0)
  app.add_handler(search_conv, group=0)
  app.add_handler(range_conv, group=0)
  app.add_handler(month_conv, group=0)
  app.add_handler(delete_conv, group=0)
  app.add_handler(send_conv, group=0)

  app.add_handler(CommandHandler("start", start), group=1)
  app.add_handler(CommandHandler("id", my_id), group=1)
  app.add_handler(
      MessageHandler(filters.Regex(r"^📅 گزارش امروز$"), today), group=1
  )
  app.add_handler(
      MessageHandler(filters.Regex(r"^⚙️ مدیریت$"), management), group=1
  )
  app.add_handler(
      MessageHandler(filters.Regex(r"^🔙 بازگشت$"), back), group=1
  )
  app.add_handler(
      MessageHandler(filters.Regex(r"^📋 لیست گزارش‌ها$"), list_reports),
      group=1,
  )
  app.add_handler(
      MessageHandler(filters.Regex(r"^👥 مدیریت کاربران$"), user_management),
      group=1,
  )
  app.add_handler(
      MessageHandler(filters.Regex(r"^💾 پشتیبان‌گیری$"), backup), group=1
  )
  app.add_handler(CallbackQueryHandler(user_callback, pattern=r"^user:"), group=1)
  app.add_handler(CallbackQueryHandler(user_action, pattern=r"^ua:"), group=1)
  app.add_handler(MessageHandler(filters.Regex(r"^❌ لغو$"), cancel), group=1)

  logger.info("Bot is running...")
  app.run_polling()


if __name__ == "__main__":
  main()