import json
import math
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TakeoverProgress:
    observed_at: float
    active: bool
    active_seconds: float
    required_seconds: float
    window_seconds: float
    progress: float
    triggered: bool

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "active_seconds": round(self.active_seconds, 3),
            "required_seconds": round(self.required_seconds, 3),
            "window_seconds": round(self.window_seconds, 3),
            "progress": round(self.progress, 4),
            "triggered": self.triggered,
        }


class MouseTakeoverDetector:
    """Measure sustained physical mouse movement inside a rolling time window."""

    def __init__(
        self,
        *,
        window_seconds: float = 3.0,
        required_seconds: float = 2.0,
        max_gap_seconds: float = 0.30,
        min_distance_pixels: float = 1.0,
    ) -> None:
        self.window_seconds = max(0.5, float(window_seconds))
        self.required_seconds = min(
            self.window_seconds,
            max(0.1, float(required_seconds)),
        )
        self.max_gap_seconds = min(
            self.window_seconds,
            max(0.02, float(max_gap_seconds)),
        )
        self.min_distance_pixels = max(0.0, float(min_distance_pixels))
        self._intervals: deque[tuple[float, float]] = deque()
        self._last_event: tuple[float, float, float] | None = None
        self._lock = threading.Lock()

    def feed(self, x: float, y: float, *, now: float) -> TakeoverProgress:
        timestamp = float(now)
        with self._lock:
            if self._last_event is not None:
                previous_time, previous_x, previous_y = self._last_event
                gap = timestamp - previous_time
                distance = math.hypot(float(x) - previous_x, float(y) - previous_y)
                if 0 < gap <= self.max_gap_seconds and distance >= self.min_distance_pixels:
                    self._intervals.append((previous_time, timestamp))
            self._last_event = (timestamp, float(x), float(y))
            return self._snapshot_locked(timestamp)

    def poll(self, *, now: float) -> TakeoverProgress:
        with self._lock:
            return self._snapshot_locked(float(now))

    def reset(self) -> None:
        with self._lock:
            self._intervals.clear()
            self._last_event = None

    def _snapshot_locked(self, now: float) -> TakeoverProgress:
        cutoff = now - self.window_seconds
        while self._intervals and self._intervals[0][1] <= cutoff:
            self._intervals.popleft()
        active_seconds = sum(
            max(0.0, end - max(start, cutoff))
            for start, end in self._intervals
        )
        active = bool(self._intervals)
        progress = min(1.0, active_seconds / self.required_seconds)
        return TakeoverProgress(
            observed_at=now,
            active=active,
            active_seconds=active_seconds,
            required_seconds=self.required_seconds,
            window_seconds=self.window_seconds,
            progress=progress,
            triggered=active_seconds >= self.required_seconds,
        )


