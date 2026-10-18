#!/usr/bin/env python3
"""Donanım Arşivi 'Sıcak Fırsatlar' -> Telegram bildirim botu.

Her yeni fırsat için: başlık, fiyat, ilk mesajdan kısa özet, mağaza linkleri,
Akakçe arama linki ve tıklanabilir butonlar gönderir.
"""
import html
import json
import os
import re
import sys
import time
from urllib.parse import quote_plus, urljoin, urlparse

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://forum.donanimarsivi.com"
WANTED_FORUM = "Sicakfirsatlar"
FORUM_URL = f"{BASE}/forumlar/{WANTED_FORUM}/"
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
INTERVAL = 120     # saniye (--loop modunda)
CHECK_EVERY = 30   # saniye (--duration modunda)

# --- Ayarlar -----------------------------------------------------------
# Sadece bu kelimelerden birini içeren başlıklar gelsin. Boşsa hepsi gelir.
# Örn: ["rtx", "ssd", "monitör"]
KEYWORDS = []
# Bu kelimelerden birini içeren başlıklar hiç gelmesin.
# Örn: ["mendil", "havlu"]
EXCLUDE_KEYWORDS = []
MAX_SUMMARY_CHARS = 400   # ilk mesajdan alınacak özet uzunluğu
MAX_LINKS = 4             # mesajda listelenecek mağaza linki sayısı
# -----------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

ITEM_SPLIT = re.compile(r'<div class="structItem structItem--thread')
TITLE_RE = re.compile(r'class="structItem-title"[^>]*>(.*?)</div>', re.S)
LINK_RE = re.compile(
    r'<a[^>]+href="([^"]*?/konu/[^"]*?\.(\d+)/?)"[^>]*>(.*?)</a>', re.S
)
FORUM_RE = re.compile(r'<li>\s*<a href="/forumlar/([^"/?#]+)[^"]*">')
LABEL_RE = re.compile(r'<span class="label[^"]*"[^>]*>(.*?)</span>', re.S)

ARTICLE_RE = re.compile(r'<article class="message message--post[^"]*"[^>]*>(.*?)</article>', re.S)
BODY_RE = re.compile(r'<div class="bbWrapper">(.*)', re.S)
EXT_LINK_RE = re.compile(r'<a [^>]*class="[^"]*link--external[^"]*"[^>]*href="([^"]+)"|'
                         r'<a [^>]*href="([^"]+)"[^>]*class="[^"]*link--external[^"]*"', re.S)
PRICE_RE = re.compile(r'(\d[\d.,]*)\s*(?:TL|tl|Tl|₺)|₺\s*(\d[\d.,]*)')
URL_RE = re.compile(r'https?://\S+')


# ---------------------------------------------------------------- liste ---
def parse_threads(page):
    """Sayfadaki konu satırlarını okur. Başka bölüme ait olanları atlar."""
    items, seen = [], set()
    for chunk in ITEM_SPLIT.split(page)[1:]:
        t = TITLE_RE.search(chunk)
        if not t:
            continue
        m = LINK_RE.search(t.group(1))
        if not m:
            continue
        href, tid, inner = m.groups()

        fm = FORUM_RE.search(chunk)
        if fm and fm.group(1).split(".")[0].lower() != WANTED_FORUM.lower():
            continue

        title = " ".join(html.unescape(re.sub(r"<[^>]+>", "", inner)).split())
        label = LABEL_RE.search(t.group(1))
        if label and title:
            title = f"{html.unescape(label.group(1)).strip()} {title}"
        if not title or tid in seen:
            continue
        seen.add(tid)
        items.append((tid, title, urljoin(BASE, href)))
    return items


