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
    MIN_RATING         skip deals rated below this many stars (default 3.5; unrated products pass)
    MIN_PRICE          skip deals cheaper than this many pounds (default 100)
    SUMMARY_HOUR       local hour after which the daily summary is sent (default 22)
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
HEALTH_FILE = DATA / "health.json"
# Shared through the git repo by the PC and GitHub runs (and edited by bot.py):
SENT_FILE = BASE / "sent.json"          # deals already sent, so neither runner repeats the other
SETTINGS_FILE = BASE / "settings.json"  # mute, channel, last daily summary
IGNORED_FILE = BASE / "ignored.txt"     # products muted with the "ignore" button

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
MIN_RATING = float(os.environ.get("MIN_RATING", "3.5"))
MIN_PRICE = float(os.environ.get("MIN_PRICE", "100"))
SUMMARY_HOUR = int(os.environ.get("SUMMARY_HOUR", "22"))
MIN_READINGS = 2        # earlier prices needed before a discovered product can alert
MIN_DAYS = 3            # days of history needed before the 30-day usual price takes over
HISTORY_DAYS = 30       # how far back the usual price looks
MAX_ALERTS_PER_RUN = 15  # don't flood Telegram if something odd happens
FORGET_AFTER_DAYS = 30   # drop discovered products not seen in search results for this long
SENT_DAYS = 3            # a deal sent within this many days isn't sent again unless the price drops further
BAD_RUNS_BEFORE_WARNING = 3  # consecutive mostly-blocked runs before the health warning

SENT = {}
SETTINGS = {}
IGNORED = set()


# ---------- shared state ----------

def load_shared():
    global SENT, SETTINGS, IGNORED
    SENT = json.loads(SENT_FILE.read_text(encoding="utf-8")) if SENT_FILE.exists() else {}
    SETTINGS = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
    IGNORED = set(IGNORED_FILE.read_text(encoding="utf-8").split()) if IGNORED_FILE.exists() else set()


def write_if_changed(path, text):
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def save_shared():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SENT_DAYS)).strftime("%Y-%m-%d")
    kept = {a: v for a, v in sorted(SENT.items()) if v[1] >= cutoff}
    # One deal per line keeps git merges simple.
    write_if_changed(SENT_FILE, "{\n" + ",\n".join(
        f'"{a}": {json.dumps(v, ensure_ascii=False)}' for a, v in kept.items()) + "\n}\n")
    # bot.py may have changed settings (mute, channel) while this run was going; keep those
    # and only write back the one key the tracker itself sets.
    if "last_summary" in SETTINGS:
        save_settings(last_summary=SETTINGS["last_summary"])


def save_settings(**changes):
    """Merge changes into settings.json on disk (None removes a key)."""
    current = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
    for key, value in changes.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    write_if_changed(SETTINGS_FILE, json.dumps(current, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    SETTINGS.update(current)
    for key in [k for k in SETTINGS if k not in current]:
        SETTINGS.pop(key)


def already_sent(asin, price):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SENT_DAYS)).strftime("%Y-%m-%d")
    entry = SENT.get(asin)
    return bool(entry) and entry[1] >= cutoff and price >= entry[0] - 0.01


def muted():
    return time.time() < SETTINGS.get("muted_until", 0)


# ---------- Telegram ----------

def send(text, image=None, chat=None, buttons=None):
    """Send a message (a photo with caption when there's an image). Returns True on success."""
    chat = chat or CHAT_ID
    if not TOKEN or not chat:
        print("[telegram not configured]", text, image or "")
        return False
    api = f"https://api.telegram.org/bot{TOKEN}"
    extra = {"reply_markup": json.dumps({"inline_keyboard": buttons})} if buttons else {}
    if image:
        r = requests.post(f"{api}/sendPhoto", data={"chat_id": chat, "photo": image, "caption": text, **extra}, timeout=30)
        if r.ok:
            return True
        print("Telegram photo failed, sending text only:", r.text)
    r = requests.post(
        f"{api}/sendMessage",
        data={"chat_id": chat, "text": text, "disable_web_page_preview": "true", **extra},
        timeout=30,
    )
    if not r.ok:
        print("Telegram error:", r.text)
    return r.ok


