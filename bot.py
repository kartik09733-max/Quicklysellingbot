"""
Auto-Ad bot for @quicklyselling  (aiogram 3.x)

Flow: /start menu -> Book an Ad -> pick plan -> send ad -> pay -> admin approves
      -> ad goes live and is reposted every REPOST_HOURS to your channels/groups.
"""
import asyncio
import html
import logging
import os
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ----------------------------- config -----------------------------
def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
TARGET_CHATS = [x.strip() for x in os.getenv("TARGET_CHATS", "").split(",") if x.strip()]
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/quicklyselling")
NETWORK_URL = os.getenv("NETWORK_URL", "https://t.me/quicklyselling")
PAYMENT_INFO = os.getenv("PAYMENT_INFO", "USDT TRC20: YOUR_WALLET_ADDRESS")
MAX_SLOTS = int(os.getenv("MAX_SLOTS", "8"))
REPOST_HOURS = int(os.getenv("REPOST_HOURS", "12"))
REF_PERCENT = float(os.getenv("REF_PERCENT", "10"))
WELCOME_IMAGE = os.getenv("WELCOME_IMAGE", "banner.jpg")  # optional

PLANS = {
    7: float(os.getenv("PRICE_7", "10")),
    30: float(os.getenv("PRICE_30", "30")),
}

# ----------------------------- database -----------------------------
db = sqlite3.connect("bot.db")
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, ref_by INTEGER, balance REAL DEFAULT 0, joined INTEGER);
CREATE TABLE IF NOT EXISTS ads(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, photo TEXT,
    days INTEGER, price REAL, status TEXT, proof TEXT,
    start_ts INTEGER, end_ts INTEGER, last_post_ts INTEGER DEFAULT 0, created INTEGER);