def fetch_threads():
    r = requests.get(
        FORUM_URL,
        params={"order": "post_date", "direction": "desc"},
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    items = parse_threads(r.text)
    if not items:
        raise RuntimeError(
            "Konu listesi bulunamadi (sayfa yapisi degismis ya da erisim engellendi)"
        )
    return items


# ------------------------------------------------------------ konu sayfası ---
def parse_first_post(page):
    """İlk mesajdan (özet metin, dış linkler) döndürür."""
    art = ARTICLE_RE.search(page)
    if not art:
        return "", []
    b = BODY_RE.search(art.group(1))
    if not b:
        return "", []
    body = b.group(1)

    links = []
    for m in EXT_LINK_RE.finditer(body):
        url = html.unescape(m.group(1) or m.group(2)).strip()
        host = urlparse(url).netloc.lower()
        if url.startswith("http") and "donanimarsivi.com" not in host and url not in links:
            links.append(url)

    body = re.sub(r'<blockquote.*?</blockquote>', ' ', body, flags=re.S)  # alıntıları at
    body = re.sub(r'<br\s*/?>|</div>|</p>', '\n', body)
    text = html.unescape(re.sub(r'<[^>]+>', '', body))
    text = URL_RE.sub('', text)  # linkler ayrıca listeleniyor
    lines = [" ".join(l.split()) for l in text.splitlines()]
    text = "\n".join(l for l in lines if l)
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    return text, links


def fetch_post(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return parse_first_post(r.text)
    except Exception as e:  # noqa: BLE001
        print(f"[uyarı] konu sayfası okunamadı ({e})", file=sys.stderr)
        return "", []


# ------------------------------------------------------------ yardımcılar ---
def _esc(x):
    return html.escape(x, quote=False)


def find_price(title):
    m = PRICE_RE.search(title)
    if not m:
        return None
    num = m.group(1) or m.group(2)
    return f"{num.strip('.,')} TL"


def product_query(title):
    """Başlıktan Akakçe araması için ürün adı çıkarır."""
    t = LABEL_RE.sub("", title)
    t = re.sub(r'^\S*[İi]ndirim\S*\s*', '', t)          # '🔥İndirim' öneki
    m = PRICE_RE.search(t)
    if m and m.start() > 8:
        t = t[:m.start()]                                 # fiyattan öncesi
    t = re.split(r'\||(?i:\btaksit\b|\bkupon\b|\bsepette\b)', t)[0]
    t = re.sub(r'[^\w\s\-+."\'/%()]', ' ', t)           # emoji vb.
    return " ".join(t.split())[:80]


def akakce_url(title):
    q = product_query(title)
    return f"https://www.akakce.com/arama/?q={quote_plus(q)}" if q else None


def short_host(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def matches(title):
    t = title.lower()
    if EXCLUDE_KEYWORDS and any(k.lower() in t for k in EXCLUDE_KEYWORDS):
        return False
    if KEYWORDS and not any(k.lower() in t for k in KEYWORDS):
        return False
    return True


def build_message(title, link, summary, links):
    parts = [f"<b>{_esc(title)}</b>"]
    price = find_price(title)
    if price:
        parts.append(f"💰 {_esc(price)}")
    if summary:
        parts.append("\n" + _esc(summary))
    if links:
        shown = links[:MAX_LINKS]
        lines = [f"• {_esc(short_host(u))}: {_esc(u)}" for u in shown]
        if len(links) > MAX_LINKS:
            lines.append(f"(+{len(links) - MAX_LINKS} link daha, konuda)")
        parts.append("\n🔗 <b>Linkler</b>\n" + "\n".join(lines))
    text = "\n".join(parts)
    if len(text) > 3900:
        text = text[:3900] + "…"

    buttons = [{"text": "📄 Konu", "url": link}]
    if links:
        buttons.append({"text": f"🛒 {short_host(links[0])}"[:30], "url": links[0]})
    ak = akakce_url(title)
    if ak:
        buttons.append({"text": "🔎 Akakçe", "url": ak})
    return text, {"inline_keyboard": [buttons]}


# --------------------------------------------------------------- telegram ---
def send(text, reply_markup=None):
    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data, timeout=20
    )
    r.raise_for_status()


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_seen(ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(ids[-500:], f)


def check_once():
    threads = fetch_threads()
    seen = load_seen()

    if seen is None:  # ilk çalıştırma: bildirim yağmuru olmasın
        save_seen([t[0] for t in threads])
        send("✅ Fırsat botu aktif. Bundan sonra yeni fırsatlar buraya gelecek.")
        print("İlk çalıştırma: mevcut konular kaydedildi.")
        return

    new = [t for t in threads if t[0] not in seen]
    sent = 0
    for tid, title, link in reversed(new):  # eskiden yeniye
        if matches(title):
            summary, links = fetch_post(link)
            text, markup = build_message(title, link, summary, links)
            try:
                send(text, markup)
            except Exception as e:  # noqa: BLE001
                print(f"[hata] gönderilemedi, sonra tekrar denenecek: {e}", file=sys.stderr)
                continue  # seen'e eklenmez, bir sonraki turda tekrar dener
            sent += 1
            time.sleep(1)
        seen.append(tid)
    if new:
        save_seen(seen)
    print(f"{len(new)} yeni konu bulundu, {sent} bildirim gönderildi.")


def main():
    if "--duration" in sys.argv:
        secs = int(sys.argv[sys.argv.index("--duration") + 1])
        end = time.time() + secs
        while True:
            try:
                check_once()
            except Exception as e:  # noqa: BLE001
                print(f"[hata] {e}", file=sys.stderr)
            if time.time() + CHECK_EVERY > end:
                break
            time.sleep(CHECK_EVERY)
    elif "--loop" in sys.argv:
        while True:
            try:
                check_once()
            except Exception as e:  # noqa: BLE001
                print(f"[hata] {e}", file=sys.stderr)
            time.sleep(INTERVAL)
    else:
        check_once()


if __name__ == "__main__":
    main()
