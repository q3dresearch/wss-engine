"""Scaffold a new domain repo from the engine's templates.

The engine owns the boilerplate so it can never drift from the code that
consumes it: a repo scaffolded by `snapshotter init` is pinned to the engine
version that generated it, and the workflows it writes are the same ones the
engine's docs describe.

Dotfiles are stored in the template tree without their leading dot
(`github/`, `gitattributes`, `gitignore`) so packaging tools include them
reliably; they are renamed on the way out.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import __version__

TEMPLATE_ROOT = Path(__file__).parent / "templates" / "domain"

# Paths that must be renamed when written out.
DOTFILES = {
    "gitattributes": ".gitattributes",
    "gitignore": ".gitignore",
}

# Directories a domain repo needs before its first run, each with a .gitkeep
# so the layout is legible in a fresh clone.
EMPTY_DIRS = ("raw", "manifest", "derived", "health", "state")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class InitError(RuntimeError):
    pass


def _title_from_name(name: str) -> str:
    stem = re.sub(r"^wss[-_]", "", name)
    return stem.replace("-", " ").replace("_", " ").title() + " History"


def _out_path(rel: Path) -> Path:
    parts = list(rel.parts)
    if parts and parts[0] == "github":
        parts[0] = ".github"
    if parts and parts[-1] in DOTFILES:
        parts[-1] = DOTFILES[parts[-1]]
    return Path(*parts)


def render(text: str, values: dict[str, str]) -> str:
    """Replace {{KEY}} tokens. GitHub's own ${{ ... }} expressions are left
    alone because only these exact keys are substituted."""
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def scaffold(
    target: Path | str,
    *,
    owner: str,
    title: str | None = None,
    name: str | None = None,
    force: bool = False,
) -> list[Path]:
    """Write a domain-repo skeleton into `target`. Returns the files written."""
    target = Path(target)
    name = name or target.name
    if not NAME_RE.match(name):
        raise InitError(f"repo name {name!r} must start alphanumeric and contain only [A-Za-z0-9._-]")
    if not TEMPLATE_ROOT.is_dir():  # pragma: no cover - packaging guard
        raise InitError(f"templates missing from the installed engine: {TEMPLATE_ROOT}")
    if target.exists() and any(target.iterdir()) and not force:
        raise InitError(f"{target} is not empty (pass --force to write into it anyway)")

    values = {
        "NAME": name,
        "TITLE": title or _title_from_name(name),
        "OWNER": owner,
        "ENGINE_VERSION": __version__,
        "YEAR": str(date.today().year),
        "DATE": date.today().isoformat(),
    }

    written: list[Path] = []
    for src in sorted(p for p in TEMPLATE_ROOT.rglob("*") if p.is_file()):
        dest = target / _out_path(src.relative_to(TEMPLATE_ROOT))
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_text(render(src.read_text(encoding="utf-8"), values), encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - templates are all text
            dest.write_bytes(src.read_bytes())
        written.append(dest)

    for dirname in EMPTY_DIRS:
        keep = target / dirname / ".gitkeep"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("", encoding="utf-8")
        written.append(keep)

    return written
