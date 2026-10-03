"""``python -m jobreach`` entry point."""

from __future__ import annotations

import sys

MIN_PYTHON_VERSION = (3, 11)


def unsupported_message(version_info: tuple[int, ...] = sys.version_info) -> str:
    """Return an actionable error message if *version_info* is below 3.11, else empty string."""
    if version_info >= MIN_PYTHON_VERSION:
        return ""
    running_version = ".".join(str(part) for part in version_info[:3])
    return (
        f"job-reach requires Python 3.11 or newer (running on Python {running_version} via {sys.executable}).\n"
        "Run the engine with a modern interpreter, for example:\n"
        "  uv run --python 3.12 --no-project python -m jobreach doctor"
    )


def main() -> int:
    message = unsupported_message()
    if message:
        sys.stderr.write(message + "\n")
        return 1
    from .cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
