"""robots.txt as RFC 9309 reads it, not as urllib.robotparser does."""

from collector.services.fetch import parse_robots

UA = "PositiveNewsCrawler/0.1 (+operator@example.invalid)"


def test_disallow_query_marker_does_not_forbid_the_site():
    """`Disallow: /?` forbade vokrugsveta.ru entirely under urllib.robotparser."""
    rules = parse_robots("User-agent: *\nDisallow: /?\nDisallow: /bitrix/\n", UA)

    assert rules.allows("https://www.vokrugsveta.ru/news/")
    assert rules.allows("https://www.vokrugsveta.ru/")
    assert not rules.allows("https://www.vokrugsveta.ru/?page=2")
    assert not rules.allows("https://www.vokrugsveta.ru/bitrix/admin/")


def test_wildcards_and_end_anchor():
    rules = parse_robots("User-agent: *\nDisallow: /*?s=\nDisallow: /*.pdf$\nDisallow: */page/\n", UA)

    assert not rules.allows("https://a.example/search?s=cats")
    assert not rules.allows("https://a.example/files/report.pdf")
    assert rules.allows("https://a.example/files/report.pdf.html")
    assert not rules.allows("https://a.example/news/page/2/")
    assert rules.allows("https://a.example/news/2026/09/cats/")


def test_longest_rule_wins_and_allow_wins_a_tie():
    rules = parse_robots("User-agent: *\nDisallow: /news/\nAllow: /news/good/\nAllow: /x\nDisallow: /x\n", UA)

    assert not rules.allows("https://a.example/news/bad/")
    assert rules.allows("https://a.example/news/good/1")
    assert rules.allows("https://a.example/x")


def test_our_own_group_beats_the_star_group():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: PositiveNewsCrawler\nDisallow: /private/\n"
    rules = parse_robots(text, UA)

    assert rules.allows("https://a.example/news/")
    assert not rules.allows("https://a.example/private/x")


def test_other_bots_groups_do_not_apply_and_grouped_agents_share_rules():
    text = (
        "User-agent: Yandex\nDisallow: /\n\n"
        "User-agent: Googlebot\nUser-agent: *\nDisallow: /admin/\n"
    )
    rules = parse_robots(text, UA)

    assert rules.allows("https://a.example/news/")
    assert not rules.allows("https://a.example/admin/")


def test_full_disallow_and_empty_disallow():
    assert not parse_robots("User-agent: *\nDisallow: /\n", UA).allows("https://a.example/news/")
    assert parse_robots("User-agent: *\nDisallow:\n", UA).allows("https://a.example/news/")
    assert parse_robots("", UA).allows("https://a.example/news/")
    assert parse_robots("User-agent: *\nDisallow: /\n", UA).allows("https://a.example/robots.txt")
