# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Check an evaluation set's format and run its expected SQL against the database.

Standalone: it does not import the askdb package. ``uv`` installs the one
dependency (pyyaml) from the header above. Run it from any folder:

    uv run <skill-dir>/scripts/check_eval_set.py <eval.yaml> --config <db.yaml>

Errors make the check fail (exit 1). Warnings are printed but do not fail it.
The database is opened read-only (SQLite ``mode=ro`` plus ``PRAGMA query_only``).

The SQL guard and config loader copy the rules in askdb's sqlguard.py and config.py.
If those rules change in askdb, change them here.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# High row cap so expected SQL is compared in full, not truncated.
EXPECTED_ROW_LIMIT = 1_000_000
TIMEOUT_SECONDS = 60
OUTCOMES = {"clarification", "declined"}
CASE_KEYS = {
    "id",
    "question",
    "category",
    "expected_sql",
    "expected_outcome",
    "today",
    "conversation",
    "notes",
}
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# SQL guard: one read-only SELECT (or WITH ... SELECT). Writes and schema changes are refused.
# Comments and quoted text are removed before these checks, so a value such as 'update' is fine.
FORBIDDEN_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex"
    r"|truncate|begin|commit|rollback|savepoint|release)\b",
    re.IGNORECASE,
)
REPLACE_INTO = re.compile(r"\breplace\s+into\b", re.IGNORECASE)


class ConfigError(Exception):
    pass


class UnsafeSQL(Exception):
    pass


class QueryError(Exception):
    pass


@dataclass
class DatabaseConfig:
    name: str
    path: str


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    total: int = field(init=False)

    def __post_init__(self) -> None:
        self.total = len(self.rows)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.summary: dict[str, Any] | None = None
        self.samples: list[dict[str, Any]] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")


def expand_env(value: str) -> str:
    """Replace each ``${NAME}`` with the value of that environment variable."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"environment variable {name} is not set")
        return os.environ[name]

    return ENV_REF.sub(replace, value)


def load_database_config(path: Path) -> DatabaseConfig:
    """Read the database YAML and expand ${NAME} in its path. Secrets never belong here."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read database config: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("database config must be a mapping")
    name = data.get("name")
    raw_path = data.get("path")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("database config needs a non-empty name")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError("database config needs a non-empty path")
    return DatabaseConfig(name=name, path=expand_env(raw_path))


