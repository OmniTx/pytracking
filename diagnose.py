# SPDX-License-Identifier: Apache-2.0
"""Standalone environment probe for the pytracking receiver.

Run it from the hosting panel's "Execute python script" box, where a path is
all that can be supplied::

    /home/<user>/repositories/pytracking/diagnose.py

It answers three questions the web routes cannot, because it runs even when
HTTP routing is broken:

    * Is this the app's virtualenv, and which Python is it?
    * Do flask and PyMySQL import, and is the environment configured?
    * Does the database connect, and is the schema migrated?
    * Which passenger_wsgi.py is on disk, and what does it define?

Secrets are never printed: the password and salt are reported only as set or
unset, and any occurrence of the password in an error message is redacted.
"""

import os
import platform
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SECRET_ENV = {"DB_PASSWORD", "PYTRACKING_IP_SALT"}
SHOWN_ENV = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "PYTRACKING_IP_SALT"]


def _redact(text, password):
    if password:
        return str(text).replace(password, "***REDACTED***")
    return str(text)


def _field(label, value):
    print("{0:<26} {1}".format(label, value))


def main():
    password = os.environ.get("DB_PASSWORD", "")

    print("=" * 70)
    print("pytracking diagnose")
    print("=" * 70)

    _field("Python", platform.python_version())
    _field("Executable", sys.executable)
    _field("Platform", platform.platform())
    _field("Working directory", os.getcwd())
    _field("Script directory", HERE)
    _field("VIRTUAL_ENV", os.environ.get("VIRTUAL_ENV", "(not set)"))

    print("\n--- dependencies ---")
    for module_name in ("flask", "pymysql"):
        try:
            __import__(module_name)
        except Exception as exc:
            _field(module_name, "IMPORT FAILED: {0}".format(exc))
            continue
        try:
            from importlib.metadata import version

            _field(module_name, version(module_name))
        except Exception:
            _field(module_name, "imported (version unknown)")

    print("\n--- environment ---")
    for name in SHOWN_ENV:
        value = os.environ.get(name)
        if not value:
            _field(name, "NOT SET")
        elif name in SECRET_ENV:
            _field(name, "set (hidden)")
        else:
            _field(name, value)

    print("\n--- repository files ---")
    for filename in ("passenger_wsgi.py", "requirements.txt", "db/run_migration.py"):
        path = os.path.join(HERE, *filename.split("/"))
        try:
            stat = os.stat(path)
            _field(
                filename,
                "{0} bytes, modified {1}".format(
                    stat.st_size, stat.st_mtime
                ),
            )
        except OSError as exc:
            _field(filename, "MISSING ({0})".format(exc.strerror or exc))

    print("\n--- database ---")
    try:
        import pymysql

        connection = pymysql.connect(
            host=os.environ["DB_HOST"],
            database=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=password,
            connect_timeout=5,
            charset="utf8mb4",
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                tables = [row[0] for row in cursor.fetchall()]
        finally:
            connection.close()
        if tables:
            _field("connect", "OK")
            _field("tables", ", ".join(tables))
        else:
            _field("connect", "OK, but no tables - run db/run_migration.py")
    except KeyError as exc:
        _field("connect", "SKIPPED: {0} not set".format(exc))
    except Exception as exc:
        _field("connect", "FAILED: {0}".format(_redact(exc, password)))

    print("\n--- wsgi module ---")
    try:
        import passenger_wsgi as app_module

        _field("loaded from", getattr(app_module, "__file__", "unknown"))
        _field("marker", getattr(app_module, "BUILD_MARKER", "ABSENT"))
        rules = sorted(
            "{0} {1}".format(
                ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"})) or "-",
                rule.rule,
            )
            for rule in app_module.application.url_map.iter_rules()
        )
        _field("routes", str(len(rules)))
        for rule in rules:
            print("    {0}".format(rule))
    except Exception as exc:
        _field("import", "FAILED: {0}".format(_redact(exc, password)))

    print("\n" + "=" * 70)
    print("If this prints and the dependencies import, the Python side is fine")
    print("and the 404s are HTTP routing (vhost not handing requests to")
    print("Passenger), not application code.")
    print("=" * 70)


if __name__ == "__main__":
    main()
