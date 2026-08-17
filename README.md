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
- `test.py` is the production daily-task runner despite its filename. Do not use it as the automated test suite; isolated tests live under `tests/`.
- `tracking_click.py` pauses at the next safe action boundary when physical mouse or keyboard input is detected.
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
- `runtime_control.py`: physical-input detection, synthetic-input suppression, persisted checkpoints, and from-the-beginning recovery tickets.
- `game_session.py`: login-page recovery, latest-server-save confirmation, remote-logout detection, and `子时` verification.
- `resource_catalog.py`: refresh policies, legacy action-anchor inference, per-point state, and acquisition ledger.
- `monitor_server.py` and `web/`: local resource dashboard and scheduler controls.
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

Verify imports and route discovery without invoking the production daily runner:

```bash
python3.12 -m unittest discover -s tests -v
python3.12 route_analyzer.py pig1 cow2
python3.12 procedure_runner.py --list
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

`test.py` is the production daily runner. Its name is historical; the commands below can perform real game actions unless `--dry-run` is present.

It supports:

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
- Pig/cow/sheep refresh timing starts at the exact slaughter click. The Changbai and Tianshan bear timers start at the named movement click that enters their auto-battle. Fruit, shrimp, and other collection routes retain their configured intervals and start at the first resource confirmation click.
- Legacy tuple routes infer this exact action from the stable `边栏 -> 空白 -> 确认` interaction shape. Repeated dialog confirmations are not counted as additional refresh anchors.
- Pen animals (`pig1`, `pig2`, `dali_pig`, and `dali_cow`) use 60 minutes. The legacy `bear2/3/4/5/6/8/9/10/11/12/13/15` names are map-cow routes for Gusu, Hangzhou, Quanzhou, Luoyang, Nanyangdu, Luoxia, Emei, Mingyue, Longquan, Shuangwang, Huashan, and Fengming; each records one cow on a 60-minute timer.
- `bear1` contains two independent resources: the Changbai bear at action 15 and the Changbai cow at action 21. `bear_tianshan` contains the Tianshan bear at action 13. These are the only two wild-bear resource points.
- Sai Bei, Tianshan, Sunset Ranch, and the other ranch routes (`bear7`, `bear14`, `cow1`, and `cow2`) use 180 minutes.
- Multi-point routes persist one timer and one learned `anchor_offset_seconds` per resource point. Route start is selected from the earliest point-specific travel window; a point due within 300 seconds waits at its interaction gate, while a farther cooling point is skipped for this pass.
- `bear7`, `cow1`, and `bear14` must complete `sleep1` and visually verify the cyan `子` period. The period is checked again before every Sai Bei/Qilian/Tianshan livestock interaction; an unreadable or daytime period fails closed before slaughter.
- Multi-point routes upload an in-game intermediate save after every successful point except the final point, which is followed by the normal `save` route. Local scheduler state and the acquisition ledger are still persisted immediately at every exact anchor.
- Routes without a proven action anchor, currently home maintenance, retain the conservative task-completion fallback.
- If a task crashes, it is marked `failed`, a failure event is logged, a screenshot is captured, and the task retries after 10 minutes.
- Failed completion does not shift the long refresh anchor.
- Installed smart procedures are discovered from `procedures/*.json` and appear in the `smart` group.
- A smart task calculates `next_due` from its `F7`-marked click with millisecond precision. Supported legacy resource tasks now use inferred action anchors with the same precision.
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

### Human Takeover And Safe Restart

Physical input monitoring is enabled by default. Synthetic `pyautogui` events are wrapped in an automation-input guard, so the scheduler does not pause itself.

When a real mouse or keyboard event is detected:

1. The scheduler stops before dispatching the next non-atomic action.
2. `runtime_control.json` records the task, route number, next action number, action label, pause reason, and pause time.
3. macOS Vision captures the game map and coordinate when readable. The old route/action remains audit evidence only.
4. Relative movement/UI delays freeze. Existing resource due times continue to follow wall-clock time because the game refreshes while the scheduler is paused.
5. Press `Ctrl-Alt-R` or use `安全从头恢复` on the authenticated dashboard. Do not manually reconstruct the old mid-route screen.
6. If the client is on a login screen, recovery opens account login, submits only the already-saved credentials, closes the update notice, checks the agreement, starts the selected character, and confirms the prompt that force-downloads the newer server save. It never selects a local upload.
7. The old action checkpoint is marked abandoned. After the quiet countdown, the interrupted task restarts at route 1/action 1. Before any carriage route, a readable map and coordinate are required.
8. If the carriage destination is the current map and its icon is hidden by the player marker, the runner first travels to neutral Luoyang or Nanyangdu, verifies that landing, and then repeats the complete target route.

The accumulated pause duration is stored per task and excluded from movement and point-arrival learning. Resource refresh clocks continue during desktop use. Cooldown points already anchored before a pause are skipped on the restarted pass; ready or unknown points are handled normally. If every active resource is ready, recovery drops the interrupted-task priority and starts a normal full queue from its first due task.

The session watchdog checks for provider login, account login, home, server-download, and remote-login screens every 15 seconds. A match pauses the scheduler and preserves the current task context. The login state machine is idempotent, so a process interruption at an intermediate login page continues by detecting that page rather than replaying earlier clicks.

Useful options:

```bash
python3.12 tracking_click.py --resume-hotkey '<ctrl>+<alt>+r'
python3.12 tracking_click.py --resume-delay-seconds 5
python3.12 tracking_click.py --no-human-input-pause
python3.12 tracking_click.py --session-check-seconds 10
python3.12 tracking_click.py --resource-gate-max-wait-seconds 300
python3.12 tracking_click.py --no-intermediate-saves
```

Normal recovery requires a recognized login page or a readable in-game map/coordinate. `强制从头恢复` skips that recognition only; it still cannot resume a saved middle action. Atomic `rapid_clicks` groups cannot pause halfway; a pending pause is honored immediately after the group finishes.

### Resource State And Acquisition Ledger

`scheduler_state.json` now includes:

- `interval_minutes`, `resource_category`, and `resource_name`;
- `human_pause_seconds` accumulated while that task was active;
- `resource_points`, with one `last_refresh_anchor` and `next_due` per animal or collection point;
- per-point `anchor_offset_seconds`, `anchor_offset_samples`, and inter-point `segment_seconds` learned from real runs after subtracting human pauses and scheduled gate waits;
- `partial_failed` when a multi-point route fails before every expected anchor is reached.
- `retry_not_before`, persisted independently from point refresh clocks, so a failed multi-point route cannot bypass its retry delay after a scheduler restart.

Every exact anchor appends a `resource_acquired` event to `resource_history.jsonl` immediately. Quantities are acquisition-event estimates, not OCR-confirmed inventory counts. Manual positive or negative adjustments can be added from the dashboard and remain distinguishable from estimated records.

On first startup, old state is migrated without deleting history. In particular, an old three-hour `dali_cow` target is recomputed as one hour from its last exact slaughter anchor.

The one-time bear/cow correction preserves the old append-only ledger, adds compensating audit records, reclassifies regional cows, and backfills the two real bear points from action logs:

```bash
python3.12 migrate_bear_cow_model.py \
  --event-file runs/tracking_current/events.jsonl
```

### Web Monitor

The scheduler serves the local dashboard by default:

```text
http://127.0.0.1:8765
```

It shows task and resource-point cooldowns, current checkpoint, recent scheduler events, acquisition history, quantity summaries, and pause/resume/stop controls. The JSON endpoints are:

```text
GET  /api/status
GET  /api/events?limit=200
GET  /api/acquisitions?limit=200
GET  /api/summary
POST /api/control/pause
POST /api/control/resume
POST /api/control/force-resume
POST /api/control/stop
POST /api/acquisitions/adjust
```

Every POST must include `X-Yanyu-Request: dashboard`; authenticated LAN requests must also include HTTP Basic credentials. The bundled dashboard supplies the header automatically.

`POST /api/control/stop` terminates the scheduler process, including its attached 8765 control server. An already-open browser page then reports `Failed to fetch`; the independent 8766 read-only monitor remains available. Restart the attached control service without dispatching overdue game tasks by adding `--start-paused`, then use `安全从头恢复` when ready.

```bash
python3.12 tracking_click.py --start-paused
```

Start a monitor-only process without opening or controlling the game:

```bash
python3.12 monitor_server.py --host 127.0.0.1 --port 8765
```

Monitor-only mode has no scheduler controls. A loopback-only monitor can accept acquisition adjustments; an unauthenticated monitor bound to a LAN address rejects every POST and is strictly read-only.

### Authenticated LAN Control

Direct LAN control requires both a mode-600 password file and TLS. The server refuses to expose an attached scheduler on a non-loopback address when either protection is missing. Create the private files once on the game Mac:

```bash
CONTROL_DIR="$HOME/.config/yanyu-daemon"
mkdir -p "$CONTROL_DIR"
chmod 700 "$CONTROL_DIR"
umask 077
python3.12 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$CONTROL_DIR/web-password"
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout "$CONTROL_DIR/web-key.pem" \
  -out "$CONTROL_DIR/web-cert.pem" \
  -subj '/CN=192.168.1.3' \
  -addext 'subjectAltName=IP:192.168.1.3,IP:127.0.0.1,DNS:localhost'
chmod 600 "$CONTROL_DIR/web-password" "$CONTROL_DIR/web-key.pem"
```

Start the scheduler with authenticated HTTPS control:

```bash
python3.12 tracking_click.py \
  --web-host 0.0.0.0 \
  --web-port 8765 \
  --web-auth-user yanyu \
  --web-auth-token-file "$HOME/.config/yanyu-daemon/web-password" \
  --web-tls-cert-file "$HOME/.config/yanyu-daemon/web-cert.pem" \
  --web-tls-key-file "$HOME/.config/yanyu-daemon/web-key.pem"
```

Open `https://192.168.1.3:8765` from another LAN device. The HTTP Basic username is `yanyu`; read the generated password on the game Mac with:

```bash
cat "$HOME/.config/yanyu-daemon/web-password"
```

The generated certificate is self-signed. Verify its SHA-256 fingerprint before accepting or importing it on another device:

```bash
openssl x509 -in "$HOME/.config/yanyu-daemon/web-cert.pem" -noout -fingerprint -sha256
```

Authenticated command-line control must include both Basic credentials and the custom write header:

```bash
PASSWORD=$(cat "$HOME/.config/yanyu-daemon/web-password")

# Inspect the checkpoint first. --insecure is only for the locally generated certificate.
curl --insecure --user "yanyu:$PASSWORD" https://127.0.0.1:8765/api/status

# Safe recovery handles a recognized login screen or any readable in-game location,
# abandons the old middle action, and restarts from route 1/action 1.
curl --insecure --user "yanyu:$PASSWORD" \
  -H 'X-Yanyu-Request: dashboard' -X POST \
  https://127.0.0.1:8765/api/control/resume

# Force still restarts from the beginning; it only skips session recognition.
curl --insecure --user "yanyu:$PASSWORD" \
  -H 'X-Yanyu-Request: dashboard' -X POST \
  https://127.0.0.1:8765/api/control/force-resume
```

The public `http://192.168.1.3:8766` page remains monitor-only. Do not forward either port from the router or expose the self-signed service to the internet. An SSH tunnel to a loopback-only service remains the preferred option outside the trusted LAN.

Change the integrated address or disable the service:

```bash
python3.12 tracking_click.py --web-port 8877
python3.12 tracking_click.py --no-web
```

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

1. Pause from the dashboard or stop with the global `Ctrl-C` hotkey. Use `--skip-task` for that task while leaving unrelated resources scheduled.
2. Inspect the failed action, current checkpoint, JSONL event, and failure screenshot:

```bash
python3.12 monitor_server.py
python3.12 recording_analyzer.py procedures/cow2.json
python3.12 route_analyzer.py cow2
```

3. For a smart procedure, record the broken segment with the same procedure name and `--smart --install`; the timestamped raw evidence remains under `recordings/` while the installed JSON is replaced.
4. For a legacy tuple route, re-record only that route:

```bash
python3.12 control_recorder.py --name cow2_new
python3.12 recording_analyzer.py recordings/cow2_new_YYYYMMDD_HHMMSS.jsonl
```

5. Copy the generated route snippet into `coordinates.py`.
6. Replace the old route name in `TASKS` inside `tracking_click.py`, for example replacing the `cow2` task routes with `("sleep1", "cow2_new", "save")`.
7. Run isolated static and unit checks before any live game execution:

```bash
python3.12 route_analyzer.py cow2_new
python3.12 -m unittest discover -s tests -v
```

8. When the resource is available and the character is in a known state, perform one explicit live run of only that task, then remove `--skip-task`.

Do not delete `scheduler_state.json` during a repair. It contains exact per-point refresh anchors. A failed task retains its prior timers and is visible as `failed`, `partial_failed`, or `post_anchor_failed` in the dashboard.

`smarter_click.py` uses an in-memory schedule:

```bash
python3.12 smarter_click.py
```

For route development, use `control_recorder.py`, `recording_analyzer.py`, `route_analyzer.py`, and the `tests/` suite first. Treat `test.py` as a production daily-task entry point, not as the repository's test command.

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

The test was repeated after granting Python Accessibility control on 2026-08-17. The Accessibility tree then exposed one generic pressable `AXTextArea`, but still no game controls. Private-source and HID-source PID events were aimed at the visible account-login button while Terminal or ToDesk remained frontmost: both preserved the cursor exactly and both were ignored by the game. A global HID event works, but moves the system cursor while the event is active. The permission is still required for foreground automation and input detection; it does not make this UIKit canvas accept background clicks.

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
python3.12 route_analyzer.py affected_route
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
python3.12 -m py_compile automation.py smart_automation.py game_session.py tracking_click.py runtime_control.py resource_catalog.py monitor_server.py
```

Route discovery:

```bash
python3.12 procedure_runner.py --list
python3.12 tracking_click.py --list
```

Route estimates and isolated tests:

```bash
python3.12 route_analyzer.py pig1 cow2 bear14
python3.12 -m unittest discover -s tests -v
```
