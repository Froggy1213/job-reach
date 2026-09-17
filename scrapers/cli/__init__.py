"""CLI-based scrapers that shell out to external tools.

These scrapers implement the same ``BaseScraper`` interface as Playwright
scrapers, but instead of controlling a browser themselves they invoke an
external CLI tool (e.g. ``opencli linkedin``) via subprocess.
"""

from scrapers.cli.linkedin import LinkedInScraper

__all__ = ["LinkedInScraper"]
