#!/usr/bin/env python3
"""Donanım Arşivi 'Sıcak Fırsatlar' -> Telegram bildirim botu.

Kullanım:
  pip install requests
  export TELEGRAM_BOT_TOKEN="123456:ABC..."
  export TELEGRAM_CHAT_ID="123456789"
  python firsat_bot.py          # bir kez kontrol eder (cron için)
  python firsat_bot.py --loop   # sürekli çalışır, 2 dakikada bir kontrol eder
"""
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://forum.donanimarsivi.com"
FORUM_URL = f"{BASE}/forumlar/Sicakfirsatlar/"
RSS_URL = FORUM_URL + "index.rss"
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
INTERVAL = 120  # saniye (--loop modunda)

# Sadece bu kelimeleri içeren başlıklar gelsin istersen doldur.
# Örn: ["rtx", "ssd", "monitör"]. Boşsa hepsi gelir.
KEYWORDS = []

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

ID_RE = re.compile(r"\.(\d+)/?(?:$|[?#])")


def thread_id(url):
    m = ID_RE.search(url)
    return m.group(1) if m else None


def fetch_rss():
    r = requests.get(RSS_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        tid = thread_id(link)
        if title and tid:
            items.append((tid, title, link))
    if not items:
        raise ValueError("RSS boş")
    return items


def fetch_html():
    """RSS çalışmazsa forum sayfasından konu linklerini ayıklar."""
    r = requests.get(FORUM_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    items, seen = [], set()
    pattern = re.compile(
        r'<a[^>]+href="(/konu/[^"]*?\.(\d+)/)"[^>]*>(.*?)</a>', re.S
    )
    for href, tid, inner in pattern.findall(r.text):
        title = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if not title or tid in seen:
            continue
        seen.add(tid)
        items.append((tid, title, BASE + href))
    return items


def fetch_threads():
    try:
        return fetch_rss()
    except Exception as e:  # noqa: BLE001
        print(f"[uyarı] RSS alınamadı ({e}), HTML'e geçiliyor", file=sys.stderr)
        return fetch_html()


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_seen(ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(ids[-500:], f)


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        },
        timeout=20,
    )
    r.raise_for_status()


def matches(title):
    if not KEYWORDS:
        return True
    t = title.lower()
    return any(k.lower() in t for k in KEYWORDS)


def check_once():
    threads = fetch_threads()
    seen = load_seen()

    # İlk çalıştırma: mevcutları kaydet, bildirim yağmuru olmasın.
    if seen is None:
        save_seen([t[0] for t in threads])
        send("✅ Fırsat botu aktif. Bundan sonra yeni fırsatlar buraya gelecek.")
        print("İlk çalıştırma: mevcut konular kaydedildi.")
        return

    new = [t for t in threads if t[0] not in seen]
    for tid, title, link in reversed(new):  # eskiden yeniye
        seen.append(tid)
        if matches(title):
            send(f'🔥 <b>{html.escape(title)}</b>\n{html.escape(link)}')
            time.sleep(1)
    if new:
        save_seen(seen)
    print(f"{len(new)} yeni konu bulundu.")


def main():
    if "--loop" in sys.argv:
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