"""
)
db.commit()


def now():
    return int(time.time())


def slots_used():
    return db.execute("SELECT COUNT(*) FROM ads WHERE status IN ('active','pending')").fetchone()[0]


# ----------------------------- ui -----------------------------
class AdForm(StatesGroup):
    content = State()
    proof = State()


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 BOOK AN AD", callback_data="book")
    kb.button(text="🎁 INVITE & EARN", callback_data="invite")
    kb.button(text="❓ SUPPORT", url=SUPPORT_URL)
    kb.button(text="🔎 NETWORK", url=NETWORK_URL)
    kb.adjust(1, 1, 2)
    return kb.as_markup()


def welcome_text():
    lines = [
        "🔎 <b>Quickly Selling Auto-Ad</b>",
        "",
        f"Your ad in the market, reposted every {REPOST_HOURS} hours.",
        "",
        "💲 <b>Prices</b>",
    ]
    p7, p30 = PLANS[7], PLANS[30]
    save = round((1 - p30 / (p7 * 30 / 7)) * 100)
    lines.append(f"7 days · ${p7:g}")
    lines.append(f"30 days · ${p30:g} · save {save}%")
    lines += ["", f"📌 {slots_used()}/{MAX_SLOTS} slots booked", "", "<i>Every ad is reviewed before it goes live.</i>"]
    return "\n".join(lines)


bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


async def send_menu(target: Message):
    if os.path.exists(WELCOME_IMAGE):
        await target.answer_photo(FSInputFile(WELCOME_IMAGE), caption=welcome_text(), reply_markup=main_menu())
    else:
        await target.answer(welcome_text(), reply_markup=main_menu())


# ----------------------------- handlers -----------------------------
@dp.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        ref = None
        if command.args and command.args.startswith("ref_"):
            try:
                ref = int(command.args[4:])
            except ValueError:
                pass
            if ref == uid:
                ref = None
        db.execute("INSERT INTO users(id,username,ref_by,joined) VALUES(?,?,?,?)",
                   (uid, m.from_user.username, ref, now()))
        db.commit()
    await send_menu(m)


@dp.callback_query(F.data == "book")
async def book(c: CallbackQuery):
    if slots_used() >= MAX_SLOTS:
        return await c.answer("All slots are booked. Check back soon!", show_alert=True)
    kb = InlineKeyboardBuilder()
    for d, p in PLANS.items():
        kb.button(text=f"{d} days · ${p:g}", callback_data=f"plan:{d}")
    kb.adjust(1)
    await c.message.answer("Choose a plan:", reply_markup=kb.as_markup())
    await c.answer()


@dp.callback_query(F.data.startswith("plan:"))
async def plan(c: CallbackQuery, state: FSMContext):
    days = int(c.data.split(":")[1])
    await state.set_state(AdForm.content)
    await state.update_data(days=days)
    await c.message.answer(
        f"Plan: <b>{days} days · ${PLANS[days]:g}</b>\n\n"
        "Now send your ad (text, or a photo with caption).\n"
        "Max ~1000 characters. Send /cancel to stop."
    )
    await c.answer()


@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Cancelled.")


@dp.message(AdForm.content, F.text | F.photo)
async def got_ad(m: Message, state: FSMContext):
    data = await state.get_data()
    days = data["days"]
    text = (m.caption or m.text or "").strip()
    photo = m.photo[-1].file_id if m.photo else None
    if not text or len(text) > 1000:
        return await m.answer("Ad text must be between 1 and 1000 characters. Try again.")
    cur = db.execute(
        "INSERT INTO ads(user_id,text,photo,days,price,status,created) VALUES(?,?,?,?,?,?,?)",
        (m.from_user.id, text, photo, days, PLANS[days], "awaiting_payment", now()),
    )
    db.commit()
    ad_id = cur.lastrowid
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ I've paid", callback_data=f"paid:{ad_id}")
    await m.answer(
        f"Ad #{ad_id} saved.\n\n<b>Pay ${PLANS[days]:g}</b> for {days} days:\n"
        f"<code>{html.escape(PAYMENT_INFO)}</code>\n\n"
        "After paying, tap the button and send your TXID or a screenshot.",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data.startswith("paid:"))
async def paid(c: CallbackQuery, state: FSMContext):
    ad_id = int(c.data.split(":")[1])
    ad = db.execute("SELECT * FROM ads WHERE id=? AND user_id=?", (ad_id, c.from_user.id)).fetchone()
    if not ad or ad["status"] != "awaiting_payment":
        return await c.answer("This ad is not awaiting payment.", show_alert=True)
    await state.set_state(AdForm.proof)
    await state.update_data(ad_id=ad_id)
    await c.message.answer("Send your TXID (text) or a payment screenshot.")
    await c.answer()


@dp.message(AdForm.proof, F.text | F.photo)
async def got_proof(m: Message, state: FSMContext):
    ad_id = (await state.get_data())["ad_id"]
    proof = m.text or "(screenshot)"
    db.execute("UPDATE ads SET status='pending', proof=? WHERE id=?", (proof, ad_id))
    db.commit()
    await state.clear()
    await m.answer("Thanks! Your payment is being checked. You'll be notified once your ad is live.")

    ad = db.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=f"ok:{ad_id}")
    kb.button(text="❌ Reject", callback_data=f"no:{ad_id}")
    caption = (
        f"<b>New ad #{ad_id}</b> from @{m.from_user.username or '-'} ({m.from_user.id})\n"
        f"{ad['days']} days · ${ad['price']:g}\nProof: {html.escape(proof)}\n\n{html.escape(ad['text'])}"
    )
    for admin in ADMIN_IDS:
        try:
            if m.photo:
                await bot.send_photo(admin, m.photo[-1].file_id, caption=f"Payment screenshot for ad #{ad_id}")
            await bot.send_message(admin, caption, reply_markup=kb.as_markup())
        except Exception as e:
            logging.warning("admin notify failed: %s", e)


@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def review(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Admins only.", show_alert=True)
    action, ad_id = c.data.split(":")
    ad = db.execute("SELECT * FROM ads WHERE id=?", (int(ad_id),)).fetchone()
    if not ad or ad["status"] != "pending":
        return await c.answer("Already handled.", show_alert=True)

    if action == "no":
        db.execute("UPDATE ads SET status='rejected' WHERE id=?", (ad["id"],))
        db.commit()
        await bot.send_message(ad["user_id"], f"Ad #{ad['id']} was rejected. Contact support if you think this is a mistake.")
        await c.message.edit_text(c.message.html_text + "\n\n❌ <b>Rejected</b>")
        return await c.answer()

    start_ts = now()
    db.execute("UPDATE ads SET status='active', start_ts=?, end_ts=? WHERE id=?",
               (start_ts, start_ts + ad["days"] * 86400, ad["id"]))
    # referral commission
    u = db.execute("SELECT ref_by FROM users WHERE id=?", (ad["user_id"],)).fetchone()
    if u and u["ref_by"]:
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                   (ad["price"] * REF_PERCENT / 100, u["ref_by"]))
    db.commit()
    await bot.send_message(ad["user_id"], f"✅ Ad #{ad['id']} is live for {ad['days']} days and will be reposted every {REPOST_HOURS}h.")
    await c.message.edit_text(c.message.html_text + "\n\n✅ <b>Approved</b>")
    await c.answer()
    await post_ad(db.execute("SELECT * FROM ads WHERE id=?", (ad["id"],)).fetchone())


@dp.callback_query(F.data == "invite")
async def invite(c: CallbackQuery):
    me = await bot.get_me()
    uid = c.from_user.id
    count = db.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (uid,)).fetchone()[0]
    bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    await c.message.answer(
        f"🎁 <b>Invite & Earn</b>\nEarn {REF_PERCENT:g}% of every ad your referrals buy.\n\n"
        f"Your link:\n<code>https://t.me/{me.username}?start=ref_{uid}</code>\n\n"
        f"Referrals: {count}\nEarned: ${(bal['balance'] if bal else 0):.2f}\n\n"
        "Contact support to withdraw."
    )
    await c.answer()


@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    rev = db.execute("SELECT COALESCE(SUM(price),0) FROM ads WHERE status IN ('active','expired')").fetchone()[0]
    await m.answer(f"Users: {users}\nSlots: {slots_used()}/{MAX_SLOTS}\nRevenue: ${rev:.2f}")


# ----------------------------- reposter -----------------------------
async def post_ad(ad):
    for chat in TARGET_CHATS:
        try:
            if ad["photo"]:
                await bot.send_photo(chat, ad["photo"], caption=ad["text"], parse_mode=None)
            else:
                await bot.send_message(chat, ad["text"], parse_mode=None, disable_web_page_preview=True)
        except Exception as e:
            logging.warning("post to %s failed: %s", chat, e)
    db.execute("UPDATE ads SET last_post_ts=? WHERE id=?", (now(), ad["id"]))
    db.commit()


async def scheduler():
    while True:
        try:
            t = now()
            for ad in db.execute("SELECT * FROM ads WHERE status='active' AND end_ts<=?", (t,)).fetchall():
                db.execute("UPDATE ads SET status='expired' WHERE id=?", (ad["id"],))
                db.commit()
                try:
                    await bot.send_message(ad["user_id"], f"Your ad #{ad['id']} has expired. Book again anytime!")
                except Exception:
                    pass
            due = db.execute(
                "SELECT * FROM ads WHERE status='active' AND last_post_ts<=?", (t - REPOST_HOURS * 3600,)
            ).fetchall()
            for ad in due:
                await post_ad(ad)
                await asyncio.sleep(3)  # avoid flood limits
        except Exception:
            logging.exception("scheduler error")
        await asyncio.sleep(60)


async def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
