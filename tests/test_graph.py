"""Graph BFS traversal over the wikilinks table."""

from __future__ import annotations

from app.models import Note, WikiLink
from app.repositories.store import Store


def _note(
    note_id: str,
    filename: str,
    *,
    links_to: tuple[tuple[str, str | None], ...] = (),
) -> Note:
    return Note(
        id=note_id,
        filename=filename,
        title=filename.lstrip("!@$%&-=.+ "),
        family="permanent",
        kind="permanent",
        source=None,
        body="body",
        frontmatter={"kind": "permanent"},
        tags=(),
        wikilinks=tuple(
            WikiLink(target_title=title, target_id=target_id) for title, target_id in links_to
        ),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


def _seed_chain(store: Store) -> None:
    # A -> B -> C -> D. Plus E -> A (tests 'in' direction).
    ids = ["A", "B", "C", "D", "E"]
    notes = {
        "A": _note("A", "! A", links_to=(("B", "B"), ("Broken", None))),
        "B": _note("B", "! B", links_to=(("C", "C"),)),
        "C": _note("C", "! C", links_to=(("D", "D"),)),
        "D": _note("D", "! D"),
        "E": _note("E", "! E", links_to=(("A", "A"),)),
    }
    for nid in ids:
        store.upsert_note(notes[nid], path=f"note/! {nid}.md", body_sha256=nid)


def test_graph_depth_1_both_directions(store: Store) -> None:
    _seed_chain(store)
    nodes, edges, broken = store.graph_neighbourhood("A", depth=1, direction="both")
    node_ids = set(nodes.keys())
    assert node_ids == {"A", "B", "E"}  # forward 1 hop to B, backward 1 hop to E
    assert ("A", "B") in edges
    assert ("E", "A") in edges
    assert nodes["A"]["depth"] == 0
    assert nodes["B"]["depth"] == 1
    assert nodes["E"]["depth"] == 1
    assert broken == ["Broken"]


def test_graph_depth_2_forward_only(store: Store) -> None:
    _seed_chain(store)
    nodes, edges, _ = store.graph_neighbourhood("A", depth=2, direction="out")
    assert set(nodes.keys()) == {"A", "B", "C"}
    assert ("A", "B") in edges
    assert ("B", "C") in edges
    assert nodes["C"]["depth"] == 2


def test_graph_depth_3_forward_reaches_leaf(store: Store) -> None:
    _seed_chain(store)
    nodes, _, _ = store.graph_neighbourhood("A", depth=3, direction="out")
    assert set(nodes.keys()) == {"A", "B", "C", "D"}
    assert nodes["D"]["depth"] == 3


def test_graph_depth_0_is_just_the_start(store: Store) -> None:
    _seed_chain(store)
    nodes, edges, _ = store.graph_neighbourhood("A", depth=0, direction="both")
    assert set(nodes.keys()) == {"A"}
    assert edges == []


def test_graph_incoming_only(store: Store) -> None:
    _seed_chain(store)
    nodes, edges, _ = store.graph_neighbourhood("A", depth=1, direction="in")
    assert set(nodes.keys()) == {"A", "E"}
    assert ("E", "A") in edges
    # Outgoing link to B should NOT appear.
    assert ("A", "B") not in edges
