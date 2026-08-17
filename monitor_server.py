import argparse
import base64
import hmac
import ipaddress
import json
import mimetypes
import os
import signal
import ssl
import threading
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from resource_catalog import ResourceLedger, policy_for_task, read_jsonl_tail


AUTH_REALM = "Yanyu Scheduler"
WRITE_REQUEST_HEADER = "X-Yanyu-Request"
WRITE_REQUEST_VALUE = "dashboard"


def read_auth_token_file(path: str | Path) -> str:
    token_path = Path(path).expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        mode = token_path.stat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot read web authentication token file {token_path}: {exc}") from exc
    if len(token) < 24:
        raise ValueError("web authentication token must contain at least 24 characters")
    if os.name != "nt" and mode & 0o077:
        raise ValueError(f"web authentication token file must use mode 600: {token_path}")
    return token


def is_loopback_host(host: str) -> bool:
    value = str(host).strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class MonitorData:
    def __init__(
        self,
        *,
        state_file: str | Path,
        event_file: str | Path,
        resource_history: str | Path,
        ledger: ResourceLedger | None = None,
        runtime_control=None,
        stop_callback=None,
    ) -> None:
        self.state_file = Path(state_file)
        self.event_file = Path(event_file)
        self.ledger = ledger or ResourceLedger(resource_history)
        self.runtime_control = runtime_control
        self.stop_callback = stop_callback

    def status(self) -> dict:
        raw_state = self._read_state()
        now = datetime.now()
        runtime = self.runtime_control.status_snapshot() if self.runtime_control else self._read_runtime_state()
        tasks = []
        points = []
        for task_name, task_state in raw_state.items():
            if not isinstance(task_state, dict):
                continue
            stored_next_due = _datetime(task_state.get("next_due"))
            last_anchor = _datetime(task_state.get("last_refresh_anchor"))
            inferred_interval = (
                max(0.0, (stored_next_due - last_anchor).total_seconds() / 60.0)
                if stored_next_due and last_anchor
                else 0.0
            )
            configured_interval = _float(task_state.get("interval_minutes"), inferred_interval)
            policy = policy_for_task(task_name, configured_interval)
            interval = policy.interval_minutes
            next_due = _datetime(task_state.get("next_due"))
            if (
                last_anchor
                and interval > 0
                and inferred_interval > 0
                and abs(inferred_interval - interval) > (2.0 / 60.0)
                and task_state.get("last_status") in {"ok", "external_ok", "post_anchor_failed"}
            ):
                next_due = last_anchor + timedelta(minutes=interval)
            lead_seconds = max(0.0, _float(task_state.get("lead_seconds"), 0.0))
            point_starts = []
            for point in (task_state.get("resource_points") or {}).values():
                point_due = _datetime(point.get("next_due"))
                if not point_due:
                    continue
                point_offset = max(
                    0.0,
                    _float(point.get("anchor_offset_seconds"), lead_seconds),
                )
                point_starts.append(point_due - timedelta(seconds=point_offset))
            start_at = (
                min(point_starts)
                if point_starts
                else (next_due - timedelta(seconds=lead_seconds) if next_due else None)
            )
            retry_not_before = _datetime(task_state.get("retry_not_before"))
            if retry_not_before is None and task_state.get("last_status") in {"failed", "partial_failed"}:
                retry_not_before = next_due
            if retry_not_before and (start_at is None or retry_not_before > start_at):
                start_at = retry_not_before
            status = _schedule_status(task_state.get("last_status"), next_due, start_at, now)
            tasks.append(
                {
                    "task": task_name,
                    "name": task_state.get("resource_name") or policy.display_name,
                    "category": task_state.get("resource_category") or policy.category,
                    "status": status,
                    "last_status": task_state.get("last_status", "unknown"),
                    "interval_minutes": interval or policy.interval_minutes,
                    "last_refresh_anchor": task_state.get("last_refresh_anchor"),
                    "next_due": next_due.isoformat(timespec="milliseconds") if next_due else None,
                    "start_at": start_at.isoformat(timespec="milliseconds") if start_at else None,
                    "seconds_remaining": round((next_due - now).total_seconds(), 1) if next_due else None,
                    "lead_seconds": lead_seconds,
                    "retry_not_before": (
                        retry_not_before.isoformat(timespec="milliseconds")
                        if retry_not_before
                        else None
                    ),
                    "failures": int(task_state.get("failures", 0)),
                    "human_pause_seconds": _float(task_state.get("human_pause_seconds"), 0.0),
                    "point_count": len(task_state.get("resource_points") or {}),
                }
            )
            for point_id, point in (task_state.get("resource_points") or {}).items():
                point_due = _datetime(point.get("next_due"))
                points.append(
                    {
                        "task": task_name,
                        "point_id": point_id,
                        "label": point.get("label") or point_id,
                        "category": point.get("category") or policy.category,
                        "status": _schedule_status(task_state.get("last_status"), point_due, point_due, now),
                        "last_refresh_anchor": point.get("last_refresh_anchor"),
                        "next_due": point.get("next_due"),
                        "seconds_remaining": round((point_due - now).total_seconds(), 1) if point_due else None,
                        "estimated_quantity": _float(point.get("estimated_quantity"), 1.0),
                        "unit": point.get("unit", "次"),
                        "samples": int(point.get("samples", 0)),
                        "anchor_offset_seconds": _float(point.get("anchor_offset_seconds"), 0.0),
                        "anchor_offset_samples": int(point.get("anchor_offset_samples", 0)),
                        "segment_seconds": _float(point.get("segment_seconds"), 0.0),
                    }
                )
        tasks.sort(key=lambda item: (_status_priority(item["status"]), item["next_due"] or "", item["task"]))
        points.sort(key=lambda item: (_status_priority(item["status"]), item["next_due"] or "", item["point_id"]))
        return {
            "generated_at": now.isoformat(timespec="milliseconds"),
            "runtime": runtime,
            "scheduler_attached": self.runtime_control is not None,
            "tasks": tasks,
            "points": points,
        }

    def events(self, limit: int) -> list[dict]:
        return list(reversed(read_jsonl_tail(self.event_file, limit=limit)))

    def acquisitions(self, limit: int) -> list[dict]:
        return list(reversed(self.ledger.recent(limit=limit)))

    def summary(self) -> dict:
        status = self.status()
        task_counts = {}
        for task in status["tasks"]:
            task_counts[task["status"]] = task_counts.get(task["status"], 0) + 1
        return {"task_counts": task_counts, "acquisitions": self.ledger.summary()}

    def control(self, action: str) -> dict:
        if self.runtime_control is None:
            raise RuntimeError("scheduler is not attached to this read-only monitor")
        if action == "pause":
            changed = self.runtime_control.request_pause(reason="web_manual_pause", source="web")
        elif action == "resume":
            changed = self.runtime_control.request_resume(source="web", force=False)
        elif action == "force-resume":
            changed = self.runtime_control.request_resume(source="web", force=True)
        elif action == "stop":
            if self.stop_callback:
                self.stop_callback()
            else:
                self.runtime_control.stop_event.set()
            changed = True
        else:
            raise ValueError(f"unknown control action: {action}")
        return {"ok": True, "changed": bool(changed), "runtime": self.runtime_control.status_snapshot()}

    def adjust(self, payload: dict) -> dict:
        task = str(payload.get("task", "")).strip()
        category = str(payload.get("category", "other")).strip() or "other"
        unit = str(payload.get("unit", "次")).strip() or "次"
        note = str(payload.get("note", "")).strip()
        if not task or len(task) > 100:
            raise ValueError("task is required and must be at most 100 characters")
        quantity = float(payload.get("quantity"))
        if not -100000 <= quantity <= 100000 or quantity == 0:
            raise ValueError("quantity must be non-zero and between -100000 and 100000")
        return self.ledger.record_adjustment(
            task_name=task,
            category=category,
            quantity=quantity,
            unit=unit,
            note=note,
        )

    def _read_state(self) -> dict:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _read_runtime_state(self) -> dict:
        return {
            "paused": False,
            "reason": None,
            "source": None,
            "paused_at": None,
            "active_pause_seconds": 0.0,
            "total_pause_seconds": 0.0,
            "checkpoint": None,
            "context": {"phase": "read_only"},
        }


