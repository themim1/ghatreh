"""قطره — زمان‌بند (APScheduler): هر دقیقه یک tick سبک."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from .db import SessionLocal
from .rubika import tick

_scheduler: BackgroundScheduler | None = None


def _job() -> None:
    with SessionLocal() as s:
        try:
            tick(s)
        except Exception as exc:  # noqa: BLE001 — زمان‌بند نباید بخوابد
            print(f"[scheduler] tick failed: {exc!r}")


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    sch = BackgroundScheduler(timezone="Asia/Tehran")
    sch.add_job(_job, "interval", seconds=60, id="tick", max_instances=1,
                coalesce=True, replace_existing=True)
    sch.start()
    _scheduler = sch
    return sch


def stop() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
