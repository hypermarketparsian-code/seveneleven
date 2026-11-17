# -*- coding: utf-8 -*-
"""
پایش ابری موجودی اسنپ‌مارکت — نسخه بدون مرورگر (برای اجرا روی GitHub Actions)
همان منطق برنامه محلی: واکشی همه محصولات فروشگاه، مقایسه با آخرین وضعیت،
ثبت تغییرات (کسری/افزایش/قیمت) و ارسال پیام به روبیکا/بله.

نتیجه در data/history.json ذخیره می‌شود تا برنامه محلی بعد از روشن شدن
کامپیوتر، تغییرات زمان خاموشی را از همان فایل بازخوانی کند.

تنظیمات فقط با متغیرهای محیطی (Environment variables / GitHub Secrets):
  SNAPP_VENDOR_CODE  کد فروشگاه            (پیش‌فرض 3eeqyo = هایپر سون الون)
  RUBIKA_TOKEN / RUBIKA_CHAT   ربات روبیکا  (اختیاری)
  BALE_TOKEN  / BALE_CHAT      ربات بله     (اختیاری)
  DATA_DIR           پوشه داده             (پیش‌فرض data کنار همین اسکریپت)
  HISTORY_MAX        حداکثر تعداد رکورد تغییرات در فایل ابری (پیش‌فرض 3000)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

SVC = "https://svc.snapp.market"
BOTAPI = "https://botapi.rubika.ir/v3"
BALEAPI = "https://api.bale.ai/bot"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
PAGE_SIZE = 200
PAGE_PAUSE = 1.0
TAIL_COOL = 20

VENDOR_CODE = os.environ.get("SNAPP_VENDOR_CODE", "3eeqyo").strip() or "3eeqyo"
VENDOR_NAME = "هایپر سون الون"
RUBIKA_TOKEN = os.environ.get("RUBIKA_TOKEN", "").strip()
RUBIKA_CHAT = os.environ.get("RUBIKA_CHAT", "").strip()
BALE_TOKEN = os.environ.get("BALE_TOKEN", "").strip()
BALE_CHAT = os.environ.get("BALE_CHAT", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", "3000") or 3000)

FA = "۰۱۲۳۴۵۶۷۸۹"


def fa(n):
    return "".join(FA[int(c)] if c.isdigit() else c for c in str(n))


# ---------- تبدیل میلادی به شمسی (الگوریتم جلالی، دقیق برای سال‌های 1178 تا 1633 میلادی) ----------
def g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def jdate(ts):
    import datetime
    dt = datetime.datetime.utcfromtimestamp(ts / 1000.0 + 3.5 * 3600)  # ساعت ایران (UTC+3:30)
    jy, jm, jd = g2j(dt.year, dt.month, dt.day)
    return "%04d/%02d/%02d" % (jy, jm, jd)


def jtime(ts):
    import datetime
    dt = datetime.datetime.utcfromtimestamp(ts / 1000.0 + 3.5 * 3600)
    return "%02d:%02d" % (dt.hour, dt.minute)


# ---------- HTTP ----------
def http_get(url, tries=5, timeout=60):
    """با مکث‌های فزاینده تلاش می‌کند؛ خطای موقت شبکه/محدودیت سرعت را عبور می‌دهد"""
    err = None
    backoff = [1, 2, 4, 8, 15]
    for i in range(tries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "application/json, */*")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return 204, b""
            err = e
        except Exception as e:
            err = e
        if i < tries - 1:
            time.sleep(backoff[min(i, len(backoff) - 1)])
    raise RuntimeError("HTTP request failed (%s): %s" % (url, err))


def http_post_json(url, body, timeout=30):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- واکشی محصولات (همان منطق برنامه محلی) ----------
def fetch_page(p):
    url = SVC + "/express-search/product-list/vendor?vendor_code=" + urllib.parse.quote(VENDOR_CODE) + \
          "&page=" + str(p) + "&pageSize=" + str(PAGE_SIZE)
    code, body = http_get(url)
    if code == 204 or not body.strip():
        return None
    return json.loads(body.decode("utf-8"))


def _page_products(j, mp):
    """موارد PRODUCT یک صفحه را داخل map ادغام می‌کند؛ تعداد ردیف‌های برگشتی"""
    fr = (j.get("data") or {}).get("finalResult") or []
    for it in fr:
        if not it or it.get("type") != "PRODUCT":
            continue
        d = it.get("data") or {}
        if not d.get("title"):
            continue
        imgs = d.get("images") or []
        img = ""
        if imgs:
            img = imgs[0].get("main") or imgs[0].get("thumb") or ""
        rec = {"id": d["id"], "t": d["title"], "b": d.get("brand") or "",
               "pr": d.get("price") or 0, "s": 0 if d.get("stock") is None else d["stock"],
               "c": d.get("root_category_title") or "سایر", "img": img}
        mp[d["id"]] = rec
    return len(fr)


def _fetch_page_retry(p, waits):
    """دریافت یک صفحه با تلاش‌های مکث‌دار؛ None یعنی پاسخ خالی/خطا بعد از همه تلاش‌ها"""
    for wait in waits:
        if wait:
            time.sleep(wait)
        try:
            j = fetch_page(p)
            if j is not None:
                return j
        except Exception:
            pass
    return None


def fetch_one_round():
    """یک دور کامل واکشی همه صفحات؛ خروجی: (map, count, ended)
    پاسخ خالی فقط وقتی «پایان واقعی لیست» شمرده می‌شود که صفحه آخرین داده ناقص بوده باشد؛
    برای اطمینان، دم لیست با مکث دوباره استعلام می‌شود تا بریدگیِ ناشی از محدودیت سرعت
    به‌اشتباه پایان لیست فرض نشود (صفحه بریده‌شده دوباره کامل می‌گیرد)."""
    j1 = _fetch_page_retry(1, (0, 5, 15, 30, 60))
    if not j1 or not j1.get("data"):
        raise RuntimeError("invalid response from snapp market")
    count = j1["data"].get("count") or 0
    total = max(1, -(-count // PAGE_SIZE))
    mp = {}
    last_p, last_n = None, 0
    p = 1
    while p <= total:
        j = _fetch_page_retry(p, (0, 5, 15, 30, 60))
        if j is None:
            if p == 1:
                raise RuntimeError("page 1 empty")
            if last_n < PAGE_SIZE:
                # احتمال پایان واقعی لیست — تایید نهایی با مکث و استعلام مجدد
                time.sleep(TAIL_COOL)
                again = _fetch_page_retry(p, (0, 10))
                if again is None:
                    if last_p is not None:
                        j3 = _fetch_page_retry(last_p, (0, 10))
                        if j3 is not None and _page_size(j3) > last_n:
                            # صفحه آخر بریده شده بود — کاملش را می‌گیریم و ادامه می‌دهیم
                            _page_products(j3, mp)
                            last_n = _page_size(j3)
                            p += 1
                            continue
                    return mp, count, True  # پایان واقعی لیست
                j = again
            else:
                raise RuntimeError("empty response on page %d while previous page was full (throttle?)" % p)
        last_n = _page_products(j, mp)
        last_p = p
        p += 1
        time.sleep(PAGE_PAUSE)
    return mp, count, False


def _page_size(j):
    return len(((j.get("data") or {}).get("finalResult")) or [])


def fetch_all():
    """واکشی کامل لیست؛ برای پوشش بالاتر (وقتی API پاسخ‌های ناقص/چرخشی می‌دهد)
    دو دور کامل گرفته و ادغام می‌شود. اگر لیست هیچ‌وقت «پایان» نیامد، خطا برمی‌گردد
    تا وضعیت ناقص هرگز ذخیره نشود (اجرای بعدی زمان‌بند دوباره تلاش می‌کند)."""
    mp = {}
    last_count = 0
    for rnd in (1, 2):
        m, count, ended = fetch_one_round()
        last_count = count
        mp.update(m)
        print("[cloud] round %d: seen=%d union=%d (count=%s ended=%s)" % (rnd, len(m), len(mp), count, ended))
        if ended and count and len(mp) >= count * 0.97:
            break
        time.sleep(2)
    if last_count and len(mp) < last_count * 0.5:
        raise RuntimeError("incomplete fetch: only %d/%d" % (len(mp), last_count))
    return mp, len(mp), last_count


# ---------- ذخیره/بارگذاری وضعیت ----------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


# ---------- ارسال پیام ----------
def send_rubika(text):
    if not RUBIKA_TOKEN or not RUBIKA_CHAT:
        return "روبیکا: تنظیم نشده"
    try:
        http_post_json(BOTAPI + "/" + RUBIKA_TOKEN + "/sendMessage",
                       {"chat_id": RUBIKA_CHAT, "text": text})
        return "روبیکا ✅"
    except Exception as e:
        return "روبیکا ❌ " + str(e)


def send_bale(text):
    if not BALE_TOKEN or not BALE_CHAT:
        return "بله: تنظیم نشده"
    try:
        http_post_json(BALEAPI + BALE_TOKEN + "/sendMessage",
                       {"chat_id": BALE_CHAT, "text": text})
        return "بله ✅"
    except Exception as e:
        return "بله ❌ " + str(e)


def build_changes_text(items, prices, ts):
    L = []
    L.append("📦 گزارش تغییرات موجودی — " + VENDOR_NAME + " (اسنپ‌مارکت)")
    L.append("📅 تاریخ: " + jdate(ts) + " — 🕐 ساعت: " + jtime(ts) + "  (پایش ابری)")
    dec = [i for i in items if i[2] < i[1]]
    inc = [i for i in items if i[2] > i[1]]
    if dec:
        L.append("")
        L.append("⬇️ کسری (" + fa(len(dec)) + " مورد):")
        for it in dec[:30]:
            L.append("• " + it[3] + " — کسر شده: " + fa(it[1] - it[2]) + " عدد — باقی‌مانده نهایی: " + fa(it[2]) + " عدد")
        if len(dec) > 30:
            L.append("… و " + fa(len(dec) - 30) + " مورد دیگر")
    if inc:
        L.append("")
        L.append("⬆️ افزایش (" + fa(len(inc)) + " مورد):")
        for it in inc[:30]:
            L.append("• " + it[3] + " — افزایش: " + fa(it[2] - it[1]) + " عدد — موجودی فعلی: " + fa(it[2]) + " عدد")
        if len(inc) > 30:
            L.append("… و " + fa(len(inc) - 30) + " مورد دیگر")
    if prices:
        L.append("")
        L.append("💱 تغییر قیمت (" + fa(len(prices)) + " مورد):")
        for it in prices[:20]:
            L.append("• " + it[3] + " — از " + fa(it[1]) + " به " + fa(it[2]) + " تومان")
        if len(prices) > 20:
            L.append("… و " + fa(len(prices) - 20) + " مورد دیگر")
    return "\n".join(L)


# ---------- اجرای اصلی ----------
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    state_path = os.path.join(DATA_DIR, "state.json")
    hist_path = os.path.join(DATA_DIR, "history.json")
    state = load_json(state_path, {"lastRefresh": 0, "products": {}})
    hist = load_json(hist_path, {"v": 2, "vendor_code": VENDOR_CODE, "vendor_name": VENDOR_NAME,
                                 "lastRefresh": 0, "products": {}, "changes": []})
    prev = state.get("products") or {}
    first_run = len(prev) == 0

    print("[cloud] fetching vendor %s (%s) ..." % (VENDOR_CODE, VENDOR_NAME))
    mp, seen, count = fetch_all()
    print("[cloud] fetched %d products (api count=%s)" % (seen, count))

    now = int(time.time() * 1000)
    items, prices = [], []
    for pid, p in mp.items():
        o = prev.get(pid)
        if o:
            p["miss"] = 0
            if not o.get("del"):
                if o.get("s") != p["s"]:
                    items.append([pid, o.get("s"), p["s"], p["t"]])
                if o.get("pr") != p["pr"]:
                    prices.append([pid, o.get("pr"), p["pr"], p["t"]])
        p["del"] = 0
    for pid, o in prev.items():
        if pid not in mp:
            q = dict(o)
            # شمارنده غیبت: محصول فقط بعد از غیبت در دو اجرای پیاپی حذف‌شده علامت می‌خورد
            # تا ناپایداری موقت لیست، فلیکر حذف/برگشت ایجاد نکند
            m = (o.get("miss") or 0) + 1
            q["miss"] = m
            q["del"] = 1 if (o.get("del") or m >= 2) else 0
            mp[pid] = q

    base = first_run or not state.get("lastRefresh")
    event = {"ts": now, "n": [[a, b, c] for a, b, c, _ in items], "p": [[a, b, c] for a, b, c, _ in prices], "base": base}
    hist.setdefault("changes", [])
    hist["changes"].insert(0, event)
    hist["changes"] = hist["changes"][:HISTORY_MAX]
    hist["products"] = mp
    hist["lastRefresh"] = now
    hist["vendor_code"] = VENDOR_CODE
    hist["vendor_name"] = VENDOR_NAME
    hist["v"] = 2
    state["products"] = mp
    state["lastRefresh"] = now
    save_json(state_path, state)
    save_json(hist_path, hist)

    if base:
        print("[cloud] base snapshot saved (first run) — no notifications")
        return 0
    if not items and not prices:
        print("[cloud] no changes")
        return 0
    print("[cloud] changes: stock=%d price=%d" % (len(items), len(prices)))
    text = build_changes_text(items, prices, now)
    r1 = send_rubika(text)
    r2 = send_bale(text)
    print("[cloud] notify ->", r1, "|", r2)
    if "تنظیم نشده" in r1 and "تنظیم نشده" in r2:
        print("[cloud] WARNING: no messenger configured (set RUBIKA_TOKEN/RUBIKA_CHAT or BALE_TOKEN/BALE_CHAT secrets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
