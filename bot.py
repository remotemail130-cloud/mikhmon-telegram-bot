#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KoGyi Mikhmon Voucher Bot
=========================
Telegram ကနေ voucher code တစ်ခု ပို့လိုက်ရင်
Router (MikroTik) ထဲကနေ အဲဒီ code ကို ရှာပြီး အချက်အလက် ပြန်ပို့ပေးတဲ့ bot။

ဘယ်လို အလုပ်လုပ်လဲ
- Mikhmon က voucher ရောင်းတိုင်း Router ထဲမှာ
  /system/script (comment = mikhmon) အဖြစ် မှတ်ထားတယ်။
  အဲဒီ record ထဲမှာ ရက်၊ အချိန်၊ username(code)၊ ဈေး၊ profile၊ comment ပါတယ်။
- Hotspot user အနေနဲ့လည်း /ip/hotspot/user ထဲမှာ ရှိတယ်။
- ဒီ bot က Router API (port 8728) နဲ့ ချိတ်ပြီး နှစ်ခုလုံးကို ရှာပြီး ပြန်ပို့တယ်။

Run နည်း (X390, Windows)
1. Python သွင်း (python.org) — install လုပ်တုန်း "Add Python to PATH" ကို အမှန်ခြစ်ပါ။
2. config.example.json ကို config.json လို့ ကူးပြီး ဖြည့်ပါ (token, router IP/user/password)။
3. pip install -r requirements.txt
4. python bot.py

Render (cloud) မှာ run ရင်: Telegram message က webhook နဲ့ လာတယ်။
Render က အိမ်တွင်း LAN (192.168.4.1) ကို တိုက်ရိုက် မရောက်နိုင်လို့
voucher lookup တွေကို /pending မှာ စုထားပြီး
X390 ပေါ်က helper.py က လာယူ၊ router ကို LAN ထဲကနေ မေး၊
Telegram Bot API ကနေ အဖြေကို တိုက်ရိုက် ပြန်ပို့ပေးတယ်။

KBZPay PDF: တစ်နေ့တာ KBZPay screenshot တွေ ပို့ပြီး /pdf လို့ ရိုက်ရင်
A4 စာရွက်ပေါ် ၂ ပုံစီ စီပြီး PDF ထုတ်ပေးတယ် (print ထုတ်ဖို့)။
Batch flow: screenshot အားလုံး တစ်ခါတည်း ပို့ပြီး /pdf ကို ချက်ချင်း ရိုက်ပါ။
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from io import BytesIO

from librouteros import connect as ros_connect
from librouteros.exceptions import LibRouterosError
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("voucher-bot")

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")


def load_config():
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    # Free server (Render) fallback: read settings from environment
    # variables instead of config.json (Render dashboard -> Environment).
    if os.environ.get("TELEGRAM_TOKEN"):
        return {
            "telegram_token": os.environ["TELEGRAM_TOKEN"],
            "router_host": os.environ.get("ROUTER_HOST", "192.168.4.1"),
            "router_port": int(os.environ.get("ROUTER_PORT", "8728")),
            "router_user": os.environ.get("ROUTER_USER", "admin"),
            "router_password": os.environ.get("ROUTER_PASSWORD", ""),
            "allowed_ids": [],
        }
    print("config.json မတွေ့ဘူး — config.example.json ကို config.json လို့ ကူးပြီး ဖြည့်ပါ။")
    sys.exit(1)


CFG = load_config()
ALLOWED_IDS = set(CFG.get("allowed_ids") or [])

# Pending voucher lookups, for the laptop helper (X390) to pick up.
# Render cannot reach the home LAN, so the bot queues lookups here;
# the helper polls /pending, queries the router on the LAN and replies
# to the user directly via the Telegram Bot API.
PENDING = {}
PENDING_LOCK = asyncio.Lock()
REQUEST_TTL = 30 * 60  # seconds; old requests are dropped
CLAIM_TIMEOUT = 120  # seconds; a stale claim becomes claimable again

