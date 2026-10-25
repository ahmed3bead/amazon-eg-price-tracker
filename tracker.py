"""Track amazon.eg prices and send Telegram alerts on drops.

Two modes run together:
  - products.txt: products you picked yourself, with optional target prices.
  - searches.txt: searches the script scans on its own, alerting on any product
    whose price falls far below its usual price.

Usage:
    python tracker.py              # check products and scan searches once
    python tracker.py --chat-id    # print the chat id(s) that messaged your bot
    python tracker.py --test       # send a test message to Telegram

Environment variables:
    TELEGRAM_TOKEN     bot token from @BotFather
    TELEGRAM_CHAT_ID   your chat id (get it with --chat-id)
    DROP_PERCENT       alert when price falls this % below the last seen price (default 10)
    DISCOVER_DROP      searches mode: alert when price falls this % below its usual price (default 25)
    LISTED_DISCOUNT    searches mode: an advertised discount of this % or more off the
                       struck-through price (default 50) also alerts, but only if ...
    LISTED_VERIFY      ... the price really fell at least this % below its usual price (default 15),
                       so fake "was" prices are ignored
    DATA_DIR           where price history is kept (default: next to this script)
"""

import html
import json
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR") or BASE)
PRODUCTS_FILE = BASE / "products.txt"
SEARCHES_FILE = BASE / "searches.txt"
STATE_FILE = DATA / "prices.json"
DISCOVER_FILE = DATA / "discovered.json"
# Deals already sent, shared through the git repo by the PC and GitHub runs so neither repeats the other.
SENT_FILE = BASE / "sent.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DROP_PERCENT = float(os.environ.get("DROP_PERCENT", "10"))
DISCOVER_DROP = float(os.environ.get("DISCOVER_DROP", "25"))
LISTED_DISCOUNT = float(os.environ.get("LISTED_DISCOUNT", "50"))
LISTED_VERIFY = float(os.environ.get("LISTED_VERIFY", "15"))
MIN_READINGS = 2        # earlier prices needed before a discovered product can alert
MIN_DAYS = 3            # days of history needed before the 30-day usual price takes over
HISTORY_DAYS = 30       # how far back the usual price looks
MAX_ALERTS_PER_RUN = 15  # don't flood Telegram if something odd happens
FORGET_AFTER_DAYS = 30   # drop discovered products not seen in search results for this long
SENT_DAYS = 3            # a deal sent within this many days isn't sent again unless the price drops further

SENT = {}


def load_sent():
    global SENT
    SENT = json.loads(SENT_FILE.read_text(encoding="utf-8")) if SENT_FILE.exists() else {}


def save_sent():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SENT_DAYS)).strftime("%Y-%m-%d")
    kept = {a: v for a, v in sorted(SENT.items()) if v[1] >= cutoff}
    # One deal per line keeps git merges simple.
    text = "{\n" + ",\n".join(f'"{a}": {json.dumps(v)}' for a, v in kept.items()) + "\n}\n"
    if not SENT_FILE.exists() or SENT_FILE.read_text(encoding="utf-8") != text:
        SENT_FILE.write_text(text, encoding="utf-8")


def already_sent(asin, price):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SENT_DAYS)).strftime("%Y-%m-%d")
    entry = SENT.get(asin)
    return bool(entry) and entry[1] >= cutoff and price >= entry[0] - 0.01


def notify(asin, price, text, image=None):
    """Send an alert unless this deal (same or lower price) already went out recently."""
    if already_sent(asin, price):
        print(f"{asin}: already sent at {SENT[asin][0]:,.2f}, skipping")
        return False
    # Kanbkam keeps years of amazon.eg price history, handy for checking a deal by eye.
    send(f"{text}\n📊 تاريخ السعر على كان بكام:\nhttps://www.kanbkam.com/eg/ar/x-{asin}", image)
    SENT[asin] = [price, datetime.now(timezone.utc).strftime("%Y-%m-%d")]
    return True


