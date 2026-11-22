# SPDX-License-Identifier: Apache-2.0
"""Run a migration SQL file against a MySQL database.

Usage::

    python db/run_migration.py --file db/migrations/001_init.sql

Environment variables (from .env)::

    DB_HOST=localhost
    DB_NAME=pytracking
    DB_USER=pytracking_user
    DB_PASSWORD=replace-me

No extra dependencies beyond flask and PyMySQL.
"""

import argparse
import os
import sys

import pymysql


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a Phase-N migration SQL file."
    )
    parser.add_argument(
        "--file",
        dest="sql_file",
        required=True,
        help="Path to the SQL migration file.",
    )
    return parser.parse_args(argv)


def run_migration(sql_file):
    """Execute *sql_file* inside a single transaction."""
    connection = None
    try:
        connection = pymysql.connect(
            host=os.environ["DB_HOST"],
            database=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            connect_timeout=5,
            autocommit=False,
        )
    except Exception as exc:
        print(f"FATAL: unable to connect to MySQL: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(sql_file, encoding="utf-8") as fh:
        sql = fh.read()

    statements = _split_statements(sql)

    try:
        with connection.cursor() as cursor:
            for i, statement in enumerate(statements, start=1):
                cursor.execute(statement)
        connection.commit()
        print(f"MIGRATION OK: applied {i} statement(s) from {sql_file}")
    except Exception as exc:
        connection.rollback()
        print(
            f"MIGRATION FAILED ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        raise
    finally:
        if connection.open:
            connection.close()


def _split_statements(sql):
    """Split a SQL file into executable statements.

    Drops ``--`` line comments and blank fragments, then splits on ``;``.
    """
    lines = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


if __name__ == "__main__":
    args = parse_args()
    try:
        run_migration(args.sql_file)
    except Exception:
        sys.exit(1)
