"""Check the access fixtures and their references using the Python standard library."""

import argparse
import csv
import json
import re
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CASE_COLUMNS = (
    "id actor action object collection before_state before_result "
    "after_state after_result sources decisions"
).split()
READ_RESULTS = {"visible", "hidden", "printable", "read-only", "blocked", "pending"}
EDIT_RESULTS = {"written", "refused", "blocked"}
MEETING_KINDS = {"brief", "published-result", "private-note", "recommendation-draft"}
EDIT_KINDS = {"edit-goal": "plan", "edit-recommendation": "recommendation-draft"}
COLLECTION_KINDS = {
    "personal-profile": "profile",
    "supervised-employees": "profile",
    "peer-feed": "achievement",
    "meeting-briefs": "brief",
    "private-notes": "private-note",
    "published-results": "published-result",
}


def csv_rows(path, columns):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != columns:
            raise ValueError(f"{path.name}: expected columns {columns}")
        rows = list(reader)
    for line, row in enumerate(rows, 2):
        if None in row or any(not value for key, value in row.items() if key != "decisions"):
            raise ValueError(f"{path.name}:{line}: missing field or extra column")
        if "decisions" in row and row["decisions"] is None:
            raise ValueError(f"{path.name}:{line}: missing decisions column")
    return rows


