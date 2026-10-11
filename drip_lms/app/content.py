"""قطره — مدیریت محتوا: Frontmatter، بارگذاری فایل/Zip، خروجی، و داده نمونه."""
from __future__ import annotations

import io
import re
import zipfile
from datetime import timedelta
from pathlib import Path

import frontmatter
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services as sv
from .db import CONTENT_DIR, Course, DailyLog, Lesson, get_setting, set_setting

REQUIRED_KEYS = ["title", "goal"]
LESSON_STATUSES = {"empty", "draft", "ai", "published"}


# ---------- پارس و ذخیره ----------
def parse_markdown(text: str) -> tuple[dict, str]:
    """Frontmatter فارسی/انگلیسی + بدنه مارک‌داون"""
    post = frontmatter.loads(text)
    meta = {str(k): v for k, v in post.metadata.items()}
    return meta, post.content


def _int(meta: dict, key: str, default: int) -> int:
    try:
        return int(str(meta.get(key, default)).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
    except (TypeError, ValueError):
        return default


def apply_meta(les: Lesson, meta: dict, body: str, source_file: str = "") -> list[str]:
    """نگاشت متادیتا به مدل درس — لیست هشدارها را برمی‌گرداند."""
    warns: list[str] = []
    les.title = str(meta.get("title") or les.title or "").strip()
    les.goal = str(meta.get("goal") or les.goal or "").strip()
    les.golden_tip = str(meta.get("golden_tip") or meta.get("tip") or les.golden_tip or "").strip()
    les.exercise_prompt = str(meta.get("exercise_prompt") or les.exercise_prompt or "").strip()
    les.exercise_expected = str(meta.get("exercise_expected") or les.exercise_expected or "").strip()
    les.body_md = body.strip()
    les.day_number = _int(meta, "day_number", les.day_number or 0)
    les.phase_number = _int(meta, "phase_number", les.phase_number or 1)
    les.week_number = _int(meta, "week_number", les.week_number or 1)
    les.read_minutes = _int(meta, "read_minutes", les.read_minutes or 10)
    les.exercise_minutes = _int(meta, "exercise_minutes", les.exercise_minutes or 35)
    kind = str(meta.get("kind") or les.kind or "lesson")
    les.kind = kind if kind in {"lesson", "project", "review"} else "lesson"
    status = str(meta.get("status") or "").strip()
    if status in LESSON_STATUSES:
        les.status = status
    if source_file:
        les.source_file = source_file
    for k in REQUIRED_KEYS:
        if not meta.get(k):
            warns.append(f"متادیتای «{k}» خالی است.")
    if not les.exercise_prompt:
        warns.append("صورت تمرین تعیین نشده است.")
    total = les.read_minutes + les.exercise_minutes
    if total > 50:
        warns.append(f"جمع زمان {sv.fa(total)} دقیقه است (مرز مینی‌پروژه).")
    return warns


def upsert_lesson(s: Session, course: Course, meta: dict, body: str, source_file: str = "",
                  mode: str = "add_new") -> tuple[Lesson | None, list[str], bool]:
    """mode: add_new | update | replace — برمی‌گرداند (درس، هشدارها، ساخته‌شد؟)"""
    day = _int(meta, "day_number", 0)
    if not day:
        return None, ["شماره روز مشخص نیست (day_number)."], False
    les = sv.lesson_for(s, course.id, day)
    created = False
    if les is None:
        les = Lesson(course_id=course.id, day_number=day, status="published")
        s.add(les)
        created = True
    elif mode == "add_new":
        return les, ["این روز از قبل وجود دارد — با حالت «فقط افزودن» رد شد."], False
    warns = apply_meta(les, meta, body, source_file)
    if les.status == "published" and les.published_at is None:
        les.published_at = sv.now()
    return les, warns, created


def _entries_from_zip(raw: bytes) -> list[tuple[str, str]] | None:
    """فایل‌های Markdown داخل Zip — None یعنی بسته سالم نبود."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None
    out: list[tuple[str, str]] = []
    for name in zf.namelist():
        if name.lower().endswith((".md", ".markdown")) and not name.startswith("__MACOSX"):
            out.append((name, zf.read(name).decode("utf-8", "replace")))
    return out


def _collect(uploads: list[tuple[str, bytes]]) -> tuple[list[tuple[str, str]], str | None]:
    """ورودی‌های خام (Zip یا MD) → فهرست (نام، متن). پیام خطای کشنده در صورت وجود."""
    entries: list[tuple[str, str]] = []
    for name, raw in uploads:
        low = name.lower()
        if low.endswith(".zip"):
            inner = _entries_from_zip(raw)
            if inner is None:
                return [], f"«{Path(name).name}» یک Zip سالم نیست."
            entries.extend(inner)
        elif low.endswith((".md", ".markdown", ".txt")):
            entries.append((name, raw.decode("utf-8", "replace")))
        else:
            return [], f"فرمت «{Path(name).name}» پشتیبانی نمی‌شود — Zip یا Markdown بفرست."
    if not entries:
        return [], "هیچ فایل .md در ورودی پیدا نشد."
    return entries, None


def import_uploads(s: Session, course: Course, uploads: list[tuple[str, bytes]],
                   mode: str = "add_new", publish_drafts: bool = False) -> dict:
    """بارگذاری گروهی چندفایلی (Zip و MD با هم) — گزارش دقیق برمی‌گرداند."""
    if not uploads:
        return {"fatal": "فایلی انتخاب نشده است.", "items": [], "summary": None}
    entries, fatal = _collect(uploads)
    if fatal:
        return {"fatal": fatal, "items": [], "summary": None}
    return _import_entries(s, course, entries, mode=mode, publish_drafts=publish_drafts)


def import_zip(s: Session, course: Course, raw: bytes, mode: str = "add_new",
               publish_drafts: bool = False) -> dict:
    """سازگاری عقب‌رو: بارگذاری یک Zip یا یک MD خام."""
    return import_uploads(s, course, [("upload.zip", raw)], mode=mode,
                          publish_drafts=publish_drafts)


def _import_entries(s: Session, course: Course, entries: list[tuple[str, str]],
                    mode: str = "add_new", publish_drafts: bool = False) -> dict:
    seen: dict[int, str] = {}
    items: list[dict] = []
    for name, text in entries:
        meta, body = parse_markdown(text)
        day = _int(meta, "day_number", 0)
        item = {"file": Path(name).name, "day": day, "level": "ok", "messages": [], "title": meta.get("title", "")}
        if not day:
            m = re.search(r"day[-_]?0*(\d+)", Path(name).stem)
            if m:
                day = int(m.group(1))
                meta["day_number"] = day
                item["day"] = day
                item["messages"].append("شماره روز از نام فایل خوانده شد.")
            else:
                item["level"] = "error"
                item["messages"].append("شماره روز پیدا نشد (نه در متادیتا، نه در نام فایل).")
                items.append(item)
                continue
        if day in seen:
            item["level"] = "error"
            item["messages"].append(f"روز {sv.fa(day)} تکراری است — با «{seen[day]}» هم‌شماره شده.")
            items.append(item)
            continue
        seen[day] = Path(name).name

        les, warns, created = upsert_lesson(s, course, meta, body, source_file=name, mode=mode)
        for w in warns:
            item["messages"].append(w)
        if warns and item["level"] == "ok":
            item["level"] = "warn"
        if not created and mode == "add_new":
            item["level"] = "warn"
        if publish_drafts and les is not None and les.status != "published":
            les.status = "published"
        items.append(item)

    s.commit()
    published = sv.published_day_numbers(s, course.id)
    current = sv.current_day(s, course)
    missing_review = [d for d in sorted(published) if sv.weekday_index(sv.today()) is not None and d % 7 == 6]
    summary = {
        "files": len(entries),
        "ok": len([i for i in items if i["level"] == "ok"]),
        "warn": len([i for i in items if i["level"] == "warn"]),
        "error": len([i for i in items if i["level"] == "error"]),
        "buffer": sv.buffer_days(s, course),
        "current": current,
        "missing_review": len([d for d in range(current + 1, current + 8) if d not in published]) if missing_review else 0,
    }
    return {"fatal": None, "items": items, "summary": summary}


def export_zip(s: Session, course: Course) -> bytes:
    """خروجی همه دروس دوره به‌صورت Zip از فایل‌های Markdown."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        rows = s.scalars(select(Lesson).where(Lesson.course_id == course.id)
                         .order_by(Lesson.day_number)).all()
        for les in rows:
            fm = "\n".join([
                "---",
                f"day_number: {les.day_number}",
                f"phase_number: {les.phase_number}",
                f"week_number: {les.week_number}",
                f"kind: {les.kind}",
                f"title: {les.title}",
                f"goal: {les.goal}",
                f"golden_tip: {les.golden_tip}",
                f"read_minutes: {les.read_minutes}",
                f"exercise_minutes: {les.exercise_minutes}",
                f"status: {les.status}",
                "---", "",
            ])
            body = les.body_md
            if les.exercise_prompt:
                body += f"\n\n## تمرین\n\n{les.exercise_prompt}\n"
            if les.exercise_expected:
                body += f"\n**خروجی مورد انتظار:** {les.exercise_expected}\n"
            zf.writestr(f"phase-{les.phase_number:02d}/week-{les.week_number:02d}/"
                        f"day-{les.day_number:03d}.md", fm + body)
    return buf.getvalue()


