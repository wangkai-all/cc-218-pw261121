"""仓内作业分析 — 数据模型。

只依赖标准库。拓扑由「实体（货架通道/主通道/复核区/月台）+ 步道图」组成，
包含版本有效区间；货位挂在货架通道实体上。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def parse_ts(value: str | int | float | None) -> float | None:
    """解析 ISO8601 或 epoch 秒，统一为 epoch 秒（UTC）。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fmt_ts(epoch: float | None) -> str:
    if epoch is None:
        return "—"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fmt_clock(epoch: float | None) -> str:
    if epoch is None:
        return "—"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M")


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True)
class Entity:
    """可被占用的物理单元：货架通道、主通道、复核区、月台。"""

    id: str
    name: str
    kind: str  # rack | cross | staging | dock | buffer
    zone: str
    capacity: int
    rect: Rect
    aliases: tuple[str, ...] = ()

    @property
    def is_rack(self) -> bool:
        return self.kind == "rack"


@dataclass(frozen=True)
class GraphNode:
    id: str
    x: float
    y: float
    entity: str


@dataclass(frozen=True)
class Edge:
    a: str
    b: str
    distance_m: float


@dataclass(frozen=True)
class Location:
    id: str
    entity: str
    zone: str
    x: float
    y: float
    node: str
    local_m: float  # 从货位到通道口节点的步行距离（米）
    kind: str = "bin"  # bin | gate | stage | dock


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    kind: str
    color: int  # 分类色槽位，0 表示中性


@dataclass
class Topology:
    version_id: str
    label: str
    effective_from: float
    effective_to: float
    entities: dict[str, Entity] = field(default_factory=dict)
    zones: dict[str, Zone] = field(default_factory=dict)
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    locations: dict[str, Location] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)  # 旧货位 -> 新货位
    changes: tuple[str, ...] = ()

    def contains(self, t: float) -> bool:
        return self.effective_from <= t < self.effective_to

    def entity_for_location(self, loc_id: str) -> str:
        loc = self.locations.get(loc_id)
        return loc.entity if loc else "unknown"

    def zone_for_location(self, loc_id: str) -> str:
        loc = self.locations.get(loc_id)
        if not loc:
            return "unknown"
        return loc.zone


@dataclass(frozen=True)
class Task:
    id: str
    wave_id: str
    zone: str
    created_at: float
    released_at: float
    due_at: float | None = None
    cancelled_at: float | None = None
    cancel_reason: str = ""
    order_ids: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    release_rank: int = 0
    priority: str = "normal"
    cancel_mode: str = "none"  # none | pre | mid
    cancel_offset_s: float | None = None

    def is_cancelled(self, t: float | None = None) -> bool:
        if self.cancelled_at is None:
            return False
        if t is None:
            return True
        return self.cancelled_at <= t


@dataclass(frozen=True)
class ScanEvent:
    id: str
    ts: float
    picker_id: str
    location_id: str
    task_id: str | None
    device_id: str
    source: str = "handheld"  # handheld | gantry | backup
    scan_kind: str = "pick"  # pick | confirm | repeat | manual
    duplicate_of: str | None = None
    note: str = ""


@dataclass(frozen=True)
class ReleasePlan:
    id: str
    name: str
    description: str
    strategy: str  # zone_batched | zone_interleaved | ...
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class Run:
    id: str
    name: str
    plan_id: str
    plan_name: str
    window_start: float
    window_end: float
    pickers: tuple[str, ...]
    tasks: tuple[Task, ...]
    scans: tuple[ScanEvent, ...]
    note: str = ""