def unique(values, field):
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError(f"{field}: expected a list of non-empty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"{field}: duplicate value")


def references(values, available, field):
    unique(values, field)
    for value in values:
        if value not in available:
            raise ValueError(f"{field}: unknown reference {value!r}")


def index(rows, group, used_ids):
    if not rows:
        raise ValueError(f"{group}: empty table")
    by_id = {}
    for row in rows:
        key = row["id"]
        if not isinstance(key, str) or not key.strip() or key in used_ids:
            raise ValueError(f"{group}: invalid or duplicate ID {key!r}")
        used_ids.add(key)
        by_id[key] = row
    return by_id


def check_fixtures(tables):
    profiles = tables["profiles"]
    for key, profile in profiles.items():
        if not profile["roles"]:
            raise ValueError(f"{key}: missing roles")
        references(profile["roles"], {"teacher", "supervisor", "admin"}, f"{key}.roles")
    supervisors = {key for key, row in profiles.items() if "supervisor" in row["roles"]}
    teachers = {key for key, row in profiles.items() if "teacher" in row["roles"]}

    for key, row in tables["assignments"].items():
        references([row["supervisor"]], supervisors, f"{key}.supervisor")
        references([row["employee"]], teachers, f"{key}.employee")
    for key, row in tables["meetings"].items():
        references([row["employee"]], teachers, f"{key}.employee")
    for key, row in tables["meeting_grants"].items():
        references([row["supervisor"]], supervisors, f"{key}.supervisor")
        references([row["meeting"]], tables["meetings"], f"{key}.meeting")
        references(row["operations"], {"read-brief", "print-brief", "edit-recommendation"},
                   f"{key}.operations")

    objects = tables["objects"]
    unique([row["marker"] for row in objects.values()], "objects.marker")
    achievements = set()
    for key, row in objects.items():
        kind = row["kind"]
        if kind in MEETING_KINDS:
            references([row["meeting"]], tables["meetings"], f"{key}.meeting")
        elif kind in {"profile", "plan", "achievement"}:
            references([row["owner"]], profiles, f"{key}.owner")
        else:
            raise ValueError(f"{key}: unknown object kind {kind!r}")
        if kind == "achievement":
            achievements.add(key)
            references(row["audience"], profiles, f"{key}.audience")
        if kind == "published-result":
            employee = tables["meetings"][row["meeting"]]["employee"]
            references(row["published_to"], {employee}, f"{key}.published_to")

    state_references = {
        "assignments": tables["assignments"], "meeting_grants": tables["meeting_grants"],
        "inactive": profiles, "published": achievements, "indexed": achievements,
    }
    for key, state in tables["states"].items():
        for field, available in state_references.items():
            references(state[field], available, f"{key}.{field}")


def check_sources(sources, decisions):
    for key, row in sources.items():
        path = (ROOT / row["path"]).resolve()
        if not path.is_relative_to(ROOT / "docs") or not path.is_file():
            raise ValueError(f"{key}: missing repository document {row['path']!r}")
        document = path.read_text(encoding="utf-8")
        headings = list(re.finditer(r"^(#{1,6}) (.+)$", document, re.MULTILINE))
        matching = [h for h in headings if h[2] == row["section"]]
        if len(matching) != 1:
            raise ValueError(f"{key}: missing or ambiguous section {row['section']!r}")
        heading = matching[0]
        end = next((h.start() for h in headings
                    if h.start() > heading.start() and len(h[1]) <= len(heading[1])), len(document))
        if row["excerpt"] not in document[heading.end():end]:
            raise ValueError(f"{key}: source excerpt not found in {row['section']!r}")

    backlog = (ROOT / "docs/architecture/backlog.md").read_text(encoding="utf-8")
    for key, row in decisions.items():
        url = row["url"]
        number = url.rsplit("/", 1)[-1]
        if f"[{key} · #{number}]({url})" not in backlog:
            raise ValueError(f"{key}: decision link absent from backlog: {url}")


def check_cases(cases, tables, sources):
    for key, row in cases.items():
        references([row["actor"]], tables["profiles"], f"{key}.actor")
        references([row["object"]], tables["objects"], f"{key}.object")
        kind = tables["objects"][row["object"]]["kind"]
        if row["action"] == "read" and COLLECTION_KINDS.get(row["collection"]) == kind:
            results = READ_RESULTS
        elif EDIT_KINDS.get(row["action"]) == kind and row["collection"] == "none":
            results = EDIT_RESULTS
        else:
            raise ValueError(f"{key}: invalid action/collection for {kind}: "
                             f"{row['action']}/{row['collection']}")
        for phase in ("before", "after"):
            state = row[f"{phase}_state"]
            result = row[f"{phase}_result"]
            references([state], tables["states"], f"{key}.{phase}_state")
            references([result], results, f"{key}.{phase}_result")
            inactive = row["actor"] in tables["states"][state]["inactive"]
            if inactive != (result == "blocked"):
                raise ValueError(f"{key}.{phase}_result: inconsistent with account activity")
            if result in {"printable", "read-only", "pending"} and kind != "brief":
                raise ValueError(f"{key}.{phase}_result: {result} requires a meeting brief")
        cited = row["sources"].split()
        if not cited:
            raise ValueError(f"{key}: missing source")
        references(cited, sources, f"{key}.sources")
        decisions = row["decisions"].split()
        references(decisions, tables["decisions"], f"{key}.decisions")
        pending = "pending" in (row["before_result"], row["after_result"])
        if pending != bool(decisions):
            raise ValueError(f"{key}: pending result and decision links must occur together")


def validate(directory):
    data = json.loads((directory / "fixtures.json").read_text(encoding="utf-8"))
    used_ids = set()
    groups = ("profiles", "assignments", "meetings", "meeting_grants", "objects", "states", "decisions")
    tables = {group: index(data[group], group, used_ids) for group in groups}
    sources = index(csv_rows(directory / "sources.csv", ["id", "path", "section", "excerpt"]),
                    "sources", used_ids)
    cases = index(csv_rows(directory / "cases.csv", CASE_COLUMNS), "cases", used_ids)
    check_fixtures(tables)
    check_sources(sources, tables["decisions"])
    check_cases(cases, tables, sources)
    pending = sum(bool(row["decisions"]) for row in cases.values())
    print(f"OK: {len(tables['profiles'])} profiles, {len(tables['objects'])} objects, "
          f"{len(tables['states'])} states, {len(cases)} cases ({pending} pending), "
          f"{len(sources)} sources, {len(used_ids)} unique IDs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=HERE,
                        help="directory containing fixtures.json, cases.csv and sources.csv")
    try:
        validate(parser.parse_args().directory)
    except (OSError, ValueError, TypeError, KeyError, csv.Error) as error:
        sys.exit(f"FAIL: {error}")