# ---------- داده نمونه ----------
TOPICS = {
    34: ("مدل داده و Migration با Alembic", "تغییر ساختار دیتابیس بدون از دست دادن داده", "Migration را قبل از deploy اجرا کن، نه بعدش.", "برای جدول `orders` یک ستون `status` اضافه کن و migration برگشت‌پذیر بنویس."),
    35: ("کوئری‌های N+1 و eager loading", "چند کوئری اضافه را در یک مسیر حذف کنی", "هر `selectinload` که فراموش شود، در تولید به صد کوئری تبدیل می‌شود.", "یک endpoint با N+1 پیدا کن و با eager loading به یک کوئری برسان؛ قبل/بعد را لاگ بگیر."),
    36: ("تراکنش‌ها و مدیریت خطا", "مرز تراکنش را دقیق بچینی", "تراکنش را کوتاه نگه دار؛ کار شبکه‌ای داخلش نگذار.", "مسیر ثبت سفارش را طوری بازنویسی کن که خطای ایمیل، سفارش را برنگرداند."),
    37: ("لاگ ساخت‌یافته", "لاگ‌ها را قابل جست‌وجو کنی", "JSON لاگ بده، نه متن آزاد.", "لاگر را به JSON تغییر بده و یک درخواست را تا انتها ردیابی کن."),
    38: ("تست‌های واحد با pytest", "اولین تست واقعی را بنویسی", "روی رفتار تست بنویس، نه روی پیاده‌سازی.", "برای تابع محاسبه تخفیف، سه تست بنویس: مرزی، خطا، حالت عادی."),
    39: ("Fixture و تست پایگاه داده", "تست وابسته به دیتابیس واقعی بنویسی", "دیتابیس تست را هر بار از صفر بساز؛ وضعیت مشترک دردسر است.", "یک fixture بساز که دیتابیس موقت ایجاد و پاک کند."),
    40: ("کش با Redis", "کوئری سنگین را کش کنی", "هر کش باید انقضا داشته باشد؛ کش بی‌انقضا، باگ پنهان است.", "نتیجه یک گزارش سنگین را ۶۰ ثانیه کش کن و نرخ hit را لاگ بگیر."),
    41: ("الگوهای Invalidations کش", "کش را درست باطل کنی", "باطل‌کردن را از منبع داده صدا بزن، نه از UI.", "پس از ویرایش کاربر، کلیدهای مرتبط را باطل کن."),
    42: ("مرور جمعه — هفته ۶", "مفاهیم هفته را بازیابی کنی", "بازیابی، نه بازخوانی.", "سه مفهوم سخت هفته را در نیم‌صفحه بنویس."),
    43: ("صف کارها با Redis و Celery", "کار سنگین را از مسیر درخواست جدا کنی", "هر کار بیش از ۵۰۰ms، بیرون از مسیر درخواست.", "یک صف بساز، سه کار بفرست و با Worker اجرا کن؛ زمان هر کار را لاگ بگیر."),
    44: ("زمان‌بند کارها و Retry", "کار شکست‌خورده را هوشمندانه دوباره اجرا کنی", "Retry بدون backoff، سرور را زیر بار حمله می‌برد.", "سیاست retry با فاصله نمایی بنویس و رفتارش را با یک خطای تصنعی تست کن."),
    45: ("Idempotency در کارها", "کار را بی‌خطر تکرارپذیر کنی", "کلید idempotency را از داده بساز، نه از زمان.", "ارسال اعلان را طوری بنویس که دوبار اجرا شدن، دوبار پیام نفرستد."),
    46: ("پایش صف با Flower", "صف را ببینی، نه حدس بزنی", "صف بی‌پایش، صف بی‌اعتماد است.", "Flower را بالا بیاور و طول صف را در یک بازه پرترافیک ثبت کن."),
    47: ("صف کارها با Redis و Celery", "یک کار سنگین را از مسیر درخواست جدا کنی و در پس‌زمینه اجرا کنی", "هر کاری بیش از ۵۰۰ میلی‌ثانیه طول بکشد، از مسیر درخواست بیرون است.", "یک صف Redis بساز، سه کار نمونه به آن بفرست و با یک Worker اجرا کن. زمان اجرای هر کار را لاگ بگیر."),
    48: ("مینی‌پروژه هفته ۷ — سرویس اعلان", "پنج مفهوم هفته را در یک سرویس ترکیب کنی", "پروژه‌ی کوچکِ تمام‌شده، از درس بزرگ نیمه‌کاره ارزشمندتر است.", "سرویسی بساز که اعلان را با صف، retry و idempotency ارسال کند و لاگ JSON بدهد."),
    49: ("مرور جمعه — هفته ۷", "خروجی هفته را جمع‌بندی کنی", "یادداشت‌های هفته را دوباره بخوان؛ همان‌ها منبع آزمون جمعه‌اند.", "سه یادداشت برتر هفته را انتخاب کن و در یک پاراگراف جمع بزن."),
    50: ("پیکربندی محیط و Secrets", "محیط‌ها را از کد جدا کنی", "هیچ کلیدی در گیت نمی‌ماند؛ حتی در مخزن شخصی.", "تنظیمات را با pydantic-settings بنویس و کلیدها را از .env بخوان."),
    51: ("داکر برای توسعه", "محیط را یک‌دست بالا بیاری", "Dockerfile توسعه و تولید باید جدا باشند.", "یک compose برای اپ + Redis بنویس و سرویس را با یک دستور بالا بیاور."),
    52: ("مهاجرت به تولید", "استقرار را تکرارپذیر کنی", "استقرار دستی، یک بار جواب می‌دهد؛ اسکریپت بنویس.", "یک اسکریپت deploy بنویس: pull، migrate، restart، health-check."),
    53: ("نظارت و هشدار", "خطا را قبل از کاربر ببینی", "هشدار باید کنش داشته باشد وگرنه نویز است.", "برای خطای ۵xx هشدار بساز و آستانه‌اش را مستند کن."),
    54: ("بهینه‌سازی کوئری‌های سنگین", "زمان پاسخ را نصف کنی", "first با EXPLAIN، بعد index.", "یک کوئری کند را با index به زیر ۵۰ms برسان."),
    55: ("امنیت API", "دسترسی‌ها را دقیق ببندی", "اعتبارسنجی را سمت سرور تکرار کن، حتی اگر UI هم چک کرده.", "برای دو endpoint محدودیت نرخ اضافه کن."),
    56: ("نسخه‌بندی API", "تغییر بی‌شکست بدهی", "نسخه را در مسیر نگه دار، نه در هدر تنها.", "v2 یک endpoint بساز و مصرف‌کننده قدیمی را نشکن."),
    57: ("مستندسازی با OpenAPI", "API را خودتوصیف کنی", "نمونه پاسخ واقعی بگذار، نه نمونه ساختگی.", "برای سه endpoint نمونه واقعی اضافه کن."),
    58: ("بازبینی کد و کیفیت", "کد را قابل نگه‌داری کنی", "کدی که خودت هم بعداً نفهمی، بدهی است.", "سه فایل بزرگ پروژه را به توابع کوچک بشکن."),
    59: ("آزمون فاز ۲", "مفاهیم فاز را بازیابی کنی", "آزمون برای یادآوری است، نه نمره.", "بیست سؤال کوتاه از فاز ۲ بساز و پاسخ‌ها را خودت بنویس."),
    60: ("آماده‌سازی فاز ۳", "مسیر فاز بعد را ببینی", "نقشه فاز بعد را قبل از شروع ببین تا سرگردان نشوی.", "سه هدف اصلی فاز ۳ را در سه خط بنویس."),
}


