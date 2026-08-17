import copy
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping


RUNTIME_CONTROL_SCHEMA_VERSION = 1


class ResumeValidationError(RuntimeError):
    pass


class RuntimeControl:
    """Coordinate human-input pausing, checkpoints, and safe resumption."""

    def __init__(
        self,
        *,
        stop_event: threading.Event,
        state_file: str | Path = "runtime_control.json",
        resume_hotkey: str = "<ctrl>+<alt>+r",
        resume_delay_seconds: float = 3.0,
        coordinate_tolerance: int = 0,
        detect_human_input: bool = True,
        synthetic_grace_seconds: float = 0.2,
        event_logger: Callable[[dict], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stop_event = stop_event
        self.state_file = Path(state_file)
        self.resume_hotkey = str(resume_hotkey)
        self.resume_delay_seconds = max(0.0, float(resume_delay_seconds))
        self.coordinate_tolerance = max(0, int(coordinate_tolerance))
        self.detect_human_input = bool(detect_human_input)
        self.synthetic_grace_seconds = max(0.0, float(synthetic_grace_seconds))
        self.event_logger = event_logger
        self._monotonic = monotonic

        self.pause_event = threading.Event()
        self.resume_requested = threading.Event()
        self._lock = threading.RLock()
        self._context: dict = {"phase": "idle"}
        self._checkpoint: dict | None = None
        self._resume_checkpoint: dict | None = None
        self._resume_request: dict | None = None
        self._paused_at: datetime | None = None
        self._pause_reason: str | None = None
        self._pause_source: str | None = None
        self._total_pause_seconds = 0.0
        self._last_user_input_monotonic = 0.0
        self._synthetic_depth = 0
        self._ignore_input_until = 0.0
        self._state_provider: Callable[[], object] | None = None
        self._listeners: list[object] = []
        self._load()

    @property
    def is_paused(self) -> bool:
        return self.pause_event.is_set()

    @property
    def total_pause_seconds(self) -> float:
        with self._lock:
            total = self._total_pause_seconds
            if self.pause_event.is_set() and self._paused_at:
                total += max(0.0, (datetime.now() - self._paused_at).total_seconds())
            return total

    def set_state_provider(self, provider: Callable[[], object] | None) -> None:
        self._state_provider = provider

    def set_context(self, **values) -> None:
        with self._lock:
            self._context.update({key: value for key, value in values.items() if value is not None})
            self._context["updated_at"] = datetime.now().isoformat(timespec="milliseconds")
            if self.pause_event.is_set() and self._checkpoint is not None:
                self._checkpoint.update(copy.deepcopy(self._context))
                self._save_locked()

    def replace_context(self, **values) -> None:
        with self._lock:
            self._context = {key: value for key, value in values.items() if value is not None}
            self._context["updated_at"] = datetime.now().isoformat(timespec="milliseconds")
            if self.pause_event.is_set() and self._checkpoint is not None:
                preserved = {
                    key: value
                    for key, value in self._checkpoint.items()
                    if key in {"map", "coordinate", "state_captured_at", "paused_at", "reason", "source", "input"}
                }
                self._checkpoint = {**copy.deepcopy(self._context), **preserved}
                self._save_locked()

    def before_action(
        self,
        *,
        route: str,
        route_index: int,
        total_routes: int,
        action_index: int,
        total_actions: int,
        action_type: str,
        action_label: str | None,
    ) -> None:
        self.set_context(
            route=route,
            route_index=route_index,
            total_routes=total_routes,
            action_index=action_index,
            next_action_index=action_index,
            total_actions=total_actions,
            action_type=action_type,
            action_label=action_label,
            phase="before_action",
        )

    def action_dispatched(self, action_index: int, dispatched_at: datetime | None = None) -> None:
        self.set_context(
            action_index=action_index,
            next_action_index=action_index + 1,
            phase="after_action",
            last_dispatched_at=(
                dispatched_at.isoformat(timespec="milliseconds") if dispatched_at else datetime.now().isoformat()
            ),
        )

    def request_pause(
        self,
        *,
        reason: str,
        source: str = "human_input",
        details: Mapping | None = None,
    ) -> bool:
        now = datetime.now()
        with self._lock:
            self._last_user_input_monotonic = self._monotonic()
            if self.stop_event.is_set() or self.pause_event.is_set():
                return False
            self._paused_at = now
            self._pause_reason = str(reason)
            self._pause_source = str(source)
            self._checkpoint = copy.deepcopy(self._context)
            self._checkpoint.update(
                {
                    "paused_at": now.isoformat(timespec="milliseconds"),
                    "reason": self._pause_reason,
                    "source": self._pause_source,
                    "input": dict(details or {}),
                }
            )
            self.pause_event.set()
            self._save_locked()
        self._emit(
            {
                "event": "automation_paused",
                "reason": reason,
                "source": source,
                "checkpoint": self.pending_checkpoint(),
            }
        )
        print(f"Automation paused ({source}: {reason}).")
        return True

    def request_resume(self, *, source: str = "resume_hotkey", force: bool = False) -> bool:
        with self._lock:
            if not self.pause_event.is_set() or self.stop_event.is_set():
                return False
            requested_at = self._monotonic()
            # Ignore the remaining key events from the resume chord itself.
            self._ignore_input_until = max(self._ignore_input_until, requested_at + 0.5)
            self._resume_request = {
                "source": str(source),
                "force": bool(force),
                "requested_at": requested_at,
            }
            self.resume_requested.set()
        self._emit({"event": "automation_resume_requested", "source": source, "force": bool(force)})
        return True

    def pending_checkpoint(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._checkpoint)

    def consume_resume_checkpoint(self) -> dict | None:
        with self._lock:
            checkpoint = copy.deepcopy(self._resume_checkpoint)
            self._resume_checkpoint = None
            self._save_locked()
            return checkpoint

    def peek_resume_checkpoint(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._resume_checkpoint)

    def clear_checkpoint(self) -> None:
        with self._lock:
            if self.pause_event.is_set():
                return
            self._checkpoint = None
            self._resume_checkpoint = None
            self._save_locked()

    def status_snapshot(self) -> dict:
        with self._lock:
            active_pause = 0.0
            if self.pause_event.is_set() and self._paused_at:
                active_pause = max(0.0, (datetime.now() - self._paused_at).total_seconds())
            return {
                "paused": self.pause_event.is_set(),
                "reason": self._pause_reason,
                "source": self._pause_source,
                "paused_at": self._paused_at.isoformat(timespec="milliseconds") if self._paused_at else None,
                "active_pause_seconds": round(active_pause, 3),
                "total_pause_seconds": round(self._total_pause_seconds + active_pause, 3),
                "resume_hotkey": self.resume_hotkey,
                "checkpoint": copy.deepcopy(self._checkpoint),
                "context": copy.deepcopy(self._context),
            }

    @contextmanager
    def automation_input(self):
        with self._lock:
            self._synthetic_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._synthetic_depth = max(0, self._synthetic_depth - 1)
                self._ignore_input_until = max(
                    self._ignore_input_until,
                    self._monotonic() + self.synthetic_grace_seconds,
                )

    def wait(self, seconds: float) -> None:
        """Wait for active automation time; human-paused time does not consume the delay."""
        remaining = max(0.0, float(seconds))
        self._raise_if_stopped()
        self.wait_if_paused()
        while remaining > 0:
            started = self._monotonic()
            if self.stop_event.wait(min(0.1, remaining)):
                self._raise_if_stopped()
            elapsed = max(0.0, self._monotonic() - started)
            remaining = max(0.0, remaining - elapsed)
            self.wait_if_paused()

    def wait_until(self, moment: datetime) -> float:
        """Wait for a wall-clock deadline and return only actively waited seconds."""
        active_wait = 0.0
        while True:
            self._raise_if_stopped()
            self.wait_if_paused()
            remaining = (moment - datetime.now()).total_seconds()
            if remaining <= 0:
                return active_wait
            started = self._monotonic()
            if self.stop_event.wait(min(0.1, remaining)):
                self._raise_if_stopped()
            active_wait += max(0.0, self._monotonic() - started)

    def wait_if_paused(self) -> float:
        if not self.pause_event.is_set():
            self._raise_if_stopped()
            return 0.0

        self._capture_checkpoint_state()
        entered = self._monotonic()
        while self.pause_event.is_set():
            self._raise_if_stopped()
            if not self.resume_requested.wait(0.1):
                continue
            with self._lock:
                request = dict(self._resume_request or {})
                self._resume_request = None
                self.resume_requested.clear()
            if not request:
                continue

            force = bool(request.get("force"))
            source = str(request.get("source", "unknown"))
            try:
                current_state = None if force else self._validate_resume_state()
            except ResumeValidationError as exc:
                print(f"Resume rejected: {exc}")
                self._emit(
                    {
                        "event": "automation_resume_rejected",
                        "source": source,
                        "reason": str(exc),
                        "checkpoint": self.pending_checkpoint(),
                    }
                )
                continue

            requested_at = float(request.get("requested_at", self._monotonic()))
            self._wait_resume_delay()
            with self._lock:
                new_input = self._last_user_input_monotonic > requested_at
            if new_input and not force:
                message = "human input was detected during the resume countdown"
                print(f"Resume rejected: {message}.")
                self._emit({"event": "automation_resume_rejected", "source": source, "reason": message})
                continue
            self._finish_resume(source=source, force=force, current_state=current_state)

        return max(0.0, self._monotonic() - entered)

    def start_listeners(self) -> None:
        if self._listeners:
            return
        from pynput import keyboard, mouse

        resume_listener = keyboard.GlobalHotKeys({self.resume_hotkey: self.request_resume})
        self._listeners = [resume_listener]
        if self.detect_human_input:
            self._listeners.extend(
                [
                    keyboard.Listener(on_press=self._on_key_press),
                    mouse.Listener(
                        on_move=self._on_mouse_move,
                        on_click=self._on_mouse_click,
                        on_scroll=self._on_mouse_scroll,
                    ),
                ]
            )
        for listener in self._listeners:
            listener.start()

    def stop_listeners(self) -> None:
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            listener.stop()
        for listener in listeners:
            if listener.is_alive():
                listener.join(timeout=1.0)

    def _on_key_press(self, key) -> None:
        self._handle_physical_input("keyboard", {"key": str(key)})

    def _on_mouse_move(self, x, y) -> None:
        self._handle_physical_input("mouse_move", {"x": int(x), "y": int(y)})

    def _on_mouse_click(self, x, y, button, pressed) -> None:
        if pressed:
            self._handle_physical_input(
                "mouse_click",
                {"x": int(x), "y": int(y), "button": str(button)},
            )

    def _on_mouse_scroll(self, x, y, dx, dy) -> None:
        self._handle_physical_input(
            "mouse_scroll",
            {"x": int(x), "y": int(y), "dx": int(dx), "dy": int(dy)},
        )

    def _handle_physical_input(self, input_type: str, details: Mapping) -> None:
        now = self._monotonic()
        with self._lock:
            if self._synthetic_depth or now <= self._ignore_input_until:
                return
            self._last_user_input_monotonic = now
            already_paused = self.pause_event.is_set()
        if not already_paused:
            self.request_pause(reason=input_type, source="human_input", details=details)

    def _capture_checkpoint_state(self) -> None:
        with self._lock:
            if self._checkpoint is None or self._checkpoint.get("state_captured_at"):
                return
        state = self._read_state()
        with self._lock:
            if self._checkpoint is None or self._checkpoint.get("state_captured_at"):
                return
            self._checkpoint.update(state)
            self._checkpoint["state_captured_at"] = datetime.now().isoformat(timespec="milliseconds")
            self._save_locked()
        self._emit({"event": "automation_checkpoint_captured", "checkpoint": self.pending_checkpoint()})

    def _validate_resume_state(self) -> dict:
        checkpoint = self.pending_checkpoint() or {}
        expected_map = checkpoint.get("map")
        expected_coordinate = self._coordinate(checkpoint.get("coordinate"))
        if not expected_map and expected_coordinate is None:
            raise ResumeValidationError("the checkpoint has no readable map or coordinate; use force resume")

        current = self._read_state()
        current_map = current.get("map")
        current_coordinate = self._coordinate(current.get("coordinate"))
        if expected_map and current_map != expected_map:
            raise ResumeValidationError(f"map mismatch: expected {expected_map}, got {current_map or 'unreadable'}")
        if expected_coordinate is not None:
            if current_coordinate is None:
                raise ResumeValidationError("the current coordinate is unreadable")
            dx = abs(expected_coordinate[0] - current_coordinate[0])
            dy = abs(expected_coordinate[1] - current_coordinate[1])
            if max(dx, dy) > self.coordinate_tolerance:
                raise ResumeValidationError(
                    f"coordinate mismatch: expected {expected_coordinate}, got {current_coordinate}"
                )
        return current

    def _read_state(self) -> dict:
        if self._state_provider is None:
            return {"map": None, "coordinate": None}
        try:
            state = self._state_provider()
            if hasattr(state, "as_dict"):
                state = state.as_dict()
            if not isinstance(state, Mapping):
                raise TypeError(f"state provider returned {type(state).__name__}")
            coordinate = self._coordinate(state.get("coordinate"))
            return {
                "map": state.get("map") or state.get("map_name"),
                "coordinate": list(coordinate) if coordinate else None,
            }
        except Exception as exc:
            self._emit({"event": "automation_state_read_failed", "error": repr(exc)})
            return {"map": None, "coordinate": None}

    def _wait_resume_delay(self) -> None:
        remaining = self.resume_delay_seconds
        while remaining > 0:
            self._raise_if_stopped()
            started = self._monotonic()
            if self.stop_event.wait(min(0.1, remaining)):
                self._raise_if_stopped()
            remaining -= max(0.0, self._monotonic() - started)

    def _finish_resume(self, *, source: str, force: bool, current_state: dict | None) -> None:
        now = datetime.now()
        with self._lock:
            duration = max(0.0, (now - self._paused_at).total_seconds()) if self._paused_at else 0.0
            self._total_pause_seconds += duration
            self._resume_checkpoint = copy.deepcopy(self._checkpoint)
            self.pause_event.clear()
            self._paused_at = None
            self._pause_reason = None
            self._pause_source = None
            self._checkpoint = None
            self._save_locked()
        print(f"Automation resumed after {duration:.1f}s pause.")
        self._emit(
            {
                "event": "automation_resumed",
                "source": source,
                "force": force,
                "pause_seconds": round(duration, 3),
                "state": current_state,
            }
        )

    def _raise_if_stopped(self) -> None:
        if self.stop_event.is_set():
            raise KeyboardInterrupt("Scheduler stop requested.")

    @staticmethod
    def _coordinate(value) -> tuple[int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None

    def _emit(self, event: dict) -> None:
        if not self.event_logger:
            return
        try:
            self.event_logger(event)
        except Exception as exc:
            print(f"Could not write runtime-control event: {exc}")

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        except (OSError, ValueError):
            raw = {}
        self._total_pause_seconds = max(0.0, float(raw.get("total_pause_seconds", 0.0)))
        if isinstance(raw.get("resume_checkpoint"), Mapping):
            self._resume_checkpoint = dict(raw["resume_checkpoint"])
        if raw.get("paused") and isinstance(raw.get("checkpoint"), Mapping):
            self._checkpoint = dict(raw["checkpoint"])
            self._pause_reason = raw.get("reason")
            self._pause_source = raw.get("source")
            try:
                self._paused_at = datetime.fromisoformat(raw.get("paused_at"))
            except (TypeError, ValueError):
                self._paused_at = datetime.now()
            self.pause_event.set()

    def _save_locked(self) -> None:
        payload = {
            "schema_version": RUNTIME_CONTROL_SCHEMA_VERSION,
            "paused": self.pause_event.is_set(),
            "reason": self._pause_reason,
            "source": self._pause_source,
            "paused_at": self._paused_at.isoformat(timespec="milliseconds") if self._paused_at else None,
            "total_pause_seconds": round(self._total_pause_seconds, 3),
            "checkpoint": self._checkpoint,
            "resume_checkpoint": self._resume_checkpoint,
            "updated_at": datetime.now().isoformat(timespec="milliseconds"),
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_file)
