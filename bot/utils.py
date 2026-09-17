"""Shared HTML utilities for bot handlers.

HTML parse mode only requires escaping ``<``, ``>``, and ``&`` --
far simpler than MarkdownV2's 18 reserved characters.
"""

from html import escape as _escape_html


def escape(text: str) -> str:
    """Escape *text* for Telegram HTML parse mode.

    Only ``<``, ``>``, and ``&`` are reserved in HTML.
    """
    return _escape_html(str(text), quote=False)


from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_keyboard() -> ReplyKeyboardMarkup:
    """Return the persistent main-menu keyboard.

    Buttons send commands so existing Command() handlers process them
    without any extra text-matching logic.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="/jobs"),
                KeyboardButton(text="/stats"),
            ],
            [
                KeyboardButton(text="/subscribe"),
                KeyboardButton(text="/unsubscribe"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Pick an action…",
    )


def job_card(job, index: int) -> str:
    """Format a single job posting as an HTML card block.

    Args:
        job: A ``JobPosting`` domain model.
        index: 1-based position in the current page.

    Returns:
        An HTML-formatted string for use in a Telegram message.
    """
    title = escape(job.title)
    company = escape(job.company)
    location = escape(job.location)
    salary = escape(job.salary or "—")
    url = escape(str(job.url))
    source = escape(job.source_platform.value)

    return (
        f"<b>{index}. <a href=\"{url}\">{title}</a></b>\n"
        f"🏢 {company}\n"
        f"📍 {location}  |  💰 {salary}\n"
        f"📡 <code>{source}</code>"
    )


def job_card_no_index(job) -> str:
    """Format a single job posting as an HTML card block (no index number).

    Used by the broadcast notifier where jobs are sent individually,
    not as part of a numbered list.

    Args:
        job: A ``JobPosting`` domain model.

    Returns:
        An HTML-formatted string for use in a Telegram message.
    """
    title = escape(job.title)
    company = escape(job.company)
    location = escape(job.location)
    salary = escape(job.salary or "—")
    url = escape(str(job.url))
    source = escape(job.source_platform.value)

    return (
        f'<b><a href="{url}">{title}</a></b>\n'
        f"🏢 {company}\n"
        f"📍 {location}  |  💰 {salary}\n"
        f"📡 <code>{source}</code>"
    )

