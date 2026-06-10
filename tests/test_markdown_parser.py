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


def test_tag_must_start_with_ascii_letter() -> None:
    # Digit-first tokens are not tags (server: [a-zA-Z] start).
    assert parse_body("#2026").tags == ()
    assert parse_body("#-dash").tags == ()


def test_tag_is_lowercased_on_extraction() -> None:
    assert parse_body("#Foo and #FOO and #foo").tags == ("foo",)


def test_tag_continuation_stops_at_non_ascii() -> None:
    # Matches the server regex exactly: `#café` yields `caf` because the
    # continuation set [a-zA-Z0-9_-] stops at the accented `é`.
    assert parse_body("#café").tags == ("caf",)


def test_tag_after_double_hash() -> None:
    # Server lookbehind is `(?<!\w)`, so a `#` glued to another `#` is fine.
    assert parse_body("##foo").tags == ("foo",)


def test_tag_glued_to_word_is_not_a_tag() -> None:
    assert parse_body("word#tag").tags == ()


def test_wikilink_all_four_forms_resolve_to_slug() -> None:
    parsed = parse_body("[[a]] [[b#h]] [[c|alias]] [[d#h|alias]]")
    assert parsed.wikilink_titles == ("a", "b", "c", "d")


def test_wikilink_heading_only_is_dropped() -> None:
    # `[[#h]]` carries no resolvable slug — server drops it, so do we.
    assert parse_body("[[#h]]").wikilink_titles == ()


def test_wikilink_slug_is_trimmed() -> None:
    assert parse_body("[[  Spaced Slug  #h]]").wikilink_titles == ("Spaced Slug",)
