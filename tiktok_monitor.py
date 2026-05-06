#!/usr/bin/env python3
"""
TikTok Follower Monitor - مراقب متابعي تيك توك
يراقب حساب تيك توك ويرسل تنبيهات عبر واتساب عند تغيير أعداد المتابعين
"""

import sqlite3
import json
import time
import re
import logging
import os
import requests
from datetime import datetime
from typing import Optional

# ─── الإعدادات ────────────────────────────────────────────────────────────────
CONFIG = {
    "tiktok_username": "haneen5928",
    "notification_method": "twilio",
    "twilio": {
        "account_sid": os.environ.get("TWILIO_SID"),
        "auth_token":  os.environ.get("TWILIO_TOKEN"),
        "from_number": "whatsapp:+14155238886",
        "to_number":   os.environ.get("TWILIO_TO"),
    },
    "interval_minutes": 10,
    "db_path": "tiktok_monitor.db",
}
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("tiktok_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. قاعدة البيانات
# ═══════════════════════════════════════════════════════════════════════════════
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL,
            followers   INTEGER NOT NULL,
            following   INTEGER NOT NULL,
            likes       INTEGER,
            videos      INTEGER,
            recorded_at TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            metric      TEXT NOT NULL,
            old_value   INTEGER,
            new_value   INTEGER,
            direction   TEXT,
            sent_at     TEXT NOT NULL
        )
    """)
    conn.commit()
    log.info("✅ قاعدة البيانات جاهزة: %s", db_path)
    return conn


def save_snapshot(conn, username, followers, following, likes=0, videos=0):
    conn.execute(
        "INSERT INTO snapshots (username,followers,following,likes,videos,recorded_at) VALUES (?,?,?,?,?,?)",
        (username, followers, following, likes, videos, datetime.now().isoformat()),
    )
    conn.commit()


def get_last_snapshot(conn, username) -> Optional[dict]:
    row = conn.execute(
        "SELECT followers,following,likes,videos FROM snapshots WHERE username=? ORDER BY id DESC LIMIT 1",
        (username,),
    ).fetchone()
    if row:
        return {"followers": row[0], "following": row[1], "likes": row[2], "videos": row[3]}
    return None


def log_alert(conn, username, metric, old_val, new_val, direction):
    conn.execute(
        "INSERT INTO alerts (username,metric,old_value,new_value,direction,sent_at) VALUES (?,?,?,?,?,?)",
        (username, metric, old_val, new_val, direction, datetime.now().isoformat()),
    )
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. جلب بيانات تيك توك (بدون Playwright)
# ═══════════════════════════════════════════════════════════════════════════════
# هيدرات متعددة لتجنب الحجب
HEADERS_LIST = [
    {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.tiktok.com/",
        "Connection": "keep-alive",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    },
]


def fetch_tiktok_stats(username: str) -> Optional[dict]:
    """يجلب إحصائيات تيك توك بثلاث طرق مختلفة كـ fallback."""

    log.info("🔍 جلب بيانات: https://www.tiktok.com/@%s", username)

    # الطريقة 1 — API داخلي
    result = _try_internal_api(username)
    if result:
        return result

    # الطريقة 2 — HTML مباشر مع regex
    result = _try_html_scrape(username)
    if result:
        return result

    # الطريقة 3 — API بديل
    result = _try_alt_api(username)
    if result:
        return result

    log.warning("⚠️  فشلت جميع طرق الجلب لـ @%s", username)
    return None


def _try_internal_api(username: str) -> Optional[dict]:
    """الطريقة 1: API داخلي لتيك توك."""
    try:
        session = requests.Session()
        headers = HEADERS_LIST[0]

        # زيارة الصفحة الرئيسية أولاً للحصول على كوكيز
        session.get("https://www.tiktok.com", headers=headers, timeout=10)
        time.sleep(1)

        url = f"https://www.tiktok.com/api/user/detail/?uniqueId={username}&aid=1988&app_language=en&count=30"
        resp = session.get(url, headers=headers, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            stats = data.get("userInfo", {}).get("stats", {})
            if stats and stats.get("followerCount", 0) > 0:
                log.info("✅ تم الجلب عبر Internal API")
                return {
                    "username":  username,
                    "followers": stats.get("followerCount", 0),
                    "following": stats.get("followingCount", 0),
                    "likes":     stats.get("heartCount", 0),
                    "videos":    stats.get("videoCount", 0),
                }
    except Exception as e:
        log.debug("Internal API failed: %s", e)
    return None


def _try_html_scrape(username: str) -> Optional[dict]:
    """الطريقة 2: جلب HTML وتحليله بـ regex."""
    for i, headers in enumerate(HEADERS_LIST):
        try:
            url = f"https://www.tiktok.com/@{username}"
            resp = requests.get(url, headers=headers, timeout=20)

            if resp.status_code != 200:
                continue

            html = resp.text

            # بحث في JSON المضمَّن
            m_f  = re.search(r'"followerCount"\s*:\s*(\d+)', html)
            m_fw = re.search(r'"followingCount"\s*:\s*(\d+)', html)
            m_l  = re.search(r'"heartCount"\s*:\s*(\d+)', html)
            m_v  = re.search(r'"videoCount"\s*:\s*(\d+)', html)

            if m_f and m_fw and int(m_f.group(1)) > 0:
                log.info("✅ تم الجلب عبر HTML scrape (headers set %d)", i+1)
                return {
                    "username":  username,
                    "followers": int(m_f.group(1)),
                    "following": int(m_fw.group(1)),
                    "likes":     int(m_l.group(1)) if m_l else 0,
                    "videos":    int(m_v.group(1)) if m_v else 0,
                }

            time.sleep(2)

        except Exception as e:
            log.debug("HTML scrape attempt %d failed: %s", i+1, e)

    return None


def _try_alt_api(username: str) -> Optional[dict]:
    """الطريقة 3: API بديل غير رسمي."""
    try:
        url = f"https://tiktok-scraper7.p.rapidapi.com/user/info?unique_id={username}"
        headers = {
            "x-rapidapi-host": "tiktok-scraper7.p.rapidapi.com",
            "x-rapidapi-key": os.environ.get("RAPIDAPI_KEY", ""),
        }
        if not headers["x-rapidapi-key"]:
            return None

        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            stats = data.get("data", {}).get("stats", {})
            if stats:
                log.info("✅ تم الجلب عبر RapidAPI")
                return {
                    "username":  username,
                    "followers": stats.get("followerCount", 0),
                    "following": stats.get("followingCount", 0),
                    "likes":     stats.get("heartCount", 0),
                    "videos":    stats.get("videoCount", 0),
                }
    except Exception as e:
        log.debug("Alt API failed: %s", e)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. الإشعارات
# ═══════════════════════════════════════════════════════════════════════════════
def send_notification(message: str) -> bool:
    method = CONFIG["notification_method"]

    if method == "console":
        print("\n" + "═"*55)
        print("🔔  تنبيه تيك توك")
        print("═"*55)
        print(message)
        print("═"*55 + "\n")
        return True

    elif method == "twilio":
        return _send_twilio(message)

    log.warning("طريقة إشعار غير معروفة: %s", method)
    return False


def _send_twilio(message: str) -> bool:
    try:
        from twilio.rest import Client
        cfg = CONFIG["twilio"]

        if not cfg["account_sid"] or not cfg["auth_token"]:
            log.error("❌ بيانات Twilio غير موجودة — تحقق من Environment Variables")
            return False

        client = Client(cfg["account_sid"], cfg["auth_token"])
        client.messages.create(
            body=message,
            from_=cfg["from_number"],
            to=cfg["to_number"],
        )
        log.info("✅ تم إرسال رسالة واتساب عبر Twilio")
        return True
    except ImportError:
        log.error("❌ twilio غير مثبَّت: pip install twilio")
    except Exception as e:
        log.error("❌ فشل إرسال Twilio: %s", e)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. منطق المقارنة والتنبيه
# ═══════════════════════════════════════════════════════════════════════════════
def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def check_and_alert(conn, current: dict) -> None:
    username = current["username"]
    prev = get_last_snapshot(conn, username)

    if prev is None:
        log.info("📝 أول تسجيل للحساب @%s", username)
        save_snapshot(conn, username,
                      current["followers"], current["following"],
                      current["likes"], current["videos"])
        msg = (
            f"🔔 بدأ مراقبة @{username}\n"
            f"👥 متابعون: {format_number(current['followers'])}\n"
            f"➡️  يتابع: {format_number(current['following'])}\n"
            f"❤️  إعجابات: {format_number(current['likes'])}\n"
            f"🎬 فيديوهات: {current['videos']}\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        send_notification(msg)
        return

    alerts = []
    metrics = [
        ("followers", "متابعون", "👥"),
        ("following", "يتابع",   "➡️"),
    ]

    for key, label, icon in metrics:
        old_val = prev[key]
        new_val = current[key]
        diff    = new_val - old_val

        if diff == 0:
            log.info("%s %s: لا تغيير (%s)", icon, label, format_number(new_val))
            continue

        direction  = "زيادة ⬆️" if diff > 0 else "نقص ⬇️"
        change_str = f"+{format_number(abs(diff))}" if diff > 0 else f"-{format_number(abs(diff))}"
        emoji      = "⬆️" if diff > 0 else "⬇️"

        msg_line = (
            f"{emoji} {label}: {direction}\n"
            f"   السابق: {format_number(old_val)}\n"
            f"   الحالي: {format_number(new_val)}\n"
            f"   التغيير: {change_str}"
        )
        alerts.append(msg_line)
        log_alert(conn, username, key, old_val, new_val, direction)
        log.info("🚨 تغيير في %s: %s", label, direction)

    if alerts:
        full_msg = (
            f"🔔 Updating — @{username}\n"
            f"{'─'*35}\n"
            + "\n\n".join(alerts) +
            f"\n{'─'*35}\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        send_notification(full_msg)
    else:
        log.info("✅ لا تغييرات في @%s", username)

    save_snapshot(conn, username,
                  current["followers"], current["following"],
                  current["likes"], current["videos"])


# ═══════════════════════════════════════════════════════════════════════════════
# 5. الحلقة الرئيسية
# ═══════════════════════════════════════════════════════════════════════════════
def run_once(conn) -> None:
    username = CONFIG["tiktok_username"]
    log.info("▶️  فحص حساب @%s ...", username)

    stats = fetch_tiktok_stats(username)
    if stats:
        log.info("📊 followers=%s | following=%s | likes=%s | videos=%s",
                 format_number(stats["followers"]),
                 format_number(stats["following"]),
                 format_number(stats["likes"]),
                 stats["videos"])
        check_and_alert(conn, stats)
    else:
        log.warning("⚠️  تعذّر جلب بيانات @%s في هذا الفحص.", username)


def main():
    log.info("🚀 بدء مراقب تيك توك — @%s", CONFIG["tiktok_username"])
    conn = init_db(CONFIG["db_path"])
    interval_sec = CONFIG["interval_minutes"] * 60

    try:
        while True:
            run_once(conn)
            log.info("⏳ الفحص القادم بعد %d دقيقة ...", CONFIG["interval_minutes"])
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        log.info("🛑 تم إيقاف المراقب.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
