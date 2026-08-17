# Yanyu Daemon

Yanyu Daemon is a macOS automation toolkit for the game window named `烟雨江湖`. It records reference coordinates from screenshots, stores reusable route procedures in `coordinates.py`, executes those routes with scaled click/drag control, and can collect run data for later tuning.

The project is built around one practical loop:

1. Capture the current game window as a reference image.
2. Inspect that image and record coordinate points.
3. Build legacy route lists or record coordinate-verified smart procedures.
4. Dry-run and execute routes.
5. Learn real movement timing and repair only the procedure affected by a game update.

For the detailed Chinese gameplay model, verified Dali routes, scheduler timing, and multi-jump recording rules, read [GAMEPLAY_AUTOMATION.md](GAMEPLAY_AUTOMATION.md).

## Safety And Assumptions

- This project directly moves and clicks your mouse. Use dry-run and step mode before running a new route.
- Keep the game window size stable between capture, coordinate picking, and route execution.
- macOS must grant Accessibility and Screen Recording permissions to the terminal or Python application that runs these scripts.
- The scripts assume the target window title or owner contains `烟雨江湖` by default.
- Generated screenshots and logs can become large. They are ignored by git.
- Never convert an unverified movement route into a rapid multi-jump sequence. Record one successful jump with the dedicated F5 mode first.

## File Guide

- `test.py`: main final runner for selected routes or the default route sequence.
- `coordinates.py`: reference pixels, in-game resource coordinates, legacy routes, and smart-route drafts.
- `automation.py`: shared window lookup, scaling, click/drag execution, world-map feature alignment, status logging, and screenshot capture helpers.
- `smart_automation.py`: local OCR, stable-coordinate waits, text-aware clicks, timing learning, and smart procedure execution.
- `procedure_runner.py`: reads live state and tests one installed smart procedure outside the scheduler.
- `high_res_catcher.py`: captures the game window and can refresh `game_screenshot.png`.
- `get_coordinates.py`: opens a screenshot and prints coordinates by mouse click.
- `control_recorder.py`: records legacy snippets or installable coordinate-verified JSON procedures.
- `recording_analyzer.py`: summarizes legacy recordings and smart procedure coverage/timing.
- `route_analyzer.py`: analyzes stored routes without opening the game.
- `pattern_learner.py`: compares per-action screenshots from a route run to flag suspicious actions.
- `tracking_click.py`: 7x24 scheduler with per-task state, exact resource anchors, retries, and failure captures.
- `GAMEPLAY_AUTOMATION.md`: detailed Chinese manual and the game-action model derived from the recorded routes.
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

There are now two deliberately separate coordinate systems:

- `pos`: reference-image pixels for UI buttons and recorded ground clicks.
- `map_pos`: the in-game grid shown in the right sidebar, such as Dali `(32,18)`, cow `(28,11)`, and pig `(29,3)`.

Do not apply one fixed formula from `map_pos` to `pos`. The game camera follows the character in the middle of a map but becomes clamped near map edges. Buildings, cliffs, and blocked tiles also change the path. Smart procedures therefore retain the ground clicks from one real run and use local macOS Vision OCR to verify the resulting in-game coordinate before continuing.

## Standard Workflow

### 1. Capture The Current Window

Capture the current game window without changing the coordinate baseline:

```bash
python3.12 high_res_catcher.py --once
```

Only replace `game_screenshot.png` when intentionally recalibrating the full reference coordinate set:

```bash
python3.12 high_res_catcher.py --once --update-reference
```

After replacing it, dry-run and re-check key UI points because existing coordinates were measured against the previous image dimensions.

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

`test.py` also discovers installed smart procedures and expands their reusable segments:

