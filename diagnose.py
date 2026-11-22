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

    _check_startup_file()
    _report_layout()

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
    _exercise_receiver(password)
    _report_passenger_logs()
    _report_http_probe()

    print("\n" + "=" * 70)
    print("If this prints and the dependencies import, the Python side is fine")
    print("and the 404s are HTTP routing (vhost not handing requests to")
    print("Passenger), not application code.")
    print("=" * 70)


def _check_startup_file():
    """Detect cPanel's placeholder stub, which is self-recursive.

    cPanel rewrites passenger_wsgi.py to::

        import imp
        wsgi = imp.load_source('wsgi', 'passenger_wsgi.py')

    Importing that re-imports itself until RecursionError, so the app can
    never start. It reappears whenever the app is saved or recreated.
    """
    print("\n--- startup file ---")
    path = os.path.join(HERE, "passenger_wsgi.py")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError as exc:
        _field("passenger_wsgi.py", "MISSING ({0})".format(exc.strerror or exc))
        return

    if "imp.load_source" in content and "passenger_wsgi.py" in content:
        _field("passenger_wsgi.py", "CPANEL STUB - app cannot start")
        print("    It re-imports itself until RecursionError. Replace it with")
        print("    the repository version (git checkout passenger_wsgi.py).")
        return
    if 'application = Flask' in content or "application = create_app()" in content:
        _field("passenger_wsgi.py", "real WSGI module")
        return
    _field("passenger_wsgi.py", "unrecognised contents")


def _report_layout():
    """Warn when the app root differs from the domain document root.

    On this host the working app keeps everything in ~/<domain>/, and the
    .htaccess PassengerAppRoot points there. A checkout in ~/repositories/
    means the docroot and the app are different trees, which is exactly the
    shape that can leave the vhost serving static files.
    """
    print("\n--- layout ---")
    home = os.path.expanduser("~")
    _field("app root", HERE)
    _field("home", home)
    try:
        domain_dirs = [
            entry
            for entry in sorted(os.listdir(home))
            if os.path.isfile(os.path.join(home, entry, ".htaccess"))
        ]
    except OSError:
        domain_dirs = []
    _field("dirs with .htaccess", ", ".join(domain_dirs) or "none")

    if not HERE.startswith(os.path.join(home, "repositories")):
        return
    print("\n    NOTE: this app lives under ~/repositories, not in a domain")
    print("    directory. Compare against a working app on this host, whose")
    print("    files sit directly in ~/<domain>/ with PassengerAppRoot set")
    print("    to that directory.")


