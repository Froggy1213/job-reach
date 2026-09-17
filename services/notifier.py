"""Scrape-then-notify service.

Encapsulates the periodic scrape → admin summary → subscriber broadcast
workflow that was previously a closure inside ``main.py``.  Extracting
it into a class makes the logic testable in isolation and keeps the
composition root focused on wiring.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramRetryAfter

from bot.utils import job_card_no_index

if TYPE_CHECKING:
    from aiogram import Bot
    from database.sqlalchemy_repository import SQLAlchemySubscriberRepository
    from scrapers.orchestrator import ScraperOrchestrator
    from models.job_posting import JobPosting

logger = logging.getLogger("job_hunter.notifier")

_MAX_MESSAGE_LENGTH = 4_000  # Telegram limit is 4096; we split before that.


class ScrapeNotifierService:
    """Run a full scrape cycle and notify admin + subscribers.

    Args:
        orchestrator: The ``ScraperOrchestrator`` to execute.
        bot: An aiogram ``Bot`` instance for sending messages.
        subscriber_repo: Repository for fetching subscriber chat IDs.
        admin_chat_id: Telegram chat ID that receives the admin summary.
    """

    def __init__(
        self,
        orchestrator: ScraperOrchestrator,
        bot: Bot,
        subscriber_repo: SQLAlchemySubscriberRepository,
        admin_chat_id: int,
    ) -> None:
        self._orchestrator = orchestrator
        self._bot = bot
        self._subscriber_repo = subscriber_repo
        self._admin_chat_id = admin_chat_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_scrape_and_notify(self) -> None:
        """Execute all scrapers and notify subscribers about new jobs.

        This is the method passed to ``AsyncIOScheduler``.  It handles
        the full lifecycle: scrape → admin summary → subscriber broadcast.
        """
        logger.info("Scheduled scrape triggered")
        try:
            result = await self._orchestrator.run_all()
        except Exception:
            logger.exception("Scheduled scrape failed")
            return

        total_new = sum(result.counts.values())
        if total_new == 0:
            logger.info("Scheduled scrape found no new jobs")
            return

        # ---- Admin summary ----
        await self._send_admin_summary(result.counts, total_new)

        # ---- Broadcast new jobs to subscribers ----
        await self._broadcast_new_jobs(result.new_jobs)

        logger.info(
            "Scheduled scrape notification sent",
            extra={"new_jobs": total_new},
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_broadcast_card(job: JobPosting) -> str:
        """Format a single job posting for broadcast (no index number)."""
        return job_card_no_index(job)

    async def _send_admin_summary(
        self,
        counts: dict,
        total_new: int,
    ) -> None:
        """Send a scrape summary to the admin chat.

        If the message would exceed Telegram's length limit it is split
        across multiple messages.
        """
        platform_lines = [
            f"  • <code>{platform.value}</code>: {count} new"
            for platform, count in counts.items()
            if count > 0
        ]
        header = (
            f"<b>\U0001f514 Scrape complete!</b>\n"
            f"Found <b>{total_new}</b> new job listing(s):\n"
        )
        footer = "\n\nUse /jobs to view them."

        # Build full text; split if too long.
        full_text = header + "\n".join(platform_lines) + footer
        if len(full_text) <= _MAX_MESSAGE_LENGTH:
            await self._safe_send_message(
                chat_id=self._admin_chat_id,
                text=full_text,
            )
            return

        # Chunk: send header + as many platform lines as fit, then
        # the remaining lines in follow-up messages.
        chunks = self._chunk_lines(header, platform_lines, footer)
        for i, chunk in enumerate(chunks):
            await self._safe_send_message(
                chat_id=self._admin_chat_id,
                text=chunk,
            )

    async def _broadcast_new_jobs(self, new_jobs: list) -> None:
        """Send each new job to every subscribed chat."""
        if not new_jobs:
            return

        subscribers = await self._subscriber_repo.get_all_subscribers()
        if not subscribers:
            return

        logger.info(
            "Broadcasting new jobs to subscribers",
            extra={"subscribers": len(subscribers), "jobs": len(new_jobs)},
        )
        for job in new_jobs:
            card = self._format_broadcast_card(job)
            text = f"<b>\U0001f195 New Job!</b>\n{card}"
            for chat_id in subscribers:
                await self._safe_send_message(
                    chat_id=chat_id,
                    text=text,
                )

    # ------------------------------------------------------------------
    # Resilience helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_lines(
        header: str,
        lines: list[str],
        footer: str,
    ) -> list[str]:
        """Split *lines* across multiple messages so each one fits
        within ``_MAX_MESSAGE_LENGTH``.

        The first chunk includes *header*; the last chunk includes *footer*.
        """
        if not lines:
            return [header + footer]

        full = header + "\n".join(lines) + footer
        if len(full) <= _MAX_MESSAGE_LENGTH:
            return [full]

        # Too long — split lines across chunks.
        chunks: list[str] = []
        current = header

        for i, line in enumerate(lines):
            is_last = i == len(lines) - 1
            overhead = len(footer) if is_last else 0
            candidate = current + line + "\n"
            if len(candidate) + overhead <= _MAX_MESSAGE_LENGTH:
                current = candidate
            else:
                chunks.append(current.rstrip("\n"))
                current = line + "\n"

        chunks.append(current.rstrip("\n") + footer)
        return chunks

    async def _safe_send_message(self, chat_id: int, text: str) -> None:
        """Send a message with retry-on-backpressure handling.

        Catches ``TelegramRetryAfter`` and sleeps for the requested
        duration before retrying.  Other exceptions are logged and
        swallowed so one failed notification doesn't block the rest.
        """
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )
        except TelegramRetryAfter as exc:
            logger.warning(
                "Telegram rate-limited, sleeping",
                extra={"chat_id": chat_id, "retry_after": exc.retry_after},
            )
            await asyncio.sleep(exc.retry_after)
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                )
            except Exception:
                logger.debug(
                    "Failed to notify subscriber after retry",
                    extra={"chat_id": chat_id},
                    exc_info=True,
                )
        except Exception:
            logger.debug(
                "Failed to notify subscriber",
                extra={"chat_id": chat_id},
                exc_info=True,
            )
