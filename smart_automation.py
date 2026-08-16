import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence


COORDINATE_PATTERN = re.compile(r"[（(]\s*(\d+)\s*[,，.。]\s*(\d+)\s*[)）]")
PROCEDURE_SCHEMA_VERSION = 1


class StateReadError(RuntimeError):
    pass


class StateTimeout(RuntimeError):
    pass


class ProcedureError(RuntimeError):
    pass


class ProcedureExecutionError(ProcedureError):
    def __init__(
        self,
        message: str,
        *,
        refresh_anchor: datetime | None,
        action_index: int | None = None,
        action_type: str | None = None,
        action_label: str | None = None,
        scheduled_wait_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.refresh_anchor = refresh_anchor
        self.action_index = action_index
        self.action_type = action_type
        self.action_label = action_label
        self.scheduled_wait_seconds = scheduled_wait_seconds


@dataclass(frozen=True)
class TextObservation:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    @property
    def top_center_y(self) -> float:
        return 1.0 - self.center_y


@dataclass(frozen=True)
class GameState:
    map_name: str | None
    coordinate: tuple[int, int] | None
    observations: tuple[TextObservation, ...] = ()

    def as_dict(self) -> dict:
        return {
            "map": self.map_name,
            "coordinate": list(self.coordinate) if self.coordinate else None,
        }


@dataclass(frozen=True)
class ProcedureResult:
    name: str
    started_at: datetime
    completed_at: datetime
    refresh_anchor: datetime | None
    actions_completed: int
    scheduled_wait_seconds: float = 0.0


def parse_game_state(observations: Sequence[TextObservation]) -> GameState:
    coordinate_candidates = []
    for observation in observations:
        match = COORDINATE_PATTERN.search(observation.text.replace(" ", ""))
        if not match:
            continue
        # The current map coordinate is shown in the right-side header.
        if observation.center_x >= 0.82 and 0.12 <= observation.top_center_y <= 0.34:
            coordinate_candidates.append((observation.confidence, observation, match))

    if not coordinate_candidates:
        return GameState(None, None, tuple(observations))

    _, coordinate_observation, match = max(coordinate_candidates, key=lambda item: item[0])
    coordinate = int(match.group(1)), int(match.group(2))
    same_line = [
        observation
        for observation in observations
        if 0.82 <= observation.center_x < coordinate_observation.center_x
        and abs(observation.center_y - coordinate_observation.center_y) <= 0.025
        and not COORDINATE_PATTERN.search(observation.text)
        and 1 <= len(observation.text.strip()) <= 10
    ]
    map_name = max(same_line, key=lambda item: item.center_x).text.strip() if same_line else None
    return GameState(map_name, coordinate, tuple(observations))


class VisionGameStateReader:
    """Read game state with the local macOS Vision framework."""

    def __init__(self, automation, *, languages: Sequence[str] = ("zh-Hans", "en-US")) -> None:
        self.automation = automation
        self.languages = tuple(languages)
        self._request_class = None
        self._handler_class = None

    def _load_vision(self) -> None:
        if self._request_class is not None:
            return
        try:
            import objc

            framework = objc.pathForFramework("/System/Library/Frameworks/Vision.framework")
            objc.loadBundle("Vision", globals(), bundle_path=framework)
            self._request_class = objc.lookUpClass("VNRecognizeTextRequest")
            self._handler_class = objc.lookUpClass("VNImageRequestHandler")
        except Exception as exc:
            raise StateReadError(f"Cannot load macOS Vision OCR: {exc}") from exc

    def observations(self) -> list[TextObservation]:
        self._load_vision()
        image_ref = self.automation.capture_cg_image()
        if image_ref is None:
            raise StateReadError("Cannot capture the game window.")

        request = self._request_class.alloc().init()
        request.setRecognitionLevel_(0)  # Fast mode is enough for the large sidebar text.
        request.setRecognitionLanguages_(list(self.languages))
        request.setUsesLanguageCorrection_(False)
        handler = self._handler_class.alloc().initWithCGImage_options_(image_ref, {})
        if not handler.performRequests_error_([request], None):
            raise StateReadError("macOS Vision OCR request failed.")

        observations = []
        for result in request.results() or []:
            candidates = result.topCandidates_(1)
            if not candidates:
                continue
            candidate = candidates[0]
            bounds = result.boundingBox()
            observations.append(
                TextObservation(
                    text=str(candidate.string()),
                    confidence=float(candidate.confidence()),
                    x=float(bounds.origin.x),
                    y=float(bounds.origin.y),
                    width=float(bounds.size.width),
                    height=float(bounds.size.height),
                )
            )
        return observations

    def read_state(self) -> GameState:
        return parse_game_state(self.observations())

    def wait_for_state(
        self,
        expected: Mapping,
        *,
        timeout: float,
        stable_samples: int = 2,
        poll_seconds: float = 0.15,
        on_sample: Callable[[GameState], None] | None = None,
    ) -> tuple[GameState, float]:
        started = time.monotonic()
        consecutive = 0
        last_state = GameState(None, None)
        seen = []
        while time.monotonic() - started <= timeout:
            try:
                last_state = self.read_state()
            except StateReadError:
                time.sleep(poll_seconds)
                continue
            if on_sample:
                on_sample(last_state)
            state_key = (last_state.map_name, last_state.coordinate)
            if not seen or seen[-1] != state_key:
                seen.append(state_key)
            if state_matches(last_state, expected):
                consecutive += 1
                if consecutive >= stable_samples:
                    return last_state, time.monotonic() - started
            else:
                consecutive = 0
            time.sleep(poll_seconds)

        expected_label = format_expected_state(expected)
        raise StateTimeout(
            f"Timed out after {timeout:.1f}s waiting for {expected_label}; "
            f"last={format_game_state(last_state)}, seen={seen[-8:]}"
        )

    def find_text(
        self,
        text: str,
        *,
        region: Sequence[float] | None = None,
        exact: bool = False,
    ) -> TextObservation:
        return find_text_observation(self.observations(), text, region=region, exact=exact)

    def wait_for_text(
        self,
        text: str,
        *,
        present: bool = True,
        region: Sequence[float] | None = None,
        exact: bool = False,
        timeout: float = 5.0,
        stable_samples: int = 2,
        poll_seconds: float = 0.15,
    ) -> tuple[TextObservation | None, float]:
        started = time.monotonic()
        consecutive = 0
        last_observation = None
        while time.monotonic() - started <= timeout:
            try:
                observations = self.observations()
            except StateReadError:
                time.sleep(poll_seconds)
                continue
            try:
                last_observation = find_text_observation(
                    observations,
                    text,
                    region=region,
                    exact=exact,
                )
                found = True
            except StateReadError:
                last_observation = None
                found = False
            if found == present:
                consecutive += 1
                if consecutive >= stable_samples:
                    return last_observation, time.monotonic() - started
            else:
                consecutive = 0
            time.sleep(poll_seconds)

        expectation = "appear" if present else "disappear"
        raise StateTimeout(f"Timed out after {timeout:.1f}s waiting for text '{text}' to {expectation}.")


class TimingStore:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.data = {}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def estimate(self, procedure: str, action_key: str) -> float | None:
        value = self.data.get(procedure, {}).get(action_key, {}).get("ewma_seconds")
        return float(value) if value is not None else None

    def record(self, procedure: str, action_key: str, elapsed: float) -> None:
        route = self.data.setdefault(procedure, {})
        current = route.get(action_key, {})
        samples = int(current.get("samples", 0)) + 1
        old = float(current.get("ewma_seconds", elapsed))
        route[action_key] = {
            "samples": samples,
            "last_seconds": round(elapsed, 3),
            "ewma_seconds": round(old * 0.7 + elapsed * 0.3, 3),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


class SmartProcedureRunner:
    def __init__(
        self,
        automation,
        *,
        named_points: Mapping[str, tuple[int, int]] | None = None,
        timing_file: str | Path | None = "runtime_profiles.json",
        state_reader: VisionGameStateReader | None = None,
    ) -> None:
        self.automation = automation
        self.named_points = named_points or {}
        self.state_reader = state_reader or VisionGameStateReader(automation)
        self.timings = TimingStore(timing_file)
        self._last_action_details = {}

    def run(
        self,
        procedure: Mapping,
        *,
        dry_run: bool = False,
        action_logger: Callable[[dict], None] | None = None,
        not_before: datetime | None = None,
        step: bool = False,
    ) -> ProcedureResult:
        validate_procedure(procedure, require_actions=True)
        name = str(procedure["name"])
        started_at = datetime.now()
        refresh_anchor = None
        scheduled_wait_seconds = 0.0
        current_index = None
        current_action = None

        try:
            if not dry_run and hasattr(self.automation, "focus_window"):
                self.automation.focus_window()
            for index, action in enumerate(procedure["actions"], start=1):
                current_index = index
                current_action = action
                timing_key = action_timing_key(action)
                before_delay = max(0.0, float(action.get("before_delay", 0.0)))
                if before_delay and not dry_run:
                    time.sleep(before_delay)
                status = {
                    "event": "smart_action",
                    "procedure": name,
                    "index": index,
                    "total": len(procedure["actions"]),
                    "type": action["type"],
                    "label": action.get("label"),
                    "segment": action.get("segment"),
                    "timing_key": timing_key,
                    "wait_until_scheduled": bool(action.get("wait_until_scheduled")),
                    "dry_run": dry_run,
                }
                print(format_smart_action(status, action))
                if action_logger:
                    action_logger(status)
                if step:
                    input("Press Enter to execute this smart action...")
                if dry_run:
                    continue
                if not_before and action.get("wait_until_scheduled"):
                    waited = sleep_until_datetime(not_before)
                    scheduled_wait_seconds += waited
                    gate_status = {
                        **status,
                        "event": "smart_scheduled_gate_reached",
                        "scheduled_for": not_before.isoformat(timespec="milliseconds"),
                        "scheduled_wait_seconds": round(waited, 3),
                    }
                    if action_logger:
                        action_logger(gate_status)
                    if waited:
                        print(
                            f"  scheduled gate reached after {waited:.3f}s at "
                            f"{not_before.isoformat(timespec='milliseconds')}"
                        )

                attempts = max(1, int(action.get("retries", 0)) + 1)
                for attempt in range(1, attempts + 1):
                    self._last_action_details = {}
                    clicked_at = self._perform(action)
                    if action.get("refresh_anchor") and clicked_at:
                        refresh_anchor = clicked_at
                    if action_logger and (clicked_at or self._last_action_details):
                        action_logger(
                            {
                                **status,
                                "event": "smart_action_dispatched",
                                "attempt": attempt,
                                "dispatched_at": (
                                    clicked_at.isoformat(timespec="milliseconds") if clicked_at else None
                                ),
                                **self._last_action_details,
                            }
                        )
                    self_verified_state = action["type"] in {"wait_state", "assert_state", "assert_not_state"}
                    expected = None if self_verified_state else action.get("expect")
                    expected_text = action.get("expect_text")
                    if not expected and not expected_text:
                        break
                    timeout = self._timeout_for(name, timing_key, action)
                    try:
                        elapsed = 0.0
                        if expected:
                            state, state_elapsed = self.state_reader.wait_for_state(expected, timeout=timeout)
                            elapsed += state_elapsed
                            status["state"] = state.as_dict()
                        if expected_text:
                            _, text_elapsed = self.state_reader.wait_for_text(
                                str(expected_text),
                                present=True,
                                region=action.get("expect_text_region"),
                                exact=bool(action.get("expect_text_exact")),
                                timeout=timeout,
                                stable_samples=max(1, int(action.get("expect_text_stable_samples", 2))),
                            )
                            elapsed += text_elapsed
                            status["text"] = str(expected_text)
                        self.timings.record(name, timing_key, elapsed)
                        status.update({"waited_seconds": round(elapsed, 3), "attempt": attempt})
                        if action_logger:
                            action_logger({**status, "event": "smart_state_reached"})
                        break
                    except StateTimeout:
                        if attempt >= attempts or action.get("refresh_anchor"):
                            raise
                        print(f"  retry {attempt}/{attempts - 1}: expected state not reached")
        except Exception as exc:
            if isinstance(exc, ProcedureExecutionError):
                raise
            action_description = (
                f"action {current_index} ({current_action.get('label') or current_action.get('type')})"
                if current_index and current_action
                else "startup"
            )
            timing = " after its refresh action" if refresh_anchor else ""
            raise ProcedureExecutionError(
                f"Procedure '{name}' failed{timing} at {action_description}: {exc}",
                refresh_anchor=refresh_anchor,
                action_index=current_index,
                action_type=current_action.get("type") if current_action else None,
                action_label=current_action.get("label") if current_action else None,
                scheduled_wait_seconds=scheduled_wait_seconds,
            ) from exc
        finally:
            self.timings.save()

        completed_at = datetime.now()
        return ProcedureResult(
            name,
            started_at,
            completed_at,
            refresh_anchor,
            len(procedure["actions"]),
            scheduled_wait_seconds,
        )

    def _perform(self, action: Mapping) -> datetime | None:
        action_type = action["type"]
        if action_type == "click":
            point = self._point(action)
            target_text = action.get("target_text")
            if target_text:
                try:
                    if action.get("target_required"):
                        observation, _ = self.state_reader.wait_for_text(
                            str(target_text),
                            present=True,
                            region=action.get("text_region"),
                            exact=bool(action.get("exact_text")),
                            timeout=float(action.get("text_timeout", 5.0)),
                            stable_samples=max(1, int(action.get("text_stable_samples", 2))),
                        )
                    else:
                        observation = self.state_reader.find_text(
                            str(target_text),
                            region=action.get("text_region"),
                            exact=bool(action.get("exact_text")),
                        )
                    point = self._observation_point(
                        observation,
                        offset=action.get("text_click_offset"),
                    )
                except (StateReadError, StateTimeout):
                    if action.get("target_required"):
                        raise
                    print(f"  text '{target_text}' not found; using recorded point {point}")
            return self.automation.click_reference(point) or datetime.now()
        if action_type == "drag":
            start = tuple(int(value) for value in action["start"])
            end = tuple(int(value) for value in action["end"])
            return (
                self.automation.drag_reference(start, end, duration=float(action.get("duration", 0.5)))
                or datetime.now()
            )
        if action_type == "click_text":
            observation, _ = self.state_reader.wait_for_text(
                str(action["text"]),
                present=True,
                region=action.get("region"),
                exact=bool(action.get("exact_text")),
                timeout=float(action.get("timeout", 5.0)),
                stable_samples=max(1, int(action.get("text_stable_samples", 2))),
            )
            point = self._observation_point(observation, offset=action.get("text_click_offset"))
            return self.automation.click_reference(point) or datetime.now()
        if action_type == "aligned_click":
            alignment = self.automation.align_reference_point(
                str(action["alignment_image"]),
                self._point(action),
                min_matches=max(4, int(action.get("min_matches", 40))),
                min_inlier_ratio=float(action.get("min_inlier_ratio", 0.45)),
                max_rotation_degrees=float(action.get("max_rotation_degrees", 4.0)),
            )
            print(
                f"  aligned point {alignment.point}: matches={alignment.matches} "
                f"inliers={alignment.inliers} ({alignment.inlier_ratio:.1%})"
            )
            self._last_action_details = {
                "aligned_point": list(alignment.point),
                "alignment_matches": alignment.matches,
                "alignment_inliers": alignment.inliers,
                "alignment_inlier_ratio": round(alignment.inlier_ratio, 4),
                "alignment_scale": round(alignment.scale, 5),
                "alignment_rotation_degrees": round(alignment.rotation_degrees, 4),
            }
            return self.automation.click_reference(alignment.point) or datetime.now()
        if action_type == "rapid_clicks":
            points = [tuple(int(value) for value in point) for point in action["points"]]
            result = self.automation.rapid_click_reference(
                points,
                intervals=action["intervals"],
                max_gap_seconds=float(action["max_gap_seconds"]),
            )
            if result.gaps:
                print(
                    f"  rapid gaps: {', '.join(f'{gap:.3f}s' for gap in result.gaps)} "
                    f"(limit {float(action['max_gap_seconds']):.3f}s)"
                )
            self._last_action_details = {
                "rapid_gaps_seconds": [round(gap, 4) for gap in result.gaps],
                "rapid_max_gap_seconds": round(max(result.gaps, default=0.0), 4),
            }
            return result.clicked_at[-1] if result.clicked_at else datetime.now()
        if action_type == "wait_state":
            self.state_reader.wait_for_state(action["expect"], timeout=float(action.get("timeout", 10.0)))
            return None
        if action_type == "wait_text":
            self.state_reader.wait_for_text(
                str(action["text"]),
                present=bool(action.get("present", True)),
                region=action.get("region"),
                exact=bool(action.get("exact_text")),
                timeout=float(action.get("timeout", 5.0)),
                stable_samples=max(1, int(action.get("stable_samples", 2))),
            )
            return None
        if action_type == "assert_state":
            state = self.state_reader.read_state()
            if not state_matches(state, action["expect"]):
                raise ProcedureError(
                    f"State assertion failed: expected {format_expected_state(action['expect'])}, "
                    f"got {format_game_state(state)}"
                )
            return None
        if action_type == "assert_not_state":
            state = self.state_reader.read_state()
            expected_map = action["expect"].get("map")
            expected_coordinate = coordinate_tuple(action["expect"].get("coordinate"))
            if (expected_map and not state.map_name) or (expected_coordinate and not state.coordinate):
                if action.get("allow_unknown"):
                    return None
                raise StateReadError("Cannot verify the negative state assertion because game state is unreadable.")
            if state_matches(state, action["expect"]):
                raise ProcedureError(
                    f"Negative state assertion failed: route must not start at "
                    f"{format_expected_state(action['expect'])}; got {format_game_state(state)}"
                )
            return None
        if action_type == "pause":
            time.sleep(max(0.0, float(action.get("seconds", 0.0))))
            return None
        raise ProcedureError(f"Unsupported smart action type: {action_type}")

    def _observation_point(
        self,
        observation: TextObservation,
        *,
        offset: Sequence[float] | None = None,
    ) -> tuple[int, int]:
        offset_x, offset_y = (offset or (0, 0))
        return (
            round(observation.center_x * self.automation.reference_width + float(offset_x)),
            round((1.0 - observation.center_y) * self.automation.reference_height + float(offset_y)),
        )

    def _point(self, action: Mapping) -> tuple[int, int]:
        if "point" in action:
            return tuple(int(value) for value in action["point"])
        point_name = action.get("point_name")
        if point_name in self.named_points:
            return self.named_points[point_name]
        raise ProcedureError(f"Unknown point for action: {action}")

    def _timeout_for(self, procedure: str, timing_key: str, action: Mapping) -> float:
        if "timeout" in action:
            return max(1.0, float(action["timeout"]))
        estimates = [float(action.get("observed_seconds", 0.0)), self.timings.estimate(procedure, timing_key) or 0.0]
        return max(5.0, max(estimates) * 1.8 + 2.0)


def load_procedures(
    directory: str | Path = "procedures",
    *,
    builtins: Mapping[str, Mapping] | None = None,
) -> dict[str, dict]:
    procedures = {name: dict(value) for name, value in (builtins or {}).items()}
    procedure_dir = Path(directory)
    if not procedure_dir.exists():
        return expand_procedure_includes(procedures)
    for path in sorted(procedure_dir.glob("*.json")):
        try:
            procedure = json.loads(path.read_text(encoding="utf-8"))
            validate_procedure(procedure, require_actions=False)
        except (OSError, ValueError, ProcedureError) as exc:
            raise ProcedureError(f"Invalid procedure file {path}: {exc}") from exc
        procedure["source"] = str(path)
        procedures[procedure["name"]] = procedure
    return expand_procedure_includes(procedures)


def expand_procedure_includes(procedures: Mapping[str, Mapping]) -> dict[str, dict]:
    expanded = {}

    def expand(name: str, stack: tuple[str, ...]) -> dict:
        if name in expanded:
            return expanded[name]
        if name not in procedures:
            parent = stack[-1] if stack else "?"
            raise ProcedureError(f"Procedure '{parent}' includes unknown procedure '{name}'.")
        if name in stack:
            cycle = " -> ".join((*stack, name))
            raise ProcedureError(f"Circular procedure include: {cycle}")

        procedure = copy.deepcopy(dict(procedures[name]))
        actions = []
        for action in procedure.get("actions", []):
            if action.get("type") != "include":
                actions.append(action)
                continue
            included_name = str(action.get("procedure", ""))
            included = expand(included_name, (*stack, name))
            for included_action in included.get("actions", []):
                item = copy.deepcopy(included_action)
                item.setdefault("segment", included_name)
                actions.append(item)
        procedure["actions"] = actions
        validate_procedure(procedure, require_actions=False)
        expanded[name] = procedure
        return procedure

    for procedure_name in procedures:
        expand(procedure_name, ())
    return expanded


def validate_procedure(procedure: Mapping, *, require_actions: bool) -> None:
    if int(procedure.get("schema_version", 0)) != PROCEDURE_SCHEMA_VERSION:
        raise ProcedureError(f"Unsupported procedure schema: {procedure.get('schema_version')}")
    if not procedure.get("name"):
        raise ProcedureError("Procedure name is required.")
    actions = procedure.get("actions")
    if not isinstance(actions, list):
        raise ProcedureError("Procedure actions must be a list.")
    if require_actions and not actions:
        raise ProcedureError(f"Procedure '{procedure['name']}' is a draft with no recorded actions.")
    schedule = procedure.get("schedule")
    lead_seconds = 0.0
    if schedule is not None:
        try:
            interval_minutes = float(schedule.get("interval_minutes", 0.0)) if isinstance(schedule, dict) else 0.0
            lead_seconds = float(schedule.get("lead_seconds", 0.0)) if isinstance(schedule, dict) else 0.0
        except (TypeError, ValueError):
            interval_minutes = 0.0
            lead_seconds = 0.0
        if interval_minutes <= 0:
            raise ProcedureError("Scheduled procedures require a positive interval_minutes value.")
    supported = {
        "click",
        "drag",
        "click_text",
        "aligned_click",
        "rapid_clicks",
        "wait_state",
        "wait_text",
        "assert_state",
        "assert_not_state",
        "pause",
        "include",
    }
    anchors = [index for index, action in enumerate(actions, start=1) if action.get("refresh_anchor")]
    if len(anchors) > 1:
        raise ProcedureError(f"Procedure '{procedure['name']}' has more than one refresh anchor: {anchors}.")
    gates = [index for index, action in enumerate(actions, start=1) if action.get("wait_until_scheduled")]
    if actions and lead_seconds > 0:
        if len(anchors) != 1:
            raise ProcedureError("A procedure with lead_seconds requires exactly one refresh anchor.")
        if len(gates) != 1 or gates[0] > anchors[0]:
            raise ProcedureError(
                "A procedure with lead_seconds requires one scheduled gate at or before its refresh anchor."
            )
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict) or action.get("type") not in supported:
            raise ProcedureError(f"Invalid action {index} in procedure '{procedure['name']}'.")
        if action["type"] == "click" and "point" not in action and "point_name" not in action:
            raise ProcedureError(f"Click action {index} requires point or point_name.")
        if action["type"] == "aligned_click":
            if "point" not in action and "point_name" not in action:
                raise ProcedureError(f"Aligned click action {index} requires point or point_name.")
            if not action.get("alignment_image"):
                raise ProcedureError(f"Aligned click action {index} requires alignment_image.")
        if action["type"] == "drag" and ("start" not in action or "end" not in action):
            raise ProcedureError(f"Drag action {index} requires start and end.")
        if action["type"] == "rapid_clicks":
            points = action.get("points")
            intervals = action.get("intervals")
            try:
                max_gap = float(action.get("max_gap_seconds", 0.0))
            except (TypeError, ValueError):
                max_gap = 0.0
            if not isinstance(points, list) or len(points) < 2:
                raise ProcedureError(f"Rapid action {index} requires at least two points.")
            if isinstance(intervals, (int, float)):
                interval_values = [float(intervals)] * (len(points) - 1)
            elif isinstance(intervals, list) and len(intervals) == len(points) - 1:
                try:
                    interval_values = [float(value) for value in intervals]
                except (TypeError, ValueError):
                    interval_values = []
            else:
                interval_values = []
            if max_gap <= 0 or not interval_values or any(value < 0 or value > max_gap for value in interval_values):
                raise ProcedureError(
                    f"Rapid action {index} requires valid intervals no greater than max_gap_seconds."
                )
        if action["type"] == "wait_text" and not action.get("text"):
            raise ProcedureError(f"Text wait action {index} requires text.")
        if action["type"] in {"wait_state", "assert_state", "assert_not_state"} and not action.get("expect"):
            raise ProcedureError(f"State action {index} requires expect.")
        if action["type"] == "include" and not action.get("procedure"):
            raise ProcedureError(f"Include action {index} requires procedure.")
        if action.get("text_click_offset") is not None and len(action["text_click_offset"]) != 2:
            raise ProcedureError(f"Action {index} text_click_offset must contain x and y.")
        if action.get("refresh_anchor") and action["type"] not in {
            "click",
            "click_text",
            "aligned_click",
            "rapid_clicks",
        }:
            raise ProcedureError(f"Refresh anchor action {index} must dispatch a click.")


def build_recorded_procedure(
    *,
    name: str,
    events: Sequence[Mapping],
    state_samples: Sequence[Mapping],
    metadata: Mapping,
    recording_ended_at: float,
) -> dict:
    events = collapse_recorded_events(
        events,
        rapid_max_gap_seconds=float(metadata.get("rapid_max_gap_seconds", 0.5)),
    )
    actions = []
    for index, event in enumerate(events):
        interval_end = (
            float(events[index + 1]["monotonic"])
            if index + 1 < len(events)
            else float(recording_ended_at)
        )
        samples = [
            sample
            for sample in state_samples
            if float(event["monotonic"]) <= float(sample["monotonic"]) < interval_end
        ]
        before_sample = latest_sample_before(state_samples, float(event["monotonic"]))
        before = before_sample["state"] if before_sample else None
        action = event_to_smart_action(
            event,
            before_sample=before_sample,
            reference_size=metadata.get("reference_size"),
        )
        stable = last_stable_state(samples)
        if stable and (event.get("checkpoint") or state_key(stable["state"]) != state_key(before)):
            expected = compact_state(stable["state"])
            if expected:
                action["expect"] = expected
                observed = max(0.0, float(stable["first_monotonic"]) - float(event["monotonic"]))
                action["observed_seconds"] = round(observed, 3)
                action["timeout"] = round(max(5.0, observed * 2.0 + 2.0), 2)
        if event.get("refresh_anchor"):
            action["refresh_anchor"] = True
        actions.append(action)

    schedule = metadata.get("schedule")
    if schedule and float(schedule.get("lead_seconds", 0.0)) > 0:
        anchor_indexes = [index for index, action in enumerate(actions) if action.get("refresh_anchor")]
        if len(anchor_indexes) == 1:
            anchor_index = anchor_indexes[0]
            gate_index = anchor_index
            for candidate in range(anchor_index - 1, -1, -1):
                action = actions[candidate]
                if action.get("type") in {"click", "click_text"} and not action.get("expect"):
                    gate_index = candidate
                    break
            actions[gate_index]["wait_until_scheduled"] = True

    procedure = {
        "schema_version": PROCEDURE_SCHEMA_VERSION,
        "name": name,
        "enabled": bool(metadata.get("enabled", True)),
        "description": metadata.get("description", f"Recorded procedure {name}"),
        "map": metadata.get("map"),
        "start_coordinate": coordinate_list(metadata.get("start_coordinate")),
        "target_coordinate": coordinate_list(metadata.get("target_coordinate")),
        "schedule": schedule,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "actions": actions,
    }
    procedure = {key: value for key, value in procedure.items() if value is not None}
    validate_procedure(procedure, require_actions=False)
    return procedure


def event_to_smart_action(
    event: Mapping,
    *,
    before_sample: Mapping | None = None,
    reference_size: Sequence[int] | None = None,
) -> dict:
    action = {
        "type": event["type"],
        "before_delay": round(float(event.get("delay", 0.0)), 3),
    }
    if event["type"] == "click":
        action["point"] = list(event["reference"])
        observed_text = observed_text_at_point(event["reference"], before_sample, reference_size)
        if observed_text:
            action["target_text"] = observed_text["text"]
            action["text_region"] = observed_text["region"]
            if observed_text.get("click_offset"):
                action["text_click_offset"] = observed_text["click_offset"]
    elif event["type"] == "drag":
        action["start"] = list(event["start_reference"])
        action["end"] = list(event["end_reference"])
        action["duration"] = float(event.get("duration", 0.5))
    elif event["type"] == "rapid_clicks":
        action["label"] = "快速连点/多段轻功"
        action["points"] = [list(point) for point in event["points"]]
        action["intervals"] = [float(value) for value in event["intervals"]]
        action["max_gap_seconds"] = float(event["max_gap_seconds"])
        action["recorded_span_seconds"] = float(event.get("recorded_span_seconds", 0.0))
    else:
        raise ProcedureError(f"Cannot convert recorded event type: {event['type']}")
    return action


def collapse_recorded_events(
    events: Sequence[Mapping],
    *,
    rapid_max_gap_seconds: float = 0.5,
) -> list[dict]:
    collapsed = []
    index = 0
    while index < len(events):
        event = events[index]
        group = event.get("rapid_group")
        if group is None:
            collapsed.append(dict(event))
            index += 1
            continue

        grouped = []
        while index < len(events) and events[index].get("rapid_group") == group:
            grouped.append(events[index])
            index += 1
        if len(grouped) < 2:
            single = dict(grouped[0])
            single.pop("rapid_group", None)
            collapsed.append(single)
            continue
        if any(item.get("type") != "click" for item in grouped):
            raise ProcedureError(f"Rapid group {group} contains a non-click action.")

        click_times = [float(item["monotonic"]) for item in grouped]
        intervals = [round(later - earlier, 3) for earlier, later in zip(click_times, click_times[1:])]
        observed_limit = max(intervals, default=0.0) + 0.05
        merged = {
            "type": "rapid_clicks",
            "delay": float(grouped[0].get("delay", 0.0)),
            "monotonic": click_times[0],
            "points": [list(item["reference"]) for item in grouped],
            "intervals": intervals,
            "max_gap_seconds": round(max(float(rapid_max_gap_seconds), observed_limit), 3),
            "recorded_span_seconds": round(click_times[-1] - click_times[0], 3),
        }
        if any(item.get("checkpoint") for item in grouped):
            merged["checkpoint"] = True
        if any(item.get("refresh_anchor") for item in grouped):
            merged["refresh_anchor"] = True
        collapsed.append(merged)
    return collapsed


def latest_state_before(samples: Sequence[Mapping], monotonic_value: float):
    sample = latest_sample_before(samples, monotonic_value)
    return sample["state"] if sample else None


def action_timing_key(action: Mapping) -> str:
    explicit = action.get("timing_id")
    if explicit:
        return f"id:{explicit}"
    identity_fields = (
        "type",
        "label",
        "segment",
        "point",
        "point_name",
        "start",
        "end",
        "points",
        "target_text",
        "text",
        "expect",
        "expect_text",
        "refresh_anchor",
    )
    identity = {key: action[key] for key in identity_fields if key in action}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{action.get('type', 'action')}:{digest}"


def latest_sample_before(samples: Sequence[Mapping], monotonic_value: float):
    candidates = [sample for sample in samples if float(sample["monotonic"]) <= monotonic_value]
    return candidates[-1] if candidates else None


def observed_text_at_point(point, sample, reference_size):
    if not sample or not reference_size or not sample.get("observations"):
        return None
    width, height = (float(value) for value in reference_size)
    x = float(point[0]) / width
    y = float(point[1]) / height
    matches = []
    for observation in sample["observations"]:
        left, top, right, bottom = (float(value) for value in observation["region"])
        margin_left = 0.06
        margin_right = 0.025
        margin_y = 0.018
        if left - margin_left <= x <= right + margin_right and top - margin_y <= y <= bottom + margin_y:
            text = str(observation["text"]).strip()
            if not text or COORDINATE_PATTERN.search(text) or len(text) > 12:
                continue
            delta_x = max(left - x, 0.0, x - right)
            delta_y = max(top - y, 0.0, y - bottom)
            inside = int(left <= x <= right and top <= y <= bottom)
            distance = (delta_x * 2.0) ** 2 + (delta_y * 4.0) ** 2
            expanded = [
                round(max(0.0, left - 0.04), 4),
                round(max(0.0, top - 0.035), 4),
                round(min(1.0, right + 0.04), 4),
                round(min(1.0, bottom + 0.035), 4),
            ]
            center_x = (left + right) * width / 2.0
            center_y = (top + bottom) * height / 2.0
            offset = [round(float(point[0]) - center_x), round(float(point[1]) - center_y)]
            result = {"text": text, "region": expanded}
            if abs(offset[0]) > 3 or abs(offset[1]) > 3:
                result["click_offset"] = offset
            matches.append(
                (
                    inside,
                    -distance,
                    float(observation.get("confidence", 0.0)),
                    -len(text),
                    result,
                )
            )
    return max(matches, key=lambda item: item[:4])[4] if matches else None


def last_stable_state(samples: Sequence[Mapping], minimum_samples: int = 2):
    runs = []
    for sample in samples:
        key = state_key(sample.get("state"))
        if key == (None, None):
            continue
        if runs and runs[-1]["key"] == key:
            runs[-1]["count"] += 1
            runs[-1]["last_monotonic"] = sample["monotonic"]
        else:
            runs.append(
                {
                    "key": key,
                    "count": 1,
                    "state": sample["state"],
                    "first_monotonic": sample["monotonic"],
                    "last_monotonic": sample["monotonic"],
                }
            )
    stable = [run for run in runs if run["count"] >= minimum_samples]
    return stable[-1] if stable else None


def state_matches(state: GameState, expected: Mapping) -> bool:
    expected_map = expected.get("map")
    expected_coordinate = coordinate_tuple(expected.get("coordinate"))
    if expected_map and (not state.map_name or expected_map not in state.map_name):
        return False
    if expected_coordinate and state.coordinate != expected_coordinate:
        return False
    return bool(expected_map or expected_coordinate)


def state_key(state) -> tuple[str | None, tuple[int, int] | None]:
    if isinstance(state, GameState):
        return state.map_name, state.coordinate
    if not state:
        return None, None
    return state.get("map"), coordinate_tuple(state.get("coordinate"))


def compact_state(state) -> dict | None:
    map_name, coordinate = state_key(state)
    if not map_name and not coordinate:
        return None
    result = {}
    if map_name:
        result["map"] = map_name
    if coordinate:
        result["coordinate"] = list(coordinate)
    return result


def coordinate_tuple(value) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = value.replace("，", ",").split(",")
        if len(parts) != 2:
            raise ValueError(f"Invalid coordinate: {value}")
        return int(parts[0].strip()), int(parts[1].strip())
    if len(value) != 2:
        raise ValueError(f"Invalid coordinate: {value}")
    return int(value[0]), int(value[1])


def coordinate_list(value) -> list[int] | None:
    coordinate = coordinate_tuple(value)
    return list(coordinate) if coordinate else None


def normalize_ocr_text(value: str) -> str:
    return re.sub(r"[\s①②③④⑤⑥⑦⑧⑨⑩]", "", value)


def find_text_observation(
    observations: Sequence[TextObservation],
    text: str,
    *,
    region: Sequence[float] | None = None,
    exact: bool = False,
) -> TextObservation:
    target = normalize_ocr_text(text)
    matches = []
    for observation in observations:
        if region and not point_in_top_region(observation.center_x, observation.top_center_y, region):
            continue
        normalized = normalize_ocr_text(observation.text)
        matches_text = target == normalized if exact else target == normalized or target in normalized
        if matches_text:
            exact_rank = int(target == normalized)
            matches.append((exact_rank, observation.confidence, -len(normalized), observation))
    if not matches:
        match_type = "exact " if exact else ""
        raise StateReadError(f"Cannot find visible {match_type}text '{text}'.")
    return max(matches, key=lambda item: item[:3])[3]


def point_in_top_region(x: float, y: float, region: Sequence[float]) -> bool:
    left, top, right, bottom = (float(value) for value in region)
    return left <= x <= right and top <= y <= bottom


def format_expected_state(expected: Mapping) -> str:
    return f"{expected.get('map') or '?'} {coordinate_tuple(expected.get('coordinate')) or '(?, ?)'}"


def format_game_state(state: GameState) -> str:
    return f"{state.map_name or '?'} {state.coordinate or '(?, ?)'}"


def format_smart_action(status: Mapping, action: Mapping) -> str:
    details = (
        action.get("label")
        or action.get("target_text")
        or action.get("point_name")
        or action.get("point")
        or action.get("text")
        or ""
    )
    if action.get("expect"):
        relation = "not " if action.get("type") == "assert_not_state" else ""
        expected = f" -> {relation}{format_expected_state(action['expect'])}"
    else:
        expected = ""
    segment = f" [{action['segment']}]" if action.get("segment") else ""
    return (
        f"[{status['procedure']} {status['index']:03d}/{status['total']:03d}] "
        f"{status['type']}{segment} {details}{expected}"
    )


def sleep_until_datetime(moment: datetime) -> float:
    remaining = (moment - datetime.now()).total_seconds()
    if remaining <= 0:
        return 0.0
    started = time.monotonic()
    deadline = started + remaining
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(remaining)
    return time.monotonic() - started
