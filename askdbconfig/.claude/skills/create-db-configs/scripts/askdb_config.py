# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Inspect a SQLite database and render its askdb database YAML.

Standalone: it does not import the askdb package. ``uv`` installs the one
dependency (pyyaml) from the header above. Run it from any folder:

    uv run .claude/skills/create-db-configs/scripts/askdb_config.py inspect <db-url>
    uv run .claude/skills/create-db-configs/scripts/askdb_config.py render <spec.json> <out.yaml>

``inspect`` only reads the database (read-only connection) and prints JSON.
``render`` takes the answers gathered from the user, writes the YAML, and checks
it with the same rules askdb applies at startup. It exits 1 on failure.

The checks copy the rules in askdb's config.py (field rules), registry.py (name
rule) and startup.py (schema rules). If those rules change in askdb, change them here.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

# A text column with at most this many distinct values is offered as a candidate for
# listing its values in the description (e.g. status codes).
ENUM_MAX_VALUES = 12
ENUM_SAMPLE_ROWS = 5000

# askdb registry.py: the database name may use only these characters.
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# askdb config.py: a column reference is table.column.
REFERENCES_PATTERN = re.compile(r"^\w+\.\w+$")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def sqlite_path(url: str) -> Path:
    """Accept ``sqlite:///path`` (SQLAlchemy style) or a plain file path. Only SQLite is supported."""
    if "://" in url and not url.startswith("sqlite:///"):
        raise SystemExit(
            f"only SQLite is supported in v1; got: {url.split('://', 1)[0]}://"
        )
    raw = url[len("sqlite:///") :] if url.startswith("sqlite:///") else url
    return Path(raw).expanduser().resolve()


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open the database read-only, at the database layer as well as the connection layer."""
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def introspect(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Tables and their columns. Raises sqlite3.Error if the file cannot be read."""
    with closing(open_read_only(path)) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            rows = conn.execute(f"PRAGMA table_info({quote(name)})").fetchall()
            tables[name] = [
                {"name": row[1], "type": row[2], "primary_key": bool(row[5])}
                for row in rows
            ]
    return tables


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def foreign_keys(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(f"PRAGMA foreign_key_list({quote(table)})").fetchall()
    return {row[3]: f"{row[2]}.{row[4]}" for row in rows if row[4]}


def enum_candidates(
    conn: sqlite3.Connection, table: str, columns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    found = []
    for col in columns:
        if col["type"].upper() not in ("TEXT", "VARCHAR", "CHAR", "CLOB", ""):
            continue
        values = conn.execute(
            f'SELECT DISTINCT "{col["name"]}" FROM (SELECT "{col["name"]}" FROM "{table}" LIMIT {ENUM_SAMPLE_ROWS}) '
            f"WHERE \"{col['name']}\" IS NOT NULL ORDER BY 1 LIMIT {ENUM_MAX_VALUES + 1}"
        ).fetchall()
        if 0 < len(values) <= ENUM_MAX_VALUES:
            found.append({"column": col["name"], "values": [row[0] for row in values]})
    return found


def inspect(url: str) -> dict[str, Any]:
    path = sqlite_path(url)
    if not path.is_file():
        raise SystemExit(f"database file not found: {path}")
    try:
        tables = introspect(path)
    except sqlite3.Error as exc:
        raise SystemExit(f"cannot open database read-only: {exc}")

    report = {"path": str(path), "tables": []}
    with closing(open_read_only(path)) as conn:
        for name, columns in tables.items():
            fks = foreign_keys(conn, name)
            cols = [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "primary_key": c["primary_key"],
                    "references": fks.get(c["name"]),
                }
                for c in columns
            ]
            row_count = conn.execute(f"SELECT COUNT(*) FROM {quote(name)}").fetchone()[0]
            report["tables"].append(
                {
                    "name": name,
                    "row_count": row_count,
                    "columns": cols,
                    "value_lists": enum_candidates(conn, name, cols),
                }
            )
    return report


def is_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def expand_env(value: str) -> str:
    """Replace each ``${NAME}`` with the value of that environment variable."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"environment variable {name} is not set")
        return os.environ[name]

    return ENV_REF.sub(replace, value)


def check_fields(data: dict[str, Any]) -> list[str]:
    """The field rules askdb's config.py enforces on a database file."""
    errors = []
    if not is_text(data["name"]):
        errors.append("name: must be a non-empty text value")
    elif not ID_PATTERN.match(data["name"]):
        errors.append("name may use only letters, digits, _ and -")
    if not is_text(data["display_name"]):
        errors.append("display_name: must be a non-empty text value")
    if not is_text(data["path"]):
        errors.append("path: must be a non-empty text value")
    else:
        try:
            expand_env(data["path"])
        except ValueError as exc:
            errors.append(f"path: {exc}")
    months = data["calendar"]["fiscal_year_start_month"]
    if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 12:
        errors.append("calendar.fiscal_year_start_month: must be a whole number from 1 to 12")
    if not isinstance(data["description"], str):
        errors.append("description: must be text")
    if not data["tables"]:
        errors.append("tables: must list at least one table")
    for table in data["tables"]:
        where = f"tables.{table['name']}"
        if not is_text(table["name"]):
            errors.append("tables: a table name must be non-empty text")
        if not isinstance(table["description"], str):
            errors.append(f"{where}.description: must be text")
        if not table["columns"]:
            errors.append(f"{where}.columns: must list at least one column")
        for col in table["columns"]:
            ref = f"{where}.{col['name']}"
            references = col.get("references")
            if references is not None and not REFERENCES_PATTERN.match(references):
                errors.append(f"{ref}.references: must look like table.column")
    return errors


def check_schema(tables_cfg: list[dict[str, Any]], tables: dict[str, list[dict[str, Any]]]) -> list[str]:
    """The schema rules askdb's startup.py enforces: tables, columns, types and references exist."""
    errors: list[str] = []
    for table in tables_cfg:
        if table["name"] not in tables:
            errors.append(f"table {table['name']} not found")
            continue
        found = {col["name"]: col for col in tables[table["name"]]}
        for column in table["columns"]:
            ref = f"{table['name']}.{column['name']}"
            if column["name"] not in found:
                errors.append(f"column {ref} not found")
            elif found[column["name"]]["type"].strip().upper() != column["type"].strip().upper():
                errors.append(
                    f"column {ref} has type {found[column['name']]['type']}, "
                    f"config says {column['type']}"
                )
    for table in tables_cfg:
        for column in table["columns"]:
            if not column.get("references"):
                continue
            target_table, target_column = column["references"].split(".")
            ref = f"{table['name']}.{column['name']}"
            if target_table not in tables:
                errors.append(f"{ref} references missing table {target_table}")
            elif target_column not in {c["name"] for c in tables[target_table]}:
                errors.append(f"{ref} references missing column {column['references']}")
    return errors


class _LiteralDumper(yaml.SafeDumper):
    """Writes multi-line strings as literal blocks, so descriptions stay readable."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_LiteralDumper.add_representer(str, _represent_str)


def dump_database_yaml(data: dict[str, Any]) -> str:
    return yaml.dump(data, Dumper=_LiteralDumper, sort_keys=False, allow_unicode=True, width=100)


def render(spec: dict[str, Any], out: Path) -> dict[str, Any]:
    """Build the YAML from the answers, validate it, and write it only if it passes."""

    def fail(errors: list[str]) -> dict[str, Any]:
        return {"ok": False, "errors": errors}

    path_text = spec.get("path")
    if not is_text(path_text):
        return fail(["path: must be a non-empty text value"])
    try:
        db_file = Path(expand_env(path_text)).expanduser().resolve()
    except ValueError as exc:
        return fail([f"path: {exc}"])
    if not db_file.is_file():
        return fail([f"database file not found: {db_file}"])
    try:
        db_tables = introspect(db_file)
    except sqlite3.Error as exc:
        return fail([f"cannot open database read-only: {exc}"])

    with closing(open_read_only(db_file)) as conn:
        fks_by_table = {name: foreign_keys(conn, name) for name in db_tables}

    tables = []
    for wanted in spec.get("tables") or []:
        table_name = wanted.get("name")
        if table_name not in db_tables:
            return fail([f"table {table_name} is not in the database"])
        described = {c["name"]: c.get("description") for c in wanted.get("columns") or []}
        cols = []
        for col in db_tables[table_name]:
            entry = {"name": col["name"], "type": col["type"]}
            if col["primary_key"]:
                entry["primary_key"] = True
            fks = fks_by_table[table_name]
            if col["name"] in fks:
                entry["references"] = fks[col["name"]]
            if described.get(col["name"]):
                entry["description"] = described[col["name"]]
            cols.append(entry)
        tables.append(
            {"name": table_name, "description": wanted.get("description"), "columns": cols}
        )

    data = {
        "name": spec.get("name"),
        "display_name": spec.get("display_name"),
        "path": path_text,
        "calendar": {"fiscal_year_start_month": spec.get("fiscal_year_start_month", 1)},
        "description": spec.get("description"),
        "tables": tables,
    }
    errors = check_fields(data)
    if errors:
        return fail(errors)
    errors = check_schema(tables, db_tables)
    if errors:
        return fail(errors)
    out.write_text(dump_database_yaml(data), encoding="utf-8")
    return {"ok": True, "written": str(out), "tables": [t["name"] for t in tables]}


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "inspect":
        print(json.dumps(inspect(argv[2]), indent=2, default=str))
        return 0
    if len(argv) == 4 and argv[1] == "render":
        spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        result = render(spec, Path(argv[3]))
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
