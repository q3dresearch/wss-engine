from __future__ import annotations

import pytest

from snapshotter.cohort import CohortCriteria, effective_members, load_vintages, select_vintage, write_vintage

CRITERIA = CohortCriteria(
    metric_key="downloads",
    created_key="createdAt",
    established_top_n=2,
    established_floor=1_000,
    new_entrant_since="2026-06-01",
)

CANDIDATES_Q3 = [
    {"id": "acme/big-old", "downloads": 900_000, "createdAt": "2024-01-01T00:00:00Z"},
    {"id": "acme/mid-old", "downloads": 500_000, "createdAt": "2024-06-01T00:00:00Z"},
    {"id": "acme/small-old", "downloads": 400, "createdAt": "2023-01-01T00:00:00Z"},
    {"id": "acme/tiny-new", "downloads": 12, "createdAt": "2026-07-04T00:00:00Z"},
]


def test_two_qualifying_paths():
    vintage = select_vintage(CANDIDATES_Q3, CRITERIA, "2026-08-01")
    by_id = {m.entity_id: m for m in vintage.members}
    assert set(by_id) == {"acme/big-old", "acme/mid-old", "acme/tiny-new"}
    assert by_id["acme/big-old"].paths == ["established"]
    assert by_id["acme/tiny-new"].paths == ["new_entrant"]  # any size — no survivor-only sample
    assert "acme/small-old" not in by_id  # old and below the floor: neither path


def test_floor_applies_within_top_n():
    criteria = CohortCriteria(
        metric_key="downloads",
        created_key="createdAt",
        established_top_n=3,
        established_floor=1_000,
        new_entrant_since="2026-06-01",
    )
    vintage = select_vintage(CANDIDATES_Q3, criteria, "2026-08-01")
    by_id = {m.entity_id: m for m in vintage.members}
    assert "acme/small-old" not in by_id  # in top 3 but under the floor


def test_vintages_are_immutable(tmp_path):
    vintage = select_vintage(CANDIDATES_Q3, CRITERIA, "2026-08-01")
    path = write_vintage(tmp_path / "cohort", vintage)
    assert path.name == "2026-08-01.yml"
    with pytest.raises(FileExistsError, match="never edited"):
        write_vintage(tmp_path / "cohort", vintage)
    loaded = load_vintages(tmp_path / "cohort")
    assert loaded[0].members[0].entity_id == vintage.members[0].entity_id


def test_effective_cohort_keeps_the_dead(tmp_path):
    cohort_dir = tmp_path / "cohort"
    write_vintage(cohort_dir, select_vintage(CANDIDATES_Q3, CRITERIA, "2026-08-01"))
    # next quarter: big-old has died and vanished from the listing
    q4 = [c for c in CANDIDATES_Q3 if c["id"] != "acme/big-old"]
    write_vintage(cohort_dir, select_vintage(q4, CRITERIA, "2026-11-01"))
    members = effective_members(cohort_dir)
    assert "acme/big-old" in members  # followed forever, including members that die
    assert members["acme/big-old"]["first_selected"] == "2026-08-01"
    assert "acme/small-old" not in members  # still under the floor in both vintages
