#!/usr/bin/env python3
"""
TikTok Follower Monitor - مراقب متابعي تيك توك
يراقب حساب تيك توك ويرسل تنبيهات عبر واتساب/SMS عند تغيير أعداد المتابعين
"""

import sqlite3
import json
import time
import re
import logging
import os
from datetime import datetime
from typing import Optional, Tuple

# ─── الإعدادات ────────────────────────────────────────────────────────────────
CONFIG = {
    # اسم حساب تيك توك بدون @
    "tiktok_username": "haneen5928",

    # خيار الإشعارات: "twilio" أو "console" (للاختبار)
    "notification_method": "twilio",

    # إعدادات Twilio (أضفها إذا اخترت twilio)
    "twilio": {
    "account_sid": os.environ.get("TWILIO_SID"),
    "auth_token":  os.environ.get("TWILIO_TOKEN"),
    "from_number": "whatsapp:+14155238886",
    "to_number":   os.environ.get("TWILIO_TO"),
},

    # كم دقيقة بين كل فحص
    "interval_minutes": 10 ,

    # مسار قاعدة البيانات
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
    """إنشاء/فتح قاعدة البيانات وجداولها."""
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


def save_snapshot(conn: sqlite3.Connection, username: str,
                    followers: int, following: int,
                    likes: int = 0, videos: int = 0) -> None:
    conn.execute(
        "INSERT INTO snapshots (username,followers,following,likes,videos,recorded_at) VALUES (?,?,?,?,?,?)",
        (username, followers, following, likes, videos, datetime.now().isoformat()),
    )
    conn.commit()


def get_last_snapshot(conn: sqlite3.Connection, username: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT followers,following,likes,videos FROM snapshots WHERE username=? ORDER BY id DESC LIMIT 1",
        (username,),
    ).fetchone()
    if row:
        return {"followers": row[0], "following": row[1], "likes": row[2], "videos": row[3]}
    return None


def log_alert(conn: sqlite3.Connection, username: str, metric: str,
                old_val: int, new_val: int, direction: str) -> None:
    conn.execute(
        "INSERT INTO alerts (username,metric,old_value,new_value,direction,sent_at) VALUES (?,?,?,?,?,?)",
        (username, metric, old_val, new_val, direction, datetime.now().isoformat()),
    )
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. جلب بيانات تيك توك
# ═══════════════════════════════════════════════════════════════════════════════
def parse_tiktok_number(text: str) -> int:
    """تحويل أرقام مثل 1.5M أو 23.4K إلى أعداد صحيحة."""
    if not text:
        return 0
    text = text.strip().replace(",", "")
    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    for suffix, mult in multipliers.items():
        if text.upper().endswith(suffix):
            try:
                return int(float(text[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def fetch_tiktok_stats(username: str) -> Optional[dict]:
    """
    يجلب إحصائيات تيك توك باستخدام Playwright (headless browser).
    يستخرج البيانات من JSON المُضمَّن في الصفحة (SIGI_STATE).
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error("❌ playwright غير مثبَّت: pip install playwright && playwright install chromium")
        return None

    url = f"https://www.tiktok.com/@{username}"
    log.info("🔍 جلب بيانات: %s", url)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = ctx.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(4_000)   # انتظر تحميل JS

            # محاولة 1: استخراج من SIGI_STATE (JSON مضمَّن)
            stats = _extract_from_sigi(page, username)
            if stats:
                browser.close()
                return stats

            # محاولة 2: استخراج من HTML المُقدَّم
            stats = _extract_from_html(page, username)
            if stats:
                browser.close()
                return stats

            log.warning("⚠️  لم نتمكن من استخراج البيانات من الصفحة.")
            browser.close()
            return None

        except PWTimeout:
            log.error("⏰ انتهت مهلة تحميل الصفحة لـ @%s", username)
            browser.close()
            return None
        except Exception as exc:
            log.error("❌ خطأ أثناء الجلب: %s", exc)
            browser.close()
            return None


def _extract_from_sigi(page, username: str) -> Optional[dict]:
    """استخراج البيانات من متغير SIGI_STATE في الصفحة."""
    try:
        sigi = page.evaluate("() => window.__UNIVERSAL_DATA_FOR_REHYDRATION__ || window.SIGI_STATE || null")
        if not sigi:
            return None

        # البحث عن بيانات المستخدم بالتكرار داخل الكائن
        sigi_str = json.dumps(sigi)
        # نبحث عن followingCount/followerCount
        m_followers = re.search(r'"followerCount"\s*:\s*(\d+)', sigi_str)
        m_following = re.search(r'"followingCount"\s*:\s*(\d+)', sigi_str)
        m_likes     = re.search(r'"heartCount"\s*:\s*(\d+)', sigi_str)
        m_videos    = re.search(r'"videoCount"\s*:\s*(\d+)', sigi_str)

        if m_followers and m_following:
            return {
                "username":  username,
                "followers": int(m_followers.group(1)),
                "following": int(m_following.group(1)),
                "likes":     int(m_likes.group(1))    if m_likes    else 0,
                "videos":    int(m_videos.group(1))   if m_videos   else 0,
            }
    except Exception as e:
        log.debug("SIGI extract failed: %s", e)
    return None


def _extract_from_html(page, username: str) -> Optional[dict]:
    """استخراج البيانات من عناصر HTML مباشرة."""
    try:
        from bs4 import BeautifulSoup
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # تيك توك يضع الأرقام في data attributes أو في نص ضمن spans خاصة
        # نبحث عن JSON داخل <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">
        script = soup.find("script", {"id": "__UNIVERSAL_DATA_FOR_REHYDRATION__"})
        if script and script.string:
            data = json.loads(script.string)
            data_str = json.dumps(data)
            m_f  = re.search(r'"followerCount"\s*:\s*(\d+)', data_str)
            m_fw = re.search(r'"followingCount"\s*:\s*(\d+)', data_str)
            m_l  = re.search(r'"heartCount"\s*:\s*(\d+)', data_str)
            m_v  = re.search(r'"videoCount"\s*:\s*(\d+)', data_str)
            if m_f and m_fw:
                return {
                    "username":  username,
                    "followers": int(m_f.group(1)),
                    "following": int(m_fw.group(1)),
                    "likes":     int(m_l.group(1)) if m_l else 0,
                    "videos":    int(m_v.group(1)) if m_v else 0,
                }
    except Exception as e:
        log.debug("HTML extract failed: %s", e)
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


def check_and_alert(conn: sqlite3.Connection, current: dict) -> None:
    username = current["username"]
    prev = get_last_snapshot(conn, username)

    if prev is None:
        log.info("📝 أول تسجيل للحساب @%s — لا يوجد مقارنة بعد.", username)
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

        direction = "زيادة ⬆️" if diff > 0 else "نقص ⬇️"
        change_str = f"+{format_number(abs(diff))}" if diff > 0 else f"-{format_number(abs(diff))}"
        emoji = "⬆️" if diff > 0 else "⬇️"

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
            f"🔔 تنبيه تيك توك — @{username}\n"
            f"{'─'*35}\n"
            + "\n\n".join(alerts) +
            f"\n{'─'*35}\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        send_notification(full_msg)
    else:
        log.info("✅ لا تغييرات في @%s", username)

    # حفظ اللقطة الجديدة دائماً
    save_snapshot(conn, username,
                    current["followers"], current["following"],
                    current["likes"], current["videos"])


# ═══════════════════════════════════════════════════════════════════════════════
# 5. الحلقة الرئيسية
# ═══════════════════════════════════════════════════════════════════════════════
def run_once(conn: sqlite3.Connection) -> None:
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
            next_run = datetime.now().strftime("%H:%M:%S")
            log.info("⏳ الفحص القادم بعد %d دقيقة ...", CONFIG["interval_minutes"])
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        log.info("🛑 تم إيقاف المراقب.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