```bash
python3.12 test.py dali_cow --dry-run
python3.12 test.py dali_pig
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
python3.12 test.py --no-sync-scheduler
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

- `F5`: start/end one atomic rapid-click or multi-jump group (smart recording).
- `F6`: require stable state verification after the previous action (smart recording).
- `F7`: mark the previous action as the exact refresh anchor (smart recording).
- `F8`: pause/resume recording.
- `F9`: stop and write files.

Output:

- `recordings/my_route_YYYYMMDD_HHMMSS.jsonl`
- `recordings/my_route_YYYYMMDD_HHMMSS.py`

The `.jsonl` file is structured training/control data. F6/F7 changes are appended as annotations that identify the affected action, so checkpoint and unique-anchor labels survive in the raw evidence. The `.py` file is a legacy route snippet.

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

## Coordinate-Verified Smart Procedures

Smart procedures are JSON files under `procedures/`. They remain compatible with the existing routes in `coordinates.py`, but add controls that delay-only tuples cannot provide:

- map and coordinate verification after movement;
- adaptive waiting until the coordinate is stable for two OCR samples;
- text-based button location with the recorded pixel as a fallback;
- an exact refresh anchor on the kill or harvest click.
- required/exact text checks, icon offsets, and post-action disappearance checks;
- ORB/RANSAC feature alignment for a world map whose pan offset depends on the current city;
- reusable `include` segments;
- atomic `rapid_clicks` for time-critical multi-jump input.

OCR runs locally through the macOS Vision framework. It does not upload screenshots and does not require another Python package beyond the installed PyObjC runtime.

Read the current game state without clicking:

```bash
python3.12 procedure_runner.py --read-state
```

For the current Dali example this should print output similar to:

```json
{"map": "大理", "coordinate": [28, 11]}
```

The known Dali world coordinates are stored in `map_pos`:

```python
map_pos = {
    '大理': {
        '马车落点': (32, 18),
        '牛': (28, 11),
        '猪': (29, 3),
    },
}
```

The installed `dali_cow` and `dali_pig` procedures reuse `_dali_travel_to_cow_area`. The world map is aligned against `assets/world_map_reference.jpg` before clicking Dali, so a persisted/current-city map pan does not move the click target. The cow route has 10 verified movement/loading checkpoints. The pig route adds six independently verified obstacle-aware steps from `(28,11)` to `(29,3)`.

The current cow flow has passed a full live run. The current 25-action pig flow has also passed a single-process scheduled run, including a `25.927s` gate wait, exact kill anchor, disappearance check, and save.

### Re-record And Install Dali Cow

Start the recorder immediately before manually performing the procedure. Include opening the carriage ticket, selecting Dali, walking through every necessary waypoint, selecting the cow, and confirming the kill:

```bash
python3.12 control_recorder.py \
  --name dali_cow \
  --smart \
  --install \
  --map 大理 \
  --start 32,18 \
  --target 28,11 \
  --interval-minutes 180 \
  --lead-seconds 75 \
  --before-route sleep1 \
  --after-route save
