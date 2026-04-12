"""API-dict → domain model conversion."""

from app.services.note_mapper import note_from_api, summary_from_api


def test_summary_from_api_handles_camel_case_dates() -> None:
    payload = {
        "id": "aaa",
        "filename": "@ Alice Voland",
        "title": "Alice Voland",
        "family": "person",
        "kind": "person",
        "source": None,
        "tags": ["friend"],
        "createdAt": "2024-01-01T10:00:00Z",
        "updatedAt": "2024-01-02T10:00:00Z",
    }
    summary = summary_from_api(payload)
    assert summary.id == "aaa"
    assert summary.filename == "@ Alice Voland"
    assert summary.tags == ("friend",)
    assert summary.updated_at == "2024-01-02T10:00:00Z"


def test_note_from_api_builds_wikilinks_from_link_map() -> None:
    payload = {
        "id": "bbb",
        "filename": "! Idea",
        "title": "Idea",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "body": "Linked to [[Other]] and [[Broken]]",
        "frontmatter": {"title": "Idea"},
        "tags": [],
        "linkMap": {"Other": "ccc", "Broken": None},
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }
    note = note_from_api(payload)
    assert note.id == "bbb"
    assert len(note.wikilinks) == 2
    targets = {link.target_title: link.target_id for link in note.wikilinks}
    assert targets == {"Other": "ccc", "Broken": None}


def test_note_from_api_tolerates_missing_optional_fields() -> None:
    payload = {
        "id": "ccc",
        "filename": "- Scratch",
        "title": "Scratch",
        "family": "fleeting",
        "kind": "fleeting",
        "body": "Just a thought",
    }
    note = note_from_api(payload)
    assert note.id == "ccc"
    assert note.body == "Just a thought"
    assert note.wikilinks == ()
    assert note.tags == ()
