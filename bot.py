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
"""

import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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


def ros_api():
    """Router API ကို ချိတ်ပြီး api object ပြန်ပေးတယ်။"""
    return ros_connect(
        username=CFG["router_user"],
        password=CFG["router_password"],
        host=CFG["router_host"],
        port=int(CFG.get("router_port", 8728)),
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
        "ဥပမာ: ABC123"
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


def start_health_server():
    """Free hosts (Render) sleep when idle - a tiny HTTP server on $PORT
    lets UptimeRobot ping the service every 5 min to keep it awake.
    Does nothing on the laptop (no PORT set there)."""
    port = os.environ.get("PORT")
    if not port:
        return
    try:
        port = int(port)
    except ValueError:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass

    def serve():
        try:
            HTTPServer(("0.0.0.0", port), Handler).serve_forever()
        except Exception as e:
            log.warning("Health server failed: %s", e)

    threading.Thread(target=serve, daemon=True).start()
    log.info("Health server listening on port %s", port)


def main():
    token = CFG.get("telegram_token", "")
    if not token or token.startswith("123456:"):
        print("config.json ထဲက telegram_token ကို အရင် ဖြည့်ပါ (@BotFather ကနေ ရတဲ့ token)။")
        sys.exit(1)
    start_health_server()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup))
    log.info("Bot စတင်နေပြီ — Telegram ကနေ voucher code ပို့ပြီး စမ်းကြည့်ပါ။")
    app.run_polling()


if __name__ == "__main__":
    main()