def read_products():
    """Each line: <amazon.eg link or ASIN> [target price]. '#' starts a comment."""
    products = []
    for line in PRODUCTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        link = parts[0]
        if re.match(r"https?://(amzn\.\w+|a\.co)/", link):  # short share links from the app
            try:
                link = requests.get(link, headers=HEADERS, timeout=30, allow_redirects=True).url
            except requests.RequestException as e:
                print(f"Could not open short link {parts[0]}: {e}")
                continue
        asin = re.search(r"(?:/dp/|/gp/product/|/gp/aw/d/|^)([A-Z0-9]{10})(?:[/?]|$)", link)
        if not asin:
            print(f"Skipping line, no ASIN found: {line}")
            continue
        target = float(parts[1].replace(",", "")) if len(parts) > 1 else None
        products.append((asin.group(1), target))
    return products


def parse_price(html):
    # Structured value Amazon embeds in the page; most reliable.
    m = re.search(r'"priceAmount":([\d.]+)', html)
    if m:
        return float(m.group(1))
    i = html.find('id="corePrice_feature_div"')
    if i >= 0:
        m = re.search(r'class="a-offscreen">\s*EGP\s*([\d,.]+)', html[i:i + 6000])
        if m:
            return float(m.group(1).replace(",", ""))
    i = html.find('id="corePriceDisplay_desktop_feature_div"')
    if i >= 0:
        seg = html[i:i + 6000]
        whole = re.search(r'a-price-whole">([\d,]+)', seg)
        frac = re.search(r'a-price-fraction">(\d+)', seg)
        if whole:
            return float(whole.group(1).replace(",", "") + "." + (frac.group(1) if frac else "0"))
    return None


SESSION = requests.Session()  # keeps Amazon's cookies between requests, which looks less bot-like
SESSION.headers.update(HEADERS)


def get_page(url, params=None, expect=None, attempts=3):
    """GET an amazon.eg page, waiting and retrying when Amazon throttles us (503, captcha,
    or a stripped-down page missing `expect`)."""
    for attempt in range(1, attempts + 1):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            if r.status_code == 200 and "validateCaptcha" not in r.text and (not expect or expect in r.text):
                return r.text
            problem = "captcha" if "validateCaptcha" in r.text else f"HTTP {r.status_code}"
        except requests.RequestException as e:
            problem = str(e)
        if attempt < attempts:
            time.sleep(random.uniform(20, 40) * attempt)
    raise RuntimeError(f"blocked ({problem}) after {attempts} tries")


def image_url(src):
    """Turn an Amazon thumbnail URL into an 800px version that looks good in Telegram."""
    if not src:
        return None
    return re.sub(r"\._[^/]*_\.(jpg|png)$", r"._AC_SL800_.\1", src)


def fetch(asin):
    """Return (title, price, image) or raise on a blocked/failed request. price is None if unavailable."""
    page = get_page(f"https://www.amazon.eg/dp/{asin}", {"language": "en_AE"}, expect="productTitle")
    m = re.search(r'<span id="productTitle"[^>]*>\s*([^<]+?)\s*</span>', page)
    title = html.unescape(m.group(1)) if m else asin
    tag = re.search(r'<img[^>]*id="landingImage"[^>]*>', page)
    img = re.search(r'\ssrc="([^"]+)"', tag.group(0)) if tag else None
    return title, parse_price(page), image_url(img.group(1) if img else None)


def send(text, image=None):
    if not TOKEN or not CHAT_ID:
        print("[telegram not configured]", text, image or "")
        return
    api = f"https://api.telegram.org/bot{TOKEN}"
    if image:
        r = requests.post(f"{api}/sendPhoto", data={"chat_id": CHAT_ID, "photo": image, "caption": text}, timeout=30)
        if r.ok:
            return
        print("Telegram photo failed, sending text only:", r.text)
    r = requests.post(
        f"{api}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"},
        timeout=30,
    )
    if not r.ok:
        print("Telegram error:", r.text)


