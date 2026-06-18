"""Scheduler worker entrypoint.

Runs APScheduler with a database-backed job store. Alongside a heartbeat, it
reconciles one check job per monitored product from each product's schedule
(every N minutes, or daily at a set time), runs the shared checker on that
cadence, and periodically re-reconciles so added/changed/removed products are
picked up without a restart. Per-domain politeness/jitter is enforced inside the
fetch path, so concurrent checks across stores stay well-behaved.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone as dt_timezone

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import models  # noqa: F401  (register tables / metadata)
from app.config import settings
from app.database import engine
from app.services import schedule as sched_math

log = logging.getLogger("worker")

_CHECK_PREFIX = "check:"
_scheduler: BlockingScheduler | None = None


def _resolve_timezone():
    try:
        return pytz.timezone(settings.timezone)
    except Exception:  # noqa: BLE001 - fall back to UTC on bad config
        log.warning("Unknown timezone %r, falling back to UTC", settings.timezone)
        return pytz.utc


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

def heartbeat() -> None:
    now = datetime.now(dt_timezone.utc).isoformat()
    jobs = 0
    if _scheduler is not None:
        try:
            jobs = sum(1 for j in _scheduler.get_jobs() if j.id.startswith(_CHECK_PREFIX))
        except Exception:  # noqa: BLE001
            jobs = 0
    log.info("Worker heartbeat at %s (%d check jobs)", now, jobs)
    try:
        from app.database import SessionLocal
        from app.services import settings_store
        db = SessionLocal()
        try:
            settings_store.set_values(db, {"worker_heartbeat_at": now,
                                           "worker_jobs": str(jobs)})
            # Pick up a log-level change made in the UI (separate web process).
            from app.logsetup import set_level
            set_level(settings_store.get_config(db).get("log_level") or settings.log_level)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist heartbeat: %s", exc)


def run_check(product_id: int) -> None:
    """Scheduled per-product check. Opens its own session; never raises."""
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import SessionLocal
        from app.models import Product
        from app.services import checker

        db = SessionLocal()
        try:
            product = db.execute(
                select(Product).where(Product.id == product_id)
                .options(selectinload(Product.urls))
            ).scalars().first()
            if product is None:
                log.info("Check job for missing product %s — skipping", product_id)
                return
            if not any(u.active for u in product.urls):
                return
            summary = checker.check_product(db, product)
            log.info("Checked product %s: %d/%d listings ok",
                     product_id, summary.ok, summary.checked)
            try:
                from app.services import alerting
                sent = alerting.deliver(db, product, summary)
                if sent:
                    log.info("Product %s: delivered %d alert(s)", product_id, sent)
            except Exception as exc:  # noqa: BLE001
                log.exception("Alert delivery failed for product %s: %s", product_id, exc)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - a bad check must not kill the scheduler
        log.exception("Check job failed for product %s: %s", product_id, exc)


def _trigger_for(product, tz):
    if (product.schedule_kind or "interval") == "daily":
        hour, minute = sched_math.parse_hhmm(product.daily_check_time)
        return CronTrigger(hour=hour, minute=minute, timezone=tz)
    interval = max(1, int(product.check_interval_minutes or settings.default_check_interval_minutes))
    return IntervalTrigger(minutes=interval, jitter=settings.check_jitter_seconds, timezone=tz)


def reconcile_jobs() -> None:
    """Sync one check job per monitored product with the current DB state."""
    sched = _scheduler
    if sched is None:
        return
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database import SessionLocal
        from app.models import Product

        tz = _resolve_timezone()
        db = SessionLocal()
        try:
            products = db.execute(
                select(Product).options(selectinload(Product.urls))
            ).scalars().unique().all()
            desired: dict[str, tuple[str, object]] = {}
            for p in products:
                if not sched_math.should_schedule(p.track_price, p.track_stock,
                                                  any(u.active for u in p.urls)):
                    continue
                sig = sched_math.schedule_signature(
                    p.schedule_kind, p.check_interval_minutes, p.daily_check_time,
                    settings.default_check_interval_minutes)
                desired[f"{_CHECK_PREFIX}{p.id}"] = (sig, p)
        finally:
            db.close()

        existing = {j.id: j for j in sched.get_jobs() if j.id.startswith(_CHECK_PREFIX)}
        added = updated = removed = 0
        for jid, (sig, p) in desired.items():
            job = existing.get(jid)
            trigger = _trigger_for(p, tz)
            if job is None:
                sched.add_job(
                    run_check, trigger=trigger, args=[p.id], id=jid, name=sig,
                    replace_existing=True, coalesce=True, max_instances=1,
                    misfire_grace_time=120,
                    next_run_time=datetime.now(tz) + timedelta(seconds=random.randint(5, 45)),
                )
                added += 1
            elif job.name != sig:
                sched.reschedule_job(jid, trigger=trigger)
                sched.modify_job(jid, name=sig)
                updated += 1
        for jid in existing:
            if jid not in desired:
                sched.remove_job(jid)
                removed += 1
        log.info("Reconciled product checks: %d active (+%d new, ~%d changed, -%d removed)",
                 len(desired), added, updated, removed)
    except Exception as exc:  # noqa: BLE001
        log.exception("Reconcile failed: %s", exc)


# --------------------------------------------------------------------------- #
# Scheduler wiring
# --------------------------------------------------------------------------- #

def build_scheduler() -> BlockingScheduler:
    global _scheduler
    tz = _resolve_timezone()
    jobstores = {"default": SQLAlchemyJobStore(engine=engine)}
    executors = {"default": ThreadPoolExecutor(max_workers=4)}
    job_defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 120}
    scheduler = BlockingScheduler(
        jobstores=jobstores, executors=executors, job_defaults=job_defaults, timezone=tz,
    )
    scheduler.add_job(heartbeat, trigger="interval", minutes=5, id="heartbeat",
                      replace_existing=True, coalesce=True, max_instances=1)
    scheduler.add_job(reconcile_jobs, trigger="interval",
                      minutes=settings.scheduler_reconcile_minutes, id="reconcile",
                      replace_existing=True, coalesce=True, max_instances=1,
                      next_run_time=datetime.now(tz))
    _scheduler = scheduler
    return scheduler


def _configure_logging() -> None:
    """Configure logging from the stored level (env default if unavailable)."""
    from app.logsetup import configure
    level = settings.log_level
    try:
        from app.database import SessionLocal
        from app.services import settings_store
        db = SessionLocal()
        try:
            level = settings_store.get_config(db).get("log_level") or level
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass
    configure(level, settings.log_file)


def main() -> None:
    _configure_logging()
    log.info("Starting %s worker (scheduler)...", settings.app_name)
    scheduler = build_scheduler()
    try:
        from app.database import SessionLocal
        from app.services import settings_store
        db = SessionLocal()
        try:
            settings_store.set_values(
                db, {"worker_started_at": datetime.now(dt_timezone.utc).isoformat()})
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record worker start: %s", exc)
    # Populate jobs + heartbeat right away so the status panel is accurate fast.
    reconcile_jobs()
    heartbeat()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down scheduler...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