# KBZPay screenshots collected for the daily PDF (per chat, in memory).
# Batch flow only: the user sends all screenshots in one go, then /pdf.
PHOTOS = {}
MAX_PHOTOS = 50


def _helper_secret_ok(request):
    secret = os.environ.get("HELPER_SECRET", "")
    if not secret:
        return True
    return request.headers.get("X-Helper-Secret") == secret


async def queue_lookup(chat_id, code):
    rid = uuid.uuid4().hex[:8]
    async with PENDING_LOCK:
        PENDING[rid] = {
            "id": rid,
            "chat_id": chat_id,
            "code": code,
            "ts": time.time(),
            "claimed": False,
            "claimed_at": 0.0,
        }
    return rid


async def _nudge_if_pending(bot, chat_id, rid):
    """If nobody picked up the lookup after 90s, tell the user the
    laptop helper is probably not running."""
    await asyncio.sleep(90)
    async with PENDING_LOCK:
        still = rid in PENDING
    if still:
        try:
            await bot.send_message(
                chat_id,
                "⚠️ Helper မဖွင့်ထားဘူးလို့ ထင်တယ် — laptop မှာ helper.py ဖွင့်ထားမှ "
                "voucher ရှာပေးနိုင်မယ်။ ဖွင့်ပြီးရင် code ပြန်ပို့ပေးပါ။",
            )
        except Exception:
            pass


def ros_api():
    """Router API ကို ချိတ်ပြီး api object ပြန်ပေးတယ်။"""
    return ros_connect(
        username=CFG["router_user"],
        password=CFG["router_password"],
        host=CFG["router_host"],
        port=int(CFG.get("router_port", 8728)),
        timeout=5,
    )


def find_voucher(code):
    """code နဲ့ ကိုက်တဲ့ hotspot user + ရောင်းတဲ့ record ကို ရှာတယ်။
    Returns: (user_dict | None, [sale_dict, ...])"""
    api = ros_api()
    try:
        users = list(api("/ip/hotspot/user/print"))
        # NOTE: librouteros 4.x (pip installed 4.2.2) — query words like
        # "?comment=mikhmon" must go through rawCmd(); Api.__call__ takes
        # only the command positionally, so passing the query as a 2nd
        # positional arg raises TypeError and every lookup would fail.
        scripts = list(api.rawCmd("/system/script/print", "?comment=mikhmon"))
    finally:
        try:
            api.close()
        except Exception:
            pass

    code_low = code.strip().lower()

    user = None
    for u in users:
        if str(u.get("name", "")).strip().lower() == code_low:
            user = u
            break

    sales = []
    for s in scripts:
        name = str(s.get("name", ""))
        parts = name.split("-|-")
        if len(parts) >= 3 and parts[2].strip().lower() == code_low:
            sales.append({
                "date": parts[0] if len(parts) > 0 else "",
                "time": parts[1] if len(parts) > 1 else "",
                "username": parts[2] if len(parts) > 2 else "",
                "price": parts[3] if len(parts) > 3 else "",
                "profile": parts[7] if len(parts) > 7 else "",
                "comment": parts[8] if len(parts) > 8 else "",
            })
    return user, sales