def strip_literals(sql: str) -> str:
    """Remove comments and quoted text, so keywords inside them are ignored."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    return sql


def validate(sql: str) -> None:
    """Raise UnsafeSQL unless the text is exactly one read-only SELECT."""
    bare = strip_literals(sql).strip().rstrip(";").strip()
    if not bare:
        raise UnsafeSQL("the query is empty")
    if ";" in bare:
        raise UnsafeSQL("only one statement is allowed")
    if not re.match(r"^(select|with)\b", bare, re.IGNORECASE):
        raise UnsafeSQL("the query must start with SELECT or WITH")
    match = FORBIDDEN_WORDS.search(bare) or REPLACE_INTO.search(bare)
    if match:
        raise UnsafeSQL(f"'{match.group(0).upper()}' is not allowed in a read-only query")


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open the database read-only, at the database layer as well as the connection layer."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_query(path: Path, sql: str, timeout: float) -> Result:
    """Run one validated SELECT and return every row. Raises QueryError on failure or timeout."""
    deadline = time.monotonic() + timeout

    def stop_after_deadline() -> int:
        return 1 if time.monotonic() > deadline else 0

    try:
        with closing(open_read_only(path)) as conn:
            conn.set_progress_handler(stop_after_deadline, 10_000)
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchall()
    except sqlite3.Error as exc:
        raise QueryError(str(exc)) from exc
    return Result(columns=columns, rows=rows[:EXPECTED_ROW_LIMIT])


def check_sql(
    report: Report, where: str, sql: str, db_path: Path
) -> Result | None:
    """Validate that the SQL is one SELECT and run it. Returns the result, or None."""
    try:
        validate(sql)
    except UnsafeSQL as exc:
        report.error(where, f"expected SQL is not a single read-only SELECT: {exc}")
        return None
    try:
        result = run_query(db_path, sql, TIMEOUT_SECONDS)
    except QueryError as exc:
        report.error(where, f"expected SQL failed against the database: {exc}")
        return None
    if result.total == 0:
        report.warn(
            where,
            "expected SQL returns no rows, so the case cannot tell a wrong answer from an empty one",
        )
    return result


def check_case(
    report: Report, index: int, case: Any, db_path: Path, seen: set[str]
) -> None:
    where = f"cases[{index}]"
    if not isinstance(case, dict):
        report.error(where, "must be a mapping")
        return
    case_id = case.get("id")
    where = f"cases[{index}] ({case_id})" if case_id else where

    for key in case:
        if key not in CASE_KEYS:
            report.error(where, f"unknown key '{key}' (check for a typo)")
    if not isinstance(case_id, str) or not ID_PATTERN.match(case_id):
        report.error(where, "id must be letters, digits, _ or -")
    elif case_id in seen:
        report.error(where, "duplicate id")
    else:
        seen.add(case_id)
    if not isinstance(case.get("question"), str) or not case["question"].strip():
        report.error(where, "question is required")
    if not isinstance(case.get("category"), str) or not case["category"].strip():
        report.error(where, "category is required")
    if "today" in case:
        try:
            date.fromisoformat(str(case["today"]))
        except ValueError:
            report.error(where, "today must be an ISO date such as 2025-06-30")

    has_sql = "expected_sql" in case
    has_outcome = "expected_outcome" in case
    if not has_sql and not has_outcome:
        report.error(
            where,
            "needs expected_sql, or expected_outcome for a clarification or decline case",
        )
    if has_outcome and case["expected_outcome"] not in OUTCOMES:
        report.error(where, f"expected_outcome must be one of {sorted(OUTCOMES)}")
    if has_outcome and has_sql:
        report.warn(
            where,
            "has both expected_outcome and expected_sql; the outcome is what is graded",
        )

    if has_sql:
        result = check_sql(report, where, str(case["expected_sql"]), db_path)
        if result:
            report.samples.append(
                {
                    "id": case_id,
                    "rows": result.total,
                    "columns": result.columns,
                    "first_rows": [list(r) for r in result.rows[:3]],
                }
            )

    for n, turn in enumerate(case.get("conversation") or []):
        if (
            not isinstance(turn, dict)
            or not turn.get("question")
            or not turn.get("sql")
        ):
            report.error(where, f"conversation turn {n + 1} needs question and sql")
            continue
        check_sql(
            report, f"{where} conversation turn {n + 1}", str(turn["sql"]), db_path
        )


def check_set(eval_path: Path, config_path: Path) -> Report:
    report = Report()
    try:
        config = load_database_config(config_path)
    except ConfigError as exc:
        report.error("config", str(exc))
        return report

    try:
        data = yaml.safe_load(eval_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        report.error("eval file", f"cannot read as YAML: {exc}")
        return report
    if not isinstance(data, dict):
        report.error("eval file", "top level must be a mapping with database and cases")
        return report
    if data.get("database") != config.name:
        report.error(
            "database",
            f"must be '{config.name}' to match the database config, got {data.get('database')!r}",
        )
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        report.error("cases", "must be a non-empty list")
        return report

    db_path = Path(config.path).expanduser().resolve()
    if not db_path.is_file():
        report.error("config", f"database file not found: {db_path}")
        return report
    seen: set[str] = set()
    for index, case in enumerate(cases):
        check_case(report, index, case, db_path, seen)

    categories: dict[str, int] = {}
    for case in cases:
        if isinstance(case, dict) and isinstance(case.get("category"), str):
            categories[case["category"]] = categories.get(case["category"], 0) + 1
    report.summary = {"cases": len(cases), "categories": categories}
    return report


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[2] != "--config":
        print(__doc__)
        return 2
    report = check_set(Path(argv[1]), Path(argv[3]))
    print(
        json.dumps(
            {
                "ok": not report.errors,
                "errors": report.errors,
                "warnings": report.warnings,
                "summary": report.summary,
                "samples": report.samples,
            },
            indent=2,
            default=str,
        )
    )
    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
