"""قطره — لایه داده (SQLite + SQLAlchemy).
مدل‌ها بر پایه سند طراحی نسخه شخصی: تک‌کاربره، Streak سراسری، سه سطح اثبات.
"""
from __future__ import annotations

import pathlib
from datetime import date, datetime

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer, String,
                        Text, UniqueConstraint, create_engine, func, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent  # drip_lms/
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CONTENT_DIR = BASE_DIR / "courses_data"
CONTENT_DIR.mkdir(parents=True, exist_ok=True)

DB_URL = f"sqlite:///{DATA_DIR / 'drip.sqlite3'}"


class Base(DeclarativeBase):
    pass


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    icon: Mapped[str] = mapped_column(String(8), default="📘")
    description: Mapped[str] = mapped_column(Text, default="")
    total_days: Mapped[int] = mapped_column(Integer, default=180)
    phases: Mapped[int] = mapped_column(Integer, default=6)
    # ۰=شنبه … ۶=جمعه — روز پروژه و روز مرور
    project_weekday: Mapped[int] = mapped_column(Integer, default=5)   # پنجشنبه
    review_weekday: Mapped[int] = mapped_column(Integer, default=6)    # جمعه
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (UniqueConstraint("course_id", "day_number", name="uq_course_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    day_number: Mapped[int] = mapped_column(Integer)
    phase_number: Mapped[int] = mapped_column(Integer, default=1)
    week_number: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(12), default="lesson")  # lesson|project|review
    title: Mapped[str] = mapped_column(String(250), default="")
    goal: Mapped[str] = mapped_column(Text, default="")              # هدف امروز (یک جمله)
    golden_tip: Mapped[str] = mapped_column(Text, default="")
    body_md: Mapped[str] = mapped_column(Text, default="")
    exercise_prompt: Mapped[str] = mapped_column(Text, default="")
    exercise_expected: Mapped[str] = mapped_column(Text, default="")
    read_minutes: Mapped[int] = mapped_column(Integer, default=10)
    exercise_minutes: Mapped[int] = mapped_column(Integer, default=35)
    status: Mapped[str] = mapped_column(String(12), default="empty")  # empty|draft|ai|published
    source_file: Mapped[str] = mapped_column(String(200), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyLog(Base):
    """یک رکورد برای هر روز دوره. level: ۰=خالی، ۱=سریع/خوداظهاری، ۲=مستند، ۳=کامل."""
    __tablename__ = "daily_logs"
    __table_args__ = (UniqueConstraint("course_id", "log_date", name="uq_course_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    day_number: Mapped[int] = mapped_column(Integer)
    log_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(10), default="done")  # done|read|token|pause
    level: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(500), default="")
    files: Mapped[list] = mapped_column(JSON, default=list)        # [{name, size, path}]
    source: Mapped[str] = mapped_column(String(8), default="web")  # web|rubika
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LessonVersion(Base):
    """تاریخچه نسخه‌های درس — ویرایش درس منتشرشده نباید نابودکننده باشد."""
    __tablename__ = "lesson_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"))
    title: Mapped[str] = mapped_column(String(250), default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    golden_tip: Mapped[str] = mapped_column(Text, default="")
    body_md: Mapped[str] = mapped_column(Text, default="")
    exercise_prompt: Mapped[str] = mapped_column(Text, default="")
    exercise_expected: Mapped[str] = mapped_column(Text, default="")
    read_minutes: Mapped[int] = mapped_column(Integer, default=10)
    exercise_minutes: Mapped[int] = mapped_column(Integer, default=35)
    status: Mapped[str] = mapped_column(String(12), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Notification(Base):
    """صندوق خروجی بازوی پیگیری (روبیکا). بدون توکن، همه ارسال‌ها این‌جا ثبت می‌شوند."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(12))          # morning|evening|weekly|congrats|phase|test
    chat_id: Mapped[str] = mapped_column(String(80), default="me")
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued|sent|failed|skipped
    reason: Mapped[str] = mapped_column(String(120), default="")
    day_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


engine = create_engine(DB_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_setting(s: Session, key: str, default: str = "") -> str:
    row = s.get(Setting, key)
    return row.value if row else default


def set_setting(s: Session, key: str, value: str) -> None:
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
    s.commit()


DEFAULTS = {
    "active_course_id": "",          # اگر خالی باشد، اولین دوره فعال انتخاب می‌شود
    "morning_time": "08:00",
    "evening_time": "18:00",
    "notify_weekdays": "0,1,2,3,4,5",  # شنبه..پنجشنبه (جمعه پیش‌فرض خاموش)
    "paused_until": "",               # تاریخ پایان توقف (YYYY-MM-DD)
    "tokens_left": "2",               # ژتون مرخصی ماهانه
    "tokens_month": "",               # ماهی که ژتون‌ها به آن تعلق دارد
    "longest_streak": "0",
    "rubika_token": "",
    "rubika_chat_id": "",
    "rubika_base_url": "https://botapi.rubika.ir/v3",   # قابل تغییر
    "scheduler_enabled": "1",
}


def ensure_defaults(s: Session) -> None:
    for k, v in DEFAULTS.items():
        if s.get(Setting, k) is None:
            s.add(Setting(key=k, value=v))
    s.commit()
