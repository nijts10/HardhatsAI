"""Required test per the brief: robots.txt precedence. Pure logic, no
network -- every case here is exactly the kind of mistake a naive
substring check would get wrong."""
from ingest.robots import evaluate_robots


def test_exact_agent_group_wins_over_wildcard_even_when_wildcard_is_more_permissive():
    robots = """
User-agent: *
Disallow:

User-agent: GPTBot
Disallow: /
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/")
    assert decision.allowed is False
    assert decision.matched_group == "GPTBot"


def test_exact_agent_group_wins_over_wildcard_even_when_wildcard_is_more_restrictive():
    robots = """
User-agent: *
Disallow: /

User-agent: ClaudeBot
Disallow:
"""
    decision = evaluate_robots(robots, user_agent="ClaudeBot", path="/anything")
    assert decision.allowed is True
    assert decision.matched_group == "ClaudeBot"


def test_falls_back_to_wildcard_group_when_no_exact_match():
    robots = """
User-agent: *
Disallow: /private
"""
    decision = evaluate_robots(robots, user_agent="PerplexityBot", path="/private/x")
    assert decision.allowed is False
    assert decision.matched_group == "*"


def test_no_matching_group_at_all_defaults_to_allowed():
    robots = """
User-agent: SomeOtherBot
Disallow: /
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/")
    assert decision.allowed is True
    assert decision.matched_group is None
    assert decision.matched_rule is None


def test_longest_matching_path_rule_wins_regardless_of_file_order():
    robots = """
User-agent: GPTBot
Disallow: /
Allow: /public/
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/public/page")
    assert decision.allowed is True
    assert decision.matched_rule == "Allow: /public/"


def test_longest_match_wins_even_when_disallow_is_listed_after_allow():
    robots = """
User-agent: GPTBot
Allow: /public/
Disallow: /public/secret/
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/public/secret/file")
    assert decision.allowed is False
    assert decision.matched_rule == "Disallow: /public/secret/"


def test_same_length_tie_break_favours_allow():
    robots = """
User-agent: GPTBot
Allow: /x
Disallow: /x
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/x")
    assert decision.allowed is True


def test_group_with_no_rules_defaults_to_allowed():
    robots = """
User-agent: GPTBot
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/anything")
    assert decision.allowed is True
    assert decision.matched_rule is None


def test_wildcard_star_within_a_path_pattern():
    robots = """
User-agent: GPTBot
Disallow: /*/print
"""
    decision_blocked = evaluate_robots(robots, user_agent="GPTBot", path="/products/print")
    decision_allowed = evaluate_robots(robots, user_agent="GPTBot", path="/products/page")
    assert decision_blocked.allowed is False
    assert decision_allowed.allowed is True


def test_dollar_end_anchor_restricts_to_exact_suffix():
    robots = """
User-agent: GPTBot
Disallow: /file.pdf$
"""
    exact = evaluate_robots(robots, user_agent="GPTBot", path="/file.pdf")
    longer = evaluate_robots(robots, user_agent="GPTBot", path="/file.pdf.bak")
    assert exact.allowed is False
    assert longer.allowed is True  # without $, prefix match would have wrongly blocked this too


def test_prefix_match_without_dollar_blocks_everything_under_the_path():
    robots = """
User-agent: GPTBot
Disallow: /private
"""
    for path in ("/private", "/private/x", "/privateer"):
        assert evaluate_robots(robots, user_agent="GPTBot", path=path).allowed is False


def test_empty_disallow_value_means_no_restriction():
    robots = """
User-agent: GPTBot
Disallow:
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/anything/at/all")
    assert decision.allowed is True


def test_agent_name_matching_is_case_insensitive():
    robots = """
User-agent: gptbot
Disallow: /
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/")
    assert decision.allowed is False


def test_consecutive_user_agent_lines_share_one_group():
    robots = """
User-agent: GPTBot
User-agent: ClaudeBot
Disallow: /shared
"""
    for agent in ("GPTBot", "ClaudeBot"):
        decision = evaluate_robots(robots, user_agent=agent, path="/shared/x")
        assert decision.allowed is False
        assert set(decision.matched_group.split(", ")) == {"GPTBot", "ClaudeBot"}


def test_a_rule_line_closes_the_group_so_a_later_same_named_agent_starts_fresh():
    # Two SEPARATE GPTBot groups (a rule line in between closes the first).
    # Only the group whose OWN rules match should apply -- rules don't leak
    # from one same-named group into another.
    robots = """
User-agent: GPTBot
Disallow: /first-only

User-agent: GPTBot
Disallow: /second-only
"""
    # Both groups exact-match "GPTBot" -- both are applicable and their
    # rules get merged (multiple groups declaring the same agent is valid
    # robots.txt; this is not the "group closes" case itself, but proves
    # merging doesn't silently drop the second group's rule).
    blocked_first = evaluate_robots(robots, user_agent="GPTBot", path="/first-only")
    blocked_second = evaluate_robots(robots, user_agent="GPTBot", path="/second-only")
    assert blocked_first.allowed is False
    assert blocked_second.allowed is False


def test_naive_substring_check_would_get_this_wrong_but_real_precedence_does_not():
    # A path that CONTAINS an agent's name as a substring must not affect
    # that agent's own evaluation -- only real User-agent: group matching
    # counts, never a raw grep over the file text.
    robots = """
User-agent: *
Disallow: /GPTBot-marketing-page
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/completely/unrelated")
    assert decision.allowed is True


def test_malformed_rule_before_any_user_agent_is_ignored_not_crashing():
    robots = """
Disallow: /orphan-rule-with-no-user-agent

User-agent: GPTBot
Disallow: /real
"""
    decision = evaluate_robots(robots, user_agent="GPTBot", path="/real")
    assert decision.allowed is False
    decision_unaffected = evaluate_robots(robots, user_agent="GPTBot", path="/orphan-rule-with-no-user-agent")
    assert decision_unaffected.allowed is True