class DesktopTakeoverHUD:
    """Drive a click-through AppKit HUD in a separate process."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled and sys.platform == "darwin")
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._hide_timer: threading.Timer | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._process and self._process.poll() is None:
                return
            self._process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--overlay-child"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )

    def show_progress(self, progress: TakeoverProgress, *, pause_hotkey: str) -> None:
        self._cancel_hide_timer()
        self._send(
            {
                "command": "progress",
                **progress.as_dict(),
                "pause_hotkey": pause_hotkey,
            }
        )

    def show_paused(self, *, auto_hide_seconds: float = 2.0) -> None:
        self._cancel_hide_timer()
        self._send({"command": "paused"})
        timer = threading.Timer(max(0.1, float(auto_hide_seconds)), self.hide)
        timer.daemon = True
        self._hide_timer = timer
        timer.start()

    def hide(self) -> None:
        self._send({"command": "hide"}, start_if_needed=False)

    def stop(self) -> None:
        self._cancel_hide_timer()
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            try:
                if process.stdin:
                    process.stdin.write(json.dumps({"command": "quit"}) + "\n")
                    process.stdin.flush()
                    process.stdin.close()
                process.wait(timeout=2.0)
            except Exception:
                if process.poll() is None:
                    process.terminate()

    def _send(self, payload: dict, *, start_if_needed: bool = True) -> None:
        if not self.enabled:
            return
        if start_if_needed:
            self.start()
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                if process is not None and process.poll() is not None:
                    self._process = None
                return
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._process = None

    def _cancel_hide_timer(self) -> None:
        timer, self._hide_timer = self._hide_timer, None
        if timer:
            timer.cancel()


def run_overlay_child() -> None:
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSColor,
        NSFont,
        NSFontWeightMedium,
        NSFontWeightSemibold,
        NSMakeRect,
        NSPanel,
        NSProgressIndicator,
        NSProgressIndicatorStyleBar,
        NSScreen,
        NSStatusWindowLevel,
        NSTextAlignmentCenter,
        NSTextField,
        NSView,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    width, height = 380.0, 82.0
    visible = NSScreen.mainScreen().visibleFrame()
    x = visible.origin.x + (visible.size.width - width) / 2
    y = visible.origin.y + visible.size.height - height - 24
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, width, height),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setIgnoresMouseEvents_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setLevel_(NSStatusWindowLevel)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary
    )

    content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    content.setWantsLayer_(True)
    content.layer().setCornerRadius_(8.0)
    content.layer().setBackgroundColor_(
        NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.86).CGColor()
    )
    panel.setContentView_(content)

    title = NSTextField.labelWithString_("")
    title.setFrame_(NSMakeRect(18, 45, width - 36, 23))
    title.setAlignment_(NSTextAlignmentCenter)
    title.setTextColor_(NSColor.whiteColor())
    title.setFont_(NSFont.systemFontOfSize_weight_(15.0, NSFontWeightSemibold))
    content.addSubview_(title)

    detail = NSTextField.labelWithString_("")
    detail.setFrame_(NSMakeRect(18, 25, width - 36, 17))
    detail.setAlignment_(NSTextAlignmentCenter)
    detail.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.82, 1.0))
    detail.setFont_(NSFont.systemFontOfSize_weight_(11.0, NSFontWeightMedium))
    content.addSubview_(detail)

    bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(24, 11, width - 48, 7))
    bar.setStyle_(NSProgressIndicatorStyleBar)
    bar.setIndeterminate_(False)
    bar.setMinValue_(0.0)
    bar.setMaxValue_(1.0)
    content.addSubview_(bar)

    def handle(payload: dict) -> None:
        command = payload.get("command")
        if command == "progress":
            active = float(payload.get("active_seconds", 0.0))
            required = float(payload.get("required_seconds", 2.0))
            title.setStringValue_(f"接管确认 {active:.1f} / {required:.1f} 秒")
            detail.setStringValue_(f"持续移动鼠标，或按 {payload.get('pause_hotkey', 'Ctrl-Alt-P')}")
            bar.setDoubleValue_(max(0.0, min(1.0, float(payload.get("progress", 0.0)))))
            panel.orderFrontRegardless()
        elif command == "paused":
            title.setStringValue_("自动控制已中断")
            detail.setStringValue_("可从当前步骤继续，或从任务开头恢复")
            bar.setDoubleValue_(1.0)
            panel.orderFrontRegardless()
        elif command == "hide":
            panel.orderOut_(None)
        elif command == "quit":
            panel.orderOut_(None)
            AppHelper.stopEventLoop()

    def read_commands() -> None:
        for line in sys.stdin:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            AppHelper.callAfter(handle, payload)
            if payload.get("command") == "quit":
                return
        AppHelper.callAfter(AppHelper.stopEventLoop)

    reader = threading.Thread(target=read_commands, name="takeover-hud-input", daemon=True)
    reader.start()
    AppHelper.runEventLoop()


if __name__ == "__main__" and "--overlay-child" in sys.argv:
    run_overlay_child()