def notify(asin, price, text, image=None, title="", drop=0):
    """Send a deal unless it's ignored, muted, or already went out recently at this price or lower."""
    if asin in IGNORED:
        print(f"{asin}: ignored by you, skipping")
        return False
    if already_sent(asin, price):
        print(f"{asin}: already sent at {SENT[asin][0]:,.2f}, skipping")
        return False
    if muted():
        print(f"{asin}: alerts are muted, holding back")
        return "muted"
    # Kanbkam keeps years of amazon.eg price history, handy for checking a deal by eye.
    text = f"{text}\n📊 تاريخ السعر على كان بكام:\nhttps://www.kanbkam.com/eg/ar/x-{asin}"
    buttons = [[
        {"text": "🔕 تجاهل المنتج ده", "callback_data": f"ignore:{asin}"},
        {"text": "👁️ تابعه", "callback_data": f"watch:{asin}"},
    ]]
    send(text, image, buttons=buttons)
    if SETTINGS.get("channel"):
        send(text, image, chat=SETTINGS["channel"])
    SENT[asin] = [price, datetime.now(timezone.utc).strftime("%Y-%m-%d"), title[:80], round(drop)]
    return True


# ---------- Amazon pages ----------

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


def money(text):
    return float(text.replace(",", ""))


def parse_price(page):
    # Structured value Amazon embeds in the page; most reliable.
    m = re.search(r'"priceAmount":([\d.]+)', page)
    if m:
        return float(m.group(1))
    i = page.find('id="corePrice_feature_div"')
    if i >= 0:
        m = re.search(r'class="a-offscreen">\s*EGP\s*([\d,.]+)', page[i:i + 6000])
        if m:
            return money(m.group(1))
    i = page.find('id="corePriceDisplay_desktop_feature_div"')
    if i >= 0:
        seg = page[i:i + 6000]
        whole = re.search(r'a-price-whole">([\d,]+)', seg)
        frac = re.search(r'a-price-fraction">(\d+)', seg)
        if whole:
            return float(whole.group(1).replace(",", "") + "." + (frac.group(1) if frac else "0"))
    return None


def parse_rating(text):
    """(stars, number of ratings) from a search result or product page, or (None, 0)."""
    stars = re.search(r"([\d.]+) out of 5 stars", text)
    count = (re.search(r"(\d[\d,]*) ratings?\b", text)
             or re.search(r'id="acrCustomerReviewText"[^>]*>\s*\(?(\d[\d,]*)', text))
    return (float(stars.group(1)) if stars else None, int(count.group(1).replace(",", "")) if count else 0)


def parse_coupon(text):
    """Coupon shown as 'Save 10% with coupon' or 'Save EGP 50.00 with coupon' -> ('%', 10) / ('EGP', 50)."""
    m = re.search(r"Save\s+([\d.]+)%[^<]{0,20}coupon", text, re.I)
    if m:
        return ("%", float(m.group(1)))
    m = re.search(r"Save\s+EGP\s*([\d,.]+)[^<]{0,20}coupon", text, re.I)
    if m:
        return ("EGP", money(m.group(1)))
    return None


def after_coupon(price, coupon):
    if not coupon:
        return price
    kind, value = coupon
    return price * (1 - value / 100) if kind == "%" else max(price - value, 0)


def section_text(page, element_id, length=4000):
    """Visible text of the page section that starts at the element with this id."""
    i = page.find(f'id="{element_id}"')
    if i < 0:
        return ""
    start = page.find(">", i) + 1
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page[start:start + length])).split())


def parse_seller(page):
    """(seller name, fulfilled by Amazon?) from a product page."""
    m = re.search(r"Sold by\s+(.{1,40}?)(?=\s+(?:Returns|Payment|Ships|Fulfilled|Customer|See more|Sold)\b|$)",
                  section_text(page, "merchantInfoFeature_feature_div"))
    seller = m.group(1).split() if m else []
    half = len(seller) // 2
    if half and seller[:half] == seller[half:]:  # Amazon renders the name twice
        seller = seller[:half]
    return " ".join(seller) or None, "Amazon" in section_text(page, "fulfillerInfoFeature_feature_div", 3000)


