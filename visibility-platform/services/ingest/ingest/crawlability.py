"""Part 5a: crawlability -- can each AI crawler even fetch the brand's
site at all, per its own robots.txt policy? One fetch of the file, then
robots.py's real precedence logic (not a substring check) evaluates every
test agent against that same fetch.

HTTP status handling on the robots.txt fetch itself matters and is easy
to get subtly wrong:
  - 200: parse the body for real.
  - 404 (very common -- most sites simply don't have a robots.txt): the
    documented convention is "no restrictions", i.e. equivalent to an
    EMPTY file, NOT "parse whatever HTML error page came back" -- that
    HTML could coincidentally contain text that looks like a directive
    and produce a wrong finding, exactly the kind of mistake the brief
    warns "destroys trust". So a 404's body is stored for audit but
    evaluated as "".
  - anything else non-2xx, or a network-level failure (timeout, DNS,
    connection refused): genuinely UNDETERMINED, not "allowed" and not
    "blocked" -- recorded as allowed=None per agent (the tri-state this
    table is built around), never guessed either way.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from . import db
from .http_fetch import Fetcher
from .robots import evaluate_robots

# Brief's explicit test-agent list -- the real crawlers/fetchers behind
# today's major AI products.
TEST_AGENTS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User",
    "Claude-SearchBot", "PerplexityBot", "Perplexity-User", "Google-Extended",
    "Applebot-Extended", "CCBot", "meta-externalagent", "Bingbot",
]


@dataclass
class CrawlabilityStats:
    allowed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    undetermined: list[str] = field(default_factory=list)
    fetch_error: Optional[str] = None


def _robots_txt_url(website_url: str) -> str:
    """robots.txt always lives at the site ROOT, never relative to
    whatever path happens to be stored in brands.website (an unconstrained
    free-text column, e.g. set to 'https://example.com/products' by
    mistake) -- resolving against that path instead of the origin would
    fetch the wrong URL entirely and silently produce a wrong verdict."""
    parts = urlsplit(website_url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def run_crawlability_check(conn, *, brand_id: str, website_url: str, fetcher: Fetcher) -> tuple[str, CrawlabilityStats]:
    robots_txt_url = _robots_txt_url(website_url)
    result = fetcher(robots_txt_url)

    stats = CrawlabilityStats()

    if result.error is not None:
        stats.fetch_error = result.error
        robots_text: Optional[str] = None
    elif result.status_code == 404:
        robots_text = ""  # no robots.txt present -- convention is "no restrictions", not "parse the 404 page"
    elif result.status_code == 200:
        robots_text = result.text or ""
    else:
        stats.fetch_error = f"unexpected robots.txt status {result.status_code}"
        robots_text = None

    # Persist whichever error (if any) was actually determined above --
    # previously only a network-level result.error made it into the row,
    # so an unexpected-status case (e.g. 500) left crawlability_checks.error
    # NULL even though every agent below is recorded as undetermined,
    # silently dropping the reason a report/CLI reader would need.
    check_id = db.insert_crawlability_check(
        conn, brand_id=brand_id, website_url=website_url, robots_txt_url=robots_txt_url,
        http_status=result.status_code, robots_txt_body=result.text, error=stats.fetch_error,
    )

    for agent in TEST_AGENTS:
        if robots_text is None:
            db.insert_crawlability_agent(conn, check_id=check_id, agent_name=agent, allowed=None,
                                          matched_rule=None, matched_group=None)
            stats.undetermined.append(agent)
            continue

        decision = evaluate_robots(robots_text, user_agent=agent, path="/")
        db.insert_crawlability_agent(
            conn, check_id=check_id, agent_name=agent, allowed=decision.allowed,
            matched_rule=decision.matched_rule, matched_group=decision.matched_group,
        )
        (stats.allowed if decision.allowed else stats.blocked).append(agent)

    return check_id, stats
