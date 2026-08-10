"""robots.txt precedence -- pure logic, no network. Deliberately its own
module, no I/O, so it's exhaustively unit-testable: the brief is explicit
that "a wrong finding here destroys trust", so this cannot be a naive
substring check (grep-ing the raw file for an agent's name and hoping)
-- it implements the real precedence rules crawlers actually use:

1. Most-specific user-agent group wins. If the file has a group naming
   the exact agent (case-insensitive), ONLY that group's rules apply --
   a `*` (wildcard) group elsewhere in the file is ignored entirely, even
   if it would otherwise be more permissive/restrictive. Only fall back
   to `*` when no exact-name group exists.
2. Within the applicable group(s), the LONGEST matching path rule wins,
   regardless of file order or Allow/Disallow. Ties (same length) go to
   Allow, per the de facto standard (Google's documented robots.txt
   interpretation) most real crawlers follow.
3. No matching group, or a group with no rules at all, or a path that
   matches no rule in the applicable group -- default is ALLOWED. A
   robots.txt only ever restricts, it never needs to explicitly permit.
4. Path patterns support the two real robots.txt wildcards: `*` (any
   sequence, including empty) and a trailing `$` (anchors end-of-path).
   Without `$`, a pattern is a PREFIX match (`Disallow: /private` blocks
   `/private`, `/private/x`, `/privateer` -- all of them).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class RobotsGroup:
    agents: list[str]
    rules: list[tuple[str, str]]  # (directive: 'allow' | 'disallow', pattern)


@dataclass
class RobotsDecision:
    allowed: bool
    matched_rule: str | None    # e.g. "Disallow: /private/*" -- None when nothing matched (default-allow)
    matched_group: str | None   # e.g. "GPTBot" or "*" -- None when no group applied to this agent at all


def parse_robots_txt(text: str) -> list[RobotsGroup]:
    """Standard grouping algorithm: consecutive User-agent: lines belong to
    ONE group and share its rules. A rule line (Allow/Disallow) closes the
    group to further User-agent lines -- the next User-agent: line starts
    a brand-new group, even if it names an agent already seen elsewhere."""
    groups: list[RobotsGroup] = []
    current_agents: list[str] = []
    current_rules: list[tuple[str, str]] = []
    seen_rule_since_agent = False

    def flush() -> None:
        nonlocal current_agents, current_rules, seen_rule_since_agent
        if current_agents:
            groups.append(RobotsGroup(agents=list(current_agents), rules=list(current_rules)))
        current_agents, current_rules, seen_rule_since_agent = [], [], False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()

        if directive == "user-agent":
            if seen_rule_since_agent:
                flush()
            current_agents.append(value)
        elif directive in ("allow", "disallow"):
            if not current_agents:
                continue  # a rule with no preceding User-agent: is malformed -- ignore it
            current_rules.append((directive, value))
            seen_rule_since_agent = True
        # sitemap/crawl-delay/other directives don't affect allow/disallow -- ignored here

    flush()
    return groups


def _pattern_regex(pattern: str) -> re.Pattern:
    if pattern == "":
        # `Disallow:` with an empty value is the documented convention for
        # "no restriction" -- make it a pattern that can never match.
        return re.compile(r"(?!)")
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    escaped = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(f"^{escaped}$" if anchored else f"^{escaped}")


def _matches(pattern: str, path: str) -> bool:
    return _pattern_regex(pattern).match(path) is not None


def evaluate_robots(robots_txt: str, *, user_agent: str, path: str = "/") -> RobotsDecision:
    groups = parse_robots_txt(robots_txt)

    exact = [g for g in groups if any(a.lower() == user_agent.lower() for a in g.agents if a != "*")]
    applicable = exact if exact else [g for g in groups if "*" in g.agents]

    if not applicable:
        return RobotsDecision(True, None, None)

    best_len = -1
    best_directive: str | None = None
    best_pattern: str | None = None
    best_agents: list[str] | None = None

    for group in applicable:
        for directive, pattern in group.rules:
            if not _matches(pattern, path):
                continue
            length = len(pattern)
            # Strictly longer always wins; on a tie, Allow beats Disallow
            # (the de facto standard tie-break) -- otherwise keep whichever
            # was found first.
            if length > best_len or (length == best_len and directive == "allow" and best_directive == "disallow"):
                best_len, best_directive, best_pattern, best_agents = length, directive, pattern, group.agents

    if best_directive is None:
        return RobotsDecision(True, None, ", ".join(applicable[0].agents))

    return RobotsDecision(best_directive == "allow", f"{best_directive.title()}: {best_pattern}", ", ".join(best_agents))
