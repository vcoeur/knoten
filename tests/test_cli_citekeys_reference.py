"""Tests for `knoten citekeys` and `knoten reference --from-source`.

Covers the pure `source_to_reference_inputs` mapping helper (no backend),
the `citekeys` read command (distinct/sorted/prefix, plain vs json), and
the `reference` command's dry-run preview + a local-backend create
round-trip. All CLI paths run in local mode — no network.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from knoten.cli.main import app
from knoten.repositories.errors import UserError
from knoten.services.notes import source_to_reference_inputs


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    """Point KNOTEN_* at a per-test tmp dir with no API URL — forces local mode."""
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    monkeypatch.setenv("KNOTEN_MODE", "local")
    return tmp_path


def _invoke(args: list[str], *, stdin: str | None = None) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(app, args, input=stdin)
    return result.exit_code, result.stdout


# ---- pure mapping helper -------------------------------------------------


def _book_source(**overrides) -> dict:
    source = {
        "title": "Radical Candor",
        "authors": [{"name": "Kim Scott", "orcid": "0000"}],
        "year": 2019,
        "publisher": "St. Martin's Press",
        "isbn_13": "9781250103505",
        "page_count": 272,
        "subjects": ["Management", "Leadership"],
        "kind": "book",
        "citation_key": "Scott2019",
    }
    source.update(overrides)
    return source


def test_helper_builds_filename_kind_and_frontmatter() -> None:
    inputs = source_to_reference_inputs(_book_source(), ai=False)
    assert inputs.filename == "Scott2019= Radical Candor"
    assert inputs.kind == "book"
    assert inputs.tags == []
    fm = inputs.frontmatter
    assert fm["family"] == "reference"
    assert fm["kind"] == "book"
    assert fm["source"] == "Scott2019"
    assert fm["title"] == "Radical Candor"
    assert fm["authors"] == ["[[@ Kim Scott]]"]
    assert fm["year"] == 2019
    assert fm["publisher"] == "St. Martin's Press"
    assert fm["isbn-13"] == "9781250103505"
    assert fm["page-count"] == 272
    assert fm["subjects"] == ["Management", "Leadership"]


def test_helper_multiple_authors_render_as_wikilinks() -> None:
    source = _book_source(authors=[{"name": "Ada Lovelace"}, {"name": "Alan Turing"}])
    inputs = source_to_reference_inputs(source, ai=False)
    assert inputs.frontmatter["authors"] == ["[[@ Ada Lovelace]]", "[[@ Alan Turing]]"]


def test_helper_sanitizes_wikilink_special_chars_in_filename_title() -> None:
    """`|`/`#`/`[`/`]` in a title break a `[[CiteKey= Title]]` wikilink (knoten
    resolves links by full filename, and `|` is the alias separator). They are
    stripped from the filename title; the original title stays in frontmatter."""
    source = {
        "title": "Working on projects | uv #docs [v2]",
        "kind": "web",
        "citation_key": "Astral2026-projects",
    }
    inputs = source_to_reference_inputs(source, ai=False)
    assert inputs.filename == "Astral2026-projects= Working on projects uv docs v2"
    assert "|" not in inputs.filename and "#" not in inputs.filename
    assert inputs.frontmatter["title"] == "Working on projects | uv #docs [v2]"


@pytest.mark.parametrize(
    ("quelle_kind", "expected"),
    [
        ("article", "article"),
        ("preprint", "article"),
        ("book", "book"),
        ("book-chapter", "book"),
        ("web", "web"),
        ("media", "media"),
        ("podcast", "document"),  # unknown → document
        (None, "document"),  # missing → document
    ],
)
def test_helper_kind_map_including_fallback(quelle_kind, expected) -> None:
    source = {"title": "T", "citation_key": "K2020"}
    if quelle_kind is not None:
        source["kind"] = quelle_kind
    inputs = source_to_reference_inputs(source, ai=False)
    assert inputs.kind == expected
    assert inputs.frontmatter["kind"] == expected


def test_helper_omits_missing_and_empty_fields() -> None:
    source = {
        "title": "Minimal",
        "citation_key": "K2021",
        "kind": "web",
        "publisher": "",  # empty string → omitted
        "edition": None,  # None → omitted
        "subjects": [],  # empty list → omitted
        "authors": [],  # empty list → omitted
    }
    inputs = source_to_reference_inputs(source, ai=False)
    fm = inputs.frontmatter
    assert set(fm) == {"family", "kind", "source", "title"}
    assert "publisher" not in fm
    assert "edition" not in fm
    assert "subjects" not in fm
    assert "authors" not in fm
    assert "isbn-13" not in fm


def test_helper_citekey_precedence_x_vcoeur_over_citation_key() -> None:
    source = _book_source(citation_key="Scott2019", x_vcoeur={"citekey": "Scott2019a"})
    inputs = source_to_reference_inputs(source, ai=False)
    assert inputs.frontmatter["source"] == "Scott2019a"
    assert inputs.filename.startswith("Scott2019a= ")


def test_helper_falls_back_to_citation_key_when_x_vcoeur_blank() -> None:
    source = _book_source(citation_key="Scott2019", x_vcoeur={"citekey": ""})
    inputs = source_to_reference_inputs(source, ai=False)
    assert inputs.frontmatter["source"] == "Scott2019"


def test_helper_raises_without_any_citekey() -> None:
    with pytest.raises(UserError):
        source_to_reference_inputs({"title": "No key", "kind": "book"}, ai=False)


def test_helper_ai_adds_tag() -> None:
    assert source_to_reference_inputs(_book_source(), ai=True).tags == ["ai"]


# ---- citekeys command ---------------------------------------------------


def _make_reference_note(citekey: str, title: str) -> None:
    """Create a reference note via the CLI so its `source` column = CiteKey."""
    code, out = _invoke(
        ["reference", "--from-source", "-", "--json"],
        stdin=json.dumps({"title": title, "kind": "book", "citation_key": citekey}),
    )
    assert code == 0, out


def test_citekeys_empty_vault_json(local_env) -> None:
    code, out = _invoke(["citekeys", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload == {"citekeys": [], "count": 0, "prefix": None}


def test_citekeys_distinct_sorted_json(local_env) -> None:
    _make_reference_note("Scott2019", "Radical Candor")
    _make_reference_note("Alice2026", "One")
    _make_reference_note("Alice2026a", "Two")

    code, out = _invoke(["citekeys", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["citekeys"] == ["Alice2026", "Alice2026a", "Scott2019"]
    assert payload["count"] == 3
    assert payload["prefix"] is None


def test_citekeys_plain_output_one_per_line(local_env) -> None:
    _make_reference_note("Scott2019", "Radical Candor")
    _make_reference_note("Alice2026", "One")

    code, out = _invoke(["citekeys"])
    assert code == 0, out
    assert out.splitlines() == ["Alice2026", "Scott2019"]


def test_citekeys_prefix_filter(local_env) -> None:
    _make_reference_note("Scott2019", "Radical Candor")
    _make_reference_note("Alice2026", "One")
    _make_reference_note("Alice2026a", "Two")

    code, out = _invoke(["citekeys", "--prefix", "Alice2026", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["citekeys"] == ["Alice2026", "Alice2026a"]
    assert payload["prefix"] == "Alice2026"


# ---- reference command --------------------------------------------------


def test_reference_dry_run_does_not_write(local_env) -> None:
    source = {
        "title": "Think Like a Commoner",
        "authors": [{"name": "David Bollier"}],
        "year": 2014,
        "kind": "book",
        "citation_key": "Bollier2014",
    }
    code, out = _invoke(
        ["reference", "--from-source", "-", "--dry-run", "--json"],
        stdin=json.dumps(source),
    )
    assert code == 0, out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["operation"] == "reference"
    assert payload["filename"] == "Bollier2014= Think Like a Commoner"
    assert payload["family"] == "reference"
    assert payload["kind"] == "book"
    assert payload["source"] == "Bollier2014"
    assert payload["frontmatter"]["authors"] == ["[[@ David Bollier]]"]

    # Nothing was created.
    code, out = _invoke(["list", "--json"])
    assert json.loads(out)["total"] == 0


def test_reference_create_round_trip(local_env) -> None:
    source = {
        "title": "Radical Candor",
        "authors": [{"name": "Kim Scott"}],
        "year": 2019,
        "publisher": "St. Martin's Press",
        "isbn_13": "9781250103505",
        "subjects": ["Management"],
        "kind": "book",
        "citation_key": "Scott2019",
    }
    code, out = _invoke(
        ["reference", "--from-source", "-", "--fields", "full", "--json"],
        stdin=json.dumps(source),
    )
    assert code == 0, out
    payload = json.loads(out)
    note_id = payload["id"]
    assert payload["filename"] == "Scott2019= Radical Candor"
    assert payload["family"] == "reference"
    assert payload["kind"] == "book"
    assert payload["source"] == "Scott2019"

    # Read it back: frontmatter round-trips through the store column.
    code, out = _invoke(["read", "--json", "--", note_id])
    assert code == 0, out
    read = json.loads(out)
    fm = read["frontmatter"]
    assert fm["source"] == "Scott2019"
    assert fm["authors"] == ["[[@ Kim Scott]]"]
    assert fm["year"] == 2019
    assert fm["isbn-13"] == "9781250103505"
    assert fm["subjects"] == ["Management"]

    # And the CiteKey now shows up in the taken set.
    code, out = _invoke(["citekeys", "--json"])
    assert "Scott2019" in json.loads(out)["citekeys"]


def test_reference_with_body_and_ai(local_env) -> None:
    source = {"title": "A Web Source", "kind": "web", "citation_key": "Site2025"}
    code, out = _invoke(
        ["reference", "--from-source", "-", "--body", "Summary blurb.", "--ai", "--json"],
        stdin=json.dumps(source),
    )
    assert code == 0, out
    note_id = json.loads(out)["id"]

    code, out = _invoke(["read", "--json", "--", note_id])
    read = json.loads(out)
    assert "#ai begin" in read["body"]
    assert "Summary blurb." in read["body"]
    assert read["kind"] == "web"
    # The `#ai` marker in the wrapped body yields the `ai` tag on ingest.
    assert "ai" in read["tags"]


def test_reference_ai_without_body_errors(local_env) -> None:
    source = {"title": "T", "kind": "book", "citation_key": "K2020"}
    code, out = _invoke(
        ["reference", "--from-source", "-", "--ai", "--json"],
        stdin=json.dumps(source),
    )
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


def test_reference_missing_citekey_errors(local_env) -> None:
    source = {"title": "No key", "kind": "book"}
    code, out = _invoke(
        ["reference", "--from-source", "-", "--json"],
        stdin=json.dumps(source),
    )
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


def test_reference_invalid_json_errors(local_env) -> None:
    code, out = _invoke(["reference", "--from-source", "-", "--json"], stdin="not json")
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


def test_reference_appears_in_schema(local_env) -> None:
    code, out = _invoke(["schema", "--json"])
    assert code == 0, out
    command_names = {c["name"] for c in json.loads(out)["commands"]}
    assert {"citekeys", "reference"} <= command_names