def alert_reason(price, last, lowest, target):
    if target is not None and price <= target and (last is None or price < last):
        return f"وصل للسعر اللي حددته ({target:,.0f} جنيه)"
    if last and price <= last * (1 - DROP_PERCENT / 100):
        return f"نزل {100 * (last - price) / last:.0f}% عن آخر مرة"
    if lowest and price < lowest and last is not None:
        return "أقل سعر من ساعة ما بدأت تتابعه"
    return None


def check():
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    products = read_products()

    for n, (asin, target) in enumerate(products):
        if n:
            time.sleep(random.uniform(4, 9))  # be gentle, avoid looking like a bot
        try:
            title, price, image = fetch(asin)
        except Exception as e:
            print(f"{asin}: failed ({e})")
            continue

        entry = state.setdefault(asin, {"history": []})
        entry["title"] = title
        if price is None:
            print(f"{asin}: no price (out of stock?)")
            continue

        last = entry.get("last")
        lowest = entry.get("lowest")
        reason = alert_reason(price, last, lowest, target)
        print(f"{asin}: {price:,.2f} EGP (last: {last}, target: {target})" + (f" -> ALERT: {reason}" if reason else ""))

        if reason:
            old = f"\nكان: {last:,.2f} جنيه" if last else ""
            notify(
                asin,
                price,
                f"🔔 {reason}\n\n{title[:120]}\n\n"
                f"السعر دلوقتي: {price:,.2f} جنيه{old}\n"
                f"https://www.amazon.eg/dp/{asin}",
                image,
            )

        entry["last"] = price
        entry["lowest"] = min(price, lowest) if lowest else price
        if not entry["history"] or entry["history"][-1][1] != price:
            entry["history"] = (entry["history"] + [[now, price]])[-100:]

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def read_searches():
    """Each line: <search words> [pages]. '#' starts a comment."""
    searches = []
    if not SEARCHES_FILE.exists():
        return searches
    for line in SEARCHES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"(.+?)\s+(\d+)$", line)
        searches.append((m.group(1), int(m.group(2))) if m else (line, 2))
    return searches


def fetch_search(words, page):
    """Return [(asin, title, price, list_price, image)] for one page of amazon.eg search results.
    list_price is the struck-through "was" price, or None when Amazon shows no discount."""
    text = get_page(
        "https://www.amazon.eg/s",
        {"k": words, "page": page, "language": "en_AE"},
        expect="s-search-result",
    )
    results = []
    blocks = re.split(r'(?=<div[^>]*data-asin="[A-Z0-9]{10}"[^>]*data-component-type="s-search-result")', text)
    for block in blocks[1:]:
        asin = re.search(r'data-asin="([A-Z0-9]{10})"', block).group(1)
        # The first plain a-price is the selling price; "a-text-price" is the struck-through list price.
        price = re.search(r'<span class="a-price"[^>]*>\s*<span class="a-offscreen">EGP\W*([\d,.]+)<', block)
        if not price:
            continue
        h2 = re.search(r"<h2[^>]*>([\s\S]{0,800}?)</h2>", block)
        title = html.unescape(re.sub(r"<[^>]+>", "", h2.group(1)).strip()) if h2 else asin
        listed = re.search(r'<span class="a-price a-text-price"[^>]*>\s*<span class="a-offscreen">EGP\W*([\d,.]+)<', block)
        listed = float(listed.group(1).replace(",", "")) if listed else None
        img = re.search(r'<img[^>]*class="s-image"[^>]*\ssrc="([^"]+)"', block)
        results.append((asin, title, float(price.group(1).replace(",", "")), listed, image_url(img.group(1) if img else None)))
    return results


