"""قطره — سرویس‌های دامنه: تاریخ شمسی، وضعیت روز، Streak، بافر محتوا، متن پیام‌ها."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import jdatetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Course, DailyLog, Lesson, get_setting, set_setting

TEHRAN = timezone(timedelta(hours=3, minutes=30))  # ایران از ۱۴۰۱ ساعت تابستانی ندارد

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
WEEKDAYS_FA = ["شنبه", "یک‌شنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
WEEKDAYS_SHORT = ["ش", "ی", "د", "س", "چ", "پ", "ج"]
J_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
            "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]


# ---------- اعداد و تاریخ ----------
def fa(value) -> str:
    return str(value).translate(FA_DIGITS)


def today() -> date:
    return datetime.now(TEHRAN).date()


def now() -> datetime:
    return datetime.now(TEHRAN).replace(tzinfo=None)


def weekday_index(d: date) -> int:
    """۰=شنبه … ۶=جمعه (مطابق jdatetime)"""
    return jdatetime.date.fromgregorian(date=d).weekday()


def weekday_fa(d: date) -> str:
    return WEEKDAYS_FA[weekday_index(d)]


def is_friday(d: date) -> bool:
    return weekday_index(d) == 6


def jdate(d: date) -> jdatetime.date:
    return jdatetime.date.fromgregorian(date=d)


def fa_date(d: date) -> str:
    j = jdate(d)
    return f"{fa(j.day)} {J_MONTHS[j.month - 1]}"


def fa_date_full(d: date) -> str:
    return f"{weekday_fa(d)} {fa_date(d)} {fa(jdate(d).year)}"


def iso(d: date) -> str:
    return d.isoformat()


def parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def week_start(d: date) -> date:
    """شنبه‌ی هفته‌ای که d در آن است."""
    return d - timedelta(days=weekday_index(d))


# ---------- دوره و روز جاری ----------
def active_course(s: Session) -> Course | None:
    cid = get_setting(s, "active_course_id")
    if cid:
        c = s.get(Course, int(cid)) if cid.isdigit() else None
        if c:
            return c
    return s.scalars(select(Course).where(Course.is_active.is_(True)).order_by(Course.id)).first()


def log_for(s: Session, course_id: int, d: date) -> DailyLog | None:
    return s.scalars(select(DailyLog).where(DailyLog.course_id == course_id,
                                            DailyLog.log_date == d)).first()


def lesson_for(s: Session, course_id: int, day_number: int) -> Lesson | None:
    return s.scalars(select(Lesson).where(Lesson.course_id == course_id,
                                          Lesson.day_number == day_number)).first()


def current_day(s: Session, course: Course) -> int:
    """اگر امروز ثبت شده باشد، همان روز؛ وگرنه روز بعد از آخرین روز ثبت‌شده."""
    today_log = log_for(s, course.id, today())
    if today_log:
        return today_log.day_number
    last = s.scalars(select(func.max(DailyLog.day_number)).where(DailyLog.course_id == course.id)).first()
    return (last or 0) + 1


def published_day_numbers(s: Session, course_id: int) -> set[int]:
    rows = s.scalars(select(Lesson.day_number).where(Lesson.course_id == course_id,
                                                     Lesson.status == "published"))
    return set(rows.all())


def buffer_days(s: Session, course: Course) -> int:
    """تعداد روزهای پیوسته‌ی منتشرشده بعد از روز جاری (قاعده: هرگز زیر ۵ روز نرو)."""
    current = current_day(s, course)
    published = published_day_numbers(s, course.id)
    n = 0
    d = current + 1
    while d in published:
        n += 1
        d += 1
    return n


# ---------- وضعیت روزها ----------
def pause_dates(s: Session, course_id: int) -> set[date]:
    rows = s.scalars(select(DailyLog.log_date).where(DailyLog.course_id == course_id,
                                                     DailyLog.kind == "pause"))
    return set(rows.all())


def day_state(s: Session, course: Course, d: date) -> str:
    """done | read | token | pause | missed | friday | future | open"""
    if d > today():
        return "future"
    log = log_for(s, course.id, d)
    if log:
        return log.kind if log.kind in {"done", "read", "token", "pause"} else "done"
    if d == today():
        return "open"
    if is_friday(d):
        return "friday"
    return "missed"


def week_map(s: Session, course: Course, ref: date | None = None) -> list[dict]:
    """نقشه هفته‌ی شمسی (شنبه تا جمعه) با وضعیت و شماره روز."""
    ref = ref or today()
    start = week_start(ref)
    logs = {l.log_date: l for l in s.scalars(select(DailyLog).where(
        DailyLog.course_id == course.id,
        DailyLog.log_date >= start, DailyLog.log_date <= start + timedelta(days=6))).all()}
    out = []
    for i in range(7):
        d = start + timedelta(days=i)
        log = logs.get(d)
        out.append({
            "date": d, "label": WEEKDAYS_SHORT[i], "weekday": WEEKDAYS_FA[i],
            "state": day_state(s, course, d),
            "day_number": log.day_number if log else None,
            "is_today": d == today(), "is_friday": i == 6, "is_project": i == 5,
        })
    return out


# ---------- Streak ----------
def compute_streak(s: Session, course: Course) -> dict:
    """Streak سراسری: جمعه نمی‌شکند · توقف/ژتون نمی‌شکند · مهلت جبران ۱ روز."""
    t = today()
    logs = {l.log_date: l for l in s.scalars(select(DailyLog).where(DailyLog.course_id == course.id)).all()}
    if not logs:
        return {"streak": 0, "record": 0, "today_state": "open", "grace_open": False}

    today_log = logs.get(t)

    def kept(d: date, is_yesterday_of_logged_today: bool = False) -> bool | None:
        log = logs.get(d)
        if log and log.kind in {"done", "read", "token"}:
            return True
        if log and log.kind == "pause":
            return None           # نه می‌شکند نه اضافه می‌کند
        if is_friday(d):
            return None           # مرور جمعه اختیاری است
        if is_yesterday_of_logged_today and today_log:
            return True           # مهلت جبران: دیروز با ثبت امروز نگه داشته می‌شود
        return False

    streak = 0
    d = t if today_log else t - timedelta(days=1)
    # دیروزِ ثبت‌نشده وقتی امروز ثبت شده باشد، در شمارش می‌آید (مهلت جبران)
    grace_used = False
    guard = 0
    while guard < 400:
        guard += 1
        res = kept(d, is_yesterday_of_logged_today=(d == t - timedelta(days=1)))
        if res is True:
            if d == t - timedelta(days=1) and not logs.get(d):
                grace_used = True
            streak += 1
        elif res is None:
            pass
        else:
            break
        d -= timedelta(days=1)
        if d < min(logs) - timedelta(days=2):
            break

    record = int(get_setting(s, "longest_streak", "0") or 0)
    if streak > record:
        set_setting(s, "longest_streak", str(streak))
        record = streak
    return {
        "streak": streak,
        "record": record,
        "today_state": day_state(s, course, t),
        "grace_open": (not today_log and day_state(s, course, t - timedelta(days=1)) == "missed"),
        "grace_used": grace_used,
    }


def course_stats(s: Session, course: Course) -> dict:
    done = s.scalars(select(func.count()).select_from(DailyLog).where(
        DailyLog.course_id == course.id, DailyLog.kind.in_(["done", "read", "token"]))).one()
    verified = s.scalars(select(func.count()).select_from(DailyLog).where(
        DailyLog.course_id == course.id, DailyLog.level >= 2)).one()
    current = current_day(s, course)
    return {
        "done": done, "verified": verified, "current": current,
        "total": course.total_days,
        "percent": round(done * 100 / max(course.total_days, 1)),
    }


# ---------- پوشش محتوا ----------
def coverage(s: Session, course: Course, start_day: int, count: int = 30) -> list[dict]:
    lessons = {l.day_number: l for l in s.scalars(select(Lesson).where(Lesson.course_id == course.id)).all()}
    out = []
    for d in range(start_day, start_day + count):
        les = lessons.get(d)
        out.append({"day": d, "status": les.status if les else "empty",
                    "title": les.title if les else "",
                    "kind": les.kind if les else "lesson"})
    return out


PROMPT_LIBRARY = [
    {
        "code": "prompt-07", "title": "درس روزانه برنامه‌نویسی", "category": "برنامه‌نویسی",
        "version": 3, "uses": 47, "featured": True,
        "vars": ["course_title", "day_number", "topic", "level", "tone"],
        "body": """تو یک مدرس ارشد برنامه‌نویسی هستی.
