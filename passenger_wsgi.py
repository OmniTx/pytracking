# SPDX-License-Identifier: Apache-2.0
"""pytracking - Phase 1 receiver with tenant resolution, origin validation,
deduplication, and client-IP hashing.

All secrets are read from environment variables; none are stored in code.
"""

import hashlib
import hmac
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request

import pymysql
from flask import Flask, Response, jsonify, request

application = Flask(__name__)

BUILD_MARKER = "pytracking-phase1-landing"

OUTBOUND_TARGET = "https://graph.facebook.com/"
OUTBOUND_TIMEOUT_SECONDS = 5

# Validation limits
MAX_BODY_BYTES = 16384  # 16 KB
MAX_EVENT_ID_LEN = 64
MAX_NAME_LEN = 64
MAX_USER_AGENT_LEN = 255


def _log_traceback():
    """Append the current traceback to pytracking-debug.log beside this file."""
    import traceback

    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pytracking-debug.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("--- {0} ---\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S")))
            traceback.print_exc(file=handle)
    except Exception:
        pass


def _no_content():
    """A 204 with no body and no tracking headers.

    Werkzeug would otherwise attach ``Content-Type: text/html``; the receiver
    must answer with none, so strip it.
    """
    response = Response(status=204)
    response.headers.pop("Content-Type", None)
    return response


def _ip_salt():
    """Return the HMAC salt from the ``PYTRACKING_IP_SALT`` environment variable."""
    salt = os.environ.get("PYTRACKING_IP_SALT")
    if not salt:
        raise RuntimeError(
            "PYTRACKING_IP_SALT environment variable is not set. "
            "Generate one with ``python -c 'import secrets; print(secrets.token_hex(32))'``"
        )
    return salt.encode("utf-8")


def hash_client_ip(remote_addr):
    """Return an HMAC-SHA256 hex digest of *remote_addr* (64 chars).

    Never stores or echoes the raw address.
    """
    return hmac.new(
        _ip_salt(),
        remote_addr.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _insert_event(connection, tenant_id, event_id, name, payload_json,
                  consent_state, client_ip_hash, user_agent, source_origin):
    """Insert a single event row.

    Raises pymysql.err.IntegrityError on a duplicate (tenant_id, event_id).
    All other exceptions are reraised.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO events "
            "(tenant_id, event_id, name, received_at, payload_json, "
            "consent_state, client_ip_hash, user_agent, source_origin) "
            "VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s)",
            (
                tenant_id,
                event_id,
                name,
                payload_json,
                consent_state,
                client_ip_hash,
                user_agent,
                source_origin,
            ),
        )


class UnknownWriteKey(Exception):
    """Raised when a write key does not resolve to a tenant."""


def _find_tenant_by_write_key(conn, write_key):
    """Resolve *write_key* to ``(tenant_id, active)``; raise UnknownWriteKey."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, active FROM tenant_keys WHERE write_key = %s LIMIT 1",
            (write_key,),
        )
        row = cursor.fetchone()
    if row is None:
        raise UnknownWriteKey(write_key)
    return row[0], bool(row[1])


