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
        for hint in _mysql_hint(exc):
            print("    -> {0}".format(hint))

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

    _report_htaccess(HERE)
    _report_http_probe()

    print("\n" + "=" * 70)
    print("If this prints and the dependencies import, the Python side is fine")
    print("and the 404s are HTTP routing (vhost not handing requests to")
    print("Passenger), not application code.")
    print("=" * 70)


def _report_http_probe():
    """Loop back to the live domain from the server itself.

    Done here rather than from a workstation because this output is what the
    host operator will act on, and it distinguishes "the network cannot reach
    us" from "the vhost is not routing to Passenger" in one shot.
    """
    print("\n--- live http probe (from this server) ---")
    import urllib.error
    import urllib.request

    domain = os.environ.get("SMOKE_DOMAIN", "https://my.herbsworld.net")
    for path in ("/healthz", "/"):
        url = domain.rstrip("/") + path
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                body = response.read(400).decode("utf-8", "replace")
                _field(url, "HTTP {0}".format(response.status))
                print("    {0}".format(body.replace("\n", " ")[:200]))
        except urllib.error.HTTPError as exc:
            body = exc.read(200).decode("utf-8", "replace")
            _field(url, "HTTP {0}".format(exc.code))
            if "litespeed" in body.lower() or "powered by" in body.lower():
                print("    served by the LiteSpeed error page, not Flask")
        except Exception as exc:
            _field(url, "FAILED: {0}".format(type(exc).__name__))


def _mysql_hint(exc):
    """Turn a common PyMySQL failure into a concrete next step."""
    code = None
    if exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]
    user = os.environ.get("DB_USER", "(DB_USER unset)")
    if code == 1045:
        return [
            "Access denied: the username or password does not match.",
            "Check DB_USER is exactly {0!r} (no truncated prefix).".format(user),
            "Reset it in cPanel > MySQL Databases, then update DB_PASSWORD.",
            "The user also needs privileges on DB_NAME.",
        ]
    if code == 1049:
        return [
            "Unknown database: DB_NAME does not exist.",
            "Create it in cPanel > MySQL Databases, or fix DB_NAME.",
        ]
    if code in (2002, 2003):
        return [
            "Cannot reach the server: check DB_HOST.",
            "On cPanel this is normally 'localhost'.",
        ]
    return []


def _find_htaccess_files(root, limit=12):
    """Return .htaccess paths under the home directory, app root first.

    cPanel writes the Passenger directives into the *domain's document root*,
    which is usually ``~/<domain>/`` -- not the application root -- so scan
    every directory in the home folder one level deep.
    """
    candidates = []
    home = os.path.expanduser("~")

    direct = os.path.join(root, ".htaccess")
    if os.path.isfile(direct):
        candidates.append(direct)

    search_dirs = [os.path.join(home, "public_html"), home]
    for directory in search_dirs:
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            nested = os.path.join(directory, entry, ".htaccess")
            if entry == ".htaccess":
                nested = os.path.join(directory, entry)
            if os.path.isfile(nested) and nested not in candidates:
                candidates.append(nested)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def _report_htaccess(root):
    """Show every .htaccess found, with its Passenger directives."""
    print("\n--- passenger wiring ---")
    files = _find_htaccess_files(root)
    if not files:
        print("No .htaccess file found in the app root or under the home")
        print("directory. If the app was created in cPanel, one should exist")
        print("in the domain's document root (e.g. ~/<domain>/.htaccess).")
        return

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError as exc:
            _field("unreadable", "{0} ({1})".format(path, exc.strerror or exc))
            continue

        lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip().lower().startswith(("passenger", "requestredirect"))
        ]
        _field("File", path)
        if lines:
            for line in lines:
                print("    {0}".format(line))
        elif content.strip():
            print("    (exists, {0} lines, but no Passenger directives)".format(
                len(content.splitlines())
            ))
        else:
            print("    (exists but is empty)")

    with_passenger = [
        path
        for path in files
        if "passenger" in open(path, encoding="utf-8", errors="replace").read().lower()
    ]
    if not with_passenger:
        print("\nNo Passenger directives in any .htaccess found.")
        print("cPanel's Setup Python App writes them to the domain document")
        print("root. If that is not under the home directory (or the app was")
        print("created without them), the vhost will never reach Passenger.")


if __name__ == "__main__":
    main()