برای دوره «{course_title}» روز {day_number} با موضوع «{topic}» یک درس ۱۰ دقیقه‌ای بنویس.
سطح: {level} — لحن: {tone}

ساختار اجباری:
1) هدف امروز در یک جمله
2) توضیح مفهوم با مثال واقعی
3) کد اجراشدنی
4) نکته طلایی (یک insight عملی)
5) تمرین با خروجی مشخص (۳۵ دقیقه)

خروجی را در قالب Markdown با Frontmatter بده (day_number, phase_number, week_number, title, goal, golden_tip, read_minutes, exercise_minutes).""",
    },
    {
        "code": "prompt-04", "title": "ساخت ۵ درس هفته از یک سرفصل", "category": "برنامه‌نویسی",
        "version": 2, "uses": 31, "featured": False,
        "vars": ["course_title", "week_number", "outline"],
        "body": """سرفصل هفته {week_number} از دوره «{course_title}»:\n{outline}\n
برای هر سرفصل یک درس جدا با ساختار استاندارد بنویس و روزها را از شماره درست ادامه بده.""",
    },
    {
        "code": "prompt-09", "title": "مینی‌پروژه پنجشنبه", "category": "برنامه‌نویسی",
        "version": 1, "uses": 11, "featured": False,
        "vars": ["course_title", "week_number", "topics"],
        "body": """موضوعات هفته {week_number} دوره «{course_title}»: {topics}