```

While recording:

- Wait until the character stops before issuing the next movement click. This gives OCR two stable samples and turns the click into a verified waypoint.
- Use as many ground waypoints as the obstacle layout requires. Do not replace a working obstacle-aware route with one long diagonal click.
- Press `F6` to force the current state to be checked after the last action.
- Immediately after the exact click that starts the resource refresh timer, press `F7`. For a cow this is normally the final kill/confirmation click, not the click that merely selects `牛`.
- Press `F8` to pause/resume and `F9` to finish.

The recorder writes:

- `recordings/dali_cow_*.jsonl`: raw mouse events;
- `recordings/dali_cow_*.states.jsonl`: OCR state samples and recognized text;
- `recordings/dali_cow_*.procedure.json`: timestamped smart procedure;
- `procedures/dali_cow.json`: installed procedure loaded by the scheduler.

If a click lands on or immediately beside recognized text such as `牛`, `羊`, `采集`, or `确认`, the procedure stores the text, original pixel, and any learned offset to the actionable icon. Critical actions can set `target_required` and `exact_text` to prevent unsafe fallback clicks.

For multi-jump recording, press `F5` before the takeoff click and again after safe landing. OCR is suspended inside the group, and replay uses absolute monotonic deadlines. See [the lightness section](GAMEPLAY_AUTOMATION.md#8-轻功与快速多段跳).

Inspect and quantify the result:

```bash
python3.12 recording_analyzer.py recordings/dali_cow_*.procedure.json
python3.12 procedure_runner.py --list
python3.12 procedure_runner.py dali_cow --dry-run
```

Run the complete scheduler-equivalent chain once before scheduling it:

```bash
python3.12 test.py sleep1 dali_cow save --log-jsonl runs/dali_cow.jsonl
```

The Dali procedures deliberately reject a start that is already on the Dali map: the world map replaces the current city's carriage icon with the player marker. `tracking_click.py` already runs `sleep1` first. For isolated inspection from another map, `procedure_runner.py dali_cow` remains available.

Successful verified waits are learned into `runtime_profiles.json`. The runner still waits for the actual coordinate; the learned value is used to choose a realistic timeout rather than replacing state verification with another fixed sleep.

A real smart run through `test.py` or `procedure_runner.py` also writes its exact refresh anchor and next target into `scheduler_state.json`. Dry-runs, failed runs, and procedures without an anchor do not change it. Use `--no-sync-scheduler` only when intentionally testing against a disposable account/state file.

### Add More Cows, Sheep, Or Collection Routes

Use a different procedure name and change `--map`, `--start`, `--target`, and `--interval-minutes`. `--install` is the only step needed to register it with `tracking_click.py`; no wildcard import or manual scheduler edit is required.

When a game update breaks one resource route, re-record only that named procedure with the same `--name --smart --install` command. The installed JSON is replaced while all unrelated scheduler tasks and their due times remain intact.

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

`tracking_click.py` is the 7x24 scheduler. It stores one schedule state per task in `scheduler_state.json`, writes structured events to `scheduler_events.jsonl`, and can capture screenshots into `scheduler_captures/`.

```bash
python3.12 tracking_click.py
```

Important behavior:

- Each scheduled task is now a route-level job such as `cow2`, `xigua`, `pingguo`, or `suancai`.
- Group names such as `1_hour`, `2_hour`, `3_hour`, `5_hour`, `6_hour`, and `daily` still work as command shortcuts.
- After a route-level task succeeds, its next due time is calculated from actual completion time, not from the originally planned start time.
- This matches game resources that refresh a fixed time after the successful harvest/kill click.
- If a task crashes, it is marked `failed`, a failure event is logged, a screenshot is captured, and the task retries after 10 minutes.
- Failed completion does not shift the long refresh anchor.
- Installed smart procedures are discovered from `procedures/*.json` and appear in the `smart` group.
- A smart task calculates `next_due` from its `F7`-marked click with millisecond precision. Legacy tasks still fall back to route completion time.
- `--dry-run` never changes `next_due`.
- Verified movement timing is learned in `runtime_profiles.json`; failures report expected, last, and recently seen coordinates.
- If a later save/cleanup action fails after the marked resource click, the task is recorded as `post_anchor_failed` and keeps the full refresh interval instead of retrying the kill after ten minutes.
- Running with `--only-task` keeps the saved due times of all inactive tasks intact.
- Smart tasks may start at `next_due - lead_seconds`, travel early, and wait at the resource-selection gate until the exact target refresh time.
- The observed pre-anchor duration updates `lead_seconds` without counting time spent waiting at the gate.
- The normal loop re-evaluates priorities after every task. Precise smart tasks rank ahead of overdue legacy backlog even when their own targets were missed during downtime; an upcoming precise target ranks ahead of an already missed one. The scheduler also reserves the final 120 seconds before a future precise start window. Tune this with `--precision-reserve-seconds`.
- Failure events include the smart action index, type, and label so one broken segment can be re-recorded.
- A global `<ctrl>+c` hotkey is enabled by default, including when the scheduler runs in a detached `screen` session. It requests a graceful stop, saves every task's current schedule, and writes `scheduler_hotkey_stop_requested` followed by `scheduler_stopped` to the JSONL log.
- Normal delays, OCR polling, and scheduled refresh waits stop immediately. An in-progress atomic `rapid_clicks` group finishes first so a multi-jump is not abandoned halfway through.

Change or disable the global stop hotkey:

```bash
python3.12 tracking_click.py --stop-hotkey '<ctrl>+<shift>+x'
python3.12 tracking_click.py --no-stop-hotkey
```

Foreground terminal `Ctrl-C` remains available. Keep custom pynput hotkey expressions quoted so the shell does not interpret angle brackets.

List scheduler groups and route-level tasks:

```bash
python3.12 tracking_click.py --list
```

Run only one task immediately:

```bash
python3.12 tracking_click.py --run-now cow2
```

Run one group immediately:

```bash
python3.12 tracking_click.py --run-now 3_hour
```

Wait for one selected task's next lead window, run it once, and exit:

```bash
python3.12 tracking_click.py --only-task dali_pig --wait-once
```

`--once` only checks tasks that are due now. `--wait-once` remains idle until the next selected task enters its lead window, which is useful for an exact refresh regression without leaving the daemon running.

Run selected task groups only:

```bash
python3.12 tracking_click.py --only-task 1_hour --only-task 3_hour
```

Skip a broken task while keeping the rest of the 7x24 scheduler alive:

```bash
python3.12 tracking_click.py --skip-task 3_hour
```

Dry-run a scheduled task without clicking:

```bash
python3.12 tracking_click.py --run-now 3_hour --dry-run
```

Use route-level screenshots when debugging:

```bash
python3.12 tracking_click.py --run-now 3_hour --capture route
```

Add per-action JSON records while debugging:

```bash
python3.12 tracking_click.py --run-now cow2 --capture route --log-actions
```

### Re-recording Failed Legacy Scheduler Functions

The following workflow is retained for old tuple routes. For an installed smart procedure, re-run the `control_recorder.py --smart --install` command above with the same name instead.

When a game update breaks one legacy route:

1. Stop the scheduler with the global `Ctrl-C` hotkey and confirm a `scheduler_stopped` event was written.
2. Run only the failed task in dry-run or route-capture mode:

```bash
python3.12 tracking_click.py --run-now cow2 --dry-run
python3.12 tracking_click.py --run-now cow2 --capture route
```

3. Identify the failed route from `scheduler_events.jsonl` and screenshots in `scheduler_captures/`.
4. Re-record just that route manually:

```bash
python3.12 control_recorder.py --name cow2_new
python3.12 recording_analyzer.py recordings/cow2_new_YYYYMMDD_HHMMSS.jsonl
```

5. Copy the generated route snippet into `coordinates.py`.
6. Replace the old route name in `TASKS` inside `tracking_click.py`, for example replacing the `cow2` task routes with `("sleep1", "cow2_new", "save")`.
7. Validate it:

```bash
python3.12 test.py cow2_new --dry-run
python3.12 tracking_click.py --run-now cow2 --only-task cow2
```

If the task was due while you were fixing it, edit `scheduler_state.json` or delete it to recalculate initial due times.

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

### Background Input Isolation

The scheduler process can run independently in `screen`, but the default `pyautogui` backend still shares the macOS cursor and foreground application.

This client was tested with three supported no-cursor approaches:

- `CGEventPostToPid` with private and HID event sources;
- explicit target PID and window routing fields;
- macOS Accessibility `AXPress` discovery.

The UIKit-based game client ignored PID-targeted mouse events even while frontmost, and its Accessibility tree exposes the game canvas as one generic element rather than individual controls. A global HID event works, but moves the system cursor while the event is active. Therefore it is not a safe background backend for simultaneous desktop use.

Run the read-only Accessibility inspection:

```bash
python3.12 ax_tree_probe.py
```

Run a PID-targeted single-click probe after stopping the scheduler:

```bash
python3.12 quartz_input_probe.py 包裹 --source hid --set-routing-fields --prime-move
```

`--delivery hid-tap` is deliberately restricted to a confirmed frontmost game window. It is diagnostic only and restores the original cursor position, but it still occupies the cursor during each event.

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
