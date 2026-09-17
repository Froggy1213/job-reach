"""/jobs command handler with pagination.

Shows 5 job postings per page with ⬅️ ➡️ inline navigation buttons.
Supports optional source-platform filtering via /jobs &lt;source&gt;.
"""

from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from bot.utils import escape, job_card, main_keyboard
from core.container import Container
from models.enums import SourcePlatform

logger = logging.getLogger("job_hunter.bot")
router = Router(name="jobs_handler")

_JOBS_PER_PAGE = 5

# ---------------------------------------------------------------------------
# /jobs command
# ---------------------------------------------------------------------------


@router.message(Command("jobs"))
async def cmd_jobs(
    message: Message,
    command: CommandObject,
    container: Container,
) -> None:
    """Handle /jobs [source] — show paginated job listings."""
    try:
        source = _parse_source(command.args)
    except ValueError:
        valid = ", ".join(p.value for p in SourcePlatform)
        await message.answer(
            f"❓ Unknown source. Available: {valid}",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    await _show_page(message, container, page=0, source=source)


# ---------------------------------------------------------------------------
# Inline keyboard callback (pagination)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("jobs:"))
async def on_jobs_page(callback: CallbackQuery, container: Container) -> None:
    """Handle ⬅️ ➡️ pagination button presses.

    Callback data format: ``jobs:{page}:{source}``
    - ``page``: 0-based page index
    - ``source``: ``"all"`` or a SourcePlatform value
    """
    if not callback.data:
        return

    _, page_str, source_str = callback.data.split(":", 2)
    page = int(page_str)
    source = None if source_str == "all" else SourcePlatform(source_str)

    if not isinstance(callback.message, Message):
        return

    await _show_page(callback.message, container, page=page, source=source, edit=True)
    await callback.answer()


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


async def _show_page(
    msg: Message,
    container: Container,
    *,
    page: int = 0,
    source: SourcePlatform | None = None,
    edit: bool = False,
) -> None:
    """Fetch jobs and render one page with navigation buttons.

    Args:
        msg: The Message to reply to or edit.
        container: DI container.
        page: 0-based page index.
        source: Optional platform filter.
        edit: If True, edit *msg* in-place; otherwise send a new message.
    """
    # Fetch with server-side pagination.
    repo = Container.require(container.repository, "repository")
    total = await repo.count_jobs(source)
    total_pages = max(1, (total + _JOBS_PER_PAGE - 1) // _JOBS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    offset = page * _JOBS_PER_PAGE
    page_jobs = await repo.get_jobs_page(
        limit=_JOBS_PER_PAGE, offset=offset, source=source,
    )

    # Build text.
    source_label = f"<code>{source.value}</code>" if source else "all"
    lines = [
        f"<b>📋 Jobs ({total} total, {source_label})</b>",
        f"Page {page + 1}/{total_pages}\n",
    ]

    if not page_jobs:
        lines.append("No jobs found. They will appear after the next scrape.")
    else:
        for i, job in enumerate(page_jobs, start=offset + 1):
            lines.append(job_card(job, i))

    text = "\n".join(lines)

    # Build inline keyboard.
    keyboard = _build_nav_keyboard(page, total_pages, source)

    if edit:
        try:
            await msg.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramBadRequest:
            # Identical content or other non-critical edit failure —
            # just acknowledge the callback silently.
            logger.debug("Edit_text no-op (content unchanged)", exc_info=True)
    else:
        await msg.answer(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_source(args: str | None) -> SourcePlatform | None:
    """Parse an optional source argument.

    Returns:
        A ``SourcePlatform`` or ``None`` (no filter).

    Raises:
        ValueError: If the argument is not a valid ``SourcePlatform`` value.
    """
    if not args or not args.strip():
        return None
    source_str = args.strip().lower()
    return SourcePlatform(source_str)


def _build_nav_keyboard(
    page: int,
    total_pages: int,
    source: SourcePlatform | None,
) -> InlineKeyboardMarkup | None:
    """Build ⬅️ ➡️ navigation buttons, or None if only one page."""
    if total_pages <= 1:
        return None

    source_key = source.value if source else "all"
    buttons: list[InlineKeyboardButton] = []

    if page > 0:
        buttons.append(InlineKeyboardButton(
            text="⬅️ Prev",
            callback_data=f"jobs:{page - 1}:{source_key}",
        ))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton(
            text="Next ➡️",
            callback_data=f"jobs:{page + 1}:{source_key}",
        ))

    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


# ---------------------------------------------------------------------------
# /scrape command (admin only)
# ---------------------------------------------------------------------------


@router.message(Command("scrape"))
async def cmd_scrape(message: Message, container: Container) -> None:
    """Manually trigger a full scrape cycle.  Admin only."""
    settings = Container.require(container.settings, "settings")
    if message.from_user is None or message.from_user.id != settings.admin_chat_id:
        return

    status_msg = await message.answer(
        "⏳ Starting scrape...", reply_markup=main_keyboard(),
    )

    orchestrator = Container.require(container.orchestrator, "orchestrator")
    result = await orchestrator.run_all()
    total_new = sum(result.counts.values())

    if total_new > 0:
        platform_lines = [
            f"  • <code>{platform.value}</code>: {count} new"
            for platform, count in result.counts.items()
            if count > 0
        ]
        summary = "\n".join(platform_lines)
        text = (
            f"<b>✅ Scrape complete!</b>\n"
            f"Found <b>{total_new}</b> new job listing(s):\n"
            f"{summary}"
        )
    else:
        text = "✅ Scrape complete! No new jobs found."

    try:
        await status_msg.edit_text(text, parse_mode="HTML")
    except TelegramBadRequest:
        logger.debug("Scrape status edit_text no-op", exc_info=True)