def fetch(asin):
    """Return a dict with title, price, image, rating, reviews, seller, fba, coupon, or raise
    on a blocked/failed request. price is None if unavailable."""
    page = get_page(f"https://www.amazon.eg/dp/{asin}", {"language": "en_AE"}, expect="productTitle")
    m = re.search(r'<span id="productTitle"[^>]*>\s*([^<]+?)\s*</span>', page)
    tag = re.search(r'<img[^>]*id="landingImage"[^>]*>', page)
    img = re.search(r'\ssrc="([^"]+)"', tag.group(0)) if tag else None
    i = page.find('id="averageCustomerReviews"')
    rating, reviews = parse_rating(page[i:i + 3000]) if i >= 0 else (None, 0)
    seller, fba = parse_seller(page)
    i = page.find('couponsInBuybox')
    return {
        "title": html.unescape(m.group(1)) if m else asin,
        "price": parse_price(page),
        "image": image_url(img.group(1) if img else None),
        "rating": rating,
        "reviews": reviews,
        "seller": seller,
        "fba": fba,
        "coupon": parse_coupon(page[i:i + 4000]) if i >= 0 else None,
    }


def details_lines(d):
    """Rating and seller lines for an alert."""
    lines = []
    if d.get("rating"):
        lines.append(f"⭐ {d['rating']:.1f} من 5 ({d['reviews']:,} تقييم)")
    else:
        lines.append("⭐ لسه مالوش تقييمات")
    seller = d.get("seller")
    if seller and seller.lower().startswith("amazon"):
        lines.append("🏪 البياع: أمازون نفسه ✅")
    elif seller:
        lines.append(f"🏪 البياع: {seller}" + (" (الشحن من أمازون)" if d.get("fba") else " ⚠️ بياع خارجي"))
    return "\n".join(lines)


def too_poorly_rated(d):
    return d.get("rating") is not None and d.get("reviews", 0) > 0 and d["rating"] < MIN_RATING


# ---------- products you picked (products.txt) ----------

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
        target = money(parts[1]) if len(parts) > 1 else None
        products.append((asin.group(1), target))
    return products


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
    products = read_products() if PRODUCTS_FILE.exists() else []

    for n, (asin, target) in enumerate(products):
        if n:
            time.sleep(random.uniform(4, 9))  # be gentle, avoid looking like a bot
        try:
            d = fetch(asin)
        except Exception as e:
            print(f"{asin}: failed ({e})")
            continue

        entry = state.setdefault(asin, {"history": []})
        entry["title"] = d["title"]
        if d["price"] is None:
            print(f"{asin}: no price (out of stock?)")
            continue

        price = after_coupon(d["price"], d["coupon"])
        last = entry.get("last")
        lowest = entry.get("lowest")
        reason = alert_reason(price, last, lowest, target)
        print(f"{asin}: {price:,.2f} EGP (last: {last}, target: {target})" + (f" -> ALERT: {reason}" if reason else ""))

        if reason:
            old = f"\nكان: {last:,.2f} جنيه" if last else ""
            notify(
                asin,
                price,
                f"🔔 {reason}\n\n{d['title'][:120]}\n\n"
                f"السعر دلوقتي: {price:,.2f} جنيه{coupon_line(d['price'], d['coupon'])}{old}\n"
                f"{details_lines(d)}\n"
                f"https://www.amazon.eg/dp/{asin}",
                d["image"],
                d["title"],
                100 * (last - price) / last if last else 0,
            )

        entry["last"] = price
        entry["lowest"] = min(price, lowest) if lowest else price
        if not entry["history"] or entry["history"][-1][1] != price:
            entry["history"] = (entry["history"] + [[now, price]])[-100:]

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def coupon_line(shelf_price, coupon):
    if not coupon:
        return ""
    label = f"{coupon[1]:.0f}%" if coupon[0] == "%" else f"{coupon[1]:,.0f} جنيه"
    return f" (بعد كوبون {label}، من غير الكوبون {shelf_price:,.2f})"


