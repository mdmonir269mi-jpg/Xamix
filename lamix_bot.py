import asyncio, json, logging, os, re, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv("lamix_env.txt")
BOT_TOKEN = os.environ["BOT_TOKEN"]
KEY = os.environ["LAMIX_KEY"]
MESSAGES_URL = os.environ["MESSAGES_URL"]
NUMBER_URL = os.environ.get("NUMBER_URL", "").strip()
AUTH_MODE = os.environ.get("AUTH_MODE", "auto").lower()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
SUPPORT = os.environ.get("SUPPORT", "@Monir12255")

SERVICES = {"fb": "📘 FACEBOOK", "ig": "📸 INSTAGRAM", "wa": "💬 WHATSAPP", "tg": "✈️ TELEGRAM"}
POLL_EVERY, POLL_MAX = 4, 20 * 60

logging.basicConfig(level=logging.INFO)
http = httpx.AsyncClient(timeout=20)
watchers: dict[str, dict] = {}
chat_number: dict[int, str] = {}
auth_ok: str | None = None

# ---------- API (auth নিজে খুঁজে নেয়) ----------
def _variants(params):
    p = dict(params or {})
    return {
        "bearer": ({"Authorization": f"Bearer {KEY}"}, p),
        "x-api-key": ({"X-API-Key": KEY}, p),
        "query": ({}, {**p, "api_key": KEY}),
    }

async def api(method, url, params=None, body=None):
    global auth_ok
    v = _variants(params)
    order = [auth_ok] if auth_ok else ([AUTH_MODE] if AUTH_MODE in v else list(v))
    last = None
    for mode in order:
        h, p = v[mode]
        try:
            r = await http.request(method, url, headers=h, params=p, json=body)
        except Exception as e:
            last = e; continue
        if r.status_code in (401, 403):
            last = RuntimeError(f"{r.status_code} (auth: {mode})"); continue
        r.raise_for_status()
        auth_ok = mode
        return r.json()
    raise last or RuntimeError("API ব্যর্থ")

