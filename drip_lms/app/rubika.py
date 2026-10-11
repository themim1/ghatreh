"""قطره — بازوی پیگیری: صندوق خروجی و اتصال روبیکا.

بدون توکن، همه پیام‌ها در جدول notifications ثبت می‌شوند (صندوق خروجی) تا
در صفحه «پیام‌های روبیکا» و «لاگ اعلان‌ها» دیده شوند. با تنظیم توکن در
تنظیمات، همان پیام از مسیر واقعی API ارسال می‌شود — بدون تغییر در بقیه کد.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services as sv
from .db import Course, Notification, get_setting


def _sent_today(s: Session, kind: str) -> bool:
    t = sv.today()
    rows = s.scalars(select(Notification).where(Notification.kind == kind)).all()
    return any(n.created_at.date() == t for n in rows)


def enqueue(s: Session, kind: str, body: str, day_number: int | None = None,
            reason: str = "") -> Notification:
    n = Notification(kind=kind, body=body, day_number=day_number, reason=reason,
                     chat_id=get_setting(s, "rubika_chat_id", "me") or "me")
    s.add(n)
    s.commit()
    try_real_send(s, n)
    return n


def try_real_send(s: Session, n: Notification) -> None:
    """اگر توکن و شناسه چت تنظیم شده باشد، پیام واقعاً ارسال می‌شود."""
    token = get_setting(s, "rubika_token").strip()
    chat_id = get_setting(s, "rubika_chat_id").strip()
    if not token or not chat_id:
        n.reason = n.reason or "بدون توکن — در صندوق خروجی"
        s.commit()
        return
    base = get_setting(s, "rubika_base_url").strip().rstrip("/")
    url = f"{base}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": n.body}).encode()
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "auth": token})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            if 200 <= resp.status < 300:
                n.status = "sent"
                n.sent_at = sv.now()
            else:
                n.status = "failed"
                n.reason = f"کد پاسخ {resp.status}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        n.status = "failed"
        n.reason = f"خطای شبکه: {type(exc).__name__}"
    s.commit()


def paused(s: Session) -> bool:
    until = sv.parse_iso(get_setting(s, "paused_until", ""))
    return bool(until and until >= sv.today())


def in_quiet_hours() -> bool:
    h = sv.now().hour
    return h >= 23 or h < 7


def tick(s: Session) -> list[Notification]:
    """موتور ارسال: صبح، عصر، تبریک، کارنامه جمعه — هر کدام حداکثر یک‌بار در روز."""
    if get_setting(s, "scheduler_enabled", "1") != "1":
        return []
    course = sv.active_course(s)
    if not course or paused(s):
        return []
    created: list[Notification] = []
    t = sv.today()
    timenow = sv.now().strftime("%H:%M")
    current = sv.current_day(s, course)
    lesson = sv.lesson_for(s, course.id, current)
    today_log = sv.log_for(s, course.id, t)

    if get_setting(s, "morning_time", "08:00") <= timenow and not in_quiet_hours():
        if lesson and lesson.status == "published" and not today_log and not _sent_today(s, "morning"):
            created.append(enqueue(s, "morning",
                                   sv.morning_message(s, course, lesson, current, None),
                                   day_number=current))

    if get_setting(s, "evening_time", "18:00") <= timenow and not in_quiet_hours():
        if not today_log and not _sent_today(s, "evening") and _sent_today(s, "morning"):
            created.append(enqueue(s, "evening", sv.evening_message(course, current),
                                   day_number=current))

    if sv.is_friday(t) and timenow >= "10:00" and not _sent_today(s, "weekly"):
        created.append(enqueue(s, "weekly", sv.weekly_message(s, course)))
    return created


def send_congrats(s: Session, course: Course, level: int, streak: int) -> Notification:
    return enqueue(s, "congrats", sv.congrats_message(streak, level))
