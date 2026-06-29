# Yanyu Daemon

Yanyu Daemon is a macOS automation toolkit for the game window named `烟雨江湖`. It records reference coordinates from screenshots, stores reusable route procedures in `coordinates.py`, executes those routes with scaled click/drag control, and can collect run data for later tuning.

The project is built around one practical loop:

1. Capture the current game window as a reference image.
2. Inspect that image and record coordinate points.
3. Build route lists from those points.
4. Dry-run and execute routes.
5. Record logs/screenshots/manual demonstrations to improve timing and reliability.

## Safety And Assumptions

- This project directly moves and clicks your mouse. Use dry-run and step mode before running a new route.
- Keep the game window size stable between capture, coordinate picking, and route execution.
- macOS must grant Accessibility and Screen Recording permissions to the terminal or Python application that runs these scripts.
- The scripts assume the target window title or owner contains `烟雨江湖` by default.
- Generated screenshots and logs can become large. They are ignored by git.

## File Guide

- `test.py`: main final runner for selected routes or the default route sequence.
- `coordinates.py`: coordinate map and named route procedures.
- `automation.py`: shared window lookup, scaling, click/drag execution, status logging, and screenshot capture helpers.
- `high_res_catcher.py`: captures the game window and can refresh `game_screenshot.png`.
- `get_coordinates.py`: opens a screenshot and prints coordinates by mouse click.
- `control_recorder.py`: records manual clicks/drags into structured JSONL and a Python route snippet.
- `recording_analyzer.py`: summarizes manual recording timing and action statistics.
- `route_analyzer.py`: analyzes stored routes without opening the game.
- `pattern_learner.py`: compares per-action screenshots from a route run to flag suspicious actions.
- `tracking_click.py`: scheduled runner that persists remaining event time on quit.
- `smarter_click.py`: scheduled runner without persisted recovery.
- `autosave.py`, `click.py`, `single_run.py`: older/specialized runners kept for reference.

## Installation

Use the same Python interpreter for installation and execution. On this machine Python 3.12 is the expected interpreter.

```bash
python3.12 -m pip install -r requirements.txt
```

If you prefer a virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Verify imports and route discovery:

```bash
python3.12 test.py --list
```

## macOS Permissions

Open System Settings and grant permissions to the terminal application you use:

- Privacy & Security -> Accessibility
- Privacy & Security -> Screen Recording

If a script can find the window but cannot click, Accessibility is usually missing. If screenshots are blank or fail, Screen Recording is usually missing.

## Coordinate Model

Coordinates in `coordinates.py` are reference-image pixels, not absolute screen pixels.

The runner uses:

```text
screen_x = window_left + reference_x * (current_window_width / reference_image_width)
screen_y = window_top  + reference_y * (current_window_height / reference_image_height)
```

This lets routes survive small window size changes, but accuracy is best when the window size matches the reference screenshot.

Two action shapes are supported:

```python
((x, y), delay_seconds)
(((start_x, start_y), (end_x, end_y)), delay_seconds)
```

The first is a click. The second is a drag.

## Standard Workflow

### 1. Capture A Fresh Reference

Capture the current game window and update `game_screenshot.png`:

```bash
python3.12 high_res_catcher.py --once --update-reference
```

Interactive capture mode is also available:

```bash
python3.12 high_res_catcher.py --update-reference
```