def format_reply(code, user, sales):
    if user is None and not sales:
        return (
            "❌ ဒီ code နဲ့ voucher မတွေ့ဘူး\n"
            "Code: " + code + "\n"
            "code မှန်မမှန် ပြန်စစ်ပေးပါ။"
        )

    lines = ["🎫 Voucher: " + code, ""]

    if sales:
        # နောက်ဆုံး ရောင်းတဲ့ record ကို အဓိက ပြတယ်
        s = sales[-1]
        if s["profile"]:
            lines.append("👤 Profile: " + s["profile"])
        if s["price"]:
            lines.append("💰 ဈေး: " + s["price"])
        if s["date"]:
            lines.append("📅 ရောင်းတဲ့ ရက်: " + s["date"] + (" " + s["time"] if s["time"] else ""))
        if s["comment"]:
            lines.append("💬 Comment: " + s["comment"])
        if len(sales) > 1:
            lines.append("🔁 ဒီ code နဲ့ ရောင်းထားတာ " + str(len(sales)) + " ခု ရှိတယ် (နောက်ဆုံးတစ်ခုကို ပြထားတယ်)")

    if user is not None:
        lines.append("")
        lines.append("📶 Hotspot user ရှိတယ်:")
        if user.get("profile"):
            lines.append("👤 Profile: " + str(user["profile"]))
        if user.get("limit-uptime"):
            lines.append("⏳ သုံးခွင့် အချိန်: " + str(user["limit-uptime"]))
        if user.get("uptime"):
            lines.append("⏱ သုံးပြီးသား အချိန်: " + str(user["uptime"]))
        disabled = str(user.get("disabled", "false")).lower() == "true"
        lines.append("📌 အနေအထား: " + ("❌ ပိတ်ထားတယ် (disabled)" if disabled else "✅ ဖွင့်ထားတယ်"))
        if user.get("comment"):
            lines.append("💬 Comment: " + str(user["comment"]))
    else:
        lines.append("")
        lines.append("📶 Hotspot user မတွေ့ဘူး (ဖျက်ထားတာ / သက်တမ်းကုန်တာ ဖြစ်နိုင်တယ်)")

    return "\n".join(lines)


def is_allowed(update):
    if not ALLOWED_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("ဒီ bot ကို သုံးခွင့် မရှိဘူး။")
        return
    await update.message.reply_text(
        "မင်္ဂလာပါ ကိုကြီး! 🎫\n"
        "Voucher code တစ်ခု ပို့လိုက်ရင် အဲဒီ voucher ရဲ့ အချက်အလက်\n"
        "(Profile, ဈေး, ရောင်းတဲ့ ရက်, အနေအထား) ကို ရှာပြီး ပြန်ပို့ပေးမယ်။\n\n"
        "ဥပမာ: ABC123\n\n"
        "KBZPay screenshot တွေ ပို့ပြီး /pdf လို့ ရိုက်ရင်\n"
        "တစ်နေ့တာ PDF ထုတ်ပေးမယ် (print ထုတ်ဖို့)။"
    )


async def lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("ဒီ bot ကို သုံးခွင့် မရှိဘူး။")
        return
    code = (update.message.text or "").strip()
    if not code:
        return
    if len(code) > 64:
        await update.message.reply_text("Code က ရှည်လွန်းတယ် — voucher code အတိုလေးပဲ ပို့ပေးပါ။")
        return
    if os.environ.get("PORT"):
        # Render (cloud): cannot reach the home LAN router, so queue
        # the lookup for the laptop helper (helper.py on the X390).
        rid = await queue_lookup(update.effective_chat.id, code)
        asyncio.create_task(_nudge_if_pending(context.bot, update.effective_chat.id, rid))
        await update.message.reply_text(f"🔍 '{code}' ကို ရှာနေတယ် — ခဏစောင့်ပါ။")
        return
    # Laptop: direct lookup, the router is on the same LAN.
    try:
        user, sales = find_voucher(code)
    except LibRouterosError as e:
        log.error("Router error: %s", e)
        await update.message.reply_text(
            "⚠️ Router နဲ့ ချိတ်လို့ မရဘူး။\n"
            "Laptop ဖွင့်ထားလား, Router API (port 8728) ဖွင့်ထားလား,\n"
            "config.json ထဲက router IP / password မှန်လား စစ်ပေးပါ။"
        )
        return
    except Exception as e:
        log.error("Lookup error: %s", e)
        await update.message.reply_text("⚠️ Error ဖြစ်သွားတယ် — ခဏနေ ပြန်စမ်းပေးပါ။")
        return
    await update.message.reply_text(format_reply(code, user, sales))


