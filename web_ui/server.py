"""Upload Assistant Web UI server."""

import contextlib
import importlib
import os
import sys
import traceback
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

from web_ui import context as context_mod
from web_ui import flask_compat as flask_compat_mod
from web_ui import security as security_mod
from web_ui.context import access_logger, console
from web_ui.extensions import limiter
from web_ui.flask_compat import CORS, Flask, Response, Session, g, get_remote_address, jsonify, redirect, request, url_for
from web_ui.paths import set_runtime_browse_roots
from web_ui.request_utils import _request_cookie_text, _request_form_text, _request_header
from web_ui.routes import account as routes_account
from web_ui.routes import browse as routes_browse
from web_ui.routes import config as routes_config
from web_ui.routes import history as routes_history
from web_ui.routes import pages as routes_pages
from web_ui.routes import review as routes_review
from web_ui.routes import sessions as routes_sessions
from web_ui.routes import system as routes_system
from web_ui.routes import trackers as routes_trackers
from web_ui.security import (
    _first_run_setup_code,
    _handle_failed_auth,
    _load_user_record,
    _readable_secret_files,
    _session_get,
    _verify_remember_token,
    _webui_auth_configured,
    session_secret,
)
from web_ui.session_state import set_execution_preview_target

# upload.py imports these from web_ui.server.
__all__ = ["app", "set_execution_preview_target", "set_runtime_browse_roots"]

sys.path.insert(0, str(Path(__file__).parent.parent))


def _parse_trusted_proxy_count(raw_value: str | None) -> int:
    """Parse the explicitly trusted number of reverse proxies."""
    value = (raw_value or "").strip()
    if not value:
        return 0
    try:
        count = int(value)
    except ValueError as exc:
        raise ValueError("UA_WEBUI_TRUSTED_PROXY_COUNT must be an integer from 0 to 10") from exc
    if count < 0 or count > 10:
        raise ValueError("UA_WEBUI_TRUSTED_PROXY_COUNT must be an integer from 0 to 10")
    return count


def _apply_proxy_fix(wsgi_app: object, trusted_proxy_count: int) -> object:
    """Trust forwarded host, scheme, and client IP only when configured."""
    if trusted_proxy_count == 0:
        return wsgi_app
    return flask_compat_mod.ProxyFix(
        wsgi_app,
        x_for=trusted_proxy_count,
        x_proto=trusted_proxy_count,
        x_host=trusted_proxy_count,
    )


app: Any = Flask(__name__)
trusted_proxy_count = _parse_trusted_proxy_count(os.environ.get("UA_WEBUI_TRUSTED_PROXY_COUNT"))
app.wsgi_app = _apply_proxy_fix(app.wsgi_app, trusted_proxy_count)
app.config["UA_WEBUI_TRUSTED_PROXY_COUNT"] = trusted_proxy_count
app.secret_key = session_secret
sess_dir = context_mod.cfg_dir / "sessions"
sess_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = False
# Ensure permanent sessions (when set) expire after 30 days to match remember-me cookie
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Prefer CacheLib's FileSystemCache when available. Set `SESSION_CACHELIB`
# to an instance of `FileSystemCache` so Flask-Session uses CacheLib's
# implementation and avoids the deprecated `SESSION_FILE_DIR` path.
_session_cache = None
try:
    FileSystemCache = importlib.import_module("cachelib.file").FileSystemCache

    with contextlib.suppress(Exception):
        _session_cache = FileSystemCache(str(sess_dir), mode=0o600)
except Exception:
    _session_cache = None

if _session_cache is not None:
    # Use CacheLib-backed cache for sessions (preferred)
    app.config["SESSION_CACHELIB"] = _session_cache
    try:
        CacheLibSessionInterface = importlib.import_module("flask_session.cachelib").CacheLibSessionInterface

        # Set the session interface directly to the CacheLib-backed implementation
        # Pass the cache as the `client` kwarg to avoid binding it to the
        # positional `app` parameter of the constructor.
        app.session_interface = CacheLibSessionInterface(client=_session_cache)
    except Exception:
        # If for some reason the adapter class isn't available, fall back
        # to letting Flask-Session initialize via Session(app).
        Session(app)
else:
    # Fallback for environments without CacheLib: keep legacy file-dir config
    app.config["SESSION_FILE_DIR"] = str(sess_dir)
    Session(app)

# Initialize Flask-Limiter for rate limiting
limiter.init_app(app)
CORS_fn = CORS


def _parse_cors_origins() -> list[str]:
    raw = os.environ.get("UA_WEBUI_CORS_ORIGINS", "").strip()
    if not raw:
        return []
    origins: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part == "*":
            # Credentialed CORS for every origin would let any website read the
            # CSRF token and act as the signed-in user.
            raise ValueError("UA_WEBUI_CORS_ORIGINS must list explicit origins; '*' is not allowed")
        if part:
            origins.append(part)
    return origins


cors_origins = _parse_cors_origins()
if not cors_origins:
    # Cross-site callers are not configured, so keep browser sessions same-site.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if cors_origins:
    # Allow the CSRF header and support credentials so browser-based
    # cross-origin requests can send cookies for authenticated sessions.
    CORS_fn(
        app,
        resources={r"/api/*": {"origins": cors_origins}},
        allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
        supports_credentials=True,
    )


@app.before_request
def _require_auth_for_webui():  # pyright: ignore[reportUnusedFunction]
    """Enforce IP and authentication checks, recording rejected API credentials."""
    # Health endpoint can be used for orchestration checks.
    if request.path == "/api/health":
        return None

    # Check IP access control
    client_ip = get_remote_address()
    if not security_mod._is_ip_allowed(client_ip):
        # Log the blocked attempt
        if access_logger:
            with contextlib.suppress(AttributeError):
                access_logger.log(
                    endpoint=request.path,
                    method=request.method,
                    remote_addr=client_ip,
                    username=None,
                    success=False,
                    status=403,
                    headers={"User-Agent": _request_header("User-Agent")},
                    details="IP blocked",
                )
        return jsonify({"error": "Access denied", "success": False}), 403

    # Try to restore session from a long-lived remember-me cookie if present
    with contextlib.suppress(Exception):
        if not security_mod._is_authenticated():
            token = _request_cookie_text("ua_remember")
            if token:
                username = _verify_remember_token(token)
                if username:
                    # Only accept remember token if it matches the persisted user (if any)
                    persisted = _load_user_record()
                    if persisted:
                        stored = persisted.get("username")
                        if stored and username == stored:
                            g.authenticated = True
                            g.username = username
        # Any failure to validate the cookie should not block request flow; fallback to normal auth

    if request.path.startswith("/api/"):
        # For API, allow basic auth
        if security_mod._webui_auth_ok():
            return None
        # Or session auth
        if security_mod._is_authenticated():
            # Set username in g for logging if available
            with contextlib.suppress(Exception):
                username = str(_session_get("username") or "")
                if username:
                    g.username = username
            return None
        # If request accepts HTML (browser), redirect to login; else 401 for API clients
        if "text/html" in (_request_header("Accept") or ""):
            if _request_header("Authorization"):
                _handle_failed_auth(client_ip)
            return redirect(url_for("pages.login_page"))
        _handle_failed_auth(client_ip)
        return jsonify({"error": "Authentication required", "success": False}), 401

    # For web routes
    if security_mod._is_authenticated():
        # Set username in g for logging if available
        with contextlib.suppress(Exception):
            username = _session_get("username")
            if isinstance(username, str) and username:
                g.username = username
        return None
    if _webui_auth_configured() and security_mod._webui_auth_ok():
        return None
    if request.path in {"/config", "/stats", "/history", "/", "/index.html"}:
        return redirect(url_for("pages.login_page"))

    return None


@app.after_request
def _set_security_headers(response: Any) -> Any:  # pyright: ignore[reportUnusedFunction]
    """Apply browser hardening headers that do not affect embedding or proxies."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        # API responses can contain configuration secrets and upload output.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


_VERSIONED_STATIC_MAX_AGE = 365 * 24 * 60 * 60
_STATIC_IMAGE_MAX_AGE = 24 * 60 * 60


@app.url_defaults
def _version_static_urls(endpoint: str, values: dict[str, Any]) -> None:  # pyright: ignore[reportUnusedFunction]
    """Tag template asset URLs with the file's mtime so changed files get new URLs."""
    filename = values.get("filename")
    if endpoint != "static" or "v" in values or not isinstance(filename, str) or not app.static_folder:
        return
    with suppress(OSError):
        values["v"] = (Path(app.static_folder) / filename).stat().st_mtime_ns


@app.after_request
def _set_static_cache_headers(response: Any) -> Any:  # pyright: ignore[reportUnusedFunction]
    """Let browsers reuse static assets instead of revalidating each one per page load.

    Versioned URLs change with the file, so they can be cached long-term. Images
    and fonts referenced by fixed paths (tracker icons, CSS) are cached for a day.
    """
    if request.endpoint != "static" or response.status_code not in {200, 304}:
        return response
    if request.args.get("v"):
        max_age = _VERSIONED_STATIC_MAX_AGE
        response.cache_control.immutable = True
    elif request.path.startswith(("/static/img/", "/static/fonts/")):
        max_age = _STATIC_IMAGE_MAX_AGE
    else:
        return response
    response.cache_control.no_cache = None
    response.cache_control.public = True
    response.cache_control.max_age = max_age
    return response


@app.after_request
def _maybe_log_api_access(response: object) -> object:
    """Log API access attempts according to configured level.

    By default only non-successful API attempts are logged (level: access_denied).
    When level=access all accesses are logged.
    When level=disabled no logging occurs.
    """
    with contextlib.suppress(Exception):
        if access_logger is None:
            return response

        path = request.path or ""
        if not path.startswith("/api/"):
            return response

        status = getattr(response, "status_code", 500)
        success = 200 <= int(status) < 300
        if not access_logger.should_log(success):
            return response

        # Determine username if available
        user: str | None = None
        try:
            # First try authenticated user
            user_value = getattr(g, "username", None) or _session_get("username")
            user = user_value if isinstance(user_value, str) else None

            # For failed auth attempts, try to extract attempted username
            if user is None and not success:
                # Check Basic auth
                if request.authorization is not None and request.authorization.username:
                    user = f"{request.authorization.username} (basic auth)"
                # Check form data (login attempts)
                elif request.method == "POST" and _request_form_text("username"):
                    user = f"{_request_form_text('username')} (login attempt)"
                # Check Bearer token (even if invalid, might give us a hint)
                elif _request_header("Authorization").startswith("Bearer "):
                    user = "bearer token attempt"
        except Exception:
            user = None

        # Minimal headers for context
        headers = dict(request.headers.items()) if request.headers else None

        remote_value = request.remote_addr or request.environ.get("REMOTE_ADDR")
        remote = remote_value if isinstance(remote_value, str) else None

        access_logger.log(
            endpoint=path,
            method=request.method,
            remote_addr=remote,
            username=user,
            success=success,
            status=int(status),
            headers=headers,
            details=None,
        )
    return response


@app.errorhandler(429)
def _rate_limit_exceeded(_e: Exception) -> Any:
    # API callers expect JSON; keep non-API responses lightweight as well.
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Too many requests. Please wait before trying again."}), 429
    return Response("Too many requests", status=429, mimetype="text/plain")


@app.route("/logout", methods=["GET", "POST"])  # prefer POST from the UI
def logout():
    # Accept both GET and POST for compatibility, but UI should use POST.
    # Remove encrypted session payload
    # Clear all server-side session data
    try:
        session.clear()
    except Exception:
        # Fallback: remove encrypted payload if clear fails
        session.pop("enc", None)

    resp = redirect(url_for("login_page"))
    # Remove remember cookie if present
    resp.delete_cookie("ua_remember")
    # Also remove the browser session cookie (Flask's session cookie name)
    try:
        resp.delete_cookie(app.session_cookie_name)
    except Exception:
        # Fallback to common cookie name
        resp.delete_cookie("session")
    return resp


@app.route("/login/recovery", methods=["GET", "POST"])
@limiter.limit("10 per minute;100 per day", key_func=get_remote_address, error_message="Too many attempts, please try again later.")
def login_recovery():
    """Handle login using a recovery code. This is a separate endpoint to
    keep recovery-code input distinct from strict 2FA inputs so password
    managers treat the 2FA input as a one-time code field.
    """
    # If GET, render the dedicated recovery page (minimal inputs)
    if request.method == "GET":
        return render_template("login_recovery.html", show_2fa=_totp_enabled())

    # POST: process recovery code login
    # Quick IP block check: short-circuit heavy work for known-bad IPs.
    if not _is_ip_allowed(get_remote_address()):
        return Response("Too many requests", status=429, mimetype="text/plain")

    username = _request_form_text("username").strip()
    password = _request_form_text("password").strip()
    recovery_code = _request_form_text("recovery_code").strip()
    remember = _request_form_text("remember") == "1"

    if not _totp_enabled():
        return render_template("login_recovery.html", error="Recovery codes are not enabled", show_2fa=False)

    persisted = _load_user_record()
    # If a persisted user exists, require those credentials + recovery code
    if persisted:
        if username and password and recovery_code and _consume_recovery_code(recovery_code) and auth_mod.verify_user(username, password):
            _mark_session_authenticated(username)
            if remember:
                session.permanent = True
                with contextlib.suppress(Exception):
                    token = _create_remember_token(username)
                    if token:
                        resp = redirect(url_for("index"))
                        resp.set_cookie("ua_remember", token, max_age=30 * 86400, httponly=True, secure=True, samesite="Lax")
                        return resp
            return redirect(url_for("index"))
        # Failed recovery attempt -> record and show recovery page
        _handle_failed_auth(get_remote_address())
        return render_template("login_recovery.html", error="Recovery code invalid", show_2fa=_totp_enabled())

    _handle_failed_auth(get_remote_address())
    return render_template("login_recovery.html", error="Recovery code invalid", show_2fa=_totp_enabled())


@app.route("/config")
def config_page():
    """Serve the config UI"""
    # Require a CSRF token or same-origin Referer for the config page when
    # the user is authenticated to reduce cross-site information leakage.
    if _is_authenticated() and not _verify_csrf_header():
        referer = _request_header("Referer")
        # Compute an "effective" scheme taking into account common proxies
        # that may not set X-Forwarded-Proto but do set Cloudflare headers.
        effective_scheme = None
        try:
            xf_proto = _request_header("X-Forwarded-Proto")
            if xf_proto:
                effective_scheme = xf_proto.split(",", 1)[0].strip()
            else:
                cf_visitor = _request_header("Cf-Visitor")
                if cf_visitor:
                    try:
                        import json as _json

                        cfv = _json.loads(cf_visitor)
                        effective_scheme = str(cfv.get("scheme")) if cfv.get("scheme") else None
                    except Exception:
                        effective_scheme = None
        except Exception:
            effective_scheme = None

        if not effective_scheme:
            try:
                effective_scheme = request.scheme
            except Exception:
                effective_scheme = "http"

        effective_host_url = f"{effective_scheme}://{request.host}/"

        # Parse the Referer and compare host:port (netloc) with the request host.
        try:
            from urllib.parse import urlparse

            parsed = urlparse(referer or "")
            referer_netloc = parsed.netloc
        except Exception:
            referer_netloc = ""

        # Accept same-origin when referer host matches request.host
        if referer_netloc != request.host:
            # Log diagnostic info to help debug reverse-proxy header mismatches
            console.print(f"[yellow]CSRF check failed for /config: host_url={effective_host_url}, Referer={referer}")
            # Return a helpful error including the observed host_url and referer
            return (
                jsonify(
                    {
                        "error": "CSRF token missing or invalid",
                        "success": False,
                        "debug": {"host_url": effective_host_url, "referer": referer, "request_host": request.host},
                    }
                ),
                403,
            )

    try:
        return render_template(
            "config.html",
            app_version=APP_VERSION,
            csrf_token=_ensure_csrf_token(),
        )
    except Exception as e:
        console.print(f"Error loading config template: {e}", markup=False)
        console.print(traceback.format_exc(), markup=False)
        return "<pre>Internal server error</pre>", 500


@app.route("/stats")
def stats_page():
    """Serve the privacy-preserving statistics dashboard."""
    if not _is_authenticated():
        return redirect(url_for("login_page"))
    return render_template("stats.html", app_version=APP_VERSION, csrf_token=_ensure_csrf_token())


def _add_stats_destination_display_names(payload: dict[str, Any]) -> None:
    """Add catalogue-backed labels without changing persisted destination keys."""
    from src.trackersetup import tracker_class_map

    rows = payload.get("uploads", {}).get("by_destination", [])
    filter_rows = payload.get("filters", {}).get("destinations", [])
    for row in [*rows, *filter_rows]:
        destination = str(row.get("destination") or "")
        tracker_class = tracker_class_map.get(destination.upper())
        row["display_name"] = str(getattr(tracker_class, "display_name", destination))
    for node in payload.get("sankey", {}).get("nodes", []):
        destination = str(node.get("destination") or "")
        if destination:
            tracker_class = tracker_class_map.get(destination.upper())
            node["label"] = str(getattr(tracker_class, "display_name", destination))


@app.route("/api/health")
@limiter.exempt
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "success": True, "message": "Upload-Assistant Web UI is running"})


@app.route("/api/stats", methods=["GET", "DELETE"])
def stats_api():
    """Read or reset local aggregate statistics for an authenticated browser."""
    if not _is_authenticated() or _get_bearer_from_header():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    from src.stats import get_empty_stats, get_stats, reset_stats, stats_collection_enabled

    if request.method == "DELETE":
        body = _request_json_dict()
        if body.get("confirmation") != "RESET":
            return jsonify({"success": False, "error": "Type RESET to confirm"}), 400
        try:
            return jsonify({"success": True, "reset_at": reset_stats(STATE_DIR)})
        except OSError, sqlite3.Error:
            return jsonify({"success": False, "error": "Unable to reset statistics"}), 500

    period = str(request.args.get("range", "30d"))
    mode = str(request.args.get("mode", "real"))
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    tracker = str(request.args.get("tracker", ""))
    time_basis = str(request.args.get("timezone", "utc"))
    local_date = request.args.get("today")
    try:
        config = _load_config_from_file(STATE_DIR / "data" / "config.py") or {}
        enabled = stats_collection_enabled(config)
        stats_kwargs = {
            "date_from": date_from,
            "date_to": date_to,
            "tracker": tracker,
            "time_basis": time_basis,
            "local_date": local_date,
        }
        payload = get_stats(period, mode, STATE_DIR, **stats_kwargs) if enabled else get_empty_stats(period, mode, **stats_kwargs)
        _add_stats_destination_display_names(payload)
        payload["enabled"] = enabled
        return jsonify(payload), 200 if payload["success"] else 500
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/history")
def history_page():
    """Serve the local upload history."""
    if not _is_authenticated():
        return redirect(url_for("login_page"))
    return render_template("history.html", app_version=APP_VERSION, csrf_token=_ensure_csrf_token())


def _history_request_error() -> tuple[Any, int] | None:
    """Allow history requests only from an authenticated browser session (no API tokens)."""
    if not _is_authenticated() or _get_bearer_from_header():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403
    return None


def _history_tracker_names(codes: Iterable[str]) -> dict[str, str]:
    from src.trackersetup import tracker_class_map

    return {code: str(getattr(tracker_class_map.get(code), "display_name", code)) for code in codes}


