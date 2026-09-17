"""Application configuration loaded from environment variables.

``Settings`` is a Pydantic ``BaseSettings`` subclass that reads from ``.env``
and environment variables automatically.  All configuration is validated at
startup so misconfiguration is caught immediately, not at runtime.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration.

    Values are read from a ``.env`` file in the project root and from
    environment variables, with environment variables taking precedence.
    Unknown extra keys in ``.env`` are silently ignored (``extra="ignore"``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(
        min_length=10,
        validation_alias="BOT_TOKEN",
        description="Telegram Bot API token from @BotFather.",
    )
    database_url: str = Field(
        default="sqlite+aiosqlite:///./jobs.db",
        validation_alias="DATABASE_URL",
        description="SQLAlchemy async database URL. Defaults to local SQLite.",
    )
    log_level: str = Field(
        default="INFO",
        validation_alias="LOG_LEVEL",
        description="Python logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    playwright_headless: bool = Field(
        default=True,
        validation_alias="PLAYWRIGHT_HEADLESS",
        description="Run Playwright browsers in headless mode.",
    )
    admin_chat_id: int = Field(
        validation_alias="ADMIN_CHAT_ID",
        description="Telegram chat ID where scheduled scrape alerts are sent.",
    )
    playwright_timeout_ms: int = Field(
        default=30000,
        validation_alias="PLAYWRIGHT_TIMEOUT_MS",
        description="Playwright page navigation timeout in milliseconds.",
    )
    scrape_interval_hours: float = Field(
        default=4.0,
        validation_alias="SCRAPE_INTERVAL_HOURS",
        description="Hours between scheduled scrape runs. Defaults to 4 hours.",
    )
