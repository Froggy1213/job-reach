"""Job Hunter Bot -- composition root.

This is the entry point and dependency-injection composition root.
Every service is instantiated and wired here explicitly -- there is
no auto-discovery, no magic, and no global mutable state.  The
dependency graph is fully visible in this single file.

Architecture layers (inner depends on outer? no -- concentric):

::

    ┌──────────────────────────────────────────────┐
    │                    main.py                    │  ← composition root
    ├──────────────────────────────────────────────┤
    │  bot/           scrapers/       database/     │  ← application layers
    │  (aiogram)      (playwright)   (sqlalchemy)   │
    ├──────────────────────────────────────────────┤
    │  core/          config/         models/       │  ← infrastructure
    └──────────────────────────────────────────────┘

Adding a new job board requires exactly three changes:
    1. Write the scraper in ``scrapers/implementations/``.
    2. Add the enum member to ``models/enums.py``.
    3. Register the instance in the ``scrapers`` list below.

No other file is touched.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.handlers import jobs as jobs_handler
from bot.handlers import start as start_handler
from bot.middlewares.container_middleware import ContainerMiddleware
from config.settings import Settings
from core.container import Container
from core.logging_setup import setup_logging
from database.engine import create_engine_and_session
from database.models import Base
from database.sqlalchemy_repository import SQLAlchemyJobRepository, SQLAlchemySubscriberRepository
from scrapers.implementations.mynavi2027 import Mynavi2027Scraper
from scrapers.implementations.wantedly import WantedlyScraper
from scrapers.cli.linkedin import LinkedInScraper
from scrapers.orchestrator import ScraperOrchestrator
from services.notifier import ScrapeNotifierService

logger = logging.getLogger("job_hunter")


async def main() -> None:
    """Bootstrap and run the bot.

    Startup order:
        1. Load & validate configuration (.env)
        2. Configure structured logging
        3. Create database engine + tables
        4. Instantiate repository (SQLAlchemy adapter)
        5. Register scrapers (Strategy pattern)
        6. Create orchestrator
        7. Build DI container
        8. Create bot + dispatcher + middleware + routers
        9. Initialise scheduler with periodic scrape job
       10. Start polling
    """
    # ---- 1. Configuration (validates .env at import time) ----
    settings = Settings()

    # ---- 2. Structured logging ----
    setup_logging(settings.log_level)
    logger.info(
        "Starting Job Hunter bot",
        extra={
            "database_url": settings.database_url,
            "headless": settings.playwright_headless,
            "admin_chat_id": settings.admin_chat_id,
        },
    )

    # ---- 3. Database ----
    engine, session_factory = create_engine_and_session(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")

    repository = SQLAlchemyJobRepository(session_factory)
    subscriber_repo = SQLAlchemySubscriberRepository(session_factory)

    # ---- 4. Scrapers (Strategy pattern -- add new boards here) ----
    scrapers = [
        Mynavi2027Scraper(headless=settings.playwright_headless, timeout_ms=settings.playwright_timeout_ms),
        WantedlyScraper(headless=settings.playwright_headless, timeout_ms=settings.playwright_timeout_ms),
        LinkedInScraper(headless=settings.playwright_headless, timeout_ms=settings.playwright_timeout_ms),
    ]
    orchestrator = ScraperOrchestrator(scrapers, repository)
    logger.info(
        "Scrapers registered",
        extra={"platforms": [p.value for p in orchestrator.platforms]},
    )

    # ---- 5. DI Container ----
    container = Container()
    container.settings = settings
    container.repository = repository
    container.orchestrator = orchestrator
    container.subscriber_repository = subscriber_repo
    container.logger = logger

    # ---- 6. Bot & Dispatcher ----
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()

    # Middleware -- injects Container into every handler's data dict
    dp.message.middleware(ContainerMiddleware(container))
    dp.callback_query.middleware(ContainerMiddleware(container))

    # Routers -- each module exports a standalone aiogram.Router
    dp.include_router(start_handler.router)
    dp.include_router(jobs_handler.router)

    # ---- 7. Scheduled scraping ----
    notifier = ScrapeNotifierService(
        orchestrator=orchestrator,
        bot=bot,
        subscriber_repo=subscriber_repo,
        admin_chat_id=settings.admin_chat_id,
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        notifier.run_scrape_and_notify,
        trigger=IntervalTrigger(hours=settings.scrape_interval_hours),
        id="scheduled_scrape",
        name="Periodic job board scrape",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),  # run immediately on startup
    )
    scheduler.start()
    logger.info(
        "Scheduler started",
        extra={"interval_hours": settings.scrape_interval_hours},
    )

    # ---- 8. Graceful shutdown ----
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: signal.Signals) -> None:
        logger.info("Received signal %s, initiating graceful shutdown", sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler, sig)

    # ---- 9. Polling ----
    logger.info("Bot starting polling")
    polling_task = asyncio.create_task(dp.start_polling(bot))

    # Wait for either the shutdown signal or polling to end on its own.
    done, _ = await asyncio.wait(
        [polling_task, asyncio.create_task(shutdown_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # ---- 10. Cleanup ----
    logger.info("Shutting down")
    scheduler.shutdown(wait=False)

    if not polling_task.done():
        await dp.stop_polling()
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

    await bot.session.close()
    await engine.dispose()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