def _exercise_receiver(password):
    """Call the receiver in-process so a failing request yields a traceback.

    Over HTTP a broken view is an opaque 500. Here the exception surfaces with
    its real type and stack, which is the only way to diagnose it without
    shell access or Passenger error pages.
    """
    print("\n--- receiver in-process ---")
    import hashlib
    import traceback

    try:
        import passenger_wsgi as app_module
    except Exception:
        _field("import", "FAILED")
        traceback.print_exc()
        return

    source = os.path.join(HERE, "passenger_wsgi.py")
    try:
        with open(source, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        _field("passenger_wsgi.py sha256", digest[:32] + "...")
    except OSError:
        pass

    # Show the deployed view itself: a stale or hand-edited file is otherwise
    # indistinguishable from a correct one at the same path.
    try:
        import inspect

        view_source = inspect.getsource(app_module.receive_event)
        lines = view_source.splitlines()
        print("receive_event source ({0} lines, first 12):".format(len(lines)))
        for line in lines[:12]:
            print("    {0}".format(line))
    except Exception as exc:
        print("could not read view source: {0}".format(exc))

    client = app_module.application.test_client()
    # Without this, Flask converts an exception into an opaque 500 response and
    # the traceback is lost - exactly what happens over HTTP.
    app_module.application.config["PROPAGATE_EXCEPTIONS"] = True
    cases = [
        ("unknown key", {"wk": "dx-unknown", "event_id": "dx-1", "name": "page_view"}),
        ("missing wk", {"event_id": "dx-2", "name": "page_view"}),
        ("missing event_id", {"wk": "x"}),
    ]
    for label, payload in cases:
        try:
            response = client.post(
                "/api/v2/ev",
                json=payload,
                headers={"Origin": "https://my.herbsworld.net"},
            )
            _field(label, "HTTP {0}".format(response.status_code))
        except Exception as exc:
            _field(label, "RAISED {0}".format(type(exc).__name__))
            traceback.print_exc()


def _report_passenger_logs():
    """Print the tail of Passenger's stderr log for this app.

    A view that raises becomes an opaque 500 over HTTP, and the traceback goes
    to stderr.log in the application root - the same file a working app on
    this host keeps there. The password is redacted before printing.
    """
    print("\n--- passenger logs ---")
    password = os.environ.get("DB_PASSWORD", "")
    candidates = [
        os.path.join(HERE, "stderr.log"),
        os.path.join(HERE, "passenger.log"),
        os.path.join(HERE, "error.log"),
    ]
    found = False
    for path in candidates:
        if not os.path.isfile(path):
            continue
        found = True
        try:
            size = os.path.getsize(path)
            with open(path, encoding="utf-8", errors="replace") as handle:
                handle.seek(max(0, size - 6000))
                tail = handle.read()
        except OSError as exc:
            _field(path, "unreadable ({0})".format(exc.strerror or exc))
            continue
        _field("file", "{0} ({1} bytes)".format(path, size))
        lines = [line for line in tail.splitlines() if line.strip()]
        for line in lines[-25:]:
            print("    {0}".format(_redact(line, password)))
    if not found:
        print("No stderr.log in the application root.")
        print("Set PYTRACKING_DEBUG=1 to have the receiver log its own")
        print("tracebacks, since Passenger may not be writing one here.")


def _report_http_probe():
    """Probe the site through the public URL and directly on the loopback.

    The public probe goes out through Cloudflare, which is a separate hop that
    can answer 404/200 on its own. Hitting 127.0.0.1 with the Host header set
    asks the origin web server directly: if Flask answers there but not in
    public, the problem is in front of the origin, not in Passenger.
    """
    print("\n--- live http probe (from this server) ---")
    import ssl
    import urllib.error
    import urllib.request

    domain = os.environ.get("SMOKE_DOMAIN", "https://my.herbsworld.net")
    host = domain.split("://", 1)[-1].split("/")[0]

    def show(label, url, headers=None, context=None):
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(
                request, timeout=10, context=context
            ) as response:
                body = response.read(300).decode("utf-8", "replace")
                server = response.headers.get("Server", "?")
                powered = response.headers.get("X-Powered-By", "")
                _field(label, "HTTP {0}".format(response.status))
                print("    server={0} powered-by={1}".format(server, powered))
                print("    {0}".format(body.replace("\n", " ")[:160]))
                return response.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read(300).decode("utf-8", "replace")
            _field(label, "HTTP {0}".format(exc.code))
            print("    {0}".format(body.replace("\n", " ")[:160]))
            if "litespeed" in body.lower() or "powered by" in body.lower():
                print("    answered by the LiteSpeed error page, not Flask")
            return exc.code, body
        except Exception as exc:
            _field(label, "FAILED: {0}".format(type(exc).__name__))
            return None, ""

    show("public /healthz", domain.rstrip("/") + "/healthz")

    # Direct to the origin, bypassing Cloudflare. Host header selects the vhost.
    show(
        "loopback http://127.0.0.1/healthz",
        "http://127.0.0.1/healthz",
        headers={"Host": host},
    )
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    show(
        "loopback https://127.0.0.1/healthz",
        "https://127.0.0.1/healthz",
        headers={"Host": host},
        context=insecure,
    )

    # Control: a vhost known to run a Python app on this host.
    show("control crawler.herbsworld.net/healthz",
         "https://crawler.herbsworld.net/healthz")


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
