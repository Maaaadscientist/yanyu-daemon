import json
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class ResourcePolicy:
    task_name: str
    display_name: str
    category: str
    interval_minutes: float
    anchor_mode: str = "inferred_action"
    confirmation_names: tuple[str, ...] = ("确认", "确认1")
    point_labels: tuple[str, ...] = ()
    estimated_quantity: float = 1.0
    unit: str = "次"


@dataclass(frozen=True)
class ResourceAnchorSpec:
    task_name: str
    route_name: str
    action_index: int
    point_id: str
    label: str
    category: str
    estimated_quantity: float = 1.0
    unit: str = "次"


PEN_LIVESTOCK = {
    "pig1": ("南岭猪圈", ("南岭猪",)),
    "pig2": ("成都猪圈", ("成都猪",)),
    "dali_pig": ("大理猪圈", ("大理猪",)),
    "dali_cow": ("大理牛棚", ("大理牛",)),
}

RANCH_LIVESTOCK = {
    "bear7": ("塞北牧场", tuple(f"塞北牲畜 {index}" for index in range(1, 6))),
    "cow2": ("落日牧场", tuple(f"落日牧场牲畜 {index}" for index in range(1, 4))),
    "cow1": ("祁连/塞北牧场", ("祁连牲畜", "塞北牲畜")),
    "bear14": ("天山牧场", ("天山羊 1", "天山羊 2", "天山牛", "天山羊 3")),
}

WILD_BEAR_TASKS = tuple(
    f"bear{index}" for index in (1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 15)
)

DISPLAY_NAMES = {
    "xigua": "龙泉镇西瓜",
    "xiangjiao": "明月峰香蕉",
    "shanzha": "泰山山楂",
    "pingguo": "华山苹果",
    "changbaipingguo": "长白山苹果",
    "lianou": "明月峰莲藕",
    "jianshui": "井水",
    "hexia1": "龙泉镇河虾",
    "hexia2": "姑苏河虾",
    "suancai": "长白山酸菜",
    "jiazhai": "家宅维护",
    "bear_tianshan": "天山中转路线",
}

FRUIT_TASKS = {"xigua", "xiangjiao", "shanzha", "pingguo", "changbaipingguo"}
COLLECTION_TASKS = {"lianou", "jianshui", "hexia1", "hexia2", "suancai"}

GENERIC_POINT_NAMES = {
    "烟雨江湖",
    "包裹",
    "叫唤马车",
    "地图上端",
    "地图下端",
    "左上角",
    "左下角",
    "右上角",
    "右下角",
    "边栏1",
    "边栏2",
    "空白",
    "确认",
    "确认1",
    "确认2",
}


def policy_for_task(task_name: str, default_interval_minutes: float) -> ResourcePolicy:
    if task_name in PEN_LIVESTOCK:
        display_name, labels = PEN_LIVESTOCK[task_name]
        return ResourcePolicy(
            task_name,
            display_name,
            "pen_livestock",
            60.0,
            point_labels=labels,
            unit="只",
        )
    if task_name in RANCH_LIVESTOCK:
        display_name, labels = RANCH_LIVESTOCK[task_name]
        return ResourcePolicy(
            task_name,
            display_name,
            "ranch_livestock",
            180.0,
            point_labels=labels,
            unit="只",
        )
    if task_name in WILD_BEAR_TASKS:
        return ResourcePolicy(
            task_name,
            f"野熊路线 {task_name.removeprefix('bear')}",
            "wild_bear",
            float(default_interval_minutes),
            point_labels=(f"野熊 {task_name.removeprefix('bear')}",),
            unit="只",
        )
    if task_name in FRUIT_TASKS:
        return ResourcePolicy(
            task_name,
            DISPLAY_NAMES.get(task_name, task_name),
            "fruit",
            float(default_interval_minutes),
            confirmation_names=("确认", "确认1", "确认2"),
            unit="次",
        )
    if task_name in COLLECTION_TASKS:
        return ResourcePolicy(
            task_name,
            DISPLAY_NAMES.get(task_name, task_name),
            "collection",
            float(default_interval_minutes),
            confirmation_names=("确认", "确认1", "确认2"),
            unit="次",
        )
    if task_name == "jiazhai":
        return ResourcePolicy(
            task_name,
            DISPLAY_NAMES[task_name],
            "home_maintenance",
            float(default_interval_minutes),
            anchor_mode="task_completion",
        )
    if task_name == "bear_tianshan":
        return ResourcePolicy(
            task_name,
            DISPLAY_NAMES[task_name],
            "travel",
            float(default_interval_minutes),
            anchor_mode="task_completion",
        )
    return ResourcePolicy(
        task_name,
        DISPLAY_NAMES.get(task_name, task_name),
        "other",
        float(default_interval_minutes),
        anchor_mode="task_completion",
    )