@app.route("/api/history", methods=["GET", "DELETE"])
@limiter.limit("600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def history_api():
    """List or clear the local upload history."""
    error = _history_request_error()
    if error is not None:
        return error

    from src.upload_history import clear_history, history_enabled, list_items

    if request.method == "DELETE":
        if _request_json_dict().get("confirmation") != "CLEAR":
            return jsonify({"success": False, "error": "Type CLEAR to confirm"}), 400
        try:
            return jsonify({"success": True, "removed": clear_history(STATE_DIR)})
        except OSError, sqlite3.Error:
            return jsonify({"success": False, "error": "Unable to clear the upload history"}), 500

    args = request.args
    try:
        page = list_items(
            STATE_DIR,
            search=str(args.get("q", ""))[:200],
            category=str(args.get("category", "")),
            tracker=str(args.get("tracker", "")),
            tracker_status=str(args.get("tracker_status", "")),
            outcome=str(args.get("outcome", "")),
            has_requests=str(args.get("requests", "")).lower() in {"1", "true", "yes"},
            after=str(args.get("after", "")),
            before=str(args.get("before", "")),
            limit=int(args.get("limit", 50)),
            offset=int(args.get("offset", 0)),
        )
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except OSError, sqlite3.Error:
        return jsonify({"success": False, "error": "Unable to read the upload history"}), 500
    trackers = {tracker["tracker"] for item in page["items"] for tracker in item["trackers"]} | set(page["facets"]["trackers"])
    config = _load_config_from_file(STATE_DIR / "data" / "config.py") or {}
    return jsonify({**page, "tracker_names": _history_tracker_names(sorted(trackers)), "enabled": history_enabled(config), "success": True})


@app.route("/api/history/export")
@limiter.limit("120 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def history_export_api():
    """Download every history entry matching the list filters as CSV or JSON."""
    error = _history_request_error()
    if error is not None:
        return error

    from src.upload_history import export_csv, export_items

    args = request.args
    export_format = str(args.get("format", "csv")).lower()
    if export_format not in {"csv", "json"}:
        return jsonify({"success": False, "error": "format must be csv or json"}), 400
    try:
        items = export_items(
            STATE_DIR,
            search=str(args.get("q", ""))[:200],
            category=str(args.get("category", "")),
            tracker=str(args.get("tracker", "")),
            tracker_status=str(args.get("tracker_status", "")),
            outcome=str(args.get("outcome", "")),
            has_requests=str(args.get("requests", "")).lower() in {"1", "true", "yes"},
            after=str(args.get("after", "")),
            before=str(args.get("before", "")),
        )
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except OSError, sqlite3.Error:
        return jsonify({"success": False, "error": "Unable to read the upload history"}), 500
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")
    if export_format == "json":
        body, mimetype = json.dumps({"items": items}, ensure_ascii=False, indent=2), "application/json"
    else:
        body, mimetype = export_csv(items), "text/csv; charset=utf-8"
    response = Response(body, mimetype=mimetype)
    response.headers["Content-Disposition"] = f'attachment; filename="upload-assistant-history-{stamp}.{export_format}"'
    return response


@app.route("/api/history/<int:item_id>", methods=["GET", "DELETE"])
@limiter.limit("600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def history_item_api(item_id: int):
    """Read or delete one upload history entry."""
    error = _history_request_error()
    if error is not None:
        return error

    from src.upload_history import delete_item, get_item

    try:
        if request.method == "DELETE":
            if not delete_item(item_id, STATE_DIR):
                return jsonify({"success": False, "error": "History entry not found"}), 404
            return jsonify({"success": True})
        item = get_item(item_id, STATE_DIR)
    except OSError, sqlite3.Error:
        return jsonify({"success": False, "error": "Unable to read the upload history"}), 500
    if item is None:
        return jsonify({"success": False, "error": "History entry not found"}), 404
    item["tracker_names"] = _history_tracker_names(tracker["tracker"] for tracker in item["trackers"])
    return jsonify({"success": True, "item": item})


@app.route("/api/update_status")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def update_status():
    """Return the cached upstream release status for the shared application rail."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    user_config = _load_config_from_file(STATE_DIR / "data" / "config.py") or {}
    example_config = _load_config_from_file(CODE_DIR / "data" / "example_config.py") or {}
    user_defaults = _as_dict(user_config.get("DEFAULT")) or {}
    example_defaults = _as_dict(example_config.get("DEFAULT")) or {}
    force = str(request.args.get("refresh", "")).strip().lower() in {"1", "true", "yes"}
    enabled = bool(user_defaults.get("update_notification", example_defaults.get("update_notification", True)))
    raw_cache_hours = user_defaults.get(
        "update_notification_cache_hours",
        example_defaults.get("update_notification_cache_hours", 4),
    )
    try:
        cache_hours = max(0.0, float(raw_cache_hours))
    except TypeError, ValueError:
        cache_hours = 4.0

    return jsonify(
        get_update_status(
            enabled=enabled or force,
            cache_hours=cache_hours,
            force=force,
        )
    )


@app.route("/api/changelog")
def changelog():
    """Return cached upstream release history for the shared changelog viewer."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    user_config = _load_config_from_file(STATE_DIR / "data" / "config.py") or {}
    example_config = _load_config_from_file(CODE_DIR / "data" / "example_config.py") or {}
    user_defaults = _as_dict(user_config.get("DEFAULT")) or {}
    example_defaults = _as_dict(example_config.get("DEFAULT")) or {}
    force = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
    raw_cache_hours = user_defaults.get(
        "update_notification_cache_hours",
        example_defaults.get("update_notification_cache_hours", 4),
    )
    try:
        cache_hours = max(0.0, float(raw_cache_hours))
    except TypeError, ValueError:
        cache_hours = 4.0

    return jsonify(
        get_changelog_history(
            cache_hours=cache_hours,
            force=force,
        )
    )


@app.route("/api/external_tools_status", methods=["POST"])
@limiter.limit("60 per hour", key_func=_rate_limit_key_func)
def external_tools_status():
    """Check configured, detected, and runtime-managed external tools."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    payload = request.get_json(silent=True)
    payload_map = cast(Mapping[str, object], payload) if isinstance(payload, Mapping) else {}
    raw_paths = payload_map.get("paths", {})
    if not isinstance(raw_paths, Mapping):
        return jsonify({"success": False, "error": "Invalid external tool paths"}), 400

    user_config = _load_config_from_file(STATE_DIR / "data" / "config.py") or {}
    example_config = _load_config_from_file(CODE_DIR / "data" / "example_config.py") or {}
    user_defaults = _as_dict(user_config.get("DEFAULT")) or {}
    example_defaults = _as_dict(example_config.get("DEFAULT")) or {}
    effective_defaults: dict[str, Any] = {}

    for key in EXTERNAL_TOOL_KEYS:
        value = raw_paths.get(key, user_defaults.get(key, example_defaults.get(key, "")))
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value) > 4096:
            return jsonify({"success": False, "error": f"Invalid path value for {key}"}), 400
        effective_defaults[key] = value

    try:
        statuses = check_external_tools(
            effective_defaults,
            state_dir=STATE_DIR,
            code_dir=CODE_DIR,
        )
    except Exception as error:
        console.print(f"External tool status check failed: {error}", markup=False)
        return jsonify({"success": False, "error": "Unable to check external tools"}), 500

    return jsonify({"success": True, "statuses": statuses})


@app.route("/api/csrf_token")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def csrf_token():
    """Return the per-session CSRF token for use by the frontend."""
    # Require authenticated web session for CSRF token access
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401

    try:
        token = _ensure_csrf_token()
        return jsonify({"csrf_token": token, "success": True})
    except Exception:
        # Returning an empty CSRF token on error is an explicit non-secret
        # failure response; suppress Bandit's B105 false positive here.
        return jsonify({"csrf_token": "", "success": False}), 500  # nosec: B105 - not a hardcoded password


@app.route("/api/2fa/status")
def twofa_status():
    """Check 2FA status"""
    # Require authenticated web session for 2FA status
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401

    # Require CSRF and same-origin for reads of auth/2fa state
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    return jsonify({"enabled": _totp_enabled(), "success": True})


@app.route("/api/access_log/level", methods=["GET", "POST"])
def access_log_level_api():
    """Get or set the access logging level.

    GET: returns current level (no auth required for read).
    POST: set level (requires web session + CSRF).

    Valid levels: access_denied (default), access, disabled
    """
    if request.method == "GET":
        # Require authenticated web session and CSRF + same-origin for reads
        if not _is_authenticated():
            return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
        if not _verify_csrf_header() or not _verify_same_origin():
            return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

        if access_logger is None:
            return jsonify({"success": False, "error": "Access logging unavailable"}), 500
        try:
            lvl = access_logger.get_level()
            return jsonify({"success": True, "level": lvl})
        except Exception:
            return jsonify({"success": False, "error": "Failed to read level"}), 500

    # POST: require authenticated web session and CSRF
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header():
        return jsonify({"success": False, "error": "CSRF validation failed"}), 403
    # Require same-origin for token management actions
    if not _verify_same_origin():
        return jsonify({"success": False, "error": "Origin validation failed"}), 403

    if access_logger is None:
        return jsonify({"success": False, "error": "Access logging unavailable"}), 500

    data: dict[str, object] = _request_json_dict()
    level = str(data.get("level", ""))
    if level not in ("access_denied", "access", "disabled"):
        return jsonify({"success": False, "error": "Invalid level"}), 400

    ok = access_logger.set_level(level)
    if ok:
        return jsonify({"success": True, "level": level})
    return jsonify({"success": False, "error": "Failed to persist level"}), 500


@app.route("/api/access_log/entries", methods=["GET"])
def access_log_entries_api():
    """Get recent access log entries.

    GET: returns recent log entries (requires web session).
    Query params: n (number of entries, default 50, max 200)
    """
    # Require authenticated web session
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401

    # Require CSRF + same-origin for reads of access-log entries
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    if access_logger is None:
        return jsonify({"success": False, "error": "Access logging unavailable"}), 500

    try:
        n = request.args.get("n", "50")
        n = int(n)
        if n < 1 or n > 200:
            n = 50
    except ValueError, TypeError:
        n = 50

    try:
        entries = access_logger.tail(n)
        return jsonify({"success": True, "entries": entries})
    except Exception:
        return jsonify({"success": False, "error": "Failed to read log entries"}), 500


@app.route("/api/ip_control", methods=["GET", "POST"])
def ip_control_api():
    """Get or set IP whitelist/blacklist.

    GET: returns current whitelist and blacklist (requires web session).
    POST: updates whitelist/blacklist. Body: {"whitelist": [...], "blacklist": [...]}
    (requires web session + CSRF).
    """
    # Require authenticated web session
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401

    if request.method == "GET":
        # Require CSRF + same-origin for reads of IP control settings
        if not _verify_csrf_header() or not _verify_same_origin():
            return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403
        try:
            whitelist = _get_ip_whitelist()
            blacklist = _get_ip_blacklist()
            return jsonify({"success": True, "whitelist": whitelist, "blacklist": blacklist})
        except Exception:
            return jsonify({"success": False, "error": "Failed to read IP control settings"}), 500

    elif request.method == "POST":
        # Require CSRF and same-origin for POST
        if not _verify_csrf_header() or not _verify_same_origin():
            return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403
        try:
            data_raw = request.get_json()
            data = _as_dict(data_raw) or {}
            if not data:
                return jsonify({"success": False, "error": "Invalid JSON"}), 400

            whitelist_raw = data.get("whitelist", [])
            blacklist_raw = data.get("blacklist", [])
            whitelist: list[str] = []
            blacklist: list[str] = []
            if isinstance(whitelist_raw, Sequence) and not isinstance(whitelist_raw, (str, bytes, bytearray)):
                whitelist_seq = cast(Sequence[Any], whitelist_raw)
                whitelist = [str(ip) for ip in whitelist_seq]
            if isinstance(blacklist_raw, Sequence) and not isinstance(blacklist_raw, (str, bytes, bytearray)):
                blacklist_seq = cast(Sequence[Any], blacklist_raw)
                blacklist = [str(ip) for ip in blacklist_seq]

            # Validate IP addresses
            import ipaddress

            for ip in [*whitelist, *blacklist]:
                if not ip:
                    return jsonify({"success": False, "error": f"Invalid IP format: {ip}"}), 400
                try:
                    ipaddress.ip_address(ip)
                except ValueError:
                    return jsonify({"success": False, "error": f"Invalid IP address: {ip}"}), 400

            # Report a policy that was not saved instead of claiming success.
            if not _set_ip_control(whitelist, blacklist):
                return jsonify({"success": False, "error": "Failed to update IP control settings"}), 500
            return jsonify({"success": True})
        except Exception as e:
            console.print(f"Error updating IP control: {e}", markup=False)
            return jsonify({"success": False, "error": "Failed to update IP control settings"}), 500
    return None


@app.route("/api/2fa/setup", methods=["POST"])
def twofa_setup():
    """Setup 2FA - generate secret and return QR code URI"""
    # Require an authenticated web session (disallow API token / basic auth)
    if not _is_authenticated():
        return jsonify({"error": "Authentication required (web session)", "success": False}), 401
    # Require CSRF + same-origin for 2FA setup (sensitive auth action)
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"error": "CSRF/Origin validation failed", "success": False}), 403
    if _totp_enabled():
        return jsonify({"error": "2FA already enabled", "success": False}), 400

    # Get username for QR code: prefer session, then persisted user, else generic
    persisted = _load_user_record()
    username = _session_get("username") or (persisted.get("username") if persisted else "user")

    # Generate secret and provisioning URI using pyotp
    secret = pyotp.random_base32()
    try:
        uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Upload-Assistant")
    except Exception:
        uri = ""
    # Generate one-time recovery codes and store temporarily in session
    recovery_codes = _generate_recovery_codes()
    _session_set("temp_totp_secret", secret)
    _session_set("temp_recovery_codes", recovery_codes)

    return jsonify({"secret": secret, "uri": uri, "recovery_codes": recovery_codes, "success": True})


@app.route("/api/2fa/enable", methods=["POST"])
def twofa_enable():
    """Enable 2FA after verification"""
    # Require an authenticated web session (disallow API token / basic auth)
    if not _is_authenticated():
        return jsonify({"error": "Authentication required (web session)", "success": False}), 401
    # Require CSRF + same-origin for enabling 2FA
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"error": "CSRF/Origin validation failed", "success": False}), 403
    data = _request_json_dict()
    code = str(data.get("code", "")).strip()

    if not code:
        return jsonify({"error": "Code required", "success": False}), 400

    temp_secret = _session_get("temp_totp_secret")
    if not temp_secret:
        return jsonify({"error": "No setup in progress", "success": False}), 400

    # Verify the code with the temporary secret
    totp = pyotp.TOTP(temp_secret)
    if not totp.verify(code):
        return jsonify({"error": "Invalid code", "success": False}), 400

    # Save the secret permanently to encrypted user record
    with suppress(Exception):
        auth_mod.set_totp_secret(temp_secret)

    # Persist recovery codes (hashes) if provided
    temp_codes_raw: object = _session_get("temp_recovery_codes", [])
    temp_codes: list[str] = []
    if isinstance(temp_codes_raw, Sequence) and not isinstance(temp_codes_raw, (str, bytes, bytearray)):
        temp_codes_seq = cast(Sequence[Any], temp_codes_raw)
        temp_codes = [str(c) for c in temp_codes_seq]
    hashes = [_hash_code(c) for c in temp_codes]
    _persist_recovery_hashes(hashes)

    # Update global variable
    global saved_totp_secret
    saved_totp_secret = temp_secret

    # Clear temp session
    _session_pop("temp_totp_secret", None)
    _session_pop("temp_recovery_codes", None)

    return jsonify({"success": True, "recovery_codes": temp_codes})


@app.route("/api/2fa/disable", methods=["POST"])
def twofa_disable():
    """Disable 2FA"""
    # Require an authenticated web session (disallow API token / basic auth)
    if not _is_authenticated():
        return jsonify({"error": "Authentication required (web session)", "success": False}), 401
    # Require CSRF + same-origin for disabling 2FA
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"error": "CSRF/Origin validation failed", "success": False}), 403
    if not _totp_enabled():
        return jsonify({"error": "2FA not enabled", "success": False}), 400

    try:
        auth_mod.set_twofa_state(None, [])
    except OSError, ValueError, TypeError, auth_mod.EncryptionError, json.JSONDecodeError:
        return jsonify({"error": "Failed to disable 2FA", "success": False}), 500

    # Update global variable
    global saved_totp_secret
    saved_totp_secret = None

    return jsonify({"success": True})


@app.route("/api/browse_roots")
@limiter.exempt
def browse_roots():
    """Return configured browse roots"""
    roots = _get_browse_roots()
    if not roots:
        return jsonify({"error": "Browsing is not configured", "success": False}), 400

    # First pass: collect all display names to detect duplicates
    name_to_roots: dict[str, list[str]] = {}
    for root in roots:
        display_name = Path(root.rstrip(os.sep)).name or root
        if display_name not in name_to_roots:
            name_to_roots[display_name] = []
        name_to_roots[display_name].append(root)

    # Second pass: build items with subtitles when needed
    items: list[BrowseItem] = []
    for root in roots:
        display_name = Path(root.rstrip(os.sep)).name or root
        try:
            stat_res = Path(root).stat()
            mtime = stat_res.st_mtime
            size = 0
        except Exception:
            mtime = 0.0
            size = 0
        item: BrowseItem = {
            "name": display_name,
            "path": root,
            "type": "folder",
            "children": [],
            "mtime": mtime,
            "size": size,
        }

        # Add subtitle if multiple roots share the same folder name
        if len(name_to_roots.get(display_name, [])) > 1:
            # Show parent path or drive letter
            parent = str(Path(root.rstrip(os.sep)).parent)
            if parent:
                # On Windows, show drive letter + parent; on Unix, show parent path
                item["subtitle"] = parent
            else:
                # Fallback to full path if no parent (e.g., drive root)
                item["subtitle"] = root

        items.append(item)

    # If caller used a bearer token, require it to be valid.
    bearer = _get_bearer_from_header()
    if bearer and not _token_is_valid(bearer):
        return jsonify({"success": False, "error": "Forbidden (invalid token)"}), 403

    return jsonify({"items": items, "success": True})


@app.route("/api/config_options")
def config_options():
    """Return config options based on example_config.py with overrides from config.py"""
    # Require an authenticated web session; disallow bearer/basic API auth for config access
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401

    # Require CSRF and same-origin for reading configuration options
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    base_dir = STATE_DIR
    example_path = CODE_DIR / "data" / "example_config.py"
    config_path = base_dir / "data" / "config.py"

    example_config_loaded = _load_config_from_file(example_path)
    example_config = example_config_loaded or {}
    user_config_loaded = _load_config_from_file(config_path)
    user_config = user_config_loaded or {}
    comments_map, subsection_map = _extract_example_metadata(example_path)

    # Determine config load status so the UI can warn the user
    # instead of silently showing defaults.
    config_warning: str | None = None
    if not config_path.exists():
        config_warning = "No config.py found — showing example defaults. Configure your settings and save, or place your config.py into the mounted data/ directory."
    elif user_config_loaded is None:
        config_warning = (
            "config.py exists but could not be loaded — showing example defaults. "
            "Check the container logs for details. The file may have a syntax error "
            "or may not contain a valid 'config' dict."
        )

    sections: list[ConfigSection] = []

    for section_name, example_section_raw in example_config.items():
        if not isinstance(example_section_raw, Mapping):
            continue
        example_section = cast(dict[str, Any], example_section_raw)

        if section_name == "DEFAULT":
            example_section = _prepare_default_webui_section(
                example_section,
                comments_map,
                subsection_map,
            )

        user_section_raw = user_config.get(section_name, {})
        user_section = cast(dict[str, Any], user_section_raw) if isinstance(user_section_raw, Mapping) else {}
        if section_name == "TORRENT_CLIENTS":
            example_section = _prepare_torrent_client_webui_section(
                example_section,
                user_section,
                comments_map,
            )
        items = _build_config_items(example_section, user_section, comments_map, subsection_map, [section_name])

        sections.append({"section": section_name, "items": items})

        if section_name == "TORRENT_CLIENTS":
            client_types: set[str] = set()
            for item in items:
                children = item.get("children")
                if children:
                    client_type_item = next((c for c in children if c.get("key") == "torrent_client"), None)
                    if client_type_item:
                        client_types.add(str(client_type_item.get("value", "unknown")))
            sections[-1]["client_types"] = sorted(client_types, key=lambda x: (x != "qbit", x))

    result: dict[str, object] = {"success": True, "sections": sections}
    if config_warning:
        result["config_warning"] = config_warning
    return jsonify(result)


def _configured_torrent_client_names(
    user_config: Mapping[str, Any],
    example_config: Mapping[str, Any],
) -> list[str]:
    """Return user-created, edited, or actively referenced torrent clients."""
    user_clients = _as_dict(user_config.get("TORRENT_CLIENTS")) or {}
    example_clients = _as_dict(example_config.get("TORRENT_CLIENTS")) or {}
    example_by_name = {str(name).casefold(): _as_dict(value) or {} for name, value in example_clients.items()}

    referenced: set[str] = set()
    default_section = _as_dict(user_config.get("DEFAULT")) or {}
    for key in ("default_torrent_client", "injecting_client_list", "searching_client_list"):
        value = default_section.get(key)
        if isinstance(value, str):
            referenced.update(name.strip().casefold() for name in value.split(",") if name.strip())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            referenced.update(str(name).strip().casefold() for name in value if str(name).strip())

    configured: list[str] = []
    for name, value in user_clients.items():
        client_name = str(name)
        normalized_name = client_name.casefold()
        client_config = _as_dict(value) or {}
        example_client = example_by_name.get(normalized_name)
        if example_client is None or client_config != example_client or normalized_name in referenced:
            configured.append(client_name)

    return sorted(configured, key=str.casefold)


@app.route("/api/torrent_clients")
def torrent_clients():
    """Return torrent clients that are configured or actively referenced."""
    # Require web session for config listing (disallow bearer token access)
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401

    # Require CSRF + same-origin for config-related reads
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    base_dir = STATE_DIR
    config_path = base_dir / "data" / "config.py"

    user_config = _load_config_from_file(config_path) or {}

    example_config = _load_config_from_file(CODE_DIR / "data" / "example_config.py") or {}
    client_names = _configured_torrent_client_names(user_config, example_config)

    return jsonify({"success": True, "clients": client_names})


_TRACKER_CONFIGURATION_KEYS = frozenset(
    {
        "api_key",
        "torznab_api_key",
        "announce_url",
        "my_announce_url",
        "username",
        "password",
        "passkey",
        "cookie_file",
        "cookies",
        "ApiUser",
        "bhd_rss_key",
        "bioma_api_key",
        "image_host_api_key",
        "ptgen_api",
    }
)

_TRACKER_REQUIRED_SETUP_KEYS = (
    "ApiUser",
    "api_key",
    "torznab_api_key",
    "announce_url",
    "my_announce_url",
    "username",
    "password",
    "passkey",
)

_TRACKER_SETUP_PLACEHOLDER_PATTERN = re.compile(
    r"<[^>]+>|\b(?:your|custom|insert|replace|example)\b|\b(?:api[ _-]?user|username|password|passkey)\b",
    re.IGNORECASE,
)

_TRACKER_DEFAULT_OVERRIDE_KEYS = (
    "add_audio_spectrogram",
    "add_bluray_link",
    "add_dynamic_hdr_plot",
    "add_logo",
    "audio_spectrogram_header",
    "bluray_image_size",
    "charLimit",
    "custom_description_header",
    "custom_footer",
    "custom_header",
    "custom_signature",
    "disc_menu_header",
    "dynamic_hdr_plot_header",
    "episode_overview",
    "fileLimit",
    "inject_delay",
    "logo_size",
    "mediainfo_header",
    "multiScreens",
    "pack_thumb_size",
    "processLimit",
    "screens_per_row",
    "screenshot_header",
    "thumbnail_size",
    "tonemapped_header",
    "use_bluray_images",
    "user_description",
)


def _has_configured_tracker_value(tracker_config: Mapping[str, Any], example_tracker_config: Mapping[str, Any]) -> bool:
    """Return whether a tracker has a meaningful user-supplied setup value."""
    for key in _TRACKER_CONFIGURATION_KEYS:
        if key not in tracker_config:
            continue

        value = tracker_config[key]
        if isinstance(value, str):
            value = value.strip()
        example_value = example_tracker_config.get(key)
        if isinstance(example_value, str):
            example_value = example_value.strip()
        if value in (None, "", False) or value == example_value:
            continue
        return True

    return False


def _tracker_setup_value_is_complete(
    key: str,
    tracker_config: Mapping[str, Any],
    example_tracker_config: Mapping[str, Any],
) -> bool:
    """Return whether a required tracker setup value is usable."""
    value = tracker_config.get(key)
    if value is None or value is False:
        return False
    if not isinstance(value, str):
        return True

    normalized = value.strip()
    if not normalized:
        return False

    example_value = example_tracker_config.get(key)
    normalized_example = example_value.strip() if isinstance(example_value, str) else ""
    placeholder_example = normalized_example.replace("_", " ").replace("-", " ")
    return not (normalized_example and normalized == normalized_example and _TRACKER_SETUP_PLACEHOLDER_PATTERN.search(placeholder_example))


def _tracker_setup_is_complete(
    tracker_config: Mapping[str, Any],
    example_tracker_config: Mapping[str, Any],
    *,
    cookie_required: bool,
    cookie_configured: bool,
    optional_setup_keys: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Return whether all detected authentication requirements are complete."""
    if cookie_required and not cookie_configured:
        return False

    required_keys = [key for key in _TRACKER_REQUIRED_SETUP_KEYS if key in example_tracker_config and key not in optional_setup_keys]
    if required_keys:
        return all(_tracker_setup_value_is_complete(key, tracker_config, example_tracker_config) for key in required_keys)

    if cookie_required:
        return True
    return _has_configured_tracker_value(tracker_config, example_tracker_config)


def _configured_tracker_names(
    trackers_section: Mapping[str, Any],
    example_trackers: Mapping[str, Any],
    supported_trackers: Mapping[str, Any],
    cookie_trackers: set[str] | None = None,
    user_config: Mapping[str, Any] | None = None,
) -> set[str]:
    """Return supported tracker names whose detected setup requirements are complete."""
    supported_names = {str(name).upper() for name in supported_trackers}
    configured: set[str] = set()
    cookie_tracker_names = {str(name).upper() for name in (cookie_trackers or set())}

    tracker_configs = {str(name).upper(): value for name, value in trackers_section.items() if isinstance(value, Mapping)}
    example_configs = {str(name).upper(): value for name, value in example_trackers.items() if isinstance(value, Mapping)}

    # Older configurations stored the BroadcasTheNet API key in DEFAULT.
    default_section = user_config.get("DEFAULT", {}) if user_config else {}
    legacy_btn_api = default_section.get("btn_api") if isinstance(default_section, Mapping) else None
    if isinstance(legacy_btn_api, str):
        legacy_btn_api = legacy_btn_api.strip()

    for tracker_name in supported_names:
        tracker_config = dict(tracker_configs.get(tracker_name, {}))
        example_tracker_config = example_configs.get(tracker_name, {})
        if tracker_name == "BROADCASTHENET" and legacy_btn_api and not tracker_config.get("api_key"):
            tracker_config["api_key"] = legacy_btn_api

        tracker_class = supported_trackers.get(tracker_name)
        cookie_required = str(getattr(tracker_class, "auth_type", "") or "").strip().lower() == "cookies"
        optional_setup_keys = {str(key) for key in (getattr(tracker_class, "optional_setup_keys", ()) or ())}
        if _tracker_setup_is_complete(
            tracker_config,
            example_tracker_config,
            cookie_required=cookie_required,
            cookie_configured=tracker_name in cookie_tracker_names,
            optional_setup_keys=optional_setup_keys,
        ):
            configured.add(tracker_name)

    return configured & supported_names


def _configured_cookie_tracker_names(
    supported_trackers: Mapping[str, Any],
    user_config: dict[str, Any],
    state_dir: Path,
    cookie_file_finder: Callable[[str, str, dict[str, Any] | None], str],
) -> set[str]:
    """Return cookie-configured trackers without one bad lookup stopping the rest."""
    configured: set[str] = set()
    for tracker_name in supported_trackers:
        try:
            has_cookie_file = Path(cookie_file_finder(str(state_dir), tracker_name, user_config)).is_file()
        except AttributeError, OSError, TypeError, ValueError:
            has_cookie_file = False
        if has_cookie_file:
            configured.add(tracker_name.upper())

    return configured


def _tracker_codebase(tracker_name: str) -> str | None:
    """Expose the framework registered for a tracker."""
    from src.trackersetup import get_tracker_framework

    framework = get_tracker_framework(tracker_name) or ""
    return {"UNIT3D": "UNIT3D", "GAZELLE": "Gazelle", "NEXUSPHP": "NexusPHP", "AVISTAZ": "AvistaZ"}.get(framework)


def _tracker_destination_type(tracker_class: Any) -> str:
    """Return the WebUI destination category without changing tracker IDs."""
    return "usenet" if bool(getattr(tracker_class, "is_usenet", False)) else "torrent"


def _tracker_supported_categories(tracker_class: Any) -> list[str]:
    """Return normalized upload categories declared by a tracker class."""
    categories = getattr(tracker_class, "supported_categories", ()) or ()
    if isinstance(categories, str):
        categories = (categories,)
    if not isinstance(categories, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(category).strip().upper() for category in categories if str(category).strip()))


def _tracker_status_from_http_code(status_code: int) -> tuple[str, str]:
    """Map a tracker homepage response to a deliberately advisory state."""
    if status_code == 429:
        return "issue", "The tracker is reachable but is rate limiting requests (HTTP 429)."
    if status_code >= 500:
        return "issue", f"The tracker returned a server error (HTTP {status_code})."
    if 100 <= status_code < 500:
        return "available", "The tracker website responded."
    return "issue", "The tracker returned an unexpected response."


def _probe_tracker_url(tracker_name: str, base_url: str) -> dict[str, Any]:
    """Perform a lightweight website reachability probe without using credentials."""
    checked_at = datetime.now(UTC).isoformat()
    parsed_url = urllib.parse.urlsplit(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return {
            "name": tracker_name,
            "state": "not_checked",
            "message": "No status URL is available for this tracker.",
            "checked_at": checked_at,
        }

    httpx = None
    try:
        httpx = _dynamic_import("httpx")
        with (
            httpx.Client(
                follow_redirects=True,
                timeout=4.0,
                headers={"User-Agent": f"Upload-Assistant-WebUI/{APP_VERSION}"},
            ) as client,
            client.stream("GET", base_url) as response,
        ):
            # Streaming avoids downloading tracker homepages just to confirm
            # that the service can answer an HTTP request.
            status_code = int(response.status_code)
        state, message = _tracker_status_from_http_code(status_code)
        result = {
            "name": tracker_name,
            "state": state,
            "message": message,
            "status_code": status_code,
            "checked_at": checked_at,
        }
        if status_code == 429:
            result["reason"] = "rate_limit"
        elif status_code >= 500:
            result["reason"] = "server_error"
        return result
    except Exception as error:
        # Keep network and DNS exception details out of the browser response.
        # A later check can distinguish a transient local-network problem from
        # a tracker outage.
        is_timeout = bool(httpx is not None and isinstance(error, getattr(httpx, "TimeoutException", ())))
        return {
            "name": tracker_name,
            "state": "unavailable",
            "reason": "timeout" if is_timeout else "connection",
            "message": ("The tracker website did not respond before the status check timed out." if is_timeout else "The tracker website could not be reached."),
            "checked_at": checked_at,
        }


def _tracker_status_cache_payload(tracker_names: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Return cached status entries, marking old results as stale."""
    now = time.time()
    payload: dict[str, dict[str, Any]] = {}
    with _tracker_status_cache_lock:
        for tracker_name in tracker_names:
            cached = _tracker_status_cache.get(tracker_name)
            if cached is None:
                payload[tracker_name] = {
                    "name": tracker_name,
                    "state": "not_checked",
                    "message": "Not checked yet.",
                    "checked_at": None,
                    "stale": False,
                }
                continue
            entry = {key: value for key, value in cached.items() if key != "_checked_epoch"}
            checked_epoch = float(cached.get("_checked_epoch", 0.0))
            entry["stale"] = now - checked_epoch > _TRACKER_STATUS_CACHE_SECONDS
            payload[tracker_name] = entry
    return payload


def _supported_tracker_status_targets() -> dict[str, str]:
    """Return trusted tracker names and homepage URLs from the class catalogue."""
    from src.trackersetup import tracker_class_map

    return {str(tracker_name).upper(): str(getattr(tracker_class, "base_url", "") or "").strip() for tracker_name, tracker_class in tracker_class_map.items()}


@app.route("/api/tracker_status")
def get_tracker_status():
    """Return cached, credential-free tracker reachability checks."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    with _tracker_status_cache_lock:
        cached_names = sorted(_tracker_status_cache)

    return jsonify(
        {
            "success": True,
            "cache_seconds": _TRACKER_STATUS_CACHE_SECONDS,
            "statuses": _tracker_status_cache_payload(cached_names),
        }
    )


@app.route("/api/tracker_status", methods=["POST"])
@limiter.limit("30 per hour", key_func=_rate_limit_key_func)
def refresh_tracker_status():
    """Refresh selected tracker reachability checks."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    try:
        supported_targets = _supported_tracker_status_targets()
    except Exception as error:
        return jsonify({"success": False, "error": f"Failed to load trackers: {error}"}), 500

    data = _request_json_dict()
    requested = data.get("trackers")
    if not isinstance(requested, list):
        return jsonify({"success": False, "error": "Trackers must be provided as a list"}), 400

    tracker_names = list(dict.fromkeys(str(name).strip().upper() for name in requested[:100] if str(name).strip()))
    unknown = [name for name in tracker_names if name not in supported_targets]
    if unknown:
        return jsonify({"success": False, "error": "One or more trackers are not supported"}), 400
    if not tracker_names:
        return jsonify({"success": False, "error": "Select at least one tracker to check"}), 400

    checked_results: list[dict[str, Any]] = []
    with _tracker_status_check_lock:
        worker_count = min(_TRACKER_STATUS_MAX_WORKERS, len(tracker_names))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(_probe_tracker_url, name, supported_targets[name]): name for name in tracker_names}
            checked_results.extend(future.result() for future in concurrent.futures.as_completed(futures))

        checked_epoch = time.time()
        with _tracker_status_cache_lock:
            for result in checked_results:
                name = str(result["name"])
                _tracker_status_cache[name] = {**result, "_checked_epoch": checked_epoch}

    return jsonify(
        {
            "success": True,
            "cache_seconds": _TRACKER_STATUS_CACHE_SECONDS,
            "statuses": _tracker_status_cache_payload(tracker_names),
        }
    )


def _cached_prowlarr_credentials(base_url: str, api_key: str, supported_trackers: set[str]) -> Any:
    """Reuse a recent Prowlarr credential report for the same connection.

    Failures are not cached, and changing the URL or API key misses the cache.
    """
    from src.prowlarr import fetch_prowlarr_credentials

    # The cached report already holds this process's credentials in memory.
    key = (base_url, api_key, frozenset(supported_trackers))
    now = time.monotonic()
    with _prowlarr_report_cache_lock:
        cached = _prowlarr_report_cache.get(key)
        generation = _prowlarr_report_cache_generation
    if cached and now - cached[0] < _PROWLARR_REPORT_CACHE_SECONDS:
        return cached[1]
    report = fetch_prowlarr_credentials(base_url, api_key, supported_trackers)
    with _prowlarr_report_cache_lock:
        # A fetch that started before an invalidation must not restore old data.
        if generation == _prowlarr_report_cache_generation:
            _prowlarr_report_cache.clear()
            _prowlarr_report_cache[key] = (now, report)
    return report


def _invalidate_prowlarr_report_cache() -> None:
    global _prowlarr_report_cache_generation
    with _prowlarr_report_cache_lock:
        _prowlarr_report_cache.clear()
        _prowlarr_report_cache_generation += 1


@app.route("/api/trackers")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def get_trackers():
    """Return supported trackers, including their default/configured status."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401

    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    base_dir = STATE_DIR
    config_path = base_dir / "data" / "config.py"
    user_config = _load_config_from_file(config_path) or {}

    # Load tracker_class_map from src.trackersetup
    try:
        from src.trackersetup import tracker_class_map
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to load trackers: {e}"}), 500

    example_config = _load_config_from_file(CODE_DIR / "data" / "example_config.py") or {}
    example_trackers_raw = example_config.get("TRACKERS", {})
    example_trackers = cast(dict[str, Any], example_trackers_raw) if isinstance(example_trackers_raw, Mapping) else {}

    from src.prowlarr import ProwlarrError, apply_prowlarr_credentials, configured_prowlarr

    prowlarr_sources: set[str] = set()
    prowlarr_cookie_trackers: set[str] = set()
    try:
        if prowlarr_connection := configured_prowlarr(user_config):
            prowlarr_report = _cached_prowlarr_credentials(
                prowlarr_connection[0],
                prowlarr_connection[1],
                set(tracker_class_map),
            )
            prowlarr_sources = apply_prowlarr_credentials(user_config, prowlarr_report)
            prowlarr_cookie_trackers = {name for name, credential in prowlarr_report.credentials.items() if credential.cookie}
    except ProwlarrError:
        # The tracker catalogue remains usable with local configuration when
        # the optional Prowlarr instance is unavailable.
        prowlarr_sources = set()
        prowlarr_cookie_trackers = set()

    trackers_section_raw = user_config.get("TRACKERS", {})
    trackers_section = cast(dict[str, Any], trackers_section_raw) if isinstance(trackers_section_raw, Mapping) else {}
    default_trackers_val = trackers_section.get("default_trackers", "")
    default_trackers_list = []
    if isinstance(default_trackers_val, str):
        default_trackers_list = [t.strip().upper() for t in default_trackers_val.split(",") if t.strip()]
    elif isinstance(default_trackers_val, list):
        default_trackers_list = [str(t).strip().upper() for t in default_trackers_val if str(t).strip()]
    default_trackers_list = [Meta.canonical_tracker_name(name) for name in default_trackers_list]

    cookie_trackers: set[str] = set()
    try:
        from src.cookie_auth import CookieDirectoryScan, find_cookie_file

        cookie_scan = CookieDirectoryScan(STATE_DIR)
    except Exception:
        # Cookie discovery is an additional configuration signal. A missing or
        # unreadable cookies directory should not prevent the selector loading.
        cookie_trackers = set()
    else:
        cookie_trackers = _configured_cookie_tracker_names(
            tracker_class_map,
            user_config,
            STATE_DIR,
            lambda base_dir, tracker, config: find_cookie_file(base_dir, tracker, config, scan=cookie_scan),
        )

    prowlarr_sources -= cookie_trackers
    cookie_trackers |= prowlarr_cookie_trackers

    configured_trackers = _configured_tracker_names(
        trackers_section,
        example_trackers,
        tracker_class_map,
        cookie_trackers,
        user_config,
    )

    trackers_data = []
    from src.api_key_expiry import get_api_key_expiry

    for tracker_name, tracker_class in tracker_class_map.items():
        display_name = getattr(tracker_class, "display_name", tracker_name)
        base_url = getattr(tracker_class, "base_url", "")
        auth_type = str(getattr(tracker_class, "auth_type", "") or "").strip().lower()
        optional_setup_keys = sorted(str(key) for key in (getattr(tracker_class, "optional_setup_keys", ()) or ()))
        destination_type = _tracker_destination_type(tracker_class)
        supported_categories = _tracker_supported_categories(tracker_class)
        expiry_supported = bool(getattr(tracker_class, "api_key_expiry_supported", False))
        tracker_config = trackers_section.get(tracker_name, {})
        api_key = str(tracker_config.get("api_key") or "").strip() if isinstance(tracker_config, dict) else ""
        favicon_url = ""
        static_dir = Path(__file__).parent / "static"
        # Try acronym first (e.g. vietmediaf.png), then display name (e.g. VietMediaF -> vietmediaf.png)
        candidate_names = [tracker_name.lower()]
        if display_name.lower() != tracker_name.lower():
            candidate_names.append(display_name.lower())
        for name in candidate_names:
            for ext in ["png", "svg", "ico"]:
                local_path = static_dir / "img" / "trackers" / f"{name}.{ext}"
                if local_path.is_file():
                    favicon_url = f"/static/img/trackers/{name}.{ext}"
                    break
            if favicon_url:
                break

        trackers_data.append(
            {
                "name": tracker_name,
                "display_name": display_name,
                "codebase": _tracker_codebase(tracker_name),
                "base_url": base_url,
                "favicon": favicon_url,
                "configured": tracker_name.upper() in configured_trackers,
                "credential_source": ("prowlarr" if tracker_name.upper() in prowlarr_sources else "local" if tracker_name.upper() in configured_trackers else None),
                "api_key_expiry_supported": expiry_supported,
                "api_key_expiry": get_api_key_expiry(tracker_name, api_key, base_url, STATE_DIR) if expiry_supported else None,
                "auth_type": auth_type,
                "optional_setup_keys": optional_setup_keys,
                "cookie_configured": tracker_name.upper() in cookie_trackers,
                "destination_type": destination_type,
                "supported_categories": supported_categories,
            }
        )

    trackers_data.sort(key=lambda x: x["display_name"].lower())

    aliases = Meta.tracker_name_aliases()
    alias_error = ""
    try:
        aliases.update({alias: Meta.canonical_tracker_name(name) for alias, name in tracker_cli_aliases(user_config).items()})
    except ValueError as error:
        # A malformed manual edit must not hide the configuration/selector UI.
        alias_error = str(error)
    return jsonify({"success": True, "default_trackers": default_trackers_list, "trackers": trackers_data, "tracker_aliases": aliases, "alias_error": alias_error})


@app.route("/api/tracker_api_key_status", methods=["POST"])
@limiter.limit("120 per hour", key_func=_rate_limit_key_func)
def tracker_api_key_status():
    """Read cached expiry or explicitly check a saved/draft tracker API key."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    import httpx

    from src.api_key_expiry import get_api_key_expiry, record_api_key_expiry
    from src.prowlarr import ProwlarrError, configured_prowlarr, fetch_prowlarr_credentials
    from src.trackersetup import tracker_class_map

    data = _request_json_dict()
    tracker = str(data.get("tracker") or "").strip().upper()
    tracker_class = tracker_class_map.get(tracker)
    if not tracker_class or not getattr(tracker_class, "api_key_expiry_supported", False):
        return jsonify({"success": False, "error": "API key expiry checks are not supported for this tracker"}), 400
    config = _load_config_from_file(STATE_DIR / "data" / "config.py") or {}
    if "api_key" in data:
        api_key = data["api_key"]
    else:
        trackers_config = config.get("TRACKERS")
        tracker_config = trackers_config.get(tracker) if isinstance(trackers_config, Mapping) else None
        api_key = tracker_config.get("api_key", "") if isinstance(tracker_config, Mapping) else ""
    if not isinstance(api_key, str) or "\r" in api_key or "\n" in api_key:
        return jsonify({"success": False, "error": "Enter a valid API key"}), 400
    api_key = api_key.strip()
    source = "local"
    refresh = data.get("refresh") is True
    if not api_key and refresh:
        try:
            if connection := configured_prowlarr(config):
                report = fetch_prowlarr_credentials(*connection, {tracker})
                credential = report.credentials.get(tracker)
                api_key = credential.api_key if credential else ""
                source = "prowlarr"
        except ProwlarrError as error:
            return jsonify({"success": False, "error": str(error)}), 400
    base_url = tracker_class.base_url
    if not refresh:
        return jsonify({"success": True, "expiry": get_api_key_expiry(tracker, api_key, base_url, STATE_DIR)})
    if not api_key:
        return jsonify({"success": False, "error": "Enter an API key or configure a Prowlarr credential source"}), 400

    # Use the existing search endpoint/permissions, not /user's additional
    # account-information permission. The destination comes from UA's tracker
    # definition; draft config cannot redirect a credential to another host.
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.get(
                tracker_class.search_url,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                params={"perPage": 1},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is False:
            return jsonify({"success": False, "error": "The tracker returned an unsuccessful API response"}), 400
        observed = record_api_key_expiry(tracker, api_key, base_url, response, STATE_DIR, payload=payload)
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        message = (
            "The tracker rejected the API key. It may be invalid, revoked or expired."
            if code == 401
            else "The tracker denied the API request. Check the key's search/download permissions and account access."
            if code == 403
            else f"The tracker returned HTTP {code}. Try again later."
        )
        return jsonify({"success": False, "error": message}), 400
    except httpx.RequestError:
        return jsonify({"success": False, "error": "The tracker could not be reached. Try again later."}), 400
    except ValueError:
        return jsonify({"success": False, "error": "The tracker did not return a valid JSON API response"}), 400

    expiry = observed or get_api_key_expiry(tracker, api_key, base_url, STATE_DIR)
    return jsonify(
        {
            "success": True,
            "expiry": expiry,
            "credential_source": source,
            "message": "API key accepted." if observed else "API key accepted, but expiry was not reported. Any date shown is the last observed expiry.",
        }
    )


@app.route("/api/config_test_prowlarr", methods=["POST"])
@limiter.limit("30 per hour", key_func=_rate_limit_key_func)
def config_test_prowlarr():
    """Test draft Prowlarr settings and return credential-free metadata."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    data = _request_json_dict()
    base_url = str(data.get("url") or "").strip()
    api_key = str(data.get("api_key") or "").strip()
    if not base_url or not api_key:
        return jsonify({"success": False, "error": "Prowlarr URL and API key are required"}), 400

    from src.prowlarr import ProwlarrError, fetch_prowlarr_credentials
    from src.trackersetup import tracker_class_map

    try:
        report = fetch_prowlarr_credentials(base_url, api_key, set(tracker_class_map), include_status=True)
    except ProwlarrError as error:
        return jsonify({"success": False, "error": str(error)}), 400
    except Exception:
        return jsonify({"success": False, "error": "Unable to test the Prowlarr connection"}), 500

    # A manual test is the way to pick up indexer changes before the cache expires.
    _invalidate_prowlarr_report_cache()

    api_key_trackers = sorted(name for name, credential in report.credentials.items() if credential.api_key)
    cookie_trackers = sorted(name for name, credential in report.credentials.items() if credential.cookie)
    return jsonify(
        {
            "success": True,
            "message": f"Connected to Prowlarr {report.version or '(version unavailable)'}. Found credentials for {len(report.credentials)} supported tracker(s).",
            "version": report.version,
            "enabled_indexers": report.enabled_indexers,
            "matched_indexers": report.matched_indexers,
            "credential_trackers": sorted(report.credentials),
            "api_key_trackers": api_key_trackers,
            "cookie_trackers": cookie_trackers,
            "masked_credentials": report.masked_credentials,
            "unsupported_indexers": report.unsupported_indexers,
        }
    )


def _config_write_route(view: Callable[..., Any]) -> Callable[..., Any]:
    """Guard and serialize every WebUI config writer with startup synchronization."""

    @wraps(view)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        if not _is_authenticated():
            return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
        if not _verify_csrf_header() or not _verify_same_origin():
            return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403
        try:
            with config_write_lock(STATE_DIR / "data" / "config.py"):
                return view(*args, **kwargs)
        except ConfigWriteConflictError as error:
            return jsonify({"success": False, "error": str(error)}), 409
        except (ValueError, SyntaxError, UnicodeError) as error:
            return jsonify({"success": False, "error": f"Configuration file could not be parsed: {error}"}), 400
        except (ConfigSyncError, OSError) as error:
            console.print(f"Failed to save configuration safely: {error}", markup=False)
            return jsonify({"success": False, "error": "Unable to save configuration safely. Pending changes are kept; please try again."}), 500

    return guarded


@app.route("/api/config_set_tracker_overrides", methods=["POST"])
@_config_write_route
def config_set_tracker_overrides():
    """Enable tracker DEFAULT overrides or retain their keys with inherited values."""
    data = _request_json_dict()
    tracker_name = str(data.get("tracker", "")).strip().upper()
    enabled = data.get("enabled")
    if not tracker_name or not isinstance(enabled, bool):
        return jsonify({"success": False, "error": "Invalid tracker override request"}), 400

    example_config = _load_config_from_file(CODE_DIR / "data" / "example_config.py") or {}
    example_trackers = _as_dict(example_config.get("TRACKERS")) or {}
    actual_example_name = next(
        (str(name) for name in example_trackers if str(name).upper() == tracker_name),
        None,
    )
    if actual_example_name is None:
        return jsonify({"success": False, "error": "Unknown tracker"}), 404
    example_tracker = _as_dict(example_trackers.get(actual_example_name)) or {}
    override_keys = [key for key in _TRACKER_DEFAULT_OVERRIDE_KEYS if key in example_tracker]
    config_path = STATE_DIR / "data" / "config.py"
    original_bytes = config_path.read_bytes()
    source = original_bytes.decode("utf-8")
    user_config = _parse_config_source(source)
    user_trackers = _as_dict(user_config.get("TRACKERS")) or {}
    actual_user_name = next(
        (str(name) for name in user_trackers if str(name).upper() == tracker_name),
        actual_example_name,
    )
    if not enabled:
        saved_tracker = _as_dict(user_trackers.get(actual_user_name)) or {}
        override_keys = [key for key in _TRACKER_DEFAULT_OVERRIDE_KEYS if key in example_tracker or key in saved_tracker]
    if not override_keys:
        return jsonify({"success": False, "error": "This tracker has no DEFAULT override block"}), 400

    try:
        if not isinstance(user_config.get("TRACKERS"), Mapping):
            source = _replace_config_value_in_source(source, ["TRACKERS"], "{}")
        if not isinstance(user_trackers.get(actual_user_name), Mapping):
            source = _replace_config_value_in_source(source, ["TRACKERS", actual_user_name], "{}")
        for key in override_keys:
            source = _replace_config_value_in_source(
                source,
                ["TRACKERS", actual_user_name, key],
                _python_literal(example_tracker[key] if enabled else None),
            )
        replace_config_source(config_path, source, original_bytes)
        try:
            _write_audit_log(
                "set_tracker_default_overrides",
                ["TRACKERS", actual_user_name],
                None,
                {"enabled": enabled, "keys": override_keys},
                True,
            )
        except Exception as audit_error:
            console.print(f"Failed to write config audit record: {audit_error}", markup=False)
    except ConfigSyncError:
        raise
    except Exception as error:
        console.print(f"Failed to update tracker DEFAULT overrides: {error}", markup=False)
        return jsonify({"success": False, "error": "An error occurred while updating tracker overrides"}), 500

    return jsonify(
        {
            "success": True,
            "tracker": actual_user_name,
            "enabled": enabled,
            "keys": override_keys,
        }
    )


def _apply_config_update(source: str, example_config: dict[str, Any], data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate and stage one edit without writing any changes to disk."""
    prior_config = _parse_config_source(source)
    path_raw = data.get("path", [])
    path: list[str] = []
    if isinstance(path_raw, list) and all(isinstance(part, str) and part for part in path_raw):
        path = list(path_raw)
    raw_value = data.get("value")

    if not path:
        raise ValueError("Invalid path")

    example_value = _get_nested_value(example_config, path)

    if example_value is None and len(path) >= 3 and path[0] == "TORRENT_CLIENTS":
        user_client = _get_nested_value(prior_config, path[:2])
        example_clients = _as_dict(example_config.get("TORRENT_CLIENTS")) or {}
        user_client_config = _as_dict(user_client)
        if user_client_config:
            match = _torrent_client_template(
                example_clients,
                str(user_client_config.get("torrent_client", "")),
            )
            if match is not None:
                _template_name, template = match
                example_value = _get_nested_value(template, path[2:])

    # Special handling for WebUI-managed fields that don't exist in example config.
    key = path[-1] if path else ""
    is_optional_arr_field = len(path) == 2 and path[0] == "DEFAULT" and re.fullmatch(r"(?:sonarr|radarr)_(?:url|api_key)_[1-3]", key) is not None
    force_remove_optional_arr_field = is_optional_arr_field and data.get("remove") is True
    is_tracker_default_override = len(path) == 3 and path[0] == "TRACKERS" and key in _TRACKER_DEFAULT_OVERRIDE_KEYS
    # Retain an explicit inheritance marker so startup sync cannot restore an
    # example override. Accept removal requests from older WebUI tabs as well.
    inherit_tracker_override = is_tracker_default_override and (raw_value is None or data.get("remove") is True)
    if is_tracker_default_override and example_value is None and isinstance(_get_nested_value(example_config, path[:2]), Mapping):
        # Existing overrides may outlive a field's entry in the tracker template.
        # Keep those displayed fields editable/inheritable without adding new ones.
        saved_tracker = _as_dict(_get_nested_value(prior_config, path[:2])) or {}
        if key in saved_tracker:
            example_value = _get_nested_value(example_config, ["DEFAULT", key])
            if example_value is None:
                example_value = saved_tracker[key] if saved_tracker[key] is not None else ""
    is_release_group_override = _is_release_group_override_path(path)
    if is_release_group_override:
        if not isinstance(_get_nested_value(example_config, path[:-1]), Mapping):
            raise ValueError("Unknown release group override scope")
        example_value = {}
    elif key in ["injecting_client_list", "searching_client_list"]:
        example_value = []  # Default to empty list
    elif is_optional_arr_field:
        example_value = ""
    elif example_value is None:
        raise ValueError("Path not found in example config")

    is_webui_prompt_timeout = path == ["DEFAULT", "webui_unattended_prompt_timeout"]
    if is_webui_prompt_timeout:
        if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)) or (isinstance(raw_value, str) and not raw_value.strip()):
            raise ValueError("Enter a timeout in minutes, or 0 to wait indefinitely")
        # The runtime accepts fractional minutes even though the example is 60.
        example_value = 60.0
    coerced_value = None if inherit_tracker_override else _coerce_config_value(raw_value, example_value)
    if is_webui_prompt_timeout and (not math.isfinite(coerced_value) or coerced_value < 0):
        raise ValueError("Unattended prompt timeout must be a finite number of minutes greater than or equal to 0")
    overlay_choices = {"overlay_position": {"left", "right"}, "overlay_layout": {"stacked", "single_line"}}
    if path[:1] == ["DEFAULT"] and len(path) == 2 and key in overlay_choices and (not isinstance(coerced_value, str) or coerced_value not in overlay_choices[key]):
        raise ValueError(f"Invalid {key} value")
    if is_release_group_override:
        _validate_release_group_overrides(coerced_value)
    new_value_literal = _python_literal(coerced_value)

    # Keep optional WebUI-managed values out of config.py when they are unused.
    # Tracker overrides instead retain None to inherit subsequent DEFAULT changes.
    key = path[-1] if path else ""
    should_remove_empty_value = (key in ["injecting_client_list", "searching_client_list"] and coerced_value == []) or (
        is_optional_arr_field and (coerced_value == "" or force_remove_optional_arr_field)
    )
    prior_value = _get_nested_value(prior_config, path)
    if should_remove_empty_value:
        updated_source = _remove_config_key_in_source(source, path)
    else:
        if len(path) == 2 and path[0] == "DEFAULT":
            from src.screenshot_overlays import OVERLAY_KEYS, overlay_enabled, overlay_options

            prior_defaults = _as_dict(prior_config.get("DEFAULT")) or {}
            if key == "frame_overlay":
                # Freeze legacy label preferences before changing the master so
                # switching off and back on restores exactly the same selections.
                for overlay_key, selected in overlay_options(prior_defaults).items():
                    if overlay_key not in prior_defaults:
                        source = _replace_config_value_in_source(source, ["DEFAULT", overlay_key], _python_literal(selected))
            elif key in OVERLAY_KEYS and "frame_overlay" not in prior_defaults:
                # Editing labels must preserve the master state shown in the UI,
                # including configs that have never saved an explicit master.
                source = _replace_config_value_in_source(source, ["DEFAULT", "frame_overlay"], _python_literal(overlay_enabled(prior_defaults)))
        updated_source = _replace_config_value_in_source(source, path, new_value_literal)
    return updated_source, {
        "path": path,
        "value": coerced_value,
        "prior_value": prior_value,
        "action": "remove_key" if should_remove_empty_value else "update_value",
    }


def _audit_config_updates(records: list[dict[str, Any]], success: bool, error: str | None = None) -> None:
    for record in records:
        try:
            _write_audit_log(
                record["action"],
                record["path"],
                record["prior_value"],
                None if record["action"] == "remove_key" else record["value"],
                success,
                error,
            )
        except Exception as audit_error:
            console.print(f"Failed to write config audit record: {audit_error}", markup=False)


@app.route("/api/config_update", methods=["POST"])
@_config_write_route
def config_update():
    """Save one field or a batch of field edits after staging every change."""
    data = _request_json_dict()
    is_batch = "updates" in data
    updates = data.get("updates") if is_batch else [data]
    if not isinstance(updates, list) or not 1 <= len(updates) <= 1000 or not all(isinstance(update, dict) for update in updates):
        return jsonify({"success": False, "error": "Provide between 1 and 1000 configuration updates"}), 400

    config_path = STATE_DIR / "data" / "config.py"
    original_bytes = config_path.read_bytes()
    source = original_bytes.decode("utf-8")
    example_config = _load_config_from_file(CODE_DIR / "data" / "example_config.py") or {}
    records: list[dict[str, Any]] = []
    try:
        for update in cast(list[dict[str, Any]], updates):
            source, record = _apply_config_update(source, example_config, update)
            records.append(record)
        if any(record["path"][0] == "TRACKERS" and (len(record["path"]) <= 2 or record["path"][2] == "cli_alias") for record in records):
            # Validate the final batch, so two aliases can be swapped together.
            tracker_cli_aliases(_parse_config_source(source))
        # A bad edit anywhere in the batch leaves the original file untouched.
        replace_config_source(config_path, source, original_bytes)
    except (ValueError, TypeError) as error:
        return jsonify({"success": False, "error": str(error)}), 400
    except ConfigSyncError as error:
        _audit_config_updates(records, False, str(error))
        raise
    except Exception as error:
        _audit_config_updates(records, False, str(error))
        return jsonify({"success": False, "error": "An error occurred while updating the configuration"}), 500

    _audit_config_updates(records, True)
    if is_batch:
        return jsonify({"success": True, "values": [_json_safe(record["value"]) for record in records]})
    return jsonify({"success": True, "value": _json_safe(records[0]["value"])})


@app.route("/api/config_remove_subsection", methods=["POST"])
@_config_write_route
def config_remove_subsection():
    """Remove a subsection (top-level key) from the user's config.py if present"""
    # Require authenticated web session and CSRF protection; disallow bearer/basic API auth
    data = _request_json_dict()
    path_raw = data.get("path", [])
    path: list[str] = []
    if isinstance(path_raw, Sequence) and not isinstance(path_raw, (str, bytes, bytearray)):
        path_items: Sequence[Any] = cast(Sequence[Any], path_raw)
        path.extend(p for p in path_items if isinstance(p, str) and p)

    if not path:
        return jsonify({"success": False, "error": "Invalid path"}), 400

    base_dir = STATE_DIR
    config_path = base_dir / "data" / "config.py"
    original_bytes = config_path.read_bytes()
    source = original_bytes.decode("utf-8")

    try:
        updated = _remove_config_key_in_source(source, path)
        if updated == source:
            # Nothing changed
            return jsonify({"success": True, "value": None})
        replace_config_source(config_path, updated, original_bytes)
        return jsonify({"success": True})
    except ConfigSyncError:
        raise
    except Exception:
        return jsonify({"success": False, "error": "An error occurred while removing the configuration subsection"}), 500


@app.route("/api/config_add_torrent_client", methods=["POST"])
@_config_write_route
def config_add_torrent_client():
    """Create a custom-named torrent client from an example template."""
    data = _request_json_dict()
    client_name = str(data.get("name", "")).strip()
    template_name = str(data.get("template", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", client_name):
        return jsonify(
            {
                "success": False,
                "error": "Client names may contain letters, numbers, hyphens, and underscores.",
            }
        ), 400

    example_config = _load_config_from_file(CODE_DIR / "data" / "example_config.py") or {}
    example_clients = _as_dict(example_config.get("TORRENT_CLIENTS")) or {}
    template = _as_dict(example_clients.get(template_name))
    if not template:
        return jsonify({"success": False, "error": "Unknown torrent client template"}), 400

    config_path = STATE_DIR / "data" / "config.py"
    original_bytes = config_path.read_bytes()
    source = original_bytes.decode("utf-8")
    user_config = _parse_config_source(source)
    user_clients = _as_dict(user_config.get("TORRENT_CLIENTS")) or {}
    if client_name.casefold() in {str(name).casefold() for name in user_clients}:
        return jsonify({"success": False, "error": "A client with that name already exists"}), 409

    try:
        if not isinstance(user_config.get("TORRENT_CLIENTS"), Mapping):
            source = _replace_config_value_in_source(source, ["TORRENT_CLIENTS"], "{}")
        updated = _replace_config_value_in_source(
            source,
            ["TORRENT_CLIENTS", client_name],
            _python_literal(dict(template)),
        )
        replace_config_source(config_path, updated, original_bytes)
        try:
            _write_audit_log(
                "add_subsection",
                ["TORRENT_CLIENTS", client_name],
                None,
                {"template": template_name},
                True,
            )
        except Exception as audit_error:
            console.print(f"Failed to write config audit record: {audit_error}", markup=False)
    except ConfigSyncError:
        raise
    except Exception as error:
        console.print(f"Failed to add torrent client: {error}", markup=False)
        return jsonify({"success": False, "error": "An error occurred while adding the torrent client"}), 500

    return jsonify(
        {
            "success": True,
            "name": client_name,
            "torrent_client": template.get("torrent_client", ""),
        }
    )


@app.route("/api/config_rename_torrent_client", methods=["POST"])
@_config_write_route
def config_rename_torrent_client():
    """Rename a torrent-client block and its DEFAULT client references."""
    data = _request_json_dict()
    old_name = str(data.get("old_name", "")).strip()
    new_name = str(data.get("new_name", "")).strip()
    if not old_name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", new_name):
        return jsonify(
            {
                "success": False,
                "error": "Client names may contain letters, numbers, hyphens, and underscores.",
            }
        ), 400
    if old_name.casefold() == new_name.casefold():
        return jsonify({"success": False, "error": "Choose a different client name"}), 400

    config_path = STATE_DIR / "data" / "config.py"
    original_bytes = config_path.read_bytes()
    source = original_bytes.decode("utf-8")
    user_config = _parse_config_source(source)
    user_clients = _as_dict(user_config.get("TORRENT_CLIENTS")) or {}
    actual_old_name = next(
        (str(name) for name in user_clients if str(name).casefold() == old_name.casefold()),
        None,
    )
    existing_new_name = next(
        (str(name) for name in user_clients if str(name).casefold() == new_name.casefold()),
        None,
    )

    # Treat a completed earlier attempt as success so Save Config can be retried.
    if actual_old_name is None and existing_new_name is not None:
        return jsonify({"success": True, "old_name": old_name, "new_name": existing_new_name})
    if actual_old_name is None:
        return jsonify({"success": False, "error": "Torrent client not found"}), 404
    if existing_new_name is not None:
        return jsonify({"success": False, "error": "A client with that name already exists"}), 409

    client_config = _as_dict(user_clients.get(actual_old_name))
    if client_config is None:
        return jsonify({"success": False, "error": "Torrent client configuration is invalid"}), 400

    try:
        updated = _replace_config_value_in_source(
            source,
            ["TORRENT_CLIENTS", new_name],
            _python_literal(dict(client_config)),
        )
        updated = _remove_config_key_in_source(updated, ["TORRENT_CLIENTS", actual_old_name])

        default_config = _as_dict(user_config.get("DEFAULT")) or {}
        reference_keys = (
            "default_torrent_client",
            "injecting_client_list",
            "searching_client_list",
        )
        updated_references: list[str] = []
        for key in reference_keys:
            current_value = default_config.get(key)
            next_value: object = current_value
            if isinstance(current_value, str) and current_value.casefold() == actual_old_name.casefold():
                next_value = new_name
            elif isinstance(current_value, list):
                next_value = [new_name if isinstance(value, str) and value.casefold() == actual_old_name.casefold() else value for value in current_value]
            if next_value != current_value:
                updated = _replace_config_value_in_source(updated, ["DEFAULT", key], _python_literal(next_value))
                updated_references.append(key)

        replace_config_source(config_path, updated, original_bytes)
        try:
            _write_audit_log(
                "rename_subsection",
                ["TORRENT_CLIENTS", actual_old_name],
                {"name": actual_old_name},
                {"name": new_name},
                True,
            )
        except Exception as audit_error:
            console.print(f"Failed to write config audit record: {audit_error}", markup=False)
    except ConfigSyncError:
        raise
    except Exception as error:
        console.print(f"Failed to rename torrent client: {error}", markup=False)
        return jsonify({"success": False, "error": "An error occurred while renaming the torrent client"}), 500

    return jsonify(
        {
            "success": True,
            "old_name": actual_old_name,
            "new_name": new_name,
            "updated_references": updated_references,
        }
    )


@app.route("/api/config_test_torrent_client", methods=["POST"])
@limiter.limit("30 per hour", key_func=_rate_limit_key_func)
def config_test_torrent_client():
    """Test a torrent-client draft without saving or mutating it."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    data = _request_json_dict()
    client_name = str(data.get("name", "")).strip()
    client_config = _as_dict(data.get("config"))
    if not client_name or client_config is None:
        return jsonify({"success": False, "error": "Invalid torrent client configuration"}), 400

    client_type = str(client_config.get("torrent_client", "")).strip().lower()
    if client_type not in {"qbit", "rtorrent", "deluge", "transmission", "watch"}:
        return jsonify({"success": False, "error": "Unsupported torrent client type"}), 400

    try:
        message = "Connection successful"
        if client_type == "qbit":
            proxy_url = str(client_config.get("qui_proxy_url", "")).strip()
            verify_certificate = bool(client_config.get("VERIFY_WEBUI_CERTIFICATE", True))
            version: str | None = None
            proxy_failure = ""
            if proxy_url:
                httpx = _dynamic_import("httpx")
                try:
                    response = httpx.get(
                        f"{proxy_url.rstrip('/')}/api/v2/app/version",
                        timeout=10.0,
                        verify=verify_certificate,
                    )
                    response.raise_for_status()
                    version = str(response.text).strip().strip('"')
                except httpx.HTTPError as error:
                    from src.torrent_clients.qbittorrent import has_direct_qbit_auth

                    status_code = getattr(getattr(error, "response", None), "status_code", None)
                    # The uploader treats HTTP 404 as an answer from qBittorrent and does not fall back.
                    if status_code == 404 or not has_direct_qbit_auth(client_config):
                        raise
                    # Report the same fallback the uploader uses, naming the error type only,
                    # since httpx messages can contain the proxy key.
                    proxy_failure = f"HTTP {status_code}" if status_code else type(error).__name__
            if version is None:
                qbittorrentapi = _dynamic_import("qbittorrentapi")
                qbit_kwargs: dict[str, object] = {
                    "host": str(client_config.get("qbit_url", "")),
                    "port": str(client_config.get("qbit_port", "")),
                    "VERIFY_WEBUI_CERTIFICATE": verify_certificate,
                    "FORCE_SCHEME_FROM_HOST": True,
                    "REQUESTS_ARGS": {"timeout": 10},
                }
                api_key = str(client_config.get("qbit_api_key", "")).strip()
                if api_key:
                    qbit_kwargs["api_key"] = api_key
                else:
                    qbit_kwargs["username"] = str(client_config.get("qbit_user", ""))
                    qbit_kwargs["password"] = str(client_config.get("qbit_pass", ""))
                qbit_client = qbittorrentapi.Client(**qbit_kwargs)
                if not api_key:
                    qbit_client.auth_log_in()
                version_value = qbit_client.app_version
                if callable(version_value):
                    version_value = version_value()
                version = str(version_value)
            message = f"Connected to qBittorrent {version}" if version else "Connected to qBittorrent"
            if proxy_failure:
                message = f"qui proxy failed ({proxy_failure}); {message} through the WebUI credentials fallback"
        elif client_type == "rtorrent":
            httpx = _dynamic_import("httpx")
            xmlrpc_client = _dynamic_import("xmlrpc.client")
            rtorrent_url = str(client_config.get("rtorrent_url", "")).strip()
            if not rtorrent_url:
                raise ValueError("Missing rTorrent URL")
            payload = xmlrpc_client.dumps((), methodname="system.client_version")
            response = httpx.post(
                rtorrent_url,
                content=payload,
                headers={"Content-Type": "text/xml"},
                timeout=10.0,
            )
            response.raise_for_status()
            values, _method = xmlrpc_client.loads(response.content)
            version = str(values[0]) if values else ""
            message = f"Connected to rTorrent {version}" if version else "Connected to rTorrent"
        elif client_type == "deluge":
            deluge_client_module = _dynamic_import("deluge_client")
            deluge_client = deluge_client_module.DelugeRPCClient(
                str(client_config.get("deluge_url", "")),
                int(client_config.get("deluge_port", 0)),
                str(client_config.get("deluge_user", "")),
                str(client_config.get("deluge_pass", "")),
            )
            deluge_client.connect()
            if not deluge_client.connected:
                raise ConnectionError("Deluge did not accept the connection")
            if hasattr(deluge_client, "disconnect"):
                deluge_client.disconnect()
            message = "Connected to Deluge"
        elif client_type == "transmission":
            transmission_rpc = _dynamic_import("transmission_rpc")
            transmission_client = transmission_rpc.Client(
                protocol=str(client_config.get("transmission_protocol", "http")),
                host=str(client_config.get("transmission_host", "")),
                port=int(client_config.get("transmission_port", 0)),
                username=str(client_config.get("transmission_username", "")),
                password=str(client_config.get("transmission_password", "")),
                path=str(client_config.get("transmission_path", "/transmission/rpc")),
                timeout=10,
            )
            transmission_client.get_session()
            message = "Connected to Transmission"
        else:
            watch_folder = Path(str(client_config.get("watch_folder", ""))).expanduser()
            if not watch_folder.is_dir():
                return jsonify({"success": False, "error": "Watch folder does not exist or is not a directory"}), 400
            if not os.access(watch_folder, os.W_OK):
                return jsonify({"success": False, "error": "Watch folder is not writable"}), 400
            message = "Watch folder is available and writable"
    except Exception as error:
        error_type = type(error).__name__
        console.print(
            f"Torrent client connection test failed for {client_name} ({client_type}): {error_type}",
            markup=False,
        )
        normalized_error = error_type.casefold()
        if "login" in normalized_error or "auth" in normalized_error:
            error_message = "Authentication failed. Check the configured credentials."
        elif "timeout" in normalized_error:
            error_message = "Connection timed out. Check the address, port, and network access."
        elif "connect" in normalized_error or "connection" in normalized_error:
            error_message = "Unable to reach the client. Check the address, port, and network access."
        elif error_type == "HTTPStatusError":
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            if status_code in {401, 403}:
                error_message = "Authentication failed. Check the configured credentials."
            elif status_code == 404:
                error_message = "The client endpoint was not found. Check the configured URL or path."
            else:
                error_message = f"The client rejected the connection (HTTP {status_code or 'error'})."
        else:
            error_message = f"Connection failed ({error_type})."
        return jsonify({"success": False, "error": error_message}), 400

    return jsonify({"success": True, "message": message})


@app.route("/api/tokens", methods=["GET", "POST", "DELETE"])
def api_tokens():
    """Manage API bearer tokens (create/list/revoke).

    Protected: requires a logged-in session or basic auth. Tokens themselves
    can be used as Bearer auth for API calls.
    """
    # Require a browser session (remember-me or login) and a valid CSRF token.
    # Disallow managing tokens via Basic or Bearer API auth to ensure token
    # lifecycle actions are only possible from the web UI with CSRF protection.
    # Use the encrypted-session helpers so we read values stored inside the
    # encrypted `enc` payload rather than top-level Flask session keys.
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header():
        return jsonify({"success": False, "error": "CSRF validation failed"}), 403

    if request.method == "GET":
        store = _list_api_tokens()
        # Return metadata only (do not leak token values)
        tokens: list[dict[str, Any]] = []
        for info in store.values():
            info_dict = _as_dict(info) or {}
            tokens.append(
                {
                    "id": info_dict.get("token_id"),
                    "user": info_dict.get("user"),
                    "label": info_dict.get("label"),
                    "created": info_dict.get("created"),
                    "expiry": info_dict.get("expiry"),
                }
            )
        read_only = False
        return jsonify({"success": True, "tokens": tokens, "read_only": read_only})

    if request.method == "POST":
        data = _request_json_dict()
        action = data.get("action")
        label = str(data.get("label", ""))
        # No expiry: tokens are non-expiring by default;
        persisted = _load_user_record()
        username = str(
            _session_get("username")
            or (request.authorization.username if request.authorization is not None else None)
            or (persisted.get("username") if persisted else None)
            or ""
        )
        if not username:
            return jsonify({"success": False, "error": "Unable to determine username for token"}), 400

        # Two-step flow supported:
        # - action == 'generate' (or persist=false): generate a token and do NOT persist it.
        # - action == 'store': persist an externally-provided token string (token field required).
        if action == "store":
            token_value = str(data.get("token") or "")
            if not token_value:
                return jsonify({"success": False, "error": "Token value required for store action"}), 400
            # Stored tokens grant upload access, so refuse short guessable values.
            if len(token_value) < 32:
                return jsonify({"success": False, "error": "Token must be at least 32 characters"}), 400
            # Persist tokens to the config store
            ok = _persist_existing_api_token(token_value, username, label=str(label))
            if ok:
                return jsonify({"success": True, "persisted": True})
            return jsonify({"success": False, "error": "Failed to persist token (already exists?)"}), 400

        # default/generate
        persist_flag = bool(data.get("persist", True))
        token = _create_api_token(username, label=label, persist=persist_flag)
        persisted = persist_flag
        return jsonify({"success": True, "token": token, "persisted": persisted})

    if request.method == "DELETE":
        data = _request_json_dict()
        tid = str(data.get("id") or "")
        if not tid:
            return jsonify({"success": False, "error": "Token id required"}), 400
        ok = _revoke_api_token(tid)
        if ok:
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Failed to revoke token"}), 500
    return None


@app.route("/api/browse")
@limiter.limit("600 per minute", key_func=_rate_limit_key_func, override_defaults=True)
def browse_path():
    """Browse filesystem paths"""
    requested: str = str(request.args.get("path", ""))
    file_filter: str = str(request.args.get("filter", "video"))  # 'video' or 'desc'
    try:
        path = _resolve_browse_path(requested)
    except ValueError as e:
        # Log details server-side, but avoid leaking paths/internal details to clients.
        console.print(f"Path resolution error for requested {requested!r}: {e}", markup=False)
        return jsonify({"error": "Invalid path specified", "success": False}), 400

    # Explicitly assert the resolved path is within allowed browse roots.
    try:
        _assert_safe_resolved_path(path)
    except ValueError:
        console.print(f"Path failed safety check: {requested!r}", markup=False)
        return jsonify({"error": "Invalid path specified", "success": False}), 400

    # Defensive sanity checks before using `path` in filesystem operations.
    safe_path = str(Path(path).resolve())
    if "\x00" in safe_path:
        console.print("Path contains invalid characters", markup=False)
        return jsonify({"error": "Invalid path specified", "success": False}), 400
    # codeql[py/path-injection]
    if not Path(safe_path).is_dir():
        console.print("Requested path is not a directory", markup=False)
        return jsonify({"error": "Invalid path specified", "success": False}), 400

    console.print("Browsing path allowed", markup=False)

    try:
        items: list[BrowseItem] = []
        try:
            # `safe_path` was computed above; perform an explicit realpath
            # containment check using stdlib functions so static analyzers
            # can reason about the safety of the listing operation.
            real_safe = os.path.realpath(safe_path)
            allowed = False
            for root in _get_browse_roots():
                root_real = os.path.realpath(str(Path(root).resolve()))
                try:
                    if os.path.commonpath([real_safe, root_real]) == root_real:
                        allowed = True
                        break
                except ValueError:
                    # Different drives on Windows - not allowed
                    continue
            if not allowed:
                console.print(f"Path failed containment check before listing: {safe_path!r}", markup=False)
                return jsonify({"error": "Invalid path specified", "success": False}), 400

            # codeql[py/path-injection]
            with os.scandir(safe_path) as entries:
                directory_entries = sorted(entries, key=lambda entry: entry.name)
            for entry in directory_entries:
                item = entry.name
                full_path = Path(safe_path) / item
                if _is_hidden_browse_entry(entry):
                    continue
                try:
                    # Ordinary entries returned by scandir are direct children of
                    # the validated parent. Resolve links and all Windows reparse
                    # points (including junctions) before exposing their targets.
                    # Use one no-follow snapshot for both classification and
                    # displayed metadata. DirEntry's cached type can predate a
                    # replacement, so it must not trigger a later following stat.
                    stat_res = entry.stat(follow_symlinks=False)
                    attributes = getattr(stat_res, "st_file_attributes", 0)
                    is_link = stat.S_ISLNK(stat_res.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    is_dir = stat.S_ISDIR(stat_res.st_mode)
                    if is_link:
                        # Directory symlinks remain expandable, but size/mtime
                        # below always describe the link itself, never its target.
                        is_dir = bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY) if sys.platform == "win32" else entry.is_dir()
                        try:
                            _assert_safe_resolved_path(full_path)
                        except ValueError:
                            continue

                    # Keep description browsing limited to known text-ish files.
                    # Default browsing should expose any file inside allowed roots
                    # so books, games, ISOs, and other upload types are visible.
                    if not is_dir and file_filter == "desc":
                        ext = Path(item.lower()).suffix
                        if ext not in SUPPORTED_DESC_EXTS:
                            continue

                    mtime = stat_res.st_mtime
                    size = stat_res.st_size if not is_dir else 0

                    items.append(
                        {
                            "name": item,
                            "path": str(full_path),
                            "type": "folder" if is_dir else "file",
                            "children": [] if is_dir else None,
                            "mtime": mtime,
                            "size": size,
                        }
                    )
                except PermissionError, OSError:
                    continue

            console.print(f"Found {len(items)} items in {path}", markup=False)

        except PermissionError:
            console.print(f"Error: Permission denied: {path}", markup=False)
            return jsonify({"error": "Permission denied", "success": False}), 403

        # If caller used a bearer token, require it to be valid. Valid bearer
        # tokens are allowed without CSRF since they are intended for programmatic
        # access. Otherwise require an authenticated web session + CSRF + same-origin.
        bearer = _get_bearer_from_header()
        if bearer:
            if not _token_is_valid(bearer):
                return jsonify({"success": False, "error": "Forbidden (invalid token)"}), 403
        else:
            # Require session-based callers to be authenticated and provide CSRF + Origin
            if not _is_authenticated():
                return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
            if not _verify_csrf_header() or not _verify_same_origin():
                return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

        return jsonify({"items": items, "success": True, "path": path, "count": len(items)})

    except Exception as e:
        console.print(f"Error browsing {path}: {e}", markup=False)
        console.print(traceback.format_exc(), markup=False)
        return jsonify({"error": "Error browsing path", "success": False}), 500


@app.route("/api/browse_search")
@limiter.limit("60 per minute", key_func=_rate_limit_key_func, override_defaults=True)
def browse_search():
    """Search filesystem for files/folders matching a query string"""
    query = (request.args.get("q") or "").strip()
    file_filter: str = request.args.get("filter", "video")
    try:
        max_results = min(int(request.args.get("max_results", "100")), 500)
        if max_results < 1:
            max_results = 100
    except ValueError, TypeError:
        max_results = 100

    if not query:
        return jsonify({"success": True, "items": [], "query": ""})

    bearer = _get_bearer_from_header()
    if bearer:
        if not _token_is_valid(bearer):
            return jsonify({"success": False, "error": "Forbidden (invalid token)"}), 403
    else:
        if not _is_authenticated():
            return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
        if not _verify_csrf_header() or not _verify_same_origin():
            return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    roots = _get_browse_roots()
    if not roots:
        return jsonify({"success": False, "error": "Browsing is not configured"}), 400

    # A supplied scope must never silently fall back to all configured roots.
    if "path" in request.args:
        if len(request.args.getlist("path")) != 1 or not request.args["path"].strip():
            return jsonify({"success": False, "error": "Select a folder to search"}), 400
        try:
            # The tree only hands out absolute paths; never join a relative
            # scope onto a default root.
            if not Path(request.args["path"]).is_absolute():
                raise ValueError("Search scope must be absolute")
            # Keep the selected path inside the configured roots before
            # resolving links. Its target may live in a different allowed root.
            lexical_scope = Path(os.path.abspath(request.args["path"]))  # noqa: PTH100
            if not any(lexical_scope.is_relative_to(root) for root in roots):
                raise ValueError("Scope is outside browse roots")
            # Validate the target with the shared resolver, without changing
            # the stricter lexical-root policy used by other browse/upload APIs.
            scope = Path(_resolve_browse_path(str(lexical_scope.resolve())))
            roots = [str(scope)]
            # Surface an unavailable/unreadable explicit selection instead of
            # returning a misleading empty successful search.
            with os.scandir(scope):
                pass
        except ValueError, OSError:
            return jsonify({"success": False, "error": "Search folder is unavailable or not allowed"}), 400

    # Split on common separators
    query_tokens = [t for t in _BROWSE_SEARCH_SEP_RE.split(query.lower()) if t]
    if not query_tokens:
        return jsonify({"success": True, "items": [], "query": query})

    def name_matches(name: str) -> bool:
        """Check if query tokens appear as whole-word ordered subsequence in the name."""
        name_tokens = [t for t in _BROWSE_SEARCH_SEP_RE.split(name.lower()) if t]
        pos = 0
        for qt in query_tokens:
            found = False
            while pos < len(name_tokens):
                if name_tokens[pos] == qt:
                    pos += 1
                    found = True
                    break
                pos += 1
            if not found:
                return False
        return True

    items: list[BrowseItem] = []
    files: list[BrowseItem] = []
    seen: set[str] = set()
    # Directories walked after the result list filled up; folders found
    # there may still displace files, but only within a bounded budget.
    dirs_after_full = 0
    budget_spent = False

    try:
        for root in roots:
            root_abs = str(Path(root).resolve())
            if not Path(root_abs).is_dir():
                continue
            try:
                for dirpath, dirnames, filenames in os.walk(root_abs, followlinks=False):
                    if len(items) + len(files) >= max_results:
                        dirs_after_full += 1

                    # Prune system/hidden directories before os.walk enters them.
                    visible = [d for d in dirnames if not _is_hidden_browse_entry(Path(dirpath) / d)]
                    # os.walk never descends symlinks; prune Windows junctions
                    # too so every walked folder lives under the root. Linked
                    # folders still match below, as the tree lists them.
                    dirnames[:] = [d for d in visible if not (Path(dirpath) / d).is_junction()]

                    # Check dirs
                    for dirname in visible:
                        if name_matches(dirname):
                            full_path = Path(dirpath) / dirname
                            try:
                                _assert_safe_resolved_path(full_path)
                            except ValueError:
                                continue
                            try:
                                stat_res = Path(full_path).stat()
                                mtime = stat_res.st_mtime
                                size = 0
                            except Exception:
                                mtime = 0.0
                                size = 0
                            if str(full_path) in seen:
                                continue
                            seen.add(str(full_path))
                            items.append(
                                {
                                    "name": dirname,
                                    "path": str(full_path),
                                    "type": "folder",
                                    "children": [],
                                    "mtime": mtime,
                                    "size": size,
                                }
                            )
                            if len(items) >= max_results:
                                break

                    if len(items) >= max_results:
                        break

                    # Keep only bounded provisional files; later folders take
                    # their places before the response is truncated.
                    for filename in filenames:
                        if len(files) >= max_results:
                            break
                        if not name_matches(filename):
                            continue
                        if _is_hidden_browse_entry(Path(dirpath) / filename):
                            continue
                        if file_filter == "desc":
                            ext = Path(filename.lower()).suffix
                            if ext not in SUPPORTED_DESC_EXTS:
                                continue
                        full_path = Path(dirpath) / filename
                        try:
                            _assert_safe_resolved_path(full_path)
                        except ValueError:
                            continue
                        try:
                            stat_res = Path(full_path).stat()
                            mtime = stat_res.st_mtime
                            size = stat_res.st_size
                        except Exception:
                            mtime = 0.0
                            size = 0
                        if str(full_path) in seen:
                            continue
                        seen.add(str(full_path))
                        files.append(
                            {
                                "name": filename,
                                "path": str(full_path),
                                "type": "file",
                                "children": None,
                                "mtime": mtime,
                                "size": size,
                            }
                        )
                    # os.walk scans before yielding; stop here rather than
                    # asking it to enumerate the first over-budget directory.
                    if len(items) + len(files) >= max_results and dirs_after_full >= _BROWSE_SEARCH_FOLDER_SCAN_DIRS:
                        budget_spent = True
                        break
            except PermissionError:
                continue
            except Exception as e:
                console.print(f"Error searching in {root}: {e}", markup=False)
                continue

            if len(items) >= max_results or budget_spent:
                break

        items.extend(files[: max_results - len(items)])
        # Sort the selected folders/files alphabetically within each group.
        items.sort(key=lambda x: (0 if x.get("type") == "folder" else 1, (x.get("name") or "").lower()))

        return jsonify({"success": True, "items": items, "query": query, "count": len(items), "truncated": len(items) >= max_results})

    except Exception as e:
        console.print(f"Error in browse_search: {e}", markup=False)
        console.print(traceback.format_exc(), markup=False)
        return jsonify({"error": "Error searching files", "success": False}), 500


@app.route("/api/execution_preview")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def execution_preview():
    """Return the current media preview for an active execution session."""

    def no_store(response: object) -> object:
        response.headers["Cache-Control"] = "no-store, max-age=0"  # type: ignore[attr-defined]
        return response

    session_id = str(request.args.get("session_id", "")).strip()
    if not session_id:
        return no_store(jsonify({"success": False, "error": "Missing session_id"})), 400

    preview = _find_execution_preview(session_id)
    if preview is None:
        return no_store(jsonify({"success": False, "error": "Session not found"})), 404

    return no_store(jsonify({"success": True, "media": preview}))


@app.route("/api/argument_presets", methods=["GET", "POST", "DELETE"])
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def argument_presets():
    """Read and persist shared Web UI argument presets in ``data``."""
    if not _is_authenticated():
        return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    with _argument_presets_lock:
        presets = _load_argument_presets()
        if request.method == "GET":
            return jsonify({"success": True, "presets": presets})

        data = _request_json_dict()
        name_value = data.get("name")
        name = name_value.strip() if isinstance(name_value, str) else ""
        if not name:
            return jsonify({"success": False, "error": "Preset name is required"}), 400

        if request.method == "DELETE":
            next_presets = [preset for preset in presets if preset["name"].casefold() != name.casefold()]
        else:
            arguments_value = data.get("arguments")
            arguments = arguments_value.strip() if isinstance(arguments_value, str) else ""
            if not arguments:
                return jsonify({"success": False, "error": "Preset arguments are required"}), 400
            replacement = {"name": name, "arguments": arguments}
            existing_index = next(
                (index for index, preset in enumerate(presets) if preset["name"].casefold() == name.casefold()),
                None,
            )
            if existing_index is None:
                next_presets = [*presets, replacement][-MAX_ARGUMENT_PRESETS:]
            else:
                next_presets = presets.copy()
                next_presets[existing_index] = replacement

        try:
            _save_argument_presets(next_presets)
        except OSError:
            return jsonify({"success": False, "error": "Failed to persist argument presets"}), 500
        return jsonify({"success": True, "presets": next_presets})


@app.route("/api/execution_preview_cover")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def execution_preview_cover():
    """Serve a local preview cover image for the active execution session."""
    session_id = str(request.args.get("session_id", "")).strip()
    if not session_id:
        return jsonify({"success": False, "error": "Missing session_id"}), 400

    cover_file = _find_execution_preview_cover_file(session_id)
    if cover_file is None:
        return jsonify({"success": False, "error": "Cover not found"}), 404

    mimetype = mimetypes.guess_type(str(cover_file))[0] or "application/octet-stream"
    return send_file(cover_file, mimetype=mimetype, conditional=True, max_age=30)


@app.route("/api/execution_item_cover")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def execution_item_cover():
    """Serve the local cover of one queue item shown in the Results panel."""
    session_id = str(request.args.get("session_id", "")).strip()
    item = str(request.args.get("item", "")).strip()
    if not session_id or not item.isdecimal():
        return jsonify({"success": False, "error": "Missing session_id or item"}), 400

    with active_processes_lock:
        covers = active_processes.get(session_id, {}).get("item_covers")
        cover_file = covers.get(int(item)) if isinstance(covers, dict) else None
    if not isinstance(cover_file, Path) or not cover_file.is_file():
        return jsonify({"success": False, "error": "Cover not found"}), 404

    mimetype = mimetypes.guess_type(str(cover_file))[0] or "application/octet-stream"
    return send_file(cover_file, mimetype=mimetype, conditional=True, max_age=30)


@app.route("/api/execution_preview_cover/regenerate", methods=["POST"])
@limiter.limit("600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def regenerate_execution_preview_cover():
    """Regenerate the local XXX preview cover from another video frame."""
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    payload = _request_json_dict()
    session_id = _stringify_preview_value(payload.get("session_id"))
    execution_path, meta_file, meta_data = _resolve_execution_preview_meta(session_id)
    if meta_file is None or meta_data is None:
        return jsonify({"success": False, "error": "Execution metadata is not available yet"}), 404
    media_id = _stringify_preview_value(payload.get("media_id"))
    current_media_id = _stringify_preview_value(meta_data.get("uuid")) or execution_path
    if not media_id or media_id != current_media_id:
        return jsonify({"success": False, "error": "The execution preview has moved to another item"}), 409
    if _stringify_preview_value(meta_data.get("category")).upper() != "XXX":
        return jsonify({"success": False, "error": "Cover regeneration is only available for XXX"}), 400

    try:
        from src.takescreens import xxx_fallback_cover

        meta = Meta(dict(meta_data))
        folder_id = meta.uuid or Path(execution_path).name
        cover = asyncio.run(xxx_fallback_cover(list(meta.filelist or []), folder_id, meta.base_dir, meta, random_frame=True))
        if not cover:
            return jsonify({"success": False, "error": "Could not generate a new cover from the source video"}), 422
        cache_key = _execution_preview_cover_cache_key(session_id, folder_id)
        return jsonify(
            {
                "success": True,
                "poster_url": _execution_preview_cover_url(session_id, cache_key),
            }
        )
    except (FileNotFoundError, ValueError) as error:
        return jsonify({"success": False, "error": str(error)}), 404
    except Exception as error:
        console.print(f"XXX cover regeneration failed for {session_id}: {error}", markup=False)
        return jsonify({"success": False, "error": "Could not regenerate the XXX cover"}), 500


@app.route("/api/execution_screenshots")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def execution_screenshots():
    """List local FFmpeg screenshots belonging to the active execution only."""
    session_id = str(request.args.get("session_id", "")).strip()
    if not session_id:
        return jsonify({"success": False, "error": "Missing session_id"}), 400
    resolved = _resolve_execution_screenshot_review(session_id)
    if resolved is None:
        return jsonify({"success": False, "error": "Screenshots are not available yet"}), 404

    from src.screenshot_review import ReviewedScreenshot, image_version, list_review_items

    temp_dir, meta_data = resolved
    meta_data = _screenshot_review_meta(temp_dir, meta_data)
    items = list_review_items(temp_dir, meta_data)
    can_capture = _screenshot_capture_supported(meta_data)
    can_discard = _hosted_image_discard_supported(meta_data)

    def image_urls(item: ReviewedScreenshot) -> tuple[str, str]:
        """Return (grid, full-size) URLs; remote hosts expose a lighter ``img_url`` thumbnail."""
        if item.path:
            local_url = f"/api/execution_screenshots/{item.id}/image?session_id={session_id}&v={image_version(temp_dir, item.id, item.path.stat().st_mtime_ns)}"
            return local_url, local_url
        remote = item.remote_image or {}
        img_url = str(remote.get("img_url") or "")
        raw_url = str(remote.get("raw_url") or "")
        return img_url or raw_url, raw_url or img_url

    return jsonify(
        {
            "success": True,
            # Additions to hosted images do not need a local frame, even after every hosted image was discarded.
            "can_add": (bool(items) or bool(meta_data.get("image_list"))) and can_capture,
            "screenshots": [
                {
                    "id": item.id,
                    "filename": item.path.name if item.path else f"Remote image {item.index + 1}",
                    "size": item.path.stat().st_size if item.path else None,
                    "source": item.source,
                    "group": item.group,
                    "image_url": urls[0],
                    "full_image_url": urls[1],
                    "can_replace": can_capture and item.source != "addition",
                    "can_delete": item.source in {"local", "addition"} or can_discard,
                }
                for item in items
                for urls in (image_urls(item),)
            ],
        }
    )


@app.route("/api/execution_mediainfo")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def execution_mediainfo():
    """Return read-only MediaInfo/BDInfo reports for the active execution only."""
    session_id = str(request.args.get("session_id", "")).strip()
    if not session_id:
        return jsonify({"success": False, "error": "Missing session_id"}), 400
    resolved = _resolve_execution_screenshot_review(session_id)
    if resolved is None:
        return jsonify({"success": False, "error": "MediaInfo is not available yet"}), 404

    from src.mediainfo_review import report_items

    temp_dir, meta_data = resolved
    kind, reports = report_items(meta_data, temp_dir)
    if not reports:
        return jsonify({"success": False, "kind": kind, "error": "MediaInfo is not available yet"}), 404
    response = jsonify({"success": True, "kind": kind, "reports": reports})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/api/execution_description")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def execution_description():
    """Return the editable base-description draft and its read-only sources."""
    session_id = str(request.args.get("session_id", "")).strip()
    resolved = _resolve_execution_description_review(session_id)
    if resolved is None:
        return jsonify({"success": False, "error": "Description is not available yet"}), 404
    from src.description_review import draft, source_items

    temp_dir, _meta_file, meta_data = resolved
    content, version = draft(meta_data, temp_dir)
    return jsonify(
        {
            "success": True,
            "content": content,
            "version": version,
            "sources": source_items(meta_data),
        }
    )


@app.route("/api/execution_description", methods=["PUT"])
@limiter.limit("600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def save_execution_description():
    """Persist an edited draft for the current execution with optimistic locking."""
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403
    payload = _request_json_dict()
    session_id = _stringify_preview_value(payload.get("session_id"))
    content = payload.get("content")
    if not isinstance(content, str):
        return jsonify({"success": False, "error": "Description content must be text"}), 400
    if len(content) > 1_000_000:
        return jsonify({"success": False, "error": "Description exceeds the 1,000,000-character limit"}), 413
    resolved = _resolve_execution_description_review(session_id)
    if resolved is None:
        return jsonify({"success": False, "error": "Description is not available yet"}), 404

    from src.description_review import ReviewConflictError, save_review

    requested_version = payload.get("version")
    if not isinstance(requested_version, int):
        return jsonify({"success": False, "error": "Description version is required"}), 400

    temp_dir, _meta_file, _meta_data = resolved
    try:
        # save_review holds the execution's file lock while it checks the version and
        # writes, so the compare-and-swap also covers the upload process's own writes.
        saved = save_review(temp_dir, content, expected_version=requested_version)
    except ReviewConflictError as conflict:
        return jsonify({"success": False, "error": "Description changed in another browser tab", "version": conflict.current_version}), 409
    except TimeoutError:
        return jsonify({"success": False, "error": "Description is being updated, try again"}), 503
    return jsonify({"success": True, "content": content, "version": saved["version"]})


@app.route("/api/execution_description/reset", methods=["POST"])
@limiter.limit("600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def reset_execution_description():
    """Restore the draft from one of the read-only sources."""
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403
    payload = _request_json_dict()
    session_id = _stringify_preview_value(payload.get("session_id"))
    source_key = _stringify_preview_value(payload.get("source_key"))
    resolved = _resolve_execution_description_review(session_id)
    if resolved is None:
        return jsonify({"success": False, "error": "Description is not available yet"}), 404
    requested_version = payload.get("version")
    if not isinstance(requested_version, int):
        return jsonify({"success": False, "error": "Description version is required"}), 400

    from src.description_review import ReviewConflictError, save_review, source_items

    temp_dir, _meta_file, meta_data = resolved
    source = next((item for item in source_items(meta_data) if item["key"] == source_key), None)
    if source is None:
        return jsonify({"success": False, "error": "Description source was not found"}), 404
    content = source["content"]
    try:
        saved = save_review(temp_dir, content, expected_version=requested_version)
    except ReviewConflictError as conflict:
        return jsonify({"success": False, "error": "Description changed in another browser tab", "version": conflict.current_version}), 409
    except TimeoutError:
        return jsonify({"success": False, "error": "Description is being updated, try again"}), 503
    return jsonify({"success": True, "content": content, "version": saved["version"]})


@app.route("/api/execution_screenshots/<screenshot_id>/image")
@limiter.limit("7200 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def execution_screenshot_image(screenshot_id: str):
    """Serve one reviewed local screenshot after resolving it through its session."""
    session_id = str(request.args.get("session_id", "")).strip()
    resolved = _resolve_execution_screenshot_review(session_id)
    if resolved is None:
        return jsonify({"success": False, "error": "Screenshots are not available yet"}), 404
    from src.screenshot_review import list_review_items

    temp_dir, meta_data = resolved
    meta_data = _screenshot_review_meta(temp_dir, meta_data)
    screenshot = next((item for item in list_review_items(temp_dir, meta_data) if item.id == screenshot_id), None)
    if screenshot is None or screenshot.path is None:
        return jsonify({"success": False, "error": "Screenshot not found"}), 404
    response = send_file(screenshot.path, mimetype="image/png", conditional=True, max_age=0)
    response.headers["Cache-Control"] = "no-store, max-age=0"  # type: ignore[attr-defined]
    return response


@app.route("/api/execution_screenshots/add", methods=["POST"])
@limiter.limit("600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def add_execution_screenshot():
    """Capture one extra local screenshot for the active execution."""
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403
    payload = _request_json_dict()
    session_id = _stringify_preview_value(payload.get("session_id"))
    group = _stringify_preview_value(payload.get("group")) or "main"
    resolved = _resolve_execution_screenshot_review(session_id)
    if resolved is None:
        return jsonify({"success": False, "error": "Screenshots are not available yet"}), 404
    temp_dir, meta_data = resolved
    meta_data = _screenshot_review_meta(temp_dir, meta_data)
    if not _screenshot_capture_supported(meta_data):
        return jsonify({"success": False, "error": "Screenshot capture is not supported for this release"}), 400

    try:
        from src.screenshot_review import add_screenshot

        asyncio.run(add_screenshot(temp_dir, meta_data, group))
    except (FileNotFoundError, ValueError) as error:
        return jsonify({"success": False, "error": str(error)}), 404
    except Exception as error:
        console.print(f"Screenshot add failed for {session_id}: {error}", markup=False)
        return jsonify({"success": False, "error": "Could not add screenshot"}), 500
    return jsonify({"success": True})


@app.route("/api/execution_screenshots/<screenshot_id>/<action>", methods=["POST"])
@limiter.limit("600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def mutate_execution_screenshot(screenshot_id: str, action: str):
    """Delete or replace a reviewed frame with CSRF and same-origin protection."""
    if action not in {"delete", "replace", "undo"}:
        return jsonify({"success": False, "error": "Unsupported screenshot action"}), 404
    if not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403
    payload = _request_json_dict()
    session_id = _stringify_preview_value(payload.get("session_id"))
    resolved = _resolve_execution_screenshot_review(session_id)
    if resolved is None:
        return jsonify({"success": False, "error": "Screenshots are not available yet"}), 404
    temp_dir, meta_data = resolved
    meta_data = _screenshot_review_meta(temp_dir, meta_data)
    if action == "replace" and not _screenshot_capture_supported(meta_data):
        return jsonify({"success": False, "error": "Screenshot capture is not supported for this release"}), 400
    if action == "delete" and re.fullmatch(r"remote-\d+", screenshot_id) and not _hosted_image_discard_supported(meta_data):
        return jsonify({"success": False, "error": "Hosted images cannot be discarded for this release"}), 400

    try:
        from src.screenshot_review import delete_screenshot, replace_screenshot, undo_remote_replacement

        if action == "delete":
            delete_screenshot(temp_dir, meta_data, screenshot_id)
        elif action == "undo":
            undo_remote_replacement(temp_dir, screenshot_id)
        else:
            asyncio.run(replace_screenshot(temp_dir, meta_data, screenshot_id))
    except (FileNotFoundError, ValueError) as error:
        return jsonify({"success": False, "error": str(error)}), 404
    except Exception as error:
        console.print(f"Screenshot {action} failed for {session_id}: {error}", markup=False)
        return jsonify({"success": False, "error": f"Could not {action} screenshot"}), 500

    return jsonify({"success": True})


def _save_queue_created_file(response: _ResponseLike) -> bool:
    """Only issued queue files count against the quota, not path validation."""
    data = response.get_json(silent=True)
    return isinstance(data, dict) and data.get("queued") is True


@app.route("/api/save_queue", methods=["POST"])
@limiter.limit("100 per hour", key_func=_rate_limit_key_func, deduct_when=_save_queue_created_file)
def save_queue():
    """Save selected items to a temporary queue text file on the server"""
    # CSRF check
    if not _verify_csrf_header():
        return jsonify({"error": "CSRF token missing or invalid", "success": False}), 403

    # Auth check
    bearer = _get_bearer_from_header()
    if bearer:
        if not _token_is_valid(bearer):
            return jsonify({"success": False, "error": "Forbidden (invalid token)"}), 403
    else:
        if not _is_authenticated():
            return jsonify({"success": False, "error": "Authentication required (web session)"}), 401
        if not _verify_same_origin():
            return jsonify({"success": False, "error": "CSRF/Origin validation failed"}), 403

    try:
        data = _as_dict(request.get_json(silent=True)) or {}
        items = data.get("items")
        if not isinstance(items, list):
            return jsonify({"error": "Items must be a list", "success": False}), 400

        if not items:
            return jsonify({"error": "No items provided", "success": False}), 400
        if len(items) > MAX_UPLOAD_PATHS:
            return jsonify({"error": f"Maximum {MAX_UPLOAD_PATHS} paths per upload", "success": False}), 400

        base_dir = STATE_DIR
        tmp_dir = base_dir / "tmp"
        filename = f"webui_queue_{int(time.time() * 1000)}_{secrets.token_hex(4)}.txt"
        file_path = tmp_dir / filename

        validated_items: list[tuple[str, list[str]]] = []
        inspected_directories: InspectedDirectories = set()
        # Item number by normalized content path, and by each of its parent folders.
        claimed_paths: dict[str, int] = {}
        claimed_parents: dict[str, int] = {}
        for number, item in enumerate(items, 1):
            if not isinstance(item, dict):
                return jsonify({"error": f"Invalid queue item {number}", "success": False, "item": number}), 400
            path = item.get("path")
            args = item.get("args", "")

            try:
                if not isinstance(path, str) or not path.strip() or not isinstance(args, str):
                    raise ValueError("Path and arguments must be strings; path cannot be empty")
                validated_path = _validate_execution_path(path.strip(), inspected_directories=inspected_directories)
                if _is_generated_queue(validated_path):
                    raise ValueError("Nested queues are not allowed")
                validated_args = _validate_upload_assistant_args(shlex.split(args) if args else [], inspected_directories=inspected_directories)
            except (ValueError, TypeError) as err:
                return jsonify({"error": f"Invalid queue item {number}: {err}", "success": False, "item": number}), 400
            content_key = os.path.normcase(validated_path)
            parent_keys = [str(parent) for parent in Path(content_key).parents]
            # The same content twice, or a folder plus something inside it, would upload twice.
            overlapping = next((claimed_paths[key] for key in (content_key, *parent_keys) if key in claimed_paths), claimed_parents.get(content_key))
            if overlapping is not None:
                return jsonify({"error": f"Invalid queue item {number}: repeats or overlaps item {overlapping}", "success": False, "item": number}), 400
            claimed_paths[content_key] = number
            for parent_key in parent_keys:
                claimed_parents.setdefault(parent_key, number)
            validated_items.append((validated_path, validated_args))

        if not validated_items:
            return jsonify({"error": "No valid items provided", "success": False}), 400

        if len(validated_items) == 1 and data.get("return_single_path") is True:
            return jsonify({"success": True, "path": validated_items[0][0], "queued": False})

        content = "".join(shlex.join([path, *args]) + "\n" for path, args in validated_items)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with file_path.open("x", encoding="utf-8", newline="\n") as f:
            f.write(content)
        key = os.path.normcase(os.path.realpath(file_path))
        evicted_key = None
        with active_processes_lock, _generated_queues_lock:
            # Bound abandoned queue registrations. Old files never become trusted
            # merely because their names have the webui_queue_ prefix.
            if len(_generated_queues) >= MAX_UPLOAD_PATHS:
                # "path" follows the current queue item; the queue file itself is the execution path.
                active_queue_keys = {os.path.normcase(os.path.realpath(str(process.get("execution_path") or process.get("path", "")))) for process in active_processes.values()}
                evicted_key = next((entry for entry in _generated_queues if entry not in active_queue_keys), None)
                if evicted_key is None:
                    file_path.unlink(missing_ok=True)
                    return jsonify({"success": False, "error": "All queue slots are active; try again after an upload finishes"}), 429
                _generated_queues.pop(evicted_key)
            _generated_queues[key] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if evicted_key is not None:
            try:
                _remove_generated_queue_file(evicted_key)
            except OSError as error:
                console.print(f"Could not remove expired WebUI queue: {error}", markup=False)
        _prune_stale_queue_logs()

        return jsonify({"success": True, "path": str(file_path), "queued": True})

    except Exception as e:
        console.print(f"Error saving queue: {e}", markup=False)
        console.print(traceback.format_exc(), markup=False)
        return jsonify({"error": "Error saving queue file", "success": False}), 500


# Background execution sessions ----------------------------------------------
#
# An upload runs in a pump thread that records its output in a bounded event
# log. Browser connections only tail that log, so closing a tab no longer kills
# the upload (unless webui_keep_uploads_on_disconnect is False) and a later
# visit can replay and follow it.

SESSION_EVENT_LOG_MAX_BYTES = 4 * 1024 * 1024
SESSION_KEEPALIVE_SECONDS = 1.0
FINISHED_SESSION_TTL_SECONDS = 24 * 60 * 60
SESSION_LABEL_MAX_LENGTH = 200


_session_settings_cache: tuple[tuple[str, int, str, int], tuple[bool, float]] | None = None


def _config_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _webui_session_settings() -> tuple[bool, float]:
    """Return (keep uploads running on disconnect, unattended prompt timeout in seconds)."""
    global _session_settings_cache
    user_path = STATE_DIR / "data" / "config.py"
    example_path = CODE_DIR / "data" / "example_config.py"
    # Polled often (session list, every execute); reparse only when a file changes.
    key = (str(user_path), _config_mtime_ns(user_path), str(example_path), _config_mtime_ns(example_path))
    if _session_settings_cache is not None and _session_settings_cache[0] == key:
        return _session_settings_cache[1]
    user_config = _load_config_from_file(user_path) or {}
    example_config = _load_config_from_file(example_path) or {}
    user_defaults = _as_dict(user_config.get("DEFAULT")) or {}
    example_defaults = _as_dict(example_config.get("DEFAULT")) or {}

    keep_value = user_defaults.get("webui_keep_uploads_on_disconnect", example_defaults.get("webui_keep_uploads_on_disconnect", True))
    keep_running = keep_value if isinstance(keep_value, bool) else str(keep_value).strip().lower() in {"1", "true", "yes", "on"}

    timeout_value = user_defaults.get("webui_unattended_prompt_timeout", example_defaults.get("webui_unattended_prompt_timeout", 60))
    try:
        timeout_minutes = max(0.0, float(timeout_value))
    except TypeError, ValueError:
        timeout_minutes = 60.0
    result = (keep_running, timeout_minutes * 60)
    _session_settings_cache = (key, result)
    return result


def _init_session_events(process_state: dict[str, object], *, label: str, keep_running: bool, prompt_timeout: float) -> None:
    now = time.time()
    process_state.update(
        {
            "label": label,
            "keep_running": keep_running,
            "prompt_timeout": prompt_timeout,
            "events": deque(),
            "events_bytes": 0,
            "events_trimmed": False,
            "next_seq": 1,
            "events_cond": threading.Condition(),
            "finished": False,
            "exit_code": None,
            "finished_at": None,
            "clients": 0,
            "detached_at": now,
            "awaiting_since": now,
            "last_input_at": now,
        }
    )


def _session_condition(process_state: Mapping[str, object]) -> threading.Condition:
    return cast(threading.Condition, process_state["events_cond"])


def _session_events(process_state: Mapping[str, object]) -> deque[tuple[int, str]]:
    return cast(deque[tuple[int, str]], process_state["events"])


def _publish_session_event(process_state: dict[str, object], event: Mapping[str, object]) -> None:
    """Append one event to the session log, trimming the oldest output past the size cap."""
    condition = _session_condition(process_state)
    with condition:
        seq = cast(int, process_state["next_seq"])
        process_state["next_seq"] = seq + 1
        payload = json.dumps({**event, "seq": seq})
        events = _session_events(process_state)
        events.append((seq, payload))
        size = cast(int, process_state["events_bytes"]) + len(payload)
        while size > SESSION_EVENT_LOG_MAX_BYTES and len(events) > 1:
            _seq, dropped = events.popleft()
            size -= len(dropped)
            process_state["events_trimmed"] = True
        process_state["events_bytes"] = size
        condition.notify_all()


def _finish_session(process_state: dict[str, object], exit_code: int | None) -> None:
    condition = _session_condition(process_state)
    with condition:
        process_state["finished"] = True
        process_state["exit_code"] = exit_code
        process_state["finished_at"] = time.time()
        process_state["awaiting_input"] = False
        condition.notify_all()


def _session_is_running(process_state: Mapping[str, object]) -> bool:
    return "events_cond" not in process_state or not process_state.get("finished")


def _prune_finished_sessions_locked() -> None:
    """Drop expired finished sessions and keep only the newest few; caller holds active_processes_lock."""
    now = time.time()
    finished = sorted(
        ((key, info) for key, info in active_processes.items() if not _session_is_running(info)),
        key=lambda item: float(cast(float, item[1].get("finished_at") or 0)),
    )
    kept = [(key, info) for key, info in finished if now - float(cast(float, info.get("finished_at") or 0)) <= FINISHED_SESSION_TTL_SECONDS]
    for key, _info in finished[: len(finished) - len(kept)] + kept[: max(0, len(kept) - MAX_RETAINED_FINISHED_SESSIONS)]:
        active_processes.pop(key, None)


def _launch_session(session_id: str, process_state: dict[str, object]) -> None:
    """Start a registered session's upload controller and the pump that records its output."""
    command, base_dir, env = cast(tuple[list[str], Path, dict[str, str]], process_state["launch"])
    # codeql[py/command-line-injection]
    process, process_mode = _spawn_webui_upload_process(command, base_dir, env)
    process_state.update({"mode": process_mode, "process": process})
    if not _session_state_is_current(session_id, process_state):
        _terminate_process_tree(process)
        _close_webui_process_io(process)
        raise RuntimeError("Execution session was replaced before subprocess startup completed")
    threading.Thread(
        target=_pump_session,
        args=(session_id, process_state, process, process_mode),
        name=f"webui-session-{session_id}",
        daemon=True,
    ).start()


def _start_scheduled_sessions() -> None:
    """Launch scheduled uploads, oldest first, while upload slots are free."""
    while True:
        with active_processes_lock:
            busy = sum(1 for info in active_processes.values() if _session_is_running(info) and not info.get("scheduled"))
            waiting = sorted(
                ((key, info) for key, info in active_processes.items() if info.get("scheduled") and _session_is_running(info)),
                key=lambda item: float(cast(float, item[1].get("scheduled_at") or 0)),
            )
            if busy >= MAX_CONCURRENT_SESSIONS or not waiting:
                return
            session_id, process_state = waiting[0]
            process_state["scheduled"] = False
        console.print(f"Starting scheduled upload session {session_id}", markup=False)
        _publish_session_event(process_state, {"type": "started"})
        try:
            _launch_session(session_id, process_state)
        except Exception as e:
            console.print(f"Could not start scheduled session {session_id}: {e}", markup=False)
            _publish_session_event(process_state, {"type": "error", "data": "Execution error"})
            _remove_session_queue_file(str(process_state.get("execution_path") or ""))
            _finish_session(process_state, None)


SESSION_LABEL_REFRESH_SECONDS = 5.0
SESSION_LABEL_SEPARATOR = " \u2013 "  # en dash between artist/author and work


def _session_display_label(meta_data: Mapping[str, object]) -> str:
    """Build a short, recognisable tab label from an upload's metadata ("" until it is known)."""

    def text(*keys: str) -> str:
        for key in keys:
            value = _stringify_preview_value(meta_data.get(key))
            if value:
                return value
        return ""

    category = text("category").upper()
    title = text("title")
    if category == "MUSIC":
        music = _music_preview_from_meta(meta_data)
        artist = _stringify_preview_value(music.get("artist")) or text("artist")
        album = _stringify_preview_value(music.get("album")) or title
        return SESSION_LABEL_SEPARATOR.join(part for part in (artist, album) if part)
    if category == "BOOK":
        author = text("author", "book_author")
        book_title = text("book_title") or title
        return SESSION_LABEL_SEPARATOR.join(part for part in (author, book_title) if part)
    if not title:
        return ""
    if category == "TV":
        tv_pack = text("tv_pack").lower() not in ("", "0", "false", "none", "null")
        episode = "" if tv_pack else text("episode")
        return f"{title} {text('season')}{episode}".strip()
    year = text("year")
    if category == "MOVIE" and year.isdigit() and int(year) > 0:
        return f"{title} ({year})"
    return title


def _refresh_session_label(session_id: str, process_state: dict[str, object]) -> None:
    """Publish a better tab label once the current item's metadata is known."""
    process_state["label_checked_at"] = time.time()
    current_path = _stringify_preview_value(process_state.get("path"))
    _path, _meta_file, meta_data = _resolve_execution_preview_meta(session_id)
    label = _session_display_label(meta_data) if meta_data is not None else ""
    detail = _stringify_preview_value(meta_data.get("name")) if meta_data is not None else ""

    item_paths = cast(list[str], process_state.get("item_paths") or [])
    if len(item_paths) > 1:
        normalized = os.path.normcase(os.path.realpath(current_path)) if current_path else ""
        if normalized in item_paths:
            label = f"[{item_paths.index(normalized) + 1}/{len(item_paths)}] {label or Path(current_path).name}"
    if not label or (label, detail) == (process_state.get("label"), process_state.get("label_detail")):
        return
    process_state["label"] = label
    process_state["label_detail"] = detail
    _publish_session_event(process_state, {"type": "label", "label": label, "detail": detail})


def _session_summary(session_id: str, process_state: Mapping[str, object]) -> dict[str, object]:
    condition = _session_condition(process_state)
    with condition:
        return {
            "session_id": session_id,
            "label": str(process_state.get("label") or Path(str(process_state.get("path") or "")).name),
            "label_detail": str(process_state.get("label_detail") or ""),
            "scheduled": bool(process_state.get("scheduled")),
            "running": not process_state.get("finished"),
            "exit_code": process_state.get("exit_code"),
            "awaiting_input": bool(process_state.get("awaiting_input")),
            "started_at": process_state.get("started_at"),
            "finished_at": process_state.get("finished_at"),
            "last_seq": cast(int, process_state["next_seq"]) - 1,
        }


def _sse(event: Mapping[str, object]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _stream_session_events(session_id: str, process_state: dict[str, object], after: int) -> Iterator[str]:
    """Replay logged events after ``after`` and follow the session until it finishes."""
    condition = _session_condition(process_state)
    with condition:
        process_state["clients"] = cast(int, process_state["clients"]) + 1
    try:
        with condition:
            backlog = [(seq, payload) for seq, payload in _session_events(process_state) if seq > after]
            trimmed = bool(process_state.get("events_trimmed")) and (not backlog or backlog[0][0] > after + 1)
        if trimmed:
            yield _sse({"type": "truncated"})
        last_seq = after
        for seq, payload in backlog:
            yield f"data: {payload}\n\n"
            last_seq = seq
        yield _sse({"type": "replay_done"})

        while True:
            with condition:
                condition.wait_for(
                    lambda seen=last_seq: cast(int, process_state["next_seq"]) - 1 > seen or bool(process_state.get("finished")),
                    timeout=SESSION_KEEPALIVE_SECONDS,
                )
                pending = [(seq, payload) for seq, payload in _session_events(process_state) if seq > last_seq]
                finished = bool(process_state.get("finished"))
            for seq, payload in pending:
                yield f"data: {payload}\n\n"
                last_seq = seq
            if not pending:
                if finished:
                    # Tells the browser the session is over, not that the connection dropped.
                    yield _sse({"type": "finished", "exit_code": process_state.get("exit_code")})
                    break
                yield _sse({"type": "keepalive"})
    finally:
        with condition:
            clients = cast(int, process_state["clients"]) - 1
            process_state["clients"] = clients
            if clients == 0:
                process_state["detached_at"] = time.time()
            orphaned = clients == 0 and not process_state.get("finished")
        if orphaned and not process_state.get("keep_running", True):
            # Legacy behaviour: the upload lives only as long as a browser follows it.
            process = cast(_WebUIProcess | None, process_state.get("process"))
            if process is not None and process.poll() is None:
                with contextlib.suppress(Exception):
                    _terminate_process_tree(process)
            _discard_session_state(session_id, process_state)


def _unanswered_prompt_expired(process_state: Mapping[str, object]) -> bool:
    timeout = float(cast(float, process_state.get("prompt_timeout") or 0))
    if timeout <= 0 or not process_state.get("awaiting_input"):
        return False
    with _session_condition(process_state):
        if cast(int, process_state["clients"]) > 0:
            return False
        since = max(
            float(cast(float, process_state["awaiting_since"])),
            float(cast(float, process_state["detached_at"])),
            float(cast(float, process_state["last_input_at"])),
        )
    return time.time() - since >= timeout


def _subprocess_html_fragment(chunk: str) -> str:
    try:
        if ansi_to_html:
            return ansi_to_html(chunk)
    except Exception as e:
        console.print(f"HTML conversion error: {e}", markup=False)
    import html as _html

    return f"<pre>{_html.escape(chunk)}</pre>"


def _remove_session_queue_file(validated_path: str) -> None:
    try:
        key = os.path.normcase(os.path.realpath(validated_path))
        with _generated_queues_lock:
            generated = _generated_queues.pop(key, None)
        if generated is not None:
            lines = Path(key).read_text(encoding="utf-8").splitlines() if Path(key).is_file() else []
            _remove_generated_queue_file(key)
            _remove_generated_queue_logs(generated, lines)
            console.print(f"Cleaned up queue file: {Path(validated_path).name}", markup=False)
    except Exception as cleanup_err:
        console.print(f"Failed to cleanup queue file: {cleanup_err}", markup=False)


def _pump_session(session_id: str, process_state: dict[str, object], process: _WebUIProcess, process_mode: str) -> None:
    """Read one upload's output into its session log until the process exits."""
    exit_code: int | None = None
    try:
        output_queue: queue.Queue[tuple[str, str]] = queue.Queue()

        if isinstance(process, _ConPtyProcess):
            # A pseudo terminal combines stdout and stderr into one ANSI stream.
            def read_stdout():
                try:
                    while True:
                        # Keep the same incremental prompt detection semantics as
                        # the pipe reader. ConPTY can return a whole prompt plus
                        # its trailing input marker in one read.
                        for char in process.read(1024):
                            output_queue.put(("stdout", char))
                except EOFError:
                    pass
                except Exception as err:
                    console.print(f"ConPTY read error: {err}", markup=False)

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = None
        else:
            # Thread to read stdout - stream raw output with ANSI codes
            def read_stdout():
                try:
                    stdout = getattr(process, "stdout", None)
                    if stdout is None:
                        return
                    while True:
                        chunk = stdout.read(1)
                        if not chunk:
                            break
                        output_queue.put(("stdout", chunk))
                except Exception as err:
                    console.print(f"stdout read error: {err}", markup=False)

            # Thread to read stderr - stream raw output
            def read_stderr():
                try:
                    stderr = getattr(process, "stderr", None)
                    if stderr is None:
                        return
                    while True:
                        chunk = stderr.read(1)
                        if not chunk:
                            break
                        output_queue.put(("stderr", chunk))
                except Exception as err:
                    console.print(f"stderr read error: {err}", markup=False)

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)

        stdout_thread.start()
        if stderr_thread is not None:
            stderr_thread.start()

        # Record threads and output queue for debugging/cleanup
        with contextlib.suppress(Exception):
            if _session_state_is_current(session_id, process_state):
                process_state["stdout_thread"] = stdout_thread
                process_state["stderr_thread"] = stderr_thread
                process_state["output_queue"] = output_queue

        console.print(
            f"Started {process_mode} reader threads for session {session_id}: stdout={stdout_thread.name}, stderr={getattr(stderr_thread, 'name', 'merged')}",
            markup=False,
        )

        def _read_output(q: queue.Queue[tuple[str, str]]) -> tuple[bool, tuple[str, str] | None]:
            try:
                return True, q.get(timeout=0.1)
            except queue.Empty:
                return False, None

        buffers: dict[str, str] = {"stdout": "", "stderr": ""}
        last_prompt_state: tuple[bool, object] = (False, None)
        held_blank: tuple[str, str] | None = None
        prompt_timed_out = False
        orphaned = False

        while process.poll() is None or not output_queue.empty():
            if not prompt_timed_out and _unanswered_prompt_expired(process_state):
                prompt_timed_out = True
                minutes = float(cast(float, process_state["prompt_timeout"])) / 60
                message = f"Upload killed: a prompt went unanswered for {minutes:g} minutes with no browser connected"
                console.print(f"{message} (session {session_id})", markup=False)
                _publish_session_event(process_state, {"type": "error", "data": message})
                with contextlib.suppress(Exception):
                    _terminate_process_tree(process)

            has_output, output = _read_output(output_queue)
            previous_prompt_type = str(process_state.get("input_type") or "text") if process_state.get("awaiting_input") else None
            if has_output and output is not None:
                output_type, char = output
                if output_type not in buffers:
                    buffers[output_type] = ""
                buffers[output_type] += char
                prompt_type = _subprocess_prompt_type(buffers[output_type], previous_prompt_type)

                if not _should_flush_subprocess_output(buffers[output_type], char):
                    continue
            else:
                # cli_ui writes its input marker without a newline.
                # Flush it when output has been idle for the queue timeout.
                pending_type = next((kind for kind, buffer in buffers.items() if _should_flush_subprocess_output(buffer, "", idle=True)), None)
                if pending_type is None:
                    # A dismissed or replaced session has nobody left to report to.
                    if not _session_state_is_current(session_id, process_state):
                        orphaned = True
                        break
                    if time.time() - float(cast(float, process_state.get("label_checked_at") or 0)) >= SESSION_LABEL_REFRESH_SECONDS:
                        _refresh_session_label(session_id, process_state)
                    continue
                output_type = pending_type
                prompt_type = _subprocess_prompt_type(buffers[output_type], previous_prompt_type)

            chunk = buffers[output_type]
            buffers[output_type] = ""
            stripped_chunk = chunk.strip()
            structured = stripped_chunk == PROMPT_SOUND_STDOUT_MARKER or stripped_chunk.startswith(STRUCTURED_STDOUT_PREFIXES)
            if held_blank is not None:
                # Structured lines start with a newline so they never join a
                # terminal line; drop the blank line that newline produced.
                if not structured:
                    _publish_session_event(process_state, {"type": "html", "data": _subprocess_html_fragment(held_blank[1]), "origin": held_blank[0]})
                held_blank = None
            if not structured and not prompt_type and chunk.endswith("\n") and _is_blank_output(chunk):
                held_blank = (output_type, chunk)
                continue

            if stripped_chunk == PROMPT_SOUND_STDOUT_MARKER:
                _publish_session_event(process_state, {"type": "prompt_sound"})
                continue

            gui_event = _subprocess_gui_event(chunk)
            if gui_event is not None:
                if gui_event:
                    _publish_session_event(process_state, {"type": "gui", "data": _resolve_gui_item_artwork(session_id, process_state, gui_event)})
                continue

            progress_event = _subprocess_progress_event(chunk)
            if progress_event is not None:
                if progress_event.get("op") == "preview_target":
                    target_path = progress_event.get("path")
                    meta_uuid = progress_event.get("meta_uuid")
                    if isinstance(target_path, str) and (meta_uuid is None or isinstance(meta_uuid, str)):
                        set_execution_preview_target(session_id, str(process_state["run_token"]), target_path, meta_uuid)
                        _refresh_session_label(session_id, process_state)
                    continue
                _set_process_progress_if_current(session_id, process_state, progress_event)
                _publish_session_event(process_state, {"type": "progress", "data": progress_event})
                continue

            # Structured progress and its blank/ANSI separator lines
            # do not answer or replace an outstanding terminal prompt.
            if prompt_type:
                _set_process_awaiting_input_if_current(session_id, process_state, True, prompt_type)
            elif re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", chunk).strip():
                _set_process_awaiting_input_if_current(session_id, process_state, False)
            # Lets background tabs flag a pending prompt without polling.
            prompt_state = (bool(process_state.get("awaiting_input")), process_state.get("input_type"))
            if prompt_state != last_prompt_state:
                if prompt_state[0] and not last_prompt_state[0]:
                    with _session_condition(process_state):
                        process_state["awaiting_since"] = time.time()
                last_prompt_state = prompt_state
                _publish_session_event(process_state, {"type": "prompt_state", "awaiting": prompt_state[0], "input_type": prompt_state[1]})

            _publish_session_event(process_state, {"type": "html", "data": _subprocess_html_fragment(chunk), "origin": output_type})

        # Flush remaining buffers as HTML
        if held_blank is not None:
            _publish_session_event(process_state, {"type": "html", "data": _subprocess_html_fragment(held_blank[1]), "origin": held_blank[0]})
        for output_type, remaining in list(buffers.items()):
            if remaining:
                _publish_session_event(process_state, {"type": "html", "data": _subprocess_html_fragment(remaining), "origin": output_type})

        if not orphaned:
            exit_code = process.wait()
    except Exception as e:
        console.print(f"Execution error for session {session_id}: {e}", markup=False)
        console.print(traceback.format_exc(), markup=False)
        _publish_session_event(process_state, {"type": "error", "data": "Execution error"})
    finally:
        with contextlib.suppress(Exception):
            if process.poll() is None:
                _terminate_process_tree(process)
        _close_webui_process_io(process)
        execution_path = str(process_state.get("execution_path") or "")
        if execution_path:
            _remove_session_queue_file(execution_path)
        if exit_code is not None:
            _publish_session_event(process_state, {"type": "exit", "code": exit_code})
        _finish_session(process_state, exit_code)
        _start_scheduled_sessions()


@app.route("/api/execute", methods=["POST", "OPTIONS"])
@limiter.limit("100 per hour", key_func=_rate_limit_key_func)
def execute_command():
    """Execute upload.py with interactive terminal support"""

    if request.method == "OPTIONS":
        return "", 204

    # Require CSRF token for execute POST requests
    if request.method == "POST" and not _verify_csrf_header():
        return jsonify({"error": "CSRF token missing or invalid", "success": False}), 403

    # If caller used a bearer token, ensure it is valid
    bearer = _get_bearer_from_header()
    if bearer and not _token_is_valid(bearer):
        return jsonify({"error": "Forbidden (invalid token)", "success": False}), 403

    try:
        # Prefer a silent JSON parse to avoid Werkzeug raising on malformed
        # payloads. If parsing fails, try form data or a few tolerant
        # fallbacks to extract common fields (path, args, session_id).
        data = None
        try:
            data = _as_dict(request.get_json(silent=True)) or {}
        except Exception:
            data = None

        if not data:
            # Try standard form-encoded body first
            try:
                if request.form:
                    data = dict(request.form.items())
            except Exception:
                data = None

        if not data:
            # As a last resort attempt to parse raw body text that may be
            # produced by shells which strip quoting or backslashes. We
            # attempt a few conservative transforms rather than executing
            # arbitrary code: 1) normalize single quotes to double quotes,
            # 2) quote unquoted object keys, then try json.loads. If that
            # fails, fall back to simple regex extraction of `path` and
            # `session_id` values.
            try:
                raw = (request.get_data(as_text=True) or "").strip()
                if raw:
                    # Quick normalization: single -> double quotes
                    candidate = raw.replace("'", '"')
                    # Quote unquoted keys like: {path:...} -> {"path":...}
                    candidate = re.sub(r"([\{\s,])([A-Za-z0-9_]+)\s*:", r'\1"\2":', candidate)
                    try:
                        data = _json_load_dict(candidate)
                    except Exception:
                        # Regex extraction fallback for minimal fields
                        d: dict[str, str] = {}
                        m_path = re.search(r'path\s*[:=]\s*["\']?([^"\'\},]+)', raw)
                        m_sess = re.search(r'session_id\s*[:=]\s*["\']?([^"\'\},]+)', raw)
                        m_args = re.search(r'args\s*[:=]\s*["\']?([^"\'\}]+)', raw)
                        if m_path:
                            d["path"] = m_path.group(1)
                        if m_sess:
                            d["session_id"] = m_sess.group(1)
                        if m_args:
                            # Trim any trailing quote/comma characters and preserve spacing
                            raw_args = m_args.group(1).strip()
                            # Defensive: some shells or quoting can produce a
                            # concatenated fragment like `--debug,session_id:...`.
                            # Strip any trailing `,session_id` fragment or any
                            # comma followed by a session_id key so args remain
                            # clean.
                            raw_args = re.split(r',\s*(?:"?session_id|session_id)\b', raw_args)[0]
                            raw_args = raw_args.rstrip(",").strip().strip('"').strip("'")
                            d["args"] = raw_args
                        if d:
                            data = d
            except Exception:
                data = None

        if not data:
            return jsonify({"error": "No JSON data received", "success": False}), 400

        path = str(data.get("path", ""))
        args = str(data.get("args", ""))
        session_id = str(data.get("session_id", "default"))
        label = str(data.get("label", "")).strip()[:SESSION_LABEL_MAX_LENGTH]
        schedule_requested = data.get("schedule") is True
        console.print(f"Execute request - Path: {path}, Args: {args}, Session: {session_id}", markup=False)

        if not path:
            return jsonify({"error": "Missing path", "success": False}), 400

        def generate():
            handed_off = False
            try:
                # Build command to run upload.py directly
                validated_path = _validate_execution_path(path)

                # Additional explicit assertion for static analysis: ensure the
                # resolved path is within allowed browse roots and contains no
                # invalid characters before using it in commands/subprocesses.
                try:
                    _assert_safe_resolved_path(validated_path)
                except ValueError:
                    yield f"data: {json.dumps({'type': 'error', 'data': 'Invalid execution path'})}\n\n"
                    return

                base_dir = STATE_DIR
                upload_script = str(CODE_DIR / "upload.py")
                command = [sys.executable, "-u", upload_script, validated_path]

                # Add arguments if provided
                if args:
                    parsed_args = shlex.split(args)
                    try:
                        validated_args = _validate_upload_assistant_args(parsed_args)
                    except ValueError as err:
                        console.print(f"Invalid execution arguments: {err}", markup=False)
                        yield f"data: {json.dumps({'type': 'error', 'data': 'Invalid execution arguments'})}\n\n"
                        return
                    command.extend(validated_args)

                item_paths = _execution_item_paths(validated_path)
                content_paths = frozenset(item_paths)
                keep_running, prompt_timeout = _webui_session_settings()
                process_state = _make_process_state(validated_path, args)
                process_state["content_paths"] = content_paths
                process_state["item_paths"] = item_paths
                process_state["execution_path"] = validated_path
                _init_session_events(
                    process_state,
                    label=label or Path(validated_path).name,
                    keep_running=keep_running,
                    prompt_timeout=prompt_timeout,
                )

                # Invalid requests must not terminate an existing session. The
                # conflict check and registration share one lock so concurrent
                # tabs cannot both pass the limit or claim the same content.
                with active_processes_lock:
                    conflict = _execution_conflict_locked(session_id, content_paths)
                    scheduled = False
                    if conflict is not None and conflict[0] == "capacity" and schedule_requested and keep_running:
                        waiting = sum(1 for key, info in active_processes.items() if key != session_id and info.get("scheduled") and _session_is_running(info))
                        if waiting < MAX_SCHEDULED_SESSIONS:
                            conflict, scheduled = None, True
                        else:
                            conflict = ("capacity", f"Maximum of {MAX_SCHEDULED_SESSIONS} scheduled uploads reached; wait for one to start")
                    existing = None
                    if conflict is None:
                        existing = active_processes.pop(session_id, None)
                        _prune_finished_sessions_locked()
                        process_state["scheduled"] = scheduled
                        process_state["scheduled_at"] = time.time()
                        active_processes[session_id] = process_state
                if conflict is not None:
                    yield _sse({"type": "error", "code": conflict[0], "data": conflict[1]})
                    return
                if existing:
                    proc = existing.get("process")
                    if proc and proc.poll() is None:
                        with contextlib.suppress(Exception):
                            _terminate_process_tree(proc)
                    elif existing.get("scheduled"):
                        # A replaced scheduled upload never started: release its
                        # queue file (unless reused here) and end its listeners.
                        existing_path = str(existing.get("execution_path") or "")
                        if existing_path and os.path.normcase(os.path.realpath(existing_path)) != os.path.normcase(os.path.realpath(validated_path)):
                            _remove_session_queue_file(existing_path)
                        if "events_cond" in existing:
                            _finish_session(existing, None)

                command_str = subprocess.list2cmdline(command)
                console.print(f"Running: {command_str}", markup=False)

                _publish_session_event(process_state, {"type": "system", "data": f"Executing: {command_str}"})

                # Run the upload controller in an isolated subprocess so Kill can
                # terminate its external workers as one process tree. Subprocess
                # stdin supports the WebUI prompt flow.
                env = _webui_subprocess_env()
                env[ROOTS_ENV] = json.dumps(_get_browse_roots())
                env.pop(QUEUE_ENV, None)
                env.pop(QUEUE_HASH_ENV, None)
                if Path(validated_path).suffix.lower() == ".txt":
                    with _generated_queues_lock:
                        queue_hash = _generated_queues.get(os.path.normcase(os.path.realpath(validated_path)))
                    if queue_hash is None:
                        _discard_session_state(session_id, process_state)
                        yield _sse({"type": "error", "data": "WebUI queue expired; select the paths again"})
                        return
                    env[QUEUE_ENV] = validated_path
                    env[QUEUE_HASH_ENV] = queue_hash

                # Sanity-check the working directory used for the subprocess.
                # `base_dir` is computed from the application `__file__`, but
                # perform lightweight validation to satisfy static analysis
                # tools and ensure we do not pass uncontrolled input here.
                if "\x00" in str(base_dir) or not str(base_dir):
                    raise ValueError("Invalid execution directory")
                if not Path(str(base_dir)).is_absolute():
                    base_dir = str(Path(str(base_dir)).resolve())

                # Validate the constructed argv structure and the values that carry
                # security meaning. Shell punctuation is valid data when passed as
                # an argv value to a subprocess that does not invoke a shell.
                try:
                    if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
                        raise ValueError("Invalid command structure")
                    if len(command) < 4 or command[0] != sys.executable or command[1] != "-u" or command[3] != validated_path:
                        raise ValueError("Invalid command structure")

                    _assert_safe_resolved_path(validated_path)

                    expected_script = os.path.realpath(str(CODE_DIR / "upload.py"))
                    script_real = os.path.realpath(command[2])
                    if script_real != expected_script:
                        raise ValueError("Invalid script path")
                except Exception as err:
                    console.print(f"Refusing to run unsafe command: {err}", markup=False)
                    _discard_session_state(session_id, process_state)
                    yield _sse({"type": "error", "data": "Unsafe execution request"})
                    return

                process_state["launch"] = (command, Path(base_dir), env)
                if scheduled:
                    # The scheduler launches it, even with no browser open; the
                    # tab watches /api/sessions instead of holding a stream open.
                    handed_off = True
                    console.print(f"Scheduled upload session {session_id}", markup=False)
                    yield _sse({"type": "scheduled"})
                    _start_scheduled_sessions()
                    return

                _launch_session(session_id, process_state)
                # From here the pump thread owns the process and its cleanup; this
                # response only follows the session log and may disconnect freely.
                handed_off = True
                yield from _stream_session_events(session_id, process_state, 0)

            except Exception as e:
                console.print(f"Execution error for session {session_id}: {e}", markup=False)
                console.print(traceback.format_exc(), markup=False)
                if not handed_off:
                    yield _sse({"type": "error", "data": "Execution error"})
                    with contextlib.suppress(Exception):
                        if "process_state" in locals():
                            _discard_session_state(session_id, cast(Mapping[str, object], process_state))
            finally:
                if not handed_off and "validated_path" in locals() and validated_path:
                    _remove_session_queue_file(validated_path)

        return Response(generate(), mimetype="text/event-stream")

    except Exception as e:
        console.print(f"Request error: {e}", markup=False)
        console.print(traceback.format_exc(), markup=False)
        return jsonify({"error": "Request error", "success": False}), 500


@app.route("/api/input", methods=["POST"])
@limiter.limit("200 per hour", key_func=_rate_limit_key_func)
def send_input():
    """Send user input to running process"""
    try:
        data = _request_json_dict()
        session_id = str(data.get("session_id", "default"))
        user_input = str(data.get("input", ""))

        # Received input for session (logged at debug level previously) - keep minimal output

        # Authorization: allow either a valid bearer token (programmatic clients)
        # or an authenticated web session. Bearer tokens are validated by
        # `_token_is_valid` (valid token grants access).
        bearer = _get_bearer_from_header()
        if bearer:
            if not _token_is_valid(bearer):
                return jsonify({"error": "Forbidden (invalid token)", "success": False}), 403
        else:
            # Require a web session plus CSRF and same-origin checks for non-token callers
            if not _is_authenticated():
                return jsonify({"error": "Authentication required (web session)", "success": False}), 401
            if not _verify_csrf_header() or not _verify_same_origin():
                return jsonify({"error": "CSRF/Origin validation failed", "success": False}), 403

        if session_id not in active_processes:
            return jsonify({"error": "No active process", "success": False}), 404

        try:
            process_info = active_processes[session_id]
            process = process_info.get("process")
            if process is None:
                return jsonify({"error": "No process found", "success": False}), 500

            if process.poll() is None:  # Process still running
                _set_process_awaiting_input(session_id, False)
                if "events_cond" in process_info:
                    with _session_condition(process_info):
                        process_info["last_input_at"] = time.time()
                _write_webui_process_input(process, user_input)
                console.print(f"Sent input for session {session_id}", markup=False)
            else:
                console.print(f"Process already terminated for session {session_id}", markup=False)
                return jsonify({"error": "Process not running", "success": False}), 400

        except Exception as e:
            console.print(f"Error handling input for session {session_id}: {e}", markup=False)
            console.print(traceback.format_exc(), markup=False)
            return jsonify({"error": "Failed to handle input", "success": False}), 500

        return jsonify({"success": True})

    except Exception as e:
        console.print(f"Input error: {e}", markup=False)
        console.print(traceback.format_exc(), markup=False)
        return jsonify({"error": "Input error", "success": False}), 500


@app.route("/api/kill", methods=["POST"])
@limiter.limit("50 per hour", key_func=_rate_limit_key_func)
def kill_process():
    """Kill a running process"""
    try:
        data = _request_json_dict()
        session_id = str(data.get("session_id", ""))

        console.print(f"Kill request for session {session_id}", markup=False)

        # Authorization: allow either a valid bearer token or an authenticated web session
        bearer = _get_bearer_from_header()
        if bearer:
            if not _token_is_valid(bearer):
                return jsonify({"error": "Forbidden (invalid token)", "success": False}), 403
        else:
            if not _is_authenticated():
                return jsonify({"error": "Authentication required (web session)", "success": False}), 401
            if not _verify_csrf_header() or not _verify_same_origin():
                return jsonify({"error": "CSRF/Origin validation failed", "success": False}), 403

        if session_id not in active_processes:
            return jsonify({"error": "No active process", "success": False}), 404

        process_info = active_processes[session_id]
        # Retrieve subprocess handle
        process = process_info.get("process")
        if process is None:
            # Kill can race with startup after the session has been registered
            # but before Popen returns. Removing this state makes the launcher
            # terminate the controller immediately if it does start. A scheduled
            # upload is cancelled the same way and its queue file is released.
            _discard_session_state(session_id, process_info)
            if process_info.get("scheduled"):
                _remove_session_queue_file(str(process_info.get("execution_path") or ""))
            return jsonify({"success": True, "message": "Execution startup cancelled"})

        terminated = False
        try:
            terminated = _terminate_process_tree(process)

            _close_webui_process_io(process)

        finally:
            # Keep the session available for another Kill attempt if the
            # process tree could not be terminated.
            # Attempt to join reader threads if present
            with contextlib.suppress(Exception):
                info = active_processes.get(session_id, {})
                stdout_t = info.get("stdout_thread")
                stderr_t = info.get("stderr_thread")
                if isinstance(stdout_t, threading.Thread):
                    console.print(f"Joining stdout thread for session {session_id}", markup=False)
                    stdout_t.join(timeout=1)
                if isinstance(stderr_t, threading.Thread):
                    console.print(f"Joining stderr thread for session {session_id}", markup=False)
                    stderr_t.join(timeout=1)

            if terminated:
                with contextlib.suppress(Exception):
                    active_processes.pop(session_id, None)

        if not terminated:
            return jsonify({"error": "Failed to terminate process tree", "success": False}), 500

        console.print(f"Process killed for session {session_id}", markup=False)
        console.print(f"Post-kill snapshot: {_debug_process_snapshot(session_id)}", markup=False)
        return jsonify({"success": True, "message": "Process terminated"})

    except Exception as e:
        console.print(f"Kill error: {e}", markup=False)
        console.print(traceback.format_exc(), markup=False)
        return jsonify({"error": "Kill error", "success": False}), 500


@app.route("/api/sessions")
@limiter.limit("3600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def list_sessions():
    """List running and recently finished uploads so a reopened page can restore its tabs."""
    with active_processes_lock:
        _prune_finished_sessions_locked()
        sessions = [(key, info) for key, info in active_processes.items() if "events_cond" in info]
    summaries = sorted((_session_summary(key, info) for key, info in sessions), key=lambda item: float(cast(float, item.get("started_at") or 0)))
    keep_running, _prompt_timeout = _webui_session_settings()
    response = jsonify(
        {
            "success": True,
            "sessions": summaries,
            "max_running": MAX_CONCURRENT_SESSIONS,
            # Scheduled uploads start later without a browser, which only fits keep-running mode.
            "scheduling": keep_running,
        }
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/api/session_stream")
@limiter.limit("3600 per hour", key_func=_rate_limit_key_func, override_defaults=True)
def session_stream():
    """Re-attach to an upload: replay its logged events after ``after`` and follow it live."""
    session_id = str(request.args.get("session_id", "")).strip()
    try:
        after = max(0, int(str(request.args.get("after", "0"))))
    except ValueError:
        return jsonify({"success": False, "error": "Invalid after"}), 400
    with active_processes_lock:
        process_state = active_processes.get(session_id)
    if process_state is None or "events_cond" not in process_state:
        return jsonify({"success": False, "error": "Session not found"}), 404
    return Response(_stream_session_events(session_id, process_state, after), mimetype="text/event-stream", headers={"Cache-Control": "no-store"})


@app.route("/api/session_dismiss", methods=["POST"])
@limiter.limit("600 per hour", key_func=_rate_limit_key_func)
def dismiss_session():
    """Forget a finished upload; running uploads must be killed instead."""
    bearer = _get_bearer_from_header()
    if bearer:
        if not _token_is_valid(bearer):
            return jsonify({"error": "Forbidden (invalid token)", "success": False}), 403
    elif not _verify_csrf_header() or not _verify_same_origin():
        return jsonify({"error": "CSRF/Origin validation failed", "success": False}), 403

    session_id = str(_request_json_dict().get("session_id", "")).strip()
    if not session_id:
        return jsonify({"error": "Missing session_id", "success": False}), 400
    with active_processes_lock:
        process_state = active_processes.get(session_id)
        if process_state is None:
            return jsonify({"success": True})
        if _session_is_running(process_state):
            return jsonify({"error": "Session is still running", "success": False}), 409
        active_processes.pop(session_id, None)
    return jsonify({"success": True})


@app.errorhandler(404)
def not_found(_e: Exception):
    return jsonify({"error": "Not found", "success": False}), 404


@app.errorhandler(500)
def internal_error(e: Exception):
    console.print(f"500 error: {e!s}", markup=False)
    console.print(traceback.format_exc(), markup=False)
    return jsonify({"error": "Internal server error", "success": False}), 500


for _blueprint in (
    routes_pages.bp,
    routes_account.bp,
    routes_system.bp,
    routes_history.bp,
    routes_config.bp,
    routes_trackers.bp,
    routes_browse.bp,
    routes_review.bp,
    routes_sessions.bp,
):
    app.register_blueprint(_blueprint)


# Startup notices: shared seedboxes expose group/world-readable credentials to
# other accounts, and a first-run WebUI needs the console-only setup code.
with contextlib.suppress(Exception):
    _exposed_secret_files = _readable_secret_files()
    if _exposed_secret_files:
        console.print(
            "Warning: these credential files are readable by other users on this system: "
            + ", ".join(str(path) for path in _exposed_secret_files)
            + ". Restrict them with 'chmod 600 <file>' (or 'chmod 700' on their directory).",
            markup=False,
        )
with contextlib.suppress(Exception):
    if _load_user_record() is None:
        _first_run_setup_code()


# Keep these helpers referenced so static analysis does not flag them as unused.
_ = _maybe_log_api_access
_ = _rate_limit_exceeded