# ---------- searches (searches.txt) ----------

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
    """Return one dict per product on a page of amazon.eg search results: asin, title, price,
    listed (struck-through "was" price or None), image, rating, reviews, coupon."""
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
        listed = re.search(r'<span class="a-price a-text-price"[^>]*>\s*<span class="a-offscreen">EGP\W*([\d,.]+)<', block)
        img = re.search(r'<img[^>]*class="s-image"[^>]*\ssrc="([^"]+)"', block)
        rating, reviews = parse_rating(block)
        results.append({
            "asin": asin,
            "title": html.unescape(re.sub(r"<[^>]+>", "", h2.group(1)).strip()) if h2 else asin,
            "price": money(price.group(1)),
            "listed": money(listed.group(1)) if listed else None,
            "image": image_url(img.group(1) if img else None),
            "rating": rating,
            "reviews": reviews,
            "coupon": parse_coupon(block),
        })
    return results


def discover():
    """Scan the searches, record prices and send deals. Returns (pages ok, pages failed, products tracked)."""
    searches = read_searches()
    if not searches:
        return 0, 0, 0
    state = json.loads(DISCOVER_FILE.read_text(encoding="utf-8")) if DISCOVER_FILE.exists() else {}
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    candidates = []
    seen = set()
    ok = failed = 0

    for words, pages in searches:
        for page in range(1, pages + 1):
            if ok or failed:
                time.sleep(random.uniform(5, 10))
            try:
                results = fetch_search(words, page)
            except Exception as e:
                print(f"search '{words}' page {page}: failed ({e})")
                failed += 1
                break
            ok += 1
            print(f"search '{words}' page {page}: {len(results)} products")
            for r in results:
                asin, price = r["asin"], r["price"]
                if asin in seen:  # same product can show up in several searches
                    continue
                seen.add(asin)
                entry = state.setdefault(asin, {"prices": []})
                entry["title"] = r["title"]
                entry["seen"] = today
                earlier = entry["prices"]
                daily = [d for d in entry.get("daily", []) if d[0] >= month_ago]
                entry.pop("listed_alerted", None)  # left over from older versions

                # What you'd actually pay: the price after any coupon.
                pay = after_coupon(price, r["coupon"])
                # Discount Amazon advertises against its struck-through price.
                listed = r["listed"]
                off = 100 * (listed - pay) / listed if listed and listed > pay else 0

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
                    drop = 100 * (usual - pay) / usual
                    # A struck-through price alone can be fake (a "was" price the product never
                    # sold at), so an advertised discount only counts if the price really fell
                    # below what we've been seeing.
                    real = drop >= DISCOVER_DROP or (off >= LISTED_DISCOUNT and drop >= LISTED_VERIFY)
                    if real:
                        # Alert once per low price; a further drop alerts again.
                        if entry.get("alerted") is None or pay < entry["alerted"]:
                            entry["alerted"] = pay
                            if pay < MIN_PRICE:
                                print(f"{asin}: deal under {MIN_PRICE:.0f} EGP, skipping")
                            elif too_poorly_rated(r):
                                print(f"{asin}: rated {r['rating']}, skipping")
                            else:
                                lowest = len(past_days) >= MIN_DAYS and pay < min(past_days)
                                candidates.append({**r, "pay": pay, "usual": usual, "drop": drop,
                                                   "off": off, "lowest": lowest})
                    else:
                        entry.pop("alerted", None)
                entry["prices"] = (earlier + [price])[-30:]
                # Keep the day's latest price, one entry per day.
                if daily and daily[-1][0] == today:
                    daily[-1][1] = price
                else:
                    daily.append([today, price])
                entry["daily"] = daily

    print(f"discovery: tracking {len(state)} products, {len(candidates)} candidate deal(s)")

    candidates.sort(key=lambda c: c["drop"], reverse=True)  # biggest drops first
    candidates = [c for c in candidates if c["asin"] not in IGNORED and not already_sent(c["asin"], c["pay"])]
    for c in candidates[:MAX_ALERTS_PER_RUN]:
        # One extra page per deal for the seller (and a rating if the search had none).
        try:
            time.sleep(random.uniform(4, 8))
            d = fetch(c["asin"])
            c["seller"], c["fba"] = d["seller"], d["fba"]
            if c["rating"] is None:
                c["rating"], c["reviews"] = d["rating"], d["reviews"]
        except Exception as e:
            print(f"{c['asin']}: couldn't load product page for seller ({e})")
        if too_poorly_rated(c):
            print(f"{c['asin']}: rated {c['rating']}, skipping")
            continue
        if notify(c["asin"], c["pay"], deal_text(c), c["image"], c["title"], c["drop"]) == "muted":
            state[c["asin"]].pop("alerted", None)  # send it after the mute ends if it's still on

    cutoff = (now - timedelta(days=FORGET_AFTER_DAYS)).strftime("%Y-%m-%d")
    state = {a: e for a, e in state.items() if e.get("seen", today) >= cutoff}
    DISCOVER_FILE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return ok, failed, len(state)


