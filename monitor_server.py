import argparse
import json
import mimetypes
import signal
import threading
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from resource_catalog import ResourceLedger, policy_for_task, read_jsonl_tail


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
            start_at = next_due - timedelta(seconds=lead_seconds) if next_due else None
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


class MonitoringServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        data: MonitorData,
        static_dir: str | Path | None = None,
        asset_dir: str | Path | None = None,
    ) -> None:
        root = Path(__file__).resolve().parent
        self.static_dir = Path(static_dir) if static_dir else root / "web"
        self.asset_dir = Path(asset_dir) if asset_dir else root / "assets"
        self.data = data
        handler = self._handler_class()
        self.httpd = ThreadingHTTPServer((host, int(port)), handler)
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

        class Handler(BaseHTTPRequestHandler):
            server_version = "YanyuMonitor/1.0"

            def do_GET(self):
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

            def _json(self, payload, status=HTTPStatus.OK):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
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
    args = parser.parse_args()
    data = MonitorData(
        state_file=args.state_file,
        event_file=args.event_file,
        resource_history=args.resource_history,
    )
    server = MonitoringServer(host=args.host, port=args.port, data=data)
    stopped = threading.Event()

    def request_stop(_signum, _frame):
        stopped.set()

    previous = signal.signal(signal.SIGINT, request_stop)
    server.start()
    host, port = server.address
    print(f"Read-only resource monitor: http://{host}:{port}")
    try:
        stopped.wait()
    finally:
        server.stop()
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    main()