def infer_legacy_anchor_specs(
    task_name: str,
    route_name: str,
    actions: Sequence,
    named_points: Mapping[str, tuple[int, int]],
    *,
    default_interval_minutes: float,
) -> tuple[ResourceAnchorSpec, ...]:
    policy = policy_for_task(task_name, default_interval_minutes)
    if policy.anchor_mode != "inferred_action" or route_name != task_name:
        return ()

    blank = named_points.get("空白")
    sidebars = {named_points.get("边栏1"), named_points.get("边栏2")}
    confirmations = {
        named_points[name]
        for name in policy.confirmation_names
        if name in named_points
    }
    reverse_names = _reverse_point_names(named_points)
    matches = []
    for offset in range(2, len(actions)):
        target = actions[offset][0]
        if not _is_point(target) or target not in confirmations:
            continue
        previous = actions[offset - 1][0]
        before_previous = actions[offset - 2][0]
        if previous != blank or before_previous not in sidebars:
            continue
        matches.append(offset)

    specs = []
    for sequence, offset in enumerate(matches, start=1):
        inferred = _nearest_route_label(actions, offset - 2, reverse_names)
        label = (
            policy.point_labels[sequence - 1]
            if sequence <= len(policy.point_labels)
            else f"{inferred or policy.display_name} {sequence}"
        )
        specs.append(
            ResourceAnchorSpec(
                task_name=task_name,
                route_name=route_name,
                action_index=offset + 1,
                point_id=f"{task_name}:{sequence}",
                label=label,
                category=policy.category,
                estimated_quantity=policy.estimated_quantity,
                unit=policy.unit,
            )
        )
    return tuple(specs)


def smart_anchor_specs(
    task_name: str,
    procedure: Mapping,
    *,
    default_interval_minutes: float,
) -> tuple[ResourceAnchorSpec, ...]:
    policy = policy_for_task(task_name, default_interval_minutes)
    specs = []
    for sequence, (index, action) in enumerate(
        (
            (index, action)
            for index, action in enumerate(procedure.get("actions", ()), start=1)
            if action.get("refresh_anchor")
        ),
        start=1,
    ):
        specs.append(
            ResourceAnchorSpec(
                task_name=task_name,
                route_name=str(procedure.get("name", task_name)),
                action_index=index,
                point_id=f"{task_name}:{sequence}",
                label=(
                    policy.point_labels[sequence - 1]
                    if sequence <= len(policy.point_labels)
                    else str(action.get("label") or f"{policy.display_name} {sequence}")
                ),
                category=policy.category,
                estimated_quantity=policy.estimated_quantity,
                unit=policy.unit,
            )
        )
    return tuple(specs)


