"""Telegram command listener: lets you manage the tracker from your phone.

Runs all the time on the PC (Task Scheduler starts it at log on). It only edits the
text/JSON files in this folder; run-pc.ps1 commits them so the GitHub backup sees them too.
It also warns you when the scheduled price checks stop running.

Commands (only from TELEGRAM_CHAT_ID):
    /add <link or ASIN> [price]   follow a product, optionally until it reaches a price
    /remove <ASIN>                stop following a product
    /search <words> [pages]       add a search the bot scans for deals
    /unsearch <words>             remove a search
    /list                         show what the bot follows
    /mute <hours>                 pause deal alerts (e.g. /mute 2 or /mute 30m)
    /unmute                       resume deal alerts
    /channel <@name | off>        also post deals to a Telegram channel
    /status                       last run, pages blocked, products tracked
"""

import json
import re
import sys
import time
from datetime import datetime, timezone

import requests

import tracker
from tracker import CHAT_ID, TOKEN

API = f"https://api.telegram.org/bot{TOKEN}"
RUN_LOG = tracker.DATA / "run.log"
STALE_MINUTES = 50  # the PC runs every 15 minutes; this long without a run means something broke

HELP = """أوامر البوت:

/add لينك أو ASIN [سعر] ← تابع منتج (ولو كتبت سعر يبعتلك لما يوصله)
/remove ASIN ← بطّل تتابع منتج
/search كلمات [عدد الصفحات] ← ضيف قسم يدوّر فيه على العروض
/unsearch كلمات ← شيل قسم
/list ← اللي البوت بيتابعه
/mute 2 ← وقّف التنبيهات ساعتين (أو /mute 30m)
/unmute ← رجّع التنبيهات
/channel @اسم_القناة ← ابعت العروض لقناة كمان (/channel off للإيقاف)
/status ← آخر تشغيلة وحالة البوت"""


def call(method, **params):
    try:
        return requests.post(f"{API}/{method}", data=params, timeout=70).json()
    except requests.RequestException as e:
        print("Telegram error:", e)
        return {}


def reply(text):
    call("sendMessage", chat_id=CHAT_ID, text=text, disable_web_page_preview="true")