class ThreadingTLSHTTPServer(ThreadingHTTPServer):
    """Accept new clients without letting one stalled TLS handshake block the server."""

    def __init__(self, server_address, handler, *, ssl_context, handshake_timeout=5.0):
        self.ssl_context = ssl_context
        self.handshake_timeout = max(0.1, float(handshake_timeout))
        super().__init__(server_address, handler)

    def get_request(self):
        connection, address = super().get_request()
        try:
            connection.settimeout(self.handshake_timeout)
            secure_connection = self.ssl_context.wrap_socket(
                connection,
                server_side=True,
                do_handshake_on_connect=False,
            )
        except Exception:
            connection.close()
            raise
        return secure_connection, address

    def process_request_thread(self, request, client_address):
        try:
            request.do_handshake()
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
            return
        super().process_request_thread(request, client_address)


class MonitoringServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        data: MonitorData,
        static_dir: str | Path | None = None,
        asset_dir: str | Path | None = None,
        auth_username: str = "yanyu",
        auth_token: str | None = None,
        tls_cert_file: str | Path | None = None,
        tls_key_file: str | Path | None = None,
    ) -> None:
        root = Path(__file__).resolve().parent
        self.static_dir = Path(static_dir) if static_dir else root / "web"
        self.asset_dir = Path(asset_dir) if asset_dir else root / "assets"
        self.data = data
        self.auth_username = str(auth_username).strip()
        self.auth_token = str(auth_token).strip() if auth_token else None
        self.tls_cert_file = Path(tls_cert_file).expanduser() if tls_cert_file else None
        self.tls_key_file = Path(tls_key_file).expanduser() if tls_key_file else None
        loopback = is_loopback_host(host)
        tls_enabled = self.tls_cert_file is not None or self.tls_key_file is not None
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ValueError("both TLS certificate and key files are required")
        if self.auth_token and len(self.auth_token) < 24:
            raise ValueError("web authentication token must contain at least 24 characters")
        if self.auth_token and (not self.auth_username or ":" in self.auth_username):
            raise ValueError("web authentication username must be non-empty and cannot contain ':'")
        if not loopback and self.auth_token and not tls_enabled:
            raise ValueError("TLS is required when authentication is enabled on a LAN address")
        if not loopback and data.runtime_control is not None and not self.auth_token:
            raise ValueError("LAN scheduler control requires a web authentication token")
        self.writes_allowed = loopback or bool(self.auth_token)
        self.scheme = "https" if tls_enabled else "http"
        context = None
        if tls_enabled:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certfile=self.tls_cert_file, keyfile=self.tls_key_file)
        handler = self._handler_class()
        self.httpd = (
            ThreadingTLSHTTPServer((host, int(port)), handler, ssl_context=context)
            if context is not None
            else ThreadingHTTPServer((host, int(port)), handler)
        )
        self.httpd.daemon_threads = True
        self.thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="resource-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.thread and self.thread.is_alive():
            self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)

    def _handler_class(self):
        data = self.data
        static_dir = self.static_dir
        asset_dir = self.asset_dir
        auth_username = self.auth_username
        auth_token = self.auth_token
        writes_allowed = self.writes_allowed

        class Handler(BaseHTTPRequestHandler):
            server_version = "YanyuMonitor/1.0"

            def do_GET(self):
                if not self._authorized():
                    return self._unauthorized()
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == "/api/status":
                    return self._json(data.status())
                if parsed.path == "/api/events":
                    return self._json({"events": data.events(_limit(query))})
                if parsed.path == "/api/acquisitions":
                    return self._json({"acquisitions": data.acquisitions(_limit(query))})
                if parsed.path == "/api/summary":
                    return self._json(data.summary())
                if parsed.path in {"/", "/index.html"}:
                    return self._file(static_dir / "index.html")
                if parsed.path.startswith("/static/"):
                    return self._safe_file(static_dir, parsed.path.removeprefix("/static/"))
                if parsed.path.startswith("/assets/"):
                    return self._safe_file(asset_dir, parsed.path.removeprefix("/assets/"))
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

            def do_POST(self):
                if not self._authorized():
                    return self._unauthorized()
                if not writes_allowed:
                    return self._json(
                        {"error": "this unauthenticated LAN monitor is read-only"},
                        HTTPStatus.FORBIDDEN,
                    )
                if not hmac.compare_digest(
                    self.headers.get(WRITE_REQUEST_HEADER, ""),
                    WRITE_REQUEST_VALUE,
                ):
                    return self._json(
                        {"error": f"missing required {WRITE_REQUEST_HEADER} header"},
                        HTTPStatus.FORBIDDEN,
                    )
                parsed = urlparse(self.path)
                try:
                    payload = self._body()
                    if parsed.path.startswith("/api/control/"):
                        action = parsed.path.removeprefix("/api/control/")
                        return self._json(data.control(action))
                    if parsed.path == "/api/acquisitions/adjust":
                        return self._json({"ok": True, "record": data.adjust(payload)})
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                except (ValueError, RuntimeError) as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)

            def log_message(self, _format, *_args):
                return

            def _authorized(self) -> bool:
                if not auth_token:
                    return True
                scheme, separator, encoded = self.headers.get("Authorization", "").partition(" ")
                if not separator or scheme.lower() != "basic":
                    return False
                try:
                    supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    return False
                return hmac.compare_digest(supplied, f"{auth_username}:{auth_token}")

            def _unauthorized(self):
                return self._json(
                    {"error": "authentication required"},
                    HTTPStatus.UNAUTHORIZED,
                    headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}", charset="UTF-8"'},
                )

            def _body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ValueError("invalid content length") from exc
                if length < 0 or length > 65536:
                    raise ValueError("request body is too large")
                if not length:
                    return {}
                try:
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ValueError("request body must be valid JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError("request body must be a JSON object")
                return value

            def _json(self, payload, status=HTTPStatus.OK, headers=None):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _safe_file(self, root: Path, relative: str):
                candidate = (root / relative).resolve()
                try:
                    candidate.relative_to(root.resolve())
                except ValueError:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return self._file(candidate)

            def _file(self, path: Path):
                try:
                    body = path.read_bytes()
                except OSError:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

        return Handler


def _schedule_status(last_status, next_due, start_at, now):
    if last_status in {"failed", "partial_failed", "post_anchor_failed"}:
        return "failed"
    if next_due is None:
        return "unknown"
    if now >= next_due:
        return "ready"
    if start_at and now >= start_at:
        return "travel_window"
    return "cooldown"


def _status_priority(status):
    return {"failed": 0, "ready": 1, "travel_window": 2, "cooldown": 3, "unknown": 4}.get(status, 5)


def _datetime(value):
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _limit(query):
    try:
        return max(1, min(1000, int(query.get("limit", [200])[0])))
    except (TypeError, ValueError):
        return 200


def main():
    parser = argparse.ArgumentParser(description="Serve the read-only resource monitoring dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-file", default="scheduler_state.json")
    parser.add_argument("--event-file", default="scheduler_events.jsonl")
    parser.add_argument("--resource-history", default="resource_history.jsonl")
    parser.add_argument("--auth-user", default="yanyu")
    parser.add_argument("--auth-token-file")
    parser.add_argument("--tls-cert-file")
    parser.add_argument("--tls-key-file")
    args = parser.parse_args()
    auth_token = read_auth_token_file(args.auth_token_file) if args.auth_token_file else None
    data = MonitorData(
        state_file=args.state_file,
        event_file=args.event_file,
        resource_history=args.resource_history,
    )
    server = MonitoringServer(
        host=args.host,
        port=args.port,
        data=data,
        auth_username=args.auth_user,
        auth_token=auth_token,
        tls_cert_file=args.tls_cert_file,
        tls_key_file=args.tls_key_file,
    )
    stopped = threading.Event()

    def request_stop(_signum, _frame):
        stopped.set()

    previous = signal.signal(signal.SIGINT, request_stop)
    server.start()
    host, port = server.address
    print(f"Read-only resource monitor: {server.scheme}://{host}:{port}")
    try:
        stopped.wait()
    finally:
        server.stop()
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    main()
