"""Frozen cohort selection — generic, not tied to any publisher.

Membership is decided manually (never on a cron), frozen on stated criteria,
and followed forever, including members that die. Two qualifying paths:

  established  — top-N by a metric, above a floor
  new_entrant  — created since a date, any size

The new-entrant path is what prevents a survivor-only sample. Each
re-selection writes a new dated vintage file; old vintages are never edited.
The effective cohort is the union of every vintage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CohortCriteria:
    metric_key: str
    created_key: str
    established_top_n: int
    established_floor: float
    new_entrant_since: str  # ISO date; entities created on/after qualify at any size
    id_key: str = "id"


@dataclass
class Member:
    entity_id: str
    paths: list[str]
    metric_value: float | None = None
    created_at: str = ""


@dataclass
class Vintage:
    selected_at: str  # ISO date the selection was frozen
    criteria: dict
    members: list[Member] = field(default_factory=list)


def select_vintage(candidates: list[dict], criteria: CohortCriteria, selected_at: str) -> Vintage:
    """Pure selection over a candidate listing. Deterministic for fixed input."""
    by_id: dict[str, Member] = {}

    def member_for(item: dict) -> Member:
        entity_id = str(item[criteria.id_key])
        if entity_id not in by_id:
            metric = item.get(criteria.metric_key)
            by_id[entity_id] = Member(
                entity_id=entity_id,
                paths=[],
                metric_value=float(metric) if metric is not None else None,
                created_at=str(item.get(criteria.created_key, "")),
            )
        return by_id[entity_id]

    ranked = sorted(
        (c for c in candidates if c.get(criteria.metric_key) is not None),
        key=lambda c: (-float(c[criteria.metric_key]), str(c[criteria.id_key])),
    )
    for item in ranked[: criteria.established_top_n]:
        if float(item[criteria.metric_key]) >= criteria.established_floor:
            member_for(item).paths.append("established")

    for item in candidates:
        created = str(item.get(criteria.created_key, ""))
        if created and created[:10] >= criteria.new_entrant_since:
            member = member_for(item)
            if "new_entrant" not in member.paths:
                member.paths.append("new_entrant")

    members = sorted(by_id.values(), key=lambda m: m.entity_id)
    return Vintage(selected_at=selected_at, criteria=asdict(criteria), members=members)


def write_vintage(cohort_dir: Path | str, vintage: Vintage) -> Path:
    """Write cohorts/<cohort>/<selected_at>.yml. Existing vintages are immutable."""
    cohort_dir = Path(cohort_dir)
    cohort_dir.mkdir(parents=True, exist_ok=True)
    path = cohort_dir / f"{vintage.selected_at}.yml"
    if path.exists():
        raise FileExistsError(
            f"{path} already exists — vintages are never edited; select under a new date"
        )
    payload = {
        "selected_at": vintage.selected_at,
        "criteria": vintage.criteria,
        "members": [
            {
                "entity_id": m.entity_id,
                "paths": m.paths,
                "metric_value": m.metric_value,
                "created_at": m.created_at,
            }
            for m in vintage.members
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def load_vintages(cohort_dir: Path | str) -> list[Vintage]:
    cohort_dir = Path(cohort_dir)
    if not cohort_dir.is_dir():
        return []
    vintages = []
    for path in sorted(p for p in cohort_dir.iterdir() if p.suffix in (".yml", ".yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        vintages.append(
            Vintage(
                selected_at=str(data["selected_at"]),
                criteria=data.get("criteria", {}),
                members=[
                    Member(
                        entity_id=m["entity_id"],
                        paths=list(m.get("paths", [])),
                        metric_value=m.get("metric_value"),
                        created_at=str(m.get("created_at", "")),
                    )
                    for m in data.get("members", [])
                ],
            )
        )
    return vintages


def effective_members(cohort_dir: Path | str) -> dict[str, dict]:
    """Union across all vintages — once in, never out, dead or alive."""
    members: dict[str, dict] = {}
    for vintage in load_vintages(cohort_dir):
        for m in vintage.members:
            entry = members.setdefault(
                m.entity_id,
                {"entity_id": m.entity_id, "first_selected": vintage.selected_at, "paths": []},
            )
            for p in m.paths:
                if p not in entry["paths"]:
                    entry["paths"].append(p)
    return members