def _get_allowed_origins(conn, tenant_id):
    """Return a list of allowed origins for *tenant_id*.

    Returns [] when no origins are configured.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT origin FROM tenant_origins WHERE tenant_id = %s",
            (tenant_id,),
        )
        return [row[0] for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@application.route("/api/v2/ev", methods=["POST"])
def receive_event():
    """Phase-1 event receiver.

    Order matters: size and shape checks first, then tenant resolution.

        1. Reject bodies over 16 KB with 413.
        2. Parse JSON; reject malformed with 400.
        3. Missing/malformed/unknown/inactive write key → silent 204.
        4. Require event_id (str ≤ 64) and name (str ≤ 64), else 400.
        5. Origin must be in the tenant allow-list, else silent 204.
        6. Insert row; dedupe on (tenant_id, event_id) → 204 on conflict.
        7. Always return 204 with no body and no tracking headers.

    Rate-limiting is deferred. TODO(#rate-limit): implement an external
    (e.g. Redis-backed) limiter because Passenger spawns multiple processes
    and in-process memory cannot enforce a shared limit.
    """

    # --- 1. Body size --------------------------------------------------------
    # Check size first: an oversize body is 413 regardless of its shape.
    # Use Content-Length when available; fall back to raw body byte length.
    content_length = request.content_length
    if content_length is None:
        content_length = len(request.get_data(cache=True))
    if content_length > MAX_BODY_BYTES:
        return Response(status=413)  # Request Entity Too Large

    # --- 2. Parse JSON -------------------------------------------------------
    try:
        body = request.get_json(force=False, silent=False)
    except Exception:
        body = None
    if not isinstance(body, dict):
        return jsonify({"error": "invalid request body"}), 400

    # --- 3. Write key: missing, malformed, unknown, or inactive all get a
    #        silent 204 so clients cannot probe key validity. ---------------
    wk = body.get("wk")
    if not isinstance(wk, str) or not wk:
        return _no_content()

    # --- 4. Required event fields (400 on missing/invalid) ------------------
    event_id = body.get("event_id")
    if event_id is None:
        return jsonify({"error": "missing field: event_id"}), 400
    if not isinstance(event_id, str) or len(event_id) > MAX_EVENT_ID_LEN:
        return jsonify({"error": "invalid field: event_id"}), 400

    name = body.get("name")
    if name is None:
        return jsonify({"error": "missing field: name"}), 400
    if not isinstance(name, str) or len(name) > MAX_NAME_LEN:
        return jsonify({"error": "invalid field: name"}), 400

    # --- 5. Resolve write key & validate -------------------------------------
    connection = None
    try:
        connection = pymysql.connect(
            host=os.environ["DB_HOST"],
            database=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            connect_timeout=5,
            charset="utf8mb4",
        )

        tenant_id, active = _find_tenant_by_write_key(connection, wk)
        if not active:
            # Key exists but is inactive → silent reject.
            return _no_content()

        # --- 5. Origin check ---------------------------------------------------
        # Fail closed: the request Origin must be in the tenant's allow-list.
        # No configured origins means nothing can match → silent reject.
        allowed = _get_allowed_origins(connection, tenant_id)
        origin_header = request.headers.get("Origin")
        if not allowed or origin_header not in allowed:
            return _no_content()

        # --- 6. Build & insert -------------------------------------------------
        client_ip = request.remote_addr or "0.0.0.0"
        client_ip_hash = hash_client_ip(client_ip)

        user_agent = (request.headers.get("User-Agent") or "")[:MAX_USER_AGENT_LEN]
        source_origin = (origin_header or "").strip()

        # Persist the payload with the write key removed: the key is a secret
        # and must not be stored in every event row.
        stored_payload = {k: v for k, v in body.items() if k != "wk"}
        payload_json = json.dumps(stored_payload, separators=(",", ":"))

        consent_state = body.get("consent_state")
        if isinstance(consent_state, bool):
            consent_state = 1 if consent_state else 0
        elif consent_state not in (None, 0, 1):
            consent_state = None

        _insert_event(
            connection,
            tenant_id,
            event_id,
            name,
            payload_json,
            consent_state,
            client_ip_hash,
            user_agent,
            source_origin,
        )
        connection.commit()

    except pymysql.err.IntegrityError:
        # Duplicate (tenant_id, event_id) → treat as success.
        pass
    except UnknownWriteKey:
        # Unknown write key → silent reject, do not reveal key validity.
        pass
    except Exception:
        # Unexpected failure. With PYTRACKING_DEBUG=1 the traceback is written
        # next to the app, because Passenger may not capture stderr here and
        # an HTTP 500 alone gives no way to tell what happened.
        if os.environ.get("PYTRACKING_DEBUG") == "1":
            _log_traceback()
            return _no_content()
        raise
    finally:
        if connection is not None and connection.open:
            connection.close()

    return _no_content()


@application.get("/healthz")
def healthz():
    return jsonify({"ok": True})


def _flask_version():
    """Return the installed Flask version without the deprecated attribute."""
    try:
        from importlib.metadata import version

        return version("flask")
    except Exception:
        return "unknown"


@application.get("/")
def landing():
    """Human-readable landing page that proves Flask served the request.

    Deliberately reports only *whether* each secret is set, never its value.
    """
    env_names = [
        "DB_HOST",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
        "PYTRACKING_IP_SALT",
    ]
    env_rows = "".join(
        '<tr><td>{0}</td><td class="{1}">{2}</td></tr>'.format(
            name,
            "ok" if os.environ.get(name) else "missing",
            "set" if os.environ.get(name) else "MISSING",
        )
        for name in env_names
    )

    module_file = getattr(sys.modules.get(__name__), "__file__", "unknown")
    try:
        stat = os.stat(module_file)
        file_info = "{0} ({1} bytes, mtime {2})".format(
            module_file,
            stat.st_size,
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(stat.st_mtime)),
        )
    except OSError:
        file_info = "{0} (stat failed)".format(module_file)

    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>pytracking is running</title>
<style>
  body {{ font: 15px/1.6 -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 2.5rem; color: #161616; background: #f4f4f4; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.5rem; }}
  .ok {{ color: #0f6b3f; font-weight: 600; }}
  .missing {{ color: #b32d0f; font-weight: 600; }}
  table {{ border-collapse: collapse; margin: .75rem 0 1.5rem; }}
  td {{ padding: .3rem 1.25rem .3rem 0; }}
  code {{ background: #e8e8e8; padding: .1rem .35rem; }}
  dt {{ font-weight: 600; margin-top: .6rem; }}
  dd {{ margin: 0 0 .2rem 0; }}
</style>
</head>
<body>
<h1>pytracking receiver is running</h1>
<p>Flask served this page, so Passenger is routing requests to Python.</p>
<table>
  <tr><td>Build marker</td><td><code>{marker}</code></td></tr>
  <tr><td>Python</td><td>{py}</td></tr>
  <tr><td>Flask</td><td>{flask_version}</td></tr>
  <tr><td>Executing file</td><td><code>{file_info}</code></td></tr>
</table>
<h2>Environment variables</h2>
<p>Values are never shown here &mdash; only whether each one is set.</p>
<table>{env_rows}</table>
<h2>Routes</h2>
<dl>
  <dt>GET /</dt><dd>this page</dd>
  <dt>GET /healthz</dt><dd><code>{{"ok": true}}</code></dd>
  <dt>POST /api/v2/ev</dt><dd>event receiver (204)</dd>
  <dt>GET /diag/db</dt><dd>MySQL connectivity</dd>
  <dt>GET /diag/outbound</dt><dd>HTTPS connectivity</dd>
</dl>
</body>
</html>""".format(
        marker=BUILD_MARKER,
        py=platform.python_version(),
        flask_version=_flask_version(),
        file_info=file_info,
        env_rows=env_rows,
    )
    return Response(html, mimetype="text/html")


class _LengthOverride(Response):
    """A Response that keeps Content-Length on a 204.

    Werkzeug strips Content-Length from 204/304 per RFC 7230, which some
    servers and proxy layers reject. This re-adds it after that processing.
    """

    def get_wsgi_headers(self, environ):
        headers = super().get_wsgi_headers(environ)
        headers["Content-Length"] = "0"
        return headers


@application.get("/diag/204")
def diag_204():
    """Try the framings a 204 can take, to find one the host accepts.

    ``?mode=`` selects the variant; see ``_no_content`` and the diagnostic
    notes. Each is a valid HTTP response, but server and proxy stacks differ
    in which ones they pass through.
    """
    mode = request.args.get("mode", "plain")

    if mode == "content-type":
        # Keep Werkzeug's default Content-Type, add nothing else.
        return Response(status=204)

    if mode == "content-length":
        # 204 carrying an explicit Content-Length: 0.
        response = _LengthOverride(status=204)
        response.headers.pop("Content-Type", None)
        return response

    if mode == "empty-200":
        # Fallback shape: an empty body with an ordinary 200.
        return Response("", status=200, mimetype="text/plain")

    # "plain": what the receiver returns - 204 with no headers, no body.
    return _no_content()


@application.get("/diag/no-content")
def diag_no_content():
    """Return a bare 204, to test how the server handles a bodyless response.

    /healthz (200 + JSON) works through Passenger on this host, but every 204
    from the receiver arrives as a 500. This isolates whether the status code
    itself is the problem rather than the view logic.
    """
    return _no_content()


@application.get("/diag/db")
def diag_db():
    connection = None
    try:
        connection = pymysql.connect(
            host=os.environ["DB_HOST"],
            database=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            connect_timeout=5,
        )
    except Exception as exc:
        return jsonify({"db": "error", "message": str(exc)})
    finally:
        if connection is not None:
            connection.close()
    return jsonify({"db": "ok"})


def _elapsed_ms(started):
    return round((time.monotonic() - started) * 1000, 2)


@application.get("/diag/outbound")
def diag_outbound():
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
            OUTBOUND_TARGET, timeout=OUTBOUND_TIMEOUT_SECONDS
        ) as response:
            status_code = response.status
    except urllib.error.HTTPError as exc:
        status_code = exc.code
    except Exception as exc:
        return jsonify(
            {
                "error": type(exc).__name__,
                "message": str(exc),
                "elapsed_ms": _elapsed_ms(started),
            }
        )
    return jsonify({"status": status_code, "elapsed_ms": _elapsed_ms(started)})


if __name__ == "__main__":
    application.run()
