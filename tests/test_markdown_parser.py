"""Tag and wiki-link extraction from markdown bodies."""

from knoten.services.markdown_parser import parse_body


def test_extracts_simple_tags_and_wikilinks() -> None:
    body = "Hello [[World]] and [[Epistemology]]. #search #encryption"
    parsed = parse_body(body)
    assert parsed.tags == ("search", "encryption")
    assert parsed.wikilink_titles == ("World", "Epistemology")


def test_ignores_tags_and_wikilinks_inside_code_fences() -> None:
    body = (
        "Before the block [[Real Link]] #real\n```python\n# not-a-tag\nlink = [[fake]]\n```\nAfter."
    )
    parsed = parse_body(body)
    assert parsed.tags == ("real",)
    assert parsed.wikilink_titles == ("Real Link",)


def test_ignores_drawing_blocks() -> None:
    body = 'Intro [[Alpha]] #intro\n```drawing\n{"content": "[[fake]] #fake"}\n```\nOutro [[Beta]]'
    parsed = parse_body(body)
    assert parsed.tags == ("intro",)
    assert parsed.wikilink_titles == ("Alpha", "Beta")


def test_deduplicates_results() -> None:
    body = "[[Same]] again [[Same]] #dup #dup"
    parsed = parse_body(body)
    assert parsed.tags == ("dup",)
    assert parsed.wikilink_titles == ("Same",)


def test_wiki_link_with_alias_uses_target_title() -> None:
    body = "See [[Real Title|displayed text]] for details."
    parsed = parse_body(body)
    assert parsed.wikilink_titles == ("Real Title",)


def test_empty_body() -> None:
    parsed = parse_body("")
    assert parsed.tags == ()
    assert parsed.wikilink_titles == ()