def first(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None

def find_list(j):
    if isinstance(j, list): return j
    if isinstance(j, dict):
        for k in ("data", "messages", "results", "items", "records", "sms"):
            if k in j:
                r = find_list(j[k])
                if r is not None: return r
    return None

def digits(x): return re.sub(r"\D", "", str(x))

def parse_messages(j):
    out = []
    for it in find_list(j) or []:
        num = first(it, ("number", "phone", "to", "recipient", "msisdn", "destination", "receiver"))
        txt = first(it, ("text", "message", "body", "content", "sms", "otp", "code"))
        if num is None or txt is None: continue
        uid = first(it, ("id", "uuid", "nid", "message_id")) or f"{num}:{txt}"
        out.append((digits(num), str(txt), str(uid)))
    return out

async def request_number(service_key):
    if not NUMBER_URL:
        raise RuntimeError("NUMBER_URL বসানো হয়নি")
    j = await api("POST", NUMBER_URL, body={"service": service_key})
    d = j.get("data", j) if isinstance(j, dict) else {}
    num = first(d, ("full_number", "number", "phone", "msisdn"))
    if not num:
        raise RuntimeError("নাম্বার পাওয়া যায়নি")
    return digits(num), str(first(d, ("country",)) or ""), str(first(d, ("operator", "carrier")) or "")

# ---------- OTP (সব ইউজারের জন্য একটাই পোলার) ----------
def add_watcher(chat_id, number):
    old = chat_number.pop(chat_id, None)
    if old: watchers.pop(old, None)
    watchers[number] = {"chat": chat_id, "expires": time.time() + POLL_MAX, "seen": set(), "got": False}
    chat_number[chat_id] = number

def drop_watcher(chat_id):
    n = chat_number.pop(chat_id, None)
    if n: watchers.pop(n, None)

async def otp_loop(app):
    while True:
        try:
            if watchers:
                for num, txt, uid in parse_messages(await api("GET", MESSAGES_URL)):
                    w = watchers.get(num)
                    if not w or uid in w["seen"]: continue
                    w["seen"].add(uid); w["got"] = True
                    m = re.search(r"\b\d{4,8}\b", txt)
                    code = m.group(0) if m else "?"
                    try:
                        await app.bot.send_message(w["chat"], f"✅ OTP পাওয়া গেছে!\n📞 `+{num}`\n🔑 কোড: `{code}`\n\n💬 {txt}", parse_mode="Markdown")
                    except Exception as e:
                        logging.warning("send: %s", e)
        except Exception as e:
            logging.warning("messages error: %s", e)
        now = time.time()
        for n, w in list(watchers.items()):
            if now > w["expires"]:
                watchers.pop(n, None); chat_number.pop(w["chat"], None)
                if not w["got"]:
                    try: await app.bot.send_message(w["chat"], "⌛ সময় শেষ, OTP আসেনি। নতুন নাম্বার নিন।")
                    except Exception: pass
        await asyncio.sleep(POLL_EVERY)

# ---------- UI ----------
MAIN_KB = ReplyKeyboardMarkup([["📱 GET NUMBER"], ["🆘 SUPPORT"]], resize_keyboard=True)

def service_kb():
    rows = [[B(v, callback_data=f"svc:{k}")] for k, v in SERVICES.items()]
    rows.append([B("❌ CLOSE", callback_data="close")])
    return M(rows)

async def safe_edit(q, text, **kw):
    try:
        await q.edit_message_text(text, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            await q.message.reply_text(text, **kw)

async def start(u: Update, c): await u.message.reply_text("স্বাগতম! নিচের মেনু থেকে বেছে নিন 👇", reply_markup=MAIN_KB)
async def menu(u: Update, c): await u.message.reply_text("📍 Select a service:", reply_markup=service_kb())
async def support(u: Update, c): await u.message.reply_text(f"🆘 সাপোর্ট: {SUPPORT}")
async def my_id(u: Update, c): await u.message.reply_text(f"আপনার id: {u.effective_user.id}")

async def test(u: Update, c):
    """অ্যাডমিন: API-র আসল response দেখায় (ডিবাগের জন্য)"""
    if u.effective_user.id not in ADMIN_IDS:
        await u.message.reply_text("শুধু অ্যাডমিন। ADMIN_IDS-এ আপনার id বসান (/id)।"); return
    try:
        j = await api("GET", MESSAGES_URL)
        txt = json.dumps(j, ensure_ascii=False)[:1500]
        await u.message.reply_text(f"auth: {auth_ok}\nপার্স হয়েছে: {len(parse_messages(j))}টা\n\n{txt}")
    except Exception as e:
        await u.message.reply_text(f"❌ {e}")

async def on_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    chat = q.message.chat_id
    if q.data == "close":
        await q.message.delete(); return
    if q.data == "back":
        drop_watcher(chat); await safe_edit(q, "📍 Select a service:", reply_markup=service_kb()); return
    if q.data.startswith("svc:"):
        key = q.data[4:]; drop_watcher(chat)
        try:
            num, country, op = await request_number(key)
        except Exception as e:
            await safe_edit(q, f"❌ এখন নাম্বার পাওয়া যায়নি।\nসাপোর্ট: {SUPPORT}\n({e})", reply_markup=service_kb()); return
        kb = M([[B("🔄 Change Number", callback_data=f"svc:{key}")], [B("⬅️ Back", callback_data="back"), B("❌ Close", callback_data="close")]])
        await safe_edit(q, f"{SERVICES[key]}\n📞 নাম্বার: `+{num}`\n🌍 {country} {op}\n\n⏳ OTP-র জন্য অপেক্ষা করছি...", parse_mode="Markdown", reply_markup=kb)
        add_watcher(chat, num)

# ---------- Render ----------
def start_health_server():
    port = os.environ.get("PORT")
    if not port: return
    class H(BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        do_HEAD = do_GET
        def log_message(self, *a): pass
    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()

async def keepalive():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    while url:
        await asyncio.sleep(600)
        try: await http.get(url)
        except Exception as e: logging.warning("keepalive: %s", e)

async def post_init(app):
    asyncio.create_task(otp_loop(app)); asyncio.create_task(keepalive())

def main():
    start_health_server()
    if not NUMBER_URL: logging.error("⚠️ NUMBER_URL বসানো নেই, নাম্বার আনা যাবে না")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(MessageHandler(filters.Regex("GET NUMBER$"), menu))
    app.add_handler(MessageHandler(filters.Regex("SUPPORT$"), support))
    app.add_handler(CallbackQueryHandler(on_cb))
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()

if __name__ == "__main__":
    main()