def yangon_today():
    """Current date/time in Myanmar (UTC+6:30), without tzdata."""
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)


def build_pdf(photo_bytes_list):
    """A4 pages, 2 screenshots side by side, date header.
    Returns the PDF as bytes. (Pillow is imported lazily.)"""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1240, 1754  # A4 @ 150 dpi
    MARGIN = 40
    HEADER_H = 90
    GAP = 20
    font = None
    for path in ("DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(path, 36)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    today = yangon_today()
    header = "KBZPay Transactions  -  " + today.strftime("%d %b %Y")
    pages = []
    for i in range(0, len(photo_bytes_list), 2):
        page = Image.new("RGB", (W, H), "white")
        draw = ImageDraw.Draw(page)
        draw.text((MARGIN, 30), header, fill="black", font=font)
        draw.line([(MARGIN, HEADER_H), (W - MARGIN, HEADER_H)], fill="black", width=2)
        pair = photo_bytes_list[i:i + 2]
        slot_w = (W - 2 * MARGIN - GAP) // 2
        slot_h = H - HEADER_H - MARGIN
        slot_x = [MARGIN, MARGIN + slot_w + GAP]
        for j, data in enumerate(pair):
            img = Image.open(BytesIO(data)).convert("RGB")
            img.thumbnail((slot_w, slot_h))
            x = slot_x[j] + (slot_w - img.width) // 2
            y = HEADER_H + 10
            page.paste(img, (x, y))
        pages.append(page)
    out = BytesIO()
    pages[0].save(out, format="PDF", save_all=True, append_images=pages[1:])
    return out.getvalue()


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    photos = update.message.photo or []
    if not photos:
        return
    lst = PHOTOS.setdefault(chat_id, [])
    if len(lst) >= MAX_PHOTOS:
        await update.message.reply_text("ဓာတ်ပုံ ၅၀ ပုံ ပြည့်သွားပြီ — /pdf လို့ ရိုက်ပြီး PDF ထုတ်လိုက်ပါ။")
        return
    photo = photos[-1]  # largest size
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        lst.append(buf.getvalue())
    except Exception as e:
        log.error("Photo download error: %s", e)
        await update.message.reply_text("⚠️ ပုံ download လုပ်လို့ မရဘူး — ပြန်ပို့ပေးပါ။")
        return
    n = len(lst)
    await update.message.reply_text(f"📸 {n} ပုံ ရပြီ — အားလုံးပို့ပြီးရင် /pdf လို့ ရိုက်ပါ။")


async def pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("ဒီ bot ကို သုံးခွင့် မရှိဘူး။")
        return
    chat_id = update.effective_chat.id
    lst = PHOTOS.get(chat_id, [])
    if not lst:
        await update.message.reply_text("ဓာတ်ပုံ မရှိသေးဘူး — KBZPay screenshot တွေ အရင် ပို့ပေးပါ။")
        return
    await update.message.reply_text(f"📄 PDF ထုတ်နေတယ် — {len(lst)} ပုံ…")
    try:
        pdf_bytes = await asyncio.to_thread(build_pdf, list(lst))
    except Exception as e:
        log.error("PDF build error: %s", e)
        await update.message.reply_text("⚠️ PDF ထုတ်လို့ မရဘူး — ပြန်စမ်းပေးပါ။")
        return
    today = yangon_today()
    filename = "KBZPay-" + today.strftime("%Y-%m-%d") + ".pdf"
    await context.bot.send_document(
        chat_id, document=BytesIO(pdf_bytes), filename=filename,
        caption=f"📄 {today.strftime('%d %b %Y')} — {len(lst)} ပုံ",
    )
    PHOTOS[chat_id] = []


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("ဒီ bot ကို သုံးခွင့် မရှိဘူး။")
        return
    PHOTOS[update.effective_chat.id] = []
    await update.message.reply_text("🗑 ဓာတ်ပုံတွေ ဖျက်ပြီးပြီ — အစကနေ ပြန်ပို့လို့ရတယ်။")


def create_web_app(app, token):
    """aiohttp app serving the Telegram webhook AND the helper queue
    endpoints (/pending, /claim, /done) on the same $PORT.
    Split out of run_webhook_mode so the routes can be tested without
    talking to Telegram."""
    from aiohttp import web

    async def handle_root(request):
        return web.Response(text="OK")

    async def handle_telegram(request):
        try:
            data = await request.json()
            update = Update.de_json(data, app.bot)
            if update is not None:
                await app.process_update(update)
        except Exception as e:
            log.error("Webhook update error: %s", e)
        return web.Response(text="OK")

    async def handle_pending(request):
        if not _helper_secret_ok(request):
            return web.Response(status=401, text="unauthorized")
        now = time.time()
        async with PENDING_LOCK:
            for rid in list(PENDING):
                r = PENDING[rid]
                if now - r["ts"] > REQUEST_TTL:
                    del PENDING[rid]
                elif r["claimed"] and now - r["claimed_at"] > CLAIM_TIMEOUT:
                    r["claimed"] = False
                    r["claimed_at"] = 0.0
            reqs = [r for r in PENDING.values() if not r["claimed"]]
        return web.json_response({"requests": reqs})

    async def handle_claim(request):
        if not _helper_secret_ok(request):
            return web.Response(status=401, text="unauthorized")
        data = await request.json()
        rid = data.get("id", "")
        async with PENDING_LOCK:
            r = PENDING.get(rid)
            if not r or r["claimed"]:
                return web.Response(status=404, text="not found")
            r["claimed"] = True
            r["claimed_at"] = time.time()
            return web.json_response(r)

    async def handle_done(request):
        if not _helper_secret_ok(request):
            return web.Response(status=401, text="unauthorized")
        data = await request.json()
        rid = data.get("id", "")
        async with PENDING_LOCK:
            PENDING.pop(rid, None)
        return web.json_response({"ok": True})

    webapp = web.Application()
    webapp.router.add_get("/", handle_root)
    webapp.router.add_post("/" + token, handle_telegram)
    webapp.router.add_get("/pending", handle_pending)
    webapp.router.add_post("/claim", handle_claim)
    webapp.router.add_post("/done", handle_done)
    return webapp


async def run_webhook_mode(app, token, port):
    """Render: webhook mode. A Telegram message wakes the sleeping
    service; the aiohttp server handles the Telegram webhook AND the
    helper queue endpoints (/pending, /claim, /done) on the same $PORT."""
    base = (
        os.environ.get("WEBHOOK_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or "https://mikhmon-telegram-bot.onrender.com"
    ).rstrip("/")
    webhook_url = base + "/" + token
    await app.initialize()
    await app.start()
    await app.bot.set_webhook(url=webhook_url)
    log.info("Bot စတင်နေပြီ (webhook) — %s", base)

    from aiohttp import web

    webapp = create_web_app(app, token)
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await app.stop()
        await app.shutdown()


def main():
    token = CFG.get("telegram_token", "")
    if not token or token.startswith("123456:"):
        print("config.json ထဲက telegram_token ကို အရင် ဖြည့်ပါ (@BotFather ကနေ ရတဲ့ token)။")
        sys.exit(1)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pdf", pdf_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup))
    app.add_handler(MessageHandler(filters.PHOTO, receive_photo))
    port = os.environ.get("PORT")
    if port:
        # Render (free): webhook mode — a Telegram message wakes the
        # sleeping service; lookups are queued for the laptop helper.
        asyncio.run(run_webhook_mode(app, token, int(port)))
    else:
        # Laptop: polling mode, no PORT set there.
        log.info("Bot စတင်နေပြီ — Telegram ကနေ voucher code ပို့ပြီး စမ်းကြည့်ပါ။")
        app.run_polling()


if __name__ == "__main__":
    main()
