"""قطره — وب‌اپ شخصی (FastAPI + Jinja2 + HTMX). تمام صفحات سرور-رندری‌اند."""
from __future__ import annotations

import io
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import markdown as md_lib
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (HTMLResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import content as ct
from . import rubika, scheduler
from . import services as sv
from .db import (BASE_DIR, UPLOAD_DIR, Course, DailyLog, Lesson, LessonVersion,
                 Notification, SessionLocal, ensure_defaults, get_setting, init_db,
                 set_setting)

APP_DIR = Path(__file__).resolve().parent
MAX_FILE = 5 * 1024 * 1024


# ---------- راه‌اندازی ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as s:
        ensure_defaults(s)
        ct.seed_demo(s)
    if get_setting_val("scheduler_enabled", "1") == "1":
        scheduler.start()
    yield
    scheduler.stop()


def get_setting_val(key: str, default: str = "") -> str:
    with SessionLocal() as s:
        return get_setting(s, key, default)


app = FastAPI(title="قطره — سامانه شخصی یادگیری قطره‌ای", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def render_md(text: str) -> str:
    return md_lib.markdown(text or "", extensions=["fenced_code", "sane_lists", "tables"])


templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["fa"] = sv.fa
templates.env.filters["jd"] = sv.fa_date
templates.env.filters["jdf"] = sv.fa_date_full
templates.env.filters["wd"] = sv.weekday_fa
templates.env.filters["md"] = render_md
templates.env.globals["today"] = sv.today


def get_db():
    with SessionLocal() as s:
        yield s


def flash(url: str, msg: str = "", kind: str = "ok") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}msg={quote(msg)}&kind={kind}", status_code=303)


def page_ctx(s: Session, request: Request, page: str, **extra):
    course = sv.active_course(s)
    ctx = {
        "request": request, "page": page, "course": course,
        "msg": request.query_params.get("msg", ""),
        "kind": request.query_params.get("kind", "ok"),
        "theme": request.cookies.get("theme", "light"),
        "now": sv.now(), "today_date": sv.today(), "one_day": timedelta(days=1),
        "settings": {k: get_setting(s, k) for k in
                     ["morning_time", "evening_time", "paused_until", "tokens_left",
                      "rubika_token", "rubika_chat_id", "longest_streak", "scheduler_enabled"]},
    }
    if course:
        ctx.update({
            "stats": sv.course_stats(s, course),
            "streak": sv.compute_streak(s, course),
            "week": sv.week_map(s, course),
            "buffer": sv.buffer_days(s, course),
        })
        ctx["current"] = ctx["stats"]["current"]
        ctx["current_lesson"] = sv.lesson_for(s, course.id, ctx["current"])
    ctx.update(extra)
    return ctx


# ---------- S1 و S2 ----------
@app.get("/", response_class=HTMLResponse)
def today_page(request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "today")
    if not ctx["course"]:
        return templates.TemplateResponse(request=request, name="no_course.html", context=ctx)
    course = ctx["course"]
    st = ctx["streak"]
    ctx["today_log"] = sv.log_for(s, course.id, sv.today())
    ctx["tomorrow_lesson"] = sv.lesson_for(s, course.id, ctx["current"] + 1)
    ctx["buffer_warn"] = ctx["buffer"] < 5
    return templates.TemplateResponse(request=request, name="today.html", context=ctx)


@app.get("/day/{n}", response_class=HTMLResponse)
def day_page(n: int, request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "day")
    course = ctx["course"]
    if not course:
        return RedirectResponse("/", status_code=303)
    if n > ctx["current"]:
        return flash(f"/day/{ctx['current']}", "درس بعدی پس از ثبت قطره امروز باز می‌شود.", "warn")
    les = sv.lesson_for(s, course.id, n)
    if not les:
        return flash("/", f"درس روز {sv.fa(n)} هنوز ساخته نشده.", "warn")
    my_log = sv.log_for(s, course.id, sv.today()) if n == ctx["current"] else None
    ctx.update({
        "lesson": les, "body_html": render_md(les.body_md),
        "log": my_log,
        "level": (my_log.level or 1) if my_log else 1,
        "note": my_log.note if my_log else "",
        "link": my_log.link if my_log else "",
        "prev_n": n - 1 if n > 1 else None,
        "next_n": n + 1 if sv.lesson_for(s, course.id, n + 1) else None,
        "next_locked": n + 1 > ctx["current"] and not sv.log_for(s, course.id, sv.today()),
        "lesson_index": n,
    })
    return templates.TemplateResponse(request=request, name="day.html", context=ctx)


@app.post("/day/{n}/submit", response_class=HTMLResponse)
async def submit_evidence(n: int, request: Request,
                          level: int = Form(1), note: str = Form(""),
                          link: str = Form(""),
                          files: list[UploadFile] = File(default=[]),
                          s: Session = Depends(get_db)):
    course = sv.active_course(s)
    les = sv.lesson_for(s, course.id, n)
    if not les:
        return HTMLResponse("<div class='callout bad'>درس پیدا نشد.</div>")

    saved: list[dict] = []
    for f in files:
        if not f or not f.filename:
            continue
        raw = await f.read()
        if len(raw) > MAX_FILE:
            return templates.TemplateResponse(request=request, name="partials/sheet_body.html", context={
                "request": request, "lesson": les, "level": level, "note": note, "link": link,
                "error": f"حجم فایل {sv.fa(round(len(raw) / 1048576, 1))} مگابایت است و سقف ۵ مگابایت. "
                         "عکس را فشرده کن یا لینک بفرست — بقیه فرم دست‌نخورده است.",
                "streak": sv.compute_streak(s, course), "lesson_index": n,
            })
        name = f"{uuid.uuid4().hex[:10]}{Path(f.filename).suffix.lower()}"
        (UPLOAD_DIR / name).write_bytes(raw)
        saved.append({"name": f.filename, "size": len(raw), "path": name})

    if level >= 2 and not saved and not link.strip():
        return templates.TemplateResponse(request=request, name="partials/sheet_body.html", context={
            "request": request, "lesson": les, "level": level, "note": note, "link": link,
            "error": "برای سطح «مستند»، یک اسکرین‌شات یا لینک لازم است. "
                     "اگر امروز عجله داری، سطح «سریع» را انتخاب کن.",
            "streak": sv.compute_streak(s, course), "lesson_index": n,
        })

    t = sv.today()
    log = sv.log_for(s, course.id, t)
    is_new = log is None
    if log is None:
        log = DailyLog(course_id=course.id, day_number=n, log_date=t)
        s.add(log)
    log.kind = "done"
    log.level = level
    log.note = note.strip()
    log.link = link.strip()
    if saved:
        log.files = (log.files or []) + saved if not is_new else saved
    log.submitted_at = sv.now()
    s.commit()

    st = sv.compute_streak(s, course)
    if is_new:
        rubika.send_congrats(s, course, level, st["streak"])
    if not request.headers.get("HX-Request"):
        return flash(f"/day/{n}", "قطره امروز ثبت شد — Streak به‌روز شد.", "ok")
    ctx = {
        "request": request, "lesson": les, "log": log, "level": level,
        "streak": st, "current": n, "next_n": n + 1, "course": course,
        "today_date": sv.today(), "now": sv.now(),
        "next_lesson": sv.lesson_for(s, course.id, n + 1),
        "lesson_index": n,
    }
    return templates.TemplateResponse(request=request, name="partials/submit_success.html", context=ctx)


@app.post("/day/{n}/read")
def only_read(n: int, s: Session = Depends(get_db)):
    course = sv.active_course(s)
    t = sv.today()
    log = sv.log_for(s, course.id, t)
    if log is None:
        s.add(DailyLog(course_id=course.id, day_number=n, log_date=t, kind="read",
                       level=0, note="", source="web"))
        s.commit()
    return flash(f"/day/{n}", "«فقط مطالعه» ثبت شد — Streak حفظ می‌شود ولی نشان تأییدشده نمی‌گیری.", "ok")


@app.post("/grace/submit")
def grace_submit(note: str = Form(""), s: Session = Depends(get_db)):
    """جبران دیروز در ۱۵ ثانیه: یک خط یادداشت، سطح ۱ — Streak حفظ می‌شود."""
    course = sv.active_course(s)
    y = sv.today() - timedelta(days=1)
    if sv.log_for(s, course.id, y):
        return flash("/", "دیروز قبلاً ثبت شده است.", "warn")
    day_no = max(sv.current_day(s, course) - 1, 1)
    s.add(DailyLog(course_id=course.id, day_number=day_no, log_date=y, kind="done",
                   level=1, note=note.strip(), source="web"))
    s.commit()
    st = sv.compute_streak(s, course)
    rubika.send_congrats(s, course, 1, st["streak"])
    return flash("/", f"مدرک دیروز ثبت شد — Streak {sv.fa(st['streak'])} حفظ شد.", "ok")


# ---------- S4 / S6 ----------
@app.get("/path", response_class=HTMLResponse)
def path_page(request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "path")
    course = ctx["course"]
    if not course:
        return RedirectResponse("/", status_code=303)
    rows = s.scalars(select(Lesson).where(Lesson.course_id == course.id)
                     .order_by(Lesson.day_number)).all()
    logs = {l.day_number: l for l in s.scalars(select(DailyLog)
                                               .where(DailyLog.course_id == course.id)).all()}
    weeks: dict[int, list] = {}
    for les in rows:
        weeks.setdefault(les.week_number, []).append({
            "lesson": les, "log": logs.get(les.day_number),
            "is_today": les.day_number == ctx["current"],
            "is_future": les.day_number > ctx["current"],
        })
    ctx["weeks"] = sorted(weeks.items())
    ctx["logs_count"] = len(logs)
    return templates.TemplateResponse(request=request, name="path.html", context=ctx)


@app.get("/evidence", response_class=HTMLResponse)
def evidence_page(request: Request, level: int = 0, q: str = "",
                  s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "evidence")
    course = ctx["course"]
    rows = s.scalars(select(DailyLog).where(DailyLog.course_id == course.id)
                     .order_by(DailyLog.log_date.desc())).all()
    if level:
        rows = [r for r in rows if r.level == level]
    if q:
        rows = [r for r in rows if q in (r.note or "")]
    ctx.update({"logs": rows, "level": level, "q": q,
                "files_total": sum(len(r.files or []) for r in rows)})
    return templates.TemplateResponse(request=request, name="evidence.html", context=ctx)


# ---------- S7 تنظیمات ----------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "settings")
    ctx["courses"] = s.scalars(select(Course)).all()
    ctx["paused"] = rubika.paused(s)
    ctx["outbox"] = s.scalars(select(Notification).order_by(Notification.id.desc()).limit(5)).all()
    return templates.TemplateResponse(request=request, name="settings.html", context=ctx)


@app.post("/settings")
def settings_save(morning_time: str = Form("08:00"), evening_time: str = Form("18:00"),
                  tokens_left: str = Form("2"), active_course_id: str = Form(""),
                  scheduler_enabled: str = Form(""),
                  rubika_token: str = Form(""), rubika_chat_id: str = Form(""),
                  s: Session = Depends(get_db)):
    set_setting(s, "morning_time", morning_time)
    set_setting(s, "evening_time", evening_time)
    set_setting(s, "tokens_left", tokens_left)
    set_setting(s, "scheduler_enabled", "1" if scheduler_enabled else "0")
    set_setting(s, "rubika_token", rubika_token.strip())
    set_setting(s, "rubika_chat_id", rubika_chat_id.strip())
    if active_course_id:
        set_setting(s, "active_course_id", active_course_id)
        for c in s.scalars(select(Course)).all():
            c.is_active = str(c.id) == active_course_id
        s.commit()
    return flash("/settings", "تنظیمات ذخیره شد.")


@app.post("/settings/pause")
def settings_pause(days: int = Form(3), s: Session = Depends(get_db)):
    until = sv.today() + timedelta(days=days)
    set_setting(s, "paused_until", sv.iso(until))
    return flash("/settings", f"یادآوری‌ها تا {sv.fa(days)} روز خاموش شد — روزها «ازدست‌رفته» حساب نمی‌شوند.", "warn")


@app.post("/settings/resume")
def settings_resume(s: Session = Depends(get_db)):
    set_setting(s, "paused_until", "")
    return flash("/settings", "یادآوری‌ها دوباره فعال شد.")


@app.post("/settings/token")
def settings_token(s: Session = Depends(get_db)):
    course = sv.active_course(s)
    left = int(get_setting(s, "tokens_left", "0") or 0)
    if left <= 0:
        return flash("/settings", "ژتون این ماه تمام شده است.", "bad")
    t = sv.today()
    if sv.log_for(s, course.id, t):
        return flash("/settings", "امروز قبلاً ثبت شده.", "warn")
    s.add(DailyLog(course_id=course.id, day_number=sv.current_day(s, course), log_date=t,
                   kind="token", level=0, note="مرخصی ژتون‌دار"))
    set_setting(s, "tokens_left", str(left - 1))
    s.commit()
    return flash("/settings", "امروز «مرخصی» شد، نه «ازدست‌رفته». Streak سالم ماند.", "ok")


# ---------- روبیکا ----------
@app.get("/rubika/preview", response_class=HTMLResponse)
def rubika_preview(request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "rubika")
    course = ctx["course"]
    les = ctx["current_lesson"]
    ctx["morning"] = sv.morning_message(s, course, les, ctx["current"], None) if les else ""
    ctx["evening"] = sv.evening_message(course, ctx["current"])
    ctx["congrats"] = sv.congrats_message(ctx["streak"]["streak"] or 1, 1)
    ctx["back"] = sv.back_message(ctx["streak"]["record"] or 19)
    ctx["weekly"] = sv.weekly_message(s, course)
    ctx["connected"] = bool(get_setting(s, "rubika_token").strip())
    return templates.TemplateResponse(request=request, name="rubika_preview.html", context=ctx)


@app.post("/rubika/test")
def rubika_test(s: Session = Depends(get_db)):
    course = sv.active_course(s)
    n = rubika.enqueue(s, "test", "این یک پیام آزمایشی از قطره است ✅\nاگر آن را می‌بینی، اتصال سالم است.")
    return flash("/rubika/preview", "پیام آزمایشی ساخته شد — در لاگ اعلان‌ها ببین.", "ok")


# ---------- پنل مدیریت محتوا ----------
@app.get("/admin", response_class=HTMLResponse)
def admin_root():
    return RedirectResponse("/admin/lessons", status_code=303)


@app.get("/admin/lessons", response_class=HTMLResponse)
def admin_lessons(request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "admin_lessons")
    course = ctx["course"]
    rows = s.scalars(select(Lesson).where(Lesson.course_id == course.id)
                     .order_by(Lesson.day_number)).all()
    ctx["lessons"] = rows
    ctx["stats_status"] = {k: len([r for r in rows if r.status == k])
                           for k in ["published", "ai", "draft", "empty"]}
    ctx["coverage"] = sv.coverage(s, course, ctx["current"], 30)
    weeks: dict[int, list] = {}
    for les in rows:
        if les.day_number >= ctx["current"] - 2:
            weeks.setdefault(les.week_number, []).append(les)
    ctx["weeks"] = sorted(weeks.items())
    ctx["ai_lessons"] = [r for r in rows if r.status == "ai"]
    return templates.TemplateResponse(request=request, name="admin/lessons.html", context=ctx)


@app.get("/admin/lesson/new", response_class=HTMLResponse)
def admin_lesson_new_page(request: Request, day: int = 0, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "admin_new")
    course = ctx["course"]
    rows = s.scalars(select(Lesson.day_number).where(Lesson.course_id == course.id)
                     .order_by(Lesson.day_number)).all()
    ctx["used_days"] = set(rows)
    ctx["suggest_day"] = sv.current_day(s, course) + 7
    ctx["total_days"] = course.total_days
    ctx["next_md"] = ct.template_markdown(ctx["suggest_day"] if not day else day)
    return templates.TemplateResponse(request=request, name="admin/new.html", context=ctx)


@app.post("/admin/lesson/new")
def admin_lesson_new(day_number: int = Form(...), s: Session = Depends(get_db)):
    course = sv.active_course(s)
    if sv.lesson_for(s, course.id, day_number):
        return flash("/admin/lessons", "این روز از قبل وجود دارد.", "warn")
    les = Lesson(course_id=course.id, day_number=day_number, status="draft",
                 phase_number=(day_number - 1) // 30 + 1, week_number=(day_number - 1) // 7 + 1)
    s.add(les)
    s.commit()
    return flash(f"/admin/lesson/{les.id}", "روز جدید ساخته شد — محتوا را وارد کن.")


@app.get("/admin/lesson/{lid}", response_class=HTMLResponse)
def admin_lesson_edit(lid: int, request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "admin_edit")
    les = s.get(Lesson, lid)
    if les is None:
        return flash("/admin/lessons", "این درس پیدا نشد.", "bad")
    ctx["lesson"] = les
    ctx["body_html"] = render_md(les.body_md)
    ctx["versions"] = s.scalars(select(LessonVersion).where(LessonVersion.lesson_id == lid)
                                .order_by(LessonVersion.id.desc()).limit(6)).all()
    ctx["prompts"] = sv.PROMPT_LIBRARY
    return templates.TemplateResponse(request=request, name="admin/edit.html", context=ctx)


@app.post("/admin/lesson/{lid}", response_class=HTMLResponse)
def admin_lesson_save_full(lid: int, title: str = Form(""), goal: str = Form(""),
                           golden_tip: str = Form(""), body_md: str = Form(""),
                           exercise_prompt: str = Form(""), exercise_expected: str = Form(""),
                           read_minutes: int = Form(10), exercise_minutes: int = Form(35),
                           kind: str = Form("lesson"), status: str = Form(""),
                           s: Session = Depends(get_db)):
    """ذخیره + اختیاری تغییر وضعیت، در یک کلیک (نسخه قبلی snapshot می‌شود)."""
    les = s.get(Lesson, lid)
    if les is None:
        return flash("/admin/lessons", "این درس پیدا نشد.", "bad")
    _snapshot(s, les)
    les.title, les.goal, les.golden_tip = title.strip(), goal.strip(), golden_tip.strip()
    les.body_md = body_md
    les.exercise_prompt, les.exercise_expected = exercise_prompt.strip(), exercise_expected.strip()
    les.read_minutes, les.exercise_minutes = read_minutes, exercise_minutes
    les.kind = kind if kind in {"lesson", "project", "review"} else "lesson"
    msg = "ذخیره شد — نسخه قبلی در تاریخچه ماند."
    if status in ct.LESSON_STATUSES:
        les.status = status
        if status == "published":
            les.published_at = sv.now()
            msg = "ذخیره و منتشر شد — از این به بعد در پیام صبحگاهی می‌آید."
            if not title.strip() or not body_md.strip():
                msg = "ذخیره شد، ولی برای انتشار، عنوان و متن درس لازم است."
                les.status = "draft"
        else:
            msg = {"draft": "ذخیره شد و به پیش‌نویس رفت.",
                   "ai": "ذخیره شد — وضعیت: AI ویرایش‌نشده.",
                   "empty": "ذخیره شد — وضعیت: خالی."}.get(status, msg)
    s.commit()
    return flash(f"/admin/lesson/{lid}", msg, "warn" if les.status != "published" else "ok")


def _snapshot(s: Session, les: Lesson) -> None:
    if les.title or les.body_md:
        s.add(LessonVersion(lesson_id=les.id, title=les.title, goal=les.goal,
                            golden_tip=les.golden_tip, body_md=les.body_md,
                            exercise_prompt=les.exercise_prompt,
                            exercise_expected=les.exercise_expected,
                            read_minutes=les.read_minutes,
                            exercise_minutes=les.exercise_minutes, status=les.status))


@app.post("/admin/lesson/{lid}/save")
def admin_lesson_save(lid: int, title: str = Form(""), goal: str = Form(""),
                      golden_tip: str = Form(""), body_md: str = Form(""),
                      exercise_prompt: str = Form(""), exercise_expected: str = Form(""),
                      read_minutes: int = Form(10), exercise_minutes: int = Form(35),
                      kind: str = Form("lesson"), s: Session = Depends(get_db)):
    les = s.get(Lesson, lid)
    if les is None:
        return flash("/admin/lessons", "این درس پیدا نشد.", "bad")
    _snapshot(s, les)
    les.title, les.goal, les.golden_tip = title.strip(), goal.strip(), golden_tip.strip()
    les.body_md = body_md
    les.exercise_prompt, les.exercise_expected = exercise_prompt.strip(), exercise_expected.strip()
    les.read_minutes, les.exercise_minutes = read_minutes, exercise_minutes
    les.kind = kind if kind in {"lesson", "project", "review"} else "lesson"
    if les.status in {"empty", "draft"} and title.strip() and body_md.strip():
        les.status = "draft"
    s.commit()
    return flash(f"/admin/lesson/{lid}", "ذخیره شد (نسخه قبلی در تاریخچه نگه داشته شد).")


@app.post("/admin/lesson/{lid}/restore/{vid}")
def admin_lesson_restore(lid: int, vid: int, s: Session = Depends(get_db)):
    les, ver = s.get(Lesson, lid), s.get(LessonVersion, vid)
    if les and ver:
        for f in ["title", "goal", "golden_tip", "body_md", "exercise_prompt",
                  "exercise_expected", "read_minutes", "exercise_minutes", "status"]:
            setattr(les, f, getattr(ver, f))
        s.commit()
    return flash(f"/admin/lesson/{lid}", "نسخه انتخابی بازگردانی شد.", "warn")


@app.post("/admin/lesson/{lid}/preview", response_class=HTMLResponse)
def admin_lesson_preview(lid: int, request: Request, title: str = Form(""),
                         goal: str = Form(""), golden_tip: str = Form(""),
                         body_md: str = Form(""), exercise_prompt: str = Form(""),
                         exercise_expected: str = Form(""), read_minutes: int = Form(10),
                         exercise_minutes: int = Form(35), kind: str = Form("lesson"),
                         s: Session = Depends(get_db)):
    les = s.get(Lesson, lid)
    if les is None:
        return HTMLResponse("<div class='callout bad'>درس پیدا نشد.</div>")
    preview = SimpleNamespace(
        id=lid, day_number=les.day_number, phase_number=les.phase_number,
        week_number=les.week_number, status=les.status,
        title=(title.strip() or les.title), goal=goal.strip(), golden_tip=golden_tip.strip(),
        body_md=body_md, exercise_prompt=exercise_prompt.strip(),
        exercise_expected=exercise_expected.strip(),
        read_minutes=read_minutes, exercise_minutes=exercise_minutes,
        kind=kind if kind in {"lesson", "project", "review"} else "lesson")
    return templates.TemplateResponse(request=request, name="partials/preview.html", context={
        "request": request, "lesson": preview, "body_html": render_md(body_md),
    })


@app.post("/admin/lesson/{lid}/status")
def admin_lesson_status(lid: int, status: str = Form(...), s: Session = Depends(get_db)):
    les = s.get(Lesson, lid)
    if les is None:
        return flash("/admin/lessons", "این درس پیدا نشد.", "bad")
    if status in ct.LESSON_STATUSES:
        les.status = status
        if status == "published":
            les.published_at = sv.now()
        s.commit()
    return flash(f"/admin/lesson/{lid}" if request_wants_edit(status) else "/admin/lessons",
                 {"published": "منتشر شد — در صف پیام قرار گرفت.", "ai": "به «AI · ویرایش‌نشده» برگشت.",
                  "draft": "به پیش‌نویس منتقل شد.", "empty": "خالی شد."}.get(status, "وضعیت تغییر کرد."),
                 "ok")


def request_wants_edit(status: str) -> bool:
    return status in {"draft", "ai", "empty"}


@app.get("/admin/upload", response_class=HTMLResponse)
def admin_upload(request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "admin_upload")
    ctx["courses"] = s.scalars(select(Course)).all()
    return templates.TemplateResponse(request=request, name="admin/upload.html", context=ctx)


@app.post("/admin/upload", response_class=HTMLResponse)
async def admin_upload_post(request: Request, mode: str = Form("add_new"),
                            publish_drafts: str = Form(""),
                            files: list[UploadFile] = File(default=[]),
                            s: Session = Depends(get_db)):
    course = sv.active_course(s)
    uploads: list[tuple[str, bytes]] = []
    for f in files:
        if f and f.filename:
            uploads.append((f.filename, await f.read()))
    report = ct.import_uploads(s, course, uploads, mode=mode, publish_drafts=bool(publish_drafts))
    ctx = page_ctx(s, request, "admin_upload", report=report,
                   filenames=[u[0] for u in uploads])
    ctx["courses"] = s.scalars(select(Course)).all()
    return templates.TemplateResponse(request=request, name="admin/upload.html", context=ctx)


@app.get("/admin/export")
def admin_export(s: Session = Depends(get_db)):
    course = sv.active_course(s)
    data = ct.export_zip(s, course)
    return StreamingResponse(io.BytesIO(data), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{course.slug}-lessons.zip"'})


@app.get("/admin/prompts", response_class=HTMLResponse)
def admin_prompts(request: Request, code: str = "prompt-07", course_title: str = "",
                  day_number: str = "", topic: str = "", level: str = "متوسط",
                  tone: str = "معلم حامی، بدون شعار", s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "admin_prompts")
    course = ctx["course"]
    day_number = day_number or str(ctx["current"] + 1)
    les = sv.lesson_for(s, course.id, int(day_number)) if day_number.isdigit() else None
    topic = topic or (les.title if les and les.title else "موضوع روز")
    course_title = course_title or course.title
    ctx["selected"] = next((p for p in sv.PROMPT_LIBRARY if p["code"] == code), sv.PROMPT_LIBRARY[0])
    ctx["vars"] = {"course_title": course_title, "day_number": sv.fa(day_number),
                   "topic": topic, "level": level, "tone": tone,
                   "week_number": str((int(day_number) - 1) // 7 + 1) if day_number.isdigit() else "۱",
                   "outline": "سرفصل‌ها این‌جا"}
    def fill(text: str) -> str:
        out = text
        for k, v in ctx["vars"].items():
            out = out.replace("{" + k + "}", v)
        return out

    ctx["prompts"] = [{**p, "filled": fill(p["body"])} for p in sv.PROMPT_LIBRARY]
    ctx["filled"] = fill(ctx["selected"]["body"])
    ctx["categories"] = sorted({p["category"] for p in sv.PROMPT_LIBRARY})
    return templates.TemplateResponse(request=request, name="admin/prompts.html", context=ctx)


@app.get("/admin/notifications", response_class=HTMLResponse)
def admin_notifications(request: Request, s: Session = Depends(get_db)):
    ctx = page_ctx(s, request, "admin_notifications")
    ctx["items"] = s.scalars(select(Notification).order_by(Notification.id.desc()).limit(40)).all()
    t = sv.today()
    ctx["today_count"] = len([n for n in ctx["items"] if n.created_at.date() == t])
    ctx["failed"] = len([n for n in ctx["items"] if n.status == "failed"])
    return templates.TemplateResponse(request=request, name="admin/notifications.html", context=ctx)


@app.post("/admin/notifications/tick")
def admin_notifications_tick(s: Session = Depends(get_db)):
    created = rubika.tick(s)
    return flash("/admin/notifications", f"موتور ارسال اجرا شد — {sv.fa(len(created))} پیام جدید.")


@app.post("/admin/notifications/{nid}/retry")
def admin_notifications_retry(nid: int, s: Session = Depends(get_db)):
    n = s.get(Notification, nid)
    n.status, n.reason = "queued", ""
    s.commit()
    rubika.try_real_send(s, n)
    return flash("/admin/notifications", "تلاش مجدد انجام شد.")


@app.get("/settings/theme")
def theme_toggle(request: Request, to: str = "dark"):
    resp = RedirectResponse(request.headers.get("referer", "/"), status_code=303)
    resp.set_cookie("theme", to, max_age=60 * 60 * 24 * 365)
    return resp


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.post("/api/tick", response_class=PlainTextResponse)
def api_tick(s: Session = Depends(get_db)):
    created = rubika.tick(s)
    return f"tick ok — {len(created)} new"