def seed_demo(s: Session) -> None:
    """دوره نمونه + دروس + لاگ‌های گذشته تا Streak و بافر واقعی نمایش داده شود."""
    existing = s.scalars(select(Course).where(Course.slug == "python-backend")).first()
    if existing:
        return
    course = Course(slug="python-backend", title="پایتون و معماری بک‌اند", icon="🐍",
                    description="از صفر تا معماری سرویس‌های واقعی — هر روز یک قطره.",
                    total_days=180, phases=6)
    s.add(course)
    s.flush()

    # ---------- دروس ----------
    for day in range(1, 65):
        topic = TOPICS.get(day)
        if not topic:
            continue
        title, goal, tip, exercise = topic
        kind = "project" if day % 7 == 6 else ("review" if day % 7 == 0 else "lesson")
        phase = (day - 1) // 30 + 1
        week = (day - 1) // 7 + 1
        status = "published"
        if day == 59:
            status = "ai"
        if day == 60:
            status = "draft"
        if day >= 61:
            continue
        body = (
            f"## چرا این موضوع مهم است؟\n\n"
            f"در روزهای قبل پایه‌ها را ساختیم. امروز می‌خواهیم {title} را طوری بچینیم که "
            f"در سرویس واقعی قابل اتکا باشد. تمرکز امروز روی یک مهارت مشخص است: {goal}.\n\n"
            f"## قدم‌های عملی\n\n"
            f"۱) مسیر فعلی را در ذهن مرور کن و نقطه‌ای که کندی یا خطا از آن می‌آید مشخص کن.\n"
            f"۲) کوچک‌ترین تغییر ممکن را انجام بده.\n"
            f"۳) نتیجه را اندازه بگیر و در یادداشت امروز بنویس.\n\n"
            f"```python\n"
            f"# نمونه اجرایی روز {day}\n"
            f"def handle_day_{day}():\n"
            f"    ...\n"
            f"```\n\n"
            f"> ⚡ **نکته طلایی:** {tip}\n"
        )
        if day == 47:
            body = (
                "## ۱) چرا صف کار لازم داریم؟\n\n"
                "اگر ارسال ایمیل، پردازش تصویر یا فراخوانی یک API کند داخل مسیر درخواست اجرا شود، "
                "کاربر باید منتظر بماند و یک خطای ساده، کل درخواست را می‌خواباند. راه‌حل استاندارد: "
                "درخواست را در صف بگذار و پاسخ را فوری برگردان.\n\n"
                "```python\n"
                "# app/tasks.py — تعریف کار پس‌زمینه\n"
                "from celery import Celery\n\n"
                "app = Celery(\"drip\", broker=\"redis://localhost:6379/0\")\n\n"
                "@app.task\n"
                "def send_daily_lesson(chat_id: str, day: int):\n"
                "    # ارسال خلاصه درس در روبیکا\n"
                "    ...\n"
                "```\n\n"
                "## ۲) انتخاب صف مناسب\n\n"
                "Redis برای صف‌های سبک و پرتعداد کافی است؛ اگر مسیر حساس و نیاز به تأیید تحویل داری، "
                "RabbitMQ بهتر می‌نشیند. برای این دوره Redis انتخاب می‌کنیم و دلیلش را در دفترچه تصمیم‌ها ثبت می‌کنیم.\n\n"
                "> ⚡ **نکته طلایی:** هر کاری که بیش از ۵۰۰ میلی‌ثانیه طول بکشد، از مسیر درخواست بیرون است — "
                "حتی اگر «الان کار می‌کند».\n"
            )
        s.add(Lesson(course_id=course.id, day_number=day, phase_number=phase, week_number=week,
                     kind=kind, title=title, goal=goal, golden_tip=tip, body_md=body,
                     exercise_prompt=exercise,
                     exercise_expected="فایل اجرایی + لاگ انجام کار + یک خط یادداشت از آنچه یاد گرفتی.",
                     read_minutes=10, exercise_minutes=60 if kind == "project" else 35,
                     status=status, published_at=sv.now() if status == "published" else None,
                     source_file="seed"))
    s.flush()

    # ---------- لاگ‌های گذشته ----------
    t = sv.today()
    workdays = [t - timedelta(days=i) for i in range(1, 17)]
    workdays = [d for d in workdays if not sv.is_friday(d)]        # جمعه‌ها ثبت نمی‌شوند
    workdays = sorted(workdays)[-14:]
    start_day = 34
    levels = [2, 2, 1, 3, 2, 2, 1, 2, 3, 2, 1, 2]
    notes = [
        "Migration برگشت‌پذیر نوشتم؛ قبل از deploy تست شد.",
        "N+1 را با selectinload حل کردم، از ۱۴۰ کوئری به ۲ رسید.",
        "امروز فقط خواندم؛ فردا تمرین را می‌فرستم.",
        "لاگ‌ها JSON شدند و یک درخواست را سرتاسر ردیابی کردم.",
        "سه تست برای تابع تخفیف نوشتم (مرزی، خطا، عادی).",
        "fixture دیتابیس موقت را ساختم؛ تست‌ها مستقل شدند.",
        "گزارش سنگین را ۶۰ ثانیه کش کردم.",
        "باطل‌سازی کش را از منبع داده صدا زدم.",
        "سه مفهوم سخت هفته را بازیابی کردم.",
        "صف را ساختم و سه کار را با Worker اجرا کردم.",
        "retry با backoff نمایی را تست کردم.",
        "کلید idempotency از داده ساخته شد، نه از زمان.",
    ]
    for i, d in enumerate(workdays):
        day_no = start_day + i
        if day_no == start_day:                       # یک روز ازدست‌رفته برای نمایش زنجیره
            continue
        idx = i - 1
        level = levels[idx % len(levels)]
        note = notes[idx % len(notes)]
        kind = "read" if level == 1 and idx % 5 == 2 else "done"
        s.add(DailyLog(course_id=course.id, day_number=day_no, log_date=d, kind=kind,
                       level=level if kind != "read" else 0, note=note,
                       link="https://gist.github.com/mehdi/8f2a1" if level >= 2 else "",
                       files=[{"name": "shot.png", "size": 482_000}] if level == 3 else [],
                       source="web"))
    s.flush()

    set_setting(s, "active_course_id", str(course.id))
    set_setting(s, "longest_streak", "19")
    s.commit()


TEMPLATE_MD = """---
day_number: {day}
title: 
phase_number: {phase}
week_number: {week}
kind: lesson
status: draft
goal: 
golden_tip: 
read_minutes: 10
exercise_minutes: 35
exercise_prompt: 
exercise_expected: 
---

## چرا این موضوع؟

## مفهوم اصلی

```python
# نمونه کد اجراشدنی
```

## اشتباه رایج

## جمع‌بندی
"""


def template_markdown(day: int) -> str:
    """اسکلت آمادهٔ Markdown برای روز مشخص — کپی کن و پر کن."""
    return TEMPLATE_MD.format(day=day, phase=(day - 1) // 30 + 1, week=(day - 1) // 7 + 1)