def lines_of(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def write_lines(path, lines):
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def asin_in(text):
    m = re.search(r"(?:/dp/|/gp/product/|/gp/aw/d/|^)([A-Z0-9]{10})(?:[/?\s]|$)", text)
    return m.group(1) if m else None


def update_settings(**changes):
    tracker.load_shared()
    tracker.save_settings(**changes)


# ---------- commands ----------

def cmd_add(args):
    if not args:
        return "اكتب اللينك بعد الأمر، مثلاً:\n/add https://www.amazon.eg/dp/B0XXXXXXXX 5000"
    link, *rest = args.split()
    price = rest[0] if rest and re.fullmatch(r"[\d,.]+", rest[0]) else None
    short = re.match(r"https?://(amzn\.\w+|a\.co)/", link)
    asin = asin_in(link)
    if not asin and not short:
        return "مش لاقي رقم المنتج (ASIN) في اللينك ده."
    lines = lines_of(tracker.PRODUCTS_FILE)
    if asin and any(asin in l and not l.lstrip().startswith("#") for l in lines):
        return f"المنتج {asin} متابَع أصلاً."
    lines.append(f"{asin or link} {price}" if price else (asin or link))
    write_lines(tracker.PRODUCTS_FILE, lines)
    target = f" لحد ما يوصل {price} جنيه" if price else ""
    return f"✅ هتابع المنتج{target}. هيبدأ في التشغيلة الجاية (خلال 15 دقيقة)."


def cmd_remove(args):
    asin = asin_in(args.strip())
    if not asin:
        return "اكتب الـ ASIN بعد الأمر، مثلاً:\n/remove B0XXXXXXXX"
    lines = lines_of(tracker.PRODUCTS_FILE)
    kept = [l for l in lines if l.lstrip().startswith("#") or asin not in l]
    if len(kept) == len(lines):
        return f"المنتج {asin} مش في القايمة."
    write_lines(tracker.PRODUCTS_FILE, kept)
    return f"✅ بطّلت أتابع {asin}."


def cmd_search(args):
    m = re.fullmatch(r"(.+?)(?:\s+(\d+))?", args.strip())
    if not args.strip():
        return "اكتب كلمة البحث بعد الأمر، مثلاً:\n/search gaming chair 2"
    words, pages = m.group(1).strip(), int(m.group(2) or 1)
    lines = lines_of(tracker.SEARCHES_FILE)
    if any(re.fullmatch(rf"{re.escape(words)}(\s+\d+)?", l.strip(), re.I) for l in lines):
        return f"البحث '{words}' موجود أصلاً."
    lines.append(f"{words} {pages}")
    write_lines(tracker.SEARCHES_FILE, lines)
    total = sum(p for _, p in tracker.read_searches())
    warn = "\n⚠️ مجموع الصفحات عدّى 25، وأمازون ممكن يرفض طلبات أكتر." if total > 25 else ""
    return f"✅ ضفت البحث '{words}' ({pages} صفحة). المجموع دلوقتي {total} صفحة.{warn}"


def cmd_unsearch(args):
    words = args.strip()
    if not words:
        return "اكتب كلمة البحث بعد الأمر، مثلاً:\n/unsearch perfume"
    lines = lines_of(tracker.SEARCHES_FILE)
    kept = [l for l in lines if not re.fullmatch(rf"{re.escape(words)}(\s+\d+)?", l.strip(), re.I)]
    if len(kept) == len(lines):
        return f"مش لاقي البحث '{words}'. ابعت /list تشوف الموجود."
    write_lines(tracker.SEARCHES_FILE, kept)
    return f"✅ شلت البحث '{words}'."


def cmd_list(_):
    tracker.load_shared()
    searches = tracker.read_searches()
    products = [l.strip() for l in lines_of(tracker.PRODUCTS_FILE) if l.strip() and not l.lstrip().startswith("#")]
    out = [f"🔎 الأقسام ({sum(p for _, p in searches)} صفحة):"]
    out += [f"• {w} ({p})" for w, p in searches] or ["• مفيش"]
    out += ["", "👁️ منتجات بتتابعها:"]
    out += [f"• {p}" for p in products] or ["• مفيش"]
    out += ["", f"🔕 منتجات متجاهَلة: {len(tracker.IGNORED)}"]
    if tracker.muted():
        until = datetime.fromtimestamp(tracker.SETTINGS["muted_until"]).strftime("%H:%M")
        out.append(f"🔕 التنبيهات متوقفة لحد {until}")
    if tracker.SETTINGS.get("channel"):
        out.append(f"📢 القناة: {tracker.SETTINGS['channel']}")
    return "\n".join(out)


def cmd_mute(args):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|h|d)?\s*", args or "1")
    if not m:
        return "مثال: /mute 2 (ساعتين) أو /mute 30m (نص ساعة)"
    amount, unit = float(m.group(1)), m.group(2) or "h"
    seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
    until = time.time() + seconds
    update_settings(muted_until=until)
    return f"🔕 وقفت التنبيهات لحد {datetime.fromtimestamp(until).strftime('%H:%M')}. العروض اللي تظهر في الوقت ده هتوصلك بعدها لو لسه شغالة."


def cmd_unmute(_):
    update_settings(muted_until=None)
    return "🔔 رجّعت التنبيهات."


def cmd_channel(args):
    name = args.strip()
    if name.lower() == "off":
        update_settings(channel=None)
        return "✅ بطّلت أبعت للقناة."
    if not re.fullmatch(r"@\w{4,}|-100\d+", name):
        return "اكتب اسم القناة بعد الأمر، مثلاً:\n/channel @my_deals\n\nولازم تضيف البوت للقناة كـ Admin الأول."
    r = call("sendMessage", chat_id=name, text="✅ القناة اتربطت بالبوت، والعروض هتتنشر هنا.")
    if not r.get("ok"):
        return ("❌ مقدرتش أبعت للقناة. اتأكد إن:\n1. اسم القناة صح.\n2. البوت مضاف للقناة كـ Admin "
                f"وعنده صلاحية نشر الرسايل.\n\nرد تليجرام: {r.get('description', '')}")
    update_settings(channel=name)
    return f"✅ العروض هتتبعت لـ {name} كمان."