def deal_text(c):
    lines = [f"🔥 نزل فعلاً {c['drop']:.0f}% عن سعره المعتاد"]
    if c["lowest"]:
        lines.append("📉 أقل سعر في آخر 30 يوم")
    lines += ["", c["title"][:120], ""]
    lines.append(f"السعر دلوقتي: {c['pay']:,.2f} جنيه{coupon_line(c['price'], c['coupon'])}")
    lines.append(f"سعره المعتاد: {c['usual']:,.2f} جنيه")
    if c["off"] >= LISTED_DISCOUNT:
        lines.append(f"🏷️ أمازون كاتب خصم {c['off']:.0f}% (من {c['listed']:,.2f} جنيه)")
    lines.append(details_lines(c))
    lines.append(f"https://www.amazon.eg/dp/{c['asin']}")
    return "\n".join(lines)


# ---------- health warning and daily summary ----------

def check_health(ok, failed):
    """Warn once when Amazon blocks most searches for several runs in a row, and say when it recovers."""
    health = json.loads(HEALTH_FILE.read_text(encoding="utf-8")) if HEALTH_FILE.exists() else {}
    total = ok + failed
    bad = total > 0 and failed > total / 2
    health["bad_streak"] = health.get("bad_streak", 0) + 1 if bad else 0
    if health["bad_streak"] >= BAD_RUNS_BEFORE_WARNING and not health.get("warned"):
        send(f"⚠️ البوت فيه مشكلة: أمازون رفض أغلب صفحات البحث في آخر {health['bad_streak']} تشغيلات "
             f"({failed} من {total} المرة دي). البوت هيفضل يحاول، وهقولك أول ما يرجع يشتغل.")
        health["warned"] = True
    elif not bad and total and health.get("warned"):
        send("✅ البوت رجع يشتغل طبيعي.")
        health["warned"] = False
    health["last_run"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    health["last_pages"] = [ok, failed]
    HEALTH_FILE.write_text(json.dumps(health), encoding="utf-8")


def daily_summary(tracked):
    """Once a day after SUMMARY_HOUR (local time): the day's best deals and how the bot is doing."""
    local = datetime.now()
    today = local.strftime("%Y-%m-%d")
    if local.hour < SUMMARY_HOUR or SETTINGS.get("last_summary") == today:
        return
    utc_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    deals = sorted((v for v in SENT.values() if v[1] == utc_today and len(v) >= 4),
                   key=lambda v: v[3], reverse=True)
    lines = [f"📋 ملخص النهارده ({today})", ""]
    if deals:
        lines.append(f"اتبعتلك {len(deals)} عرض. أحسنهم:")
        for price, _, title, drop in deals[:5]:
            lines.append(f"• {title[:60]}: {price:,.0f} جنيه (‎-{drop}%)")
    else:
        lines.append("مفيش عروض قوية النهارده.")
    lines += ["", f"📦 بتابع {tracked:,} منتج"]
    if muted():
        lines.append("🔕 التنبيهات متوقفة دلوقتي")
    if send("\n".join(lines)):
        SETTINGS["last_summary"] = today


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
        load_shared()
        try:
            check()
            ok, failed, tracked = discover()
            check_health(ok, failed)
            daily_summary(tracked)
        finally:
            save_shared()