Press the backtick key, `` ` ``, to capture. Press `Ctrl-C` to quit.

### 2. Pick Coordinates

Open the latest `screenshot_high_res_*.png` and print a ready-to-paste coordinate line:

```bash
python3.12 get_coordinates.py --name 新位置
```

Move the mouse inside the image analyzer. Left click the target point. The script prints:

```python
'新位置':(123,456),
```

Paste that line into the `pos = { ... }` dictionary in `coordinates.py`.

You can also inspect a specific screenshot:

```bash
python3.12 get_coordinates.py screenshot_high_res_20250522_110634.png --name 新位置
```

### 3. Build A Route

Add a route list in `coordinates.py`:

```python
my_route = [
    (pos['烟雨江湖'], 1.0),
    (pos['包裹'], 0.5),
    (pos['空白'], 1.0),
]
```

Use longer delays after map transitions, loading screens, battle transitions, dialogs, and repeated confirmation chains.

### 4. Analyze Before Running

List available routes:

```bash
python3.12 test.py --list
```

Analyze the default final sequence:

```bash
python3.12 test.py --analyze
```

Analyze one route:

```bash
python3.12 route_analyzer.py xiaotili
```

The analyzer reports action counts, estimated runtime, repeated points, and edge warnings.

### 5. Dry-Run

Print mapped screen coordinates without moving or clicking:

```bash
python3.12 test.py --dry-run
```

Dry-run selected routes:

```bash
python3.12 test.py luoyangrichang1 save --dry-run
```

### 6. Run

Run the default final route sequence:

```bash
python3.12 test.py
```

Run selected routes:

```bash
python3.12 test.py luoyangrichang1 hangzhouxiuwei save
```

Shorten the startup delay:

```bash
python3.12 test.py save --startup-delay 1
```

## Main Runner Options

`test.py` supports:

```bash
python3.12 test.py --list
python3.12 test.py --analyze
python3.12 test.py --dry-run
python3.12 test.py --step
python3.12 test.py --log-jsonl runs/latest.jsonl
python3.12 test.py --capture-dir runs/latest
python3.12 test.py --capture-dir runs/latest --capture-each-action
python3.12 test.py --reference game_screenshot.png
python3.12 test.py --startup-delay 5
```

Recommended for a new or risky route:

```bash
mkdir -p runs/latest
python3.12 test.py my_route --step --log-jsonl runs/latest.jsonl --capture-dir runs/latest --capture-each-action
```

## Manual Procedure Recording

Use `control_recorder.py` when you want the program to observe how you manually perform a new procedure.

```bash
python3.12 control_recorder.py --name my_route
```

Controls:

- `F8`: pause/resume recording.
- `F9`: stop and write files.

Output:

- `recordings/my_route_YYYYMMDD_HHMMSS.jsonl`
- `recordings/my_route_YYYYMMDD_HHMMSS.py`

The `.jsonl` file is structured training/control data. The `.py` file is a route snippet that can be copied into `coordinates.py`.

Example snippet:

```python
my_route = [
    ((510, 882), 1.0),
    (((1024, 66), (66, 1622)), 0.742),
]
```

Quantify the recording:

```bash
python3.12 recording_analyzer.py recordings/my_route_YYYYMMDD_HHMMSS.jsonl
```

The analyzer reports:

- click count
- drag count
- total wait time
- mouse press/hold time
- estimated route time
- drag distance summary
- unusually slow waits

This data is useful for discovering typical movement speed, transition waits, redundant clicks, and actions that should become explicit route steps.

## Pattern Learning From Automated Runs

Run with per-action screenshots:

```bash
mkdir -p runs/latest
python3.12 test.py my_route --log-jsonl runs/latest.jsonl --capture-dir runs/latest --capture-each-action
```

Analyze screenshot changes:

```bash
python3.12 pattern_learner.py --log-jsonl runs/latest.jsonl --capture-dir runs/latest
```

Interpretation:

- `low-change`: the click may have done nothing, may be redundant, or the UI state did not visibly change.
- `high-change`: likely a transition, dialog, loading screen, or battle state. Timing around this action matters.
- Repeated `low-change` actions are good candidates for cleanup.
- Failure after a `high-change` action often means the next delay is too short.

## Scheduled Runners

`tracking_click.py` and `smarter_click.py` run recurring route groups.

`tracking_click.py` is more robust because it writes remaining event time on `Ctrl-C` and tries to resume from the log:

```bash
python3.12 tracking_click.py
```

`smarter_click.py` uses an in-memory schedule:

```bash
python3.12 smarter_click.py
```

For most manual route development, use `test.py`. Use scheduled runners only after the individual routes are stable.

## Data Files And Git Hygiene

Ignored generated files:

- `event_log.txt`
- `screenshot_high_res_*.png`
- `__pycache__/`
- `.venv/`

Recommended generated directories:

- `runs/`
- `recordings/`

If recordings become large, keep them local or archive them separately.

## Troubleshooting

### `ModuleNotFoundError`

Make sure installation and execution use the same interpreter:

```bash
python3.12 -m pip install -r requirements.txt
python3.12 test.py --list
```

### Window Not Found

The default app/window name is `烟雨江湖`. If needed:

```bash
python3.12 high_res_catcher.py --app "JiangHu-mobile" --once
```

For runner scripts, update the `game_title` argument where `GameAutomation` is created.

### Clicks Are Offset

Refresh the reference screenshot:

```bash
python3.12 high_res_catcher.py --once --update-reference
```

Then re-check coordinates with:

```bash
python3.12 get_coordinates.py --name test_point
python3.12 test.py --dry-run
```

Also confirm the game window size did not change after coordinate capture.

### Screenshot Analyzer Coordinates Feel Wrong

`get_coordinates.py` uses an autosized OpenCV window so image coordinates match screenshot pixels. If macOS or OpenCV scales the display unexpectedly, inspect a smaller screenshot or move the analyzer window to a non-scaled display.

### Automation Runs Too Fast

Increase the delay value after the previous action in `coordinates.py`. Delays are stored on the action that is about to execute:

```python
(pos['确认'], 2.0)
```

This waits two seconds before clicking `确认`.

## Development Checks

Syntax check:

```bash
python3.12 -m py_compile *.py
```

Route discovery:

```bash
python3.12 test.py --list
```

Default route estimate:

```bash
python3.12 test.py --analyze
```