def cmd_status(_):
    health = json.loads(tracker.HEALTH_FILE.read_text(encoding="utf-8")) if tracker.HEALTH_FILE.exists() else {}
    if not health.get("last_run"):
        return "لسه مفيش تشغيلة اتسجلت."
    last = datetime.fromisoformat(health["last_run"])
    minutes = int((datetime.now(timezone.utc) - last).total_seconds() // 60)
    ok, failed = health.get("last_pages", [0, 0])
    state = json.loads(tracker.DISCOVER_FILE.read_text(encoding="utf-8")) if tracker.DISCOVER_FILE.exists() else {}
    tracker.load_shared()
    mood = "✅ شغال طبيعي" if minutes < STALE_MINUTES and failed <= (ok + failed) / 2 else "⚠️ فيه مشكلة"
    return (f"{mood}\n\n⏱️ آخر تشغيلة: من {minutes} دقيقة\n"
            f"📄 الصفحات: {ok} نجحت، {failed} اترفضت\n"
            f"📦 بيتابع {len(state):,} منتج\n"
            f"🔔 عروض اتبعتت آخر 3 أيام: {len(tracker.SENT)}"
            + ("\n🔕 التنبيهات متوقفة" if tracker.muted() else ""))


COMMANDS = {
    "/add": cmd_add, "/remove": cmd_remove, "/search": cmd_search, "/unsearch": cmd_unsearch,
    "/list": cmd_list, "/mute": cmd_mute, "/unmute": cmd_unmute, "/channel": cmd_channel,
    "/status": cmd_status, "/start": lambda _: HELP, "/help": lambda _: HELP,
}


# ---------- alert buttons ----------

def on_button(query):
    action, _, asin = query.get("data", "").partition(":")
    if action == "ignore":
        ignored = set(lines_of(tracker.IGNORED_FILE))
        ignored.add(asin)
        write_lines(tracker.IGNORED_FILE, sorted(ignored))
        answer = "🔕 تمام، مش هبعتلك المنتج ده تاني."
    elif action == "watch":
        lines = lines_of(tracker.PRODUCTS_FILE)
        if not any(asin in l and not l.lstrip().startswith("#") for l in lines):
            write_lines(tracker.PRODUCTS_FILE, lines + [asin])
        answer = "👁️ هتابعه، وهبعتلك لو نزل أكتر."
    else:
        answer = ""
    call("answerCallbackQuery", callback_query_id=query["id"], text=answer)


# ---------- main loop ----------

def check_runs(state):
    """Warn once if the scheduled checks stopped, and say when they're back."""
    if not RUN_LOG.exists():
        return
    minutes = (time.time() - RUN_LOG.stat().st_mtime) / 60
    if minutes > STALE_MINUTES and not state.get("warned"):
        reply(f"⚠️ مفيش تشغيلة اتعملت على الجهاز من {minutes:.0f} دقيقة. "
              "اتأكد إن المهمة 'Amazon EG Price Tracker' شغالة في Task Scheduler.")
        state["warned"] = True
    elif minutes <= STALE_MINUTES and state.get("warned"):
        reply("✅ التشغيلات رجعت تشتغل على الجهاز.")
        state["warned"] = False


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required")
    call("setMyCommands", commands=json.dumps([
        {"command": c.strip("/"), "description": d} for c, d in [
            ("/list", "اللي البوت بيتابعه"), ("/add", "تابع منتج"), ("/remove", "بطّل تتابع منتج"),
            ("/search", "ضيف قسم"), ("/unsearch", "شيل قسم"), ("/mute", "وقّف التنبيهات"),
            ("/unmute", "رجّع التنبيهات"), ("/channel", "ابعت العروض لقناة"), ("/status", "حالة البوت"),
            ("/help", "كل الأوامر")]
    ], ensure_ascii=False))
    offset, run_state = None, {}
    print("bot listening")
    while True:
        r = call("getUpdates", timeout=50, offset=offset or "", allowed_updates='["message","callback_query"]')
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            if "callback_query" in u:
                q = u["callback_query"]
                if str(q["from"]["id"]) == str(CHAT_ID):
                    on_button(q)
                continue
            msg = u.get("message", {})
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue  # ignore everyone but you
            text = msg.get("text", "").strip()
            command, _, args = text.partition(" ")
            handler = COMMANDS.get(command.split("@")[0].lower())
            try:
                reply(handler(args) if handler else HELP)
            except Exception as e:
                reply(f"❌ حصل خطأ: {e}")
        check_runs(run_state)
        if not r:
            time.sleep(10)  # network trouble; don't spin


if __name__ == "__main__":
    main()