def next_due_for_points(
    resource_points: Mapping[str, Mapping],
    *,
    fallback_anchor: datetime,
    interval_minutes: float,
    padding_seconds: float = 0.0,
) -> datetime:
    due_times = []
    for point in resource_points.values():
        value = point.get("next_due")
        if isinstance(value, datetime):
            due_times.append(value)
        elif value:
            try:
                due_times.append(datetime.fromisoformat(str(value)))
            except ValueError:
                pass
    if due_times:
        return max(due_times)
    return fallback_anchor + timedelta(minutes=interval_minutes, seconds=padding_seconds)


def resource_status(next_due: datetime | None, last_status: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if last_status in {"failed", "post_anchor_failed"}:
        return "failed"
    if next_due is None:
        return "unknown"
    if now >= next_due:
        return "ready"
    return "cooldown"


class ResourceLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record_anchor(
        self,
        *,
        task_name: str,
        spec: ResourceAnchorSpec,
        anchored_at: datetime,
        next_due: datetime,
        source: str,
    ) -> dict:
        record = {
            "id": uuid.uuid4().hex,
            "event": "resource_acquired",
            "time": anchored_at.isoformat(timespec="milliseconds"),
            "task": task_name,
            "point_id": spec.point_id,
            "point_label": spec.label,
            "category": spec.category,
            "quantity": spec.estimated_quantity,
            "unit": spec.unit,
            "estimated": True,
            "source": source,
            "next_due": next_due.isoformat(timespec="milliseconds"),
        }
        self._append(record)
        return record

    def record_adjustment(
        self,
        *,
        task_name: str,
        category: str,
        quantity: float,
        unit: str = "次",
        note: str = "",
    ) -> dict:
        record = {
            "id": uuid.uuid4().hex,
            "event": "resource_adjustment",
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "task": str(task_name),
            "point_id": None,
            "point_label": "手工调整",
            "category": str(category),
            "quantity": float(quantity),
            "unit": str(unit),
            "estimated": False,
            "source": "web_manual_adjustment",
            "note": str(note)[:500],
            "next_due": None,
        }
        self._append(record)
        return record

    def recent(self, limit: int = 200) -> list[dict]:
        return read_jsonl_tail(self.path, limit=limit)

    def summary(self) -> dict:
        records = self.recent(limit=100000)
        by_task = defaultdict(float)
        by_category = defaultdict(float)
        today_by_task = defaultdict(float)
        today = datetime.now().date()
        for record in records:
            try:
                quantity = float(record.get("quantity", 0.0))
            except (TypeError, ValueError):
                continue
            task = str(record.get("task") or "unknown")
            category = str(record.get("category") or "other")
            by_task[task] += quantity
            by_category[category] += quantity
            try:
                occurred = datetime.fromisoformat(str(record.get("time"))).date()
            except (TypeError, ValueError):
                occurred = None
            if occurred == today:
                today_by_task[task] += quantity
        return {
            "records": len(records),
            "by_task": dict(sorted(by_task.items())),
            "by_category": dict(sorted(by_category.items())),
            "today_by_task": dict(sorted(today_by_task.items())),
        }

    def _append(self, record: Mapping) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(dict(record), ensure_ascii=False) + "\n")


def read_jsonl_tail(path: str | Path, *, limit: int = 200) -> list[dict]:
    path = Path(path)
    if not path.exists() or limit <= 0:
        return []
    records = deque(maxlen=min(100000, int(limit)))
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        return []
    return list(records)


def _reverse_point_names(named_points: Mapping[str, tuple[int, int]]) -> dict[tuple[int, int], list[str]]:
    reverse = defaultdict(list)
    for name, point in named_points.items():
        if _is_point(point):
            reverse[tuple(point)].append(str(name))
    return reverse


def _nearest_route_label(actions: Sequence, before_index: int, reverse_names: Mapping) -> str | None:
    for offset in range(before_index - 1, -1, -1):
        target = actions[offset][0]
        if not _is_point(target):
            continue
        names = [name for name in reverse_names.get(tuple(target), ()) if name not in GENERIC_POINT_NAMES]
        if names:
            return names[0]
    return None


def _is_point(value) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, int) for item in value)
    )