def discover():
    searches = read_searches()
    if not searches:
        return
    state = json.loads(DISCOVER_FILE.read_text(encoding="utf-8")) if DISCOVER_FILE.exists() else {}
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    alerts = []
    seen = set()
    first = True

    for words, pages in searches:
        for page in range(1, pages + 1):
            if not first:
                time.sleep(random.uniform(5, 10))
            first = False
            try:
                results = fetch_search(words, page)
            except Exception as e:
                print(f"search '{words}' page {page}: failed ({e})")
                break
            print(f"search '{words}' page {page}: {len(results)} products")
            for asin, title, price, listed, image in results:
                if asin in seen:  # same product can show up in several searches
                    continue
                seen.add(asin)
                entry = state.setdefault(asin, {"prices": []})
                entry["title"] = title
                entry["seen"] = today
                earlier = entry["prices"]
                daily = [d for d in entry.get("daily", []) if d[0] >= month_ago]

                entry.pop("listed_alerted", None)  # left over from older versions
                # Discount Amazon advertises against its struck-through price.
                off = 100 * (listed - price) / listed if listed and listed > price else 0

                # Usual price: median of one price per day over the past 30 days. Until a product
                # has a few days of history, fall back to its most recent readings.
                past_days = [p for d, p in daily if d < today]
                if len(past_days) >= MIN_DAYS:
                    usual = statistics.median(past_days)
                elif len(earlier) >= MIN_READINGS:
                    usual = statistics.median(earlier)
                else:
                    usual = None

                if usual:
                    drop = 100 * (usual - price) / usual
                    # A struck-through price alone can be fake (a "was" price the product never
                    # sold at), so an advertised discount only counts if the price really fell
                    # below what we've been seeing.
                    real = drop >= DISCOVER_DROP or (off >= LISTED_DISCOUNT and drop >= LISTED_VERIFY)
                    if real:
                        # Alert once per low price; a further drop alerts again.
                        if entry.get("alerted") is None or price < entry["alerted"]:
                            shown = listed if off >= LISTED_DISCOUNT else None
                            alerts.append((drop, asin, title, price, usual, shown, image))
                            entry["alerted"] = price
                    else:
                        entry.pop("alerted", None)
                entry["prices"] = (earlier + [price])[-30:]
                # Keep the day's latest price, one entry per day.
                if daily and daily[-1][0] == today:
                    daily[-1][1] = price
                else:
                    daily.append([today, price])
                entry["daily"] = daily

    cutoff = (now - timedelta(days=FORGET_AFTER_DAYS)).strftime("%Y-%m-%d")
    state = {a: e for a, e in state.items() if e.get("seen", today) >= cutoff}
    DISCOVER_FILE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"discovery: tracking {len(state)} products, {len(alerts)} alert(s)")

    alerts.sort(reverse=True)  # biggest discounts first
    alerts = [a for a in alerts if not already_sent(a[1], a[3])]
    for drop, asin, title, price, usual, listed, image in alerts[:MAX_ALERTS_PER_RUN]:
        claimed = ""
        if listed:
            claimed = f"🏷️ أمازون كاتب خصم {100 * (listed - price) / listed:.0f}% (من {listed:,.2f} جنيه)\n"
        notify(
            asin,
            price,
            f"🔥 نزل فعلاً {drop:.0f}% عن سعره المعتاد\n\n{title[:120]}\n\n"
            f"السعر دلوقتي: {price:,.2f} جنيه\n"
            f"سعره المعتاد: {usual:,.2f} جنيه\n{claimed}"
            f"https://www.amazon.eg/dp/{asin}",
            image,
        )


def print_chat_ids():
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=30).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in r.get("result", []) if "message" in u}
    if not chats:
        print("No messages yet. Send any message to your bot first, then run this again.")
    for cid, name in chats.items():
        print(f"chat id: {cid}  ({name})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles choke on Arabic otherwise
    if "--chat-id" in sys.argv:
        print_chat_ids()
    elif "--test" in sys.argv:
        send("✅ البوت شغال وهيبعتلك لما الأسعار تنزل")
    else:
        DATA.mkdir(parents=True, exist_ok=True)
        load_sent()
        try:
            check()
            discover()
        finally:
            save_sent()