یک مینی‌پروژه ۶۰ دقیقه‌ای بنویس که هر پنج مفهوم هفته را ترکیب کند، با خروجی مشخص و چک‌لیست تحویل.""",
    },
    {
        "code": "prompt-11", "title": "تبدیل فلوی محصول به درس", "category": "مدیریت محصول",
        "version": 1, "uses": 9, "featured": False,
        "vars": ["course_title", "day_number", "topic"],
        "body": """تو یک طراح محصول هستی. برای روز {day_number} از دوره «{course_title}» موضوع «{topic}»
یک درس با مثال واقعی از یک محصول ایرانی بنویس؛ تمرین باید روی داده یا فلو واقعی اجرا شود.""",
    },
]


# ---------- متن پیام‌ها ----------
def morning_message(s: Session, course: Course, les: Lesson, current: int, link: str | None) -> str:
    lines = [
        f"☀️ روز {fa(current)} از {fa(course.total_days)} — {course.title}",
        "",
        f"🎯 هدف امروز: {les.goal or les.title}",
        "",
        f"⏱ مطالعه {fa(les.read_minutes)} دقیقه · 🏋️ تمرین {fa(les.exercise_minutes)} دقیقه",
    ]
    if les.exercise_prompt:
        lines.append(f"🏋️ تمرین: {les.exercise_prompt.splitlines()[0][:90]}")
    lines.append("")
    if link:
        lines.append(f"📖 مطالعه درس امروز: {link}")
    else:
        lines.append(f"💻 در سامانه، کد روز را بزن: Q-{fa(current)}")
    lines.append("")
    lines.append("دستورها: وضعیت · توقف ۵ · ساعت 21:00 · امروز")
    return "\n".join(lines)


def evening_message(course: Course, current: int) -> str:
    return (
        "⏰ ۴ ساعت تا پایان امروز.\n"
        f"تمرین روز {fa(current)} ثبت نشده. با ۱۵ ثانیه هم می‌شود امروز را نگه داشت:\n\n"
        "۱ · انجام دادم ✓\n"
        "۲ · اسکرین‌شات می‌فرستم\n"
        "۳ · ثبت در وب‌اپ\n"
        "۴ · امشب نمی‌رسم"
    )


def congrats_message(streak: int, level: int) -> str:
    level_fa = {1: "سریع (خوداظهاری)", 2: "مستند — تأییدشده", 3: "کامل"}.get(level, "سریع")
    extra = "\nاگر اسکرین‌شات یا لینک بفرستی، به «مستند» ارتقا می‌گیرد." if level == 1 else ""
    return f"✅ قطره امروز ثبت شد.\n🔥 Streak {fa(streak)}\nسطح: {level_fa}{extra}"


def back_message(record: int) -> str:
    return (
        "صبح بخیر 👋 دیروز ثبت نشد و Streak صفر شد — "
        f"ولی رکورد {fa(record)} روزه‌ات سر جایش است.\n\n"
        "امروز یک کار سبک داریم؛ با ۱۰ دقیقه شروع می‌کنیم."
    )


def weekly_message(s: Session, course: Course) -> str:
    start = week_start(today())
    logs = s.scalars(select(DailyLog).where(DailyLog.course_id == course.id,
                                            DailyLog.log_date >= start)).all()
    done = len([l for l in logs if l.kind in {"done", "read", "token"}])
    verified = len([l for l in logs if l.level >= 2])
    minutes = sum((lesson_for(s, course.id, l.day_number).exercise_minutes + 10)
                  if lesson_for(s, course.id, l.day_number) else 35 for l in logs)
    return (
        f"🗓 کارنامه هفته — {course.title}\n\n"
        f"روزهای ثبت‌شده: {fa(done)}\n"
        f"مدارک تأییدشده: {fa(verified)}\n"
        f"دقایق یادگیری: {fa(minutes)}\n\n"
        "خروجی را در وب‌اپ ببین: /path"
    )
