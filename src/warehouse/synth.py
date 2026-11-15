"""本地合成数据生成器。

生成两版仓内拓扑、作业单、拣货扫码流水、两套发单方案的回放数据与真值。
所有随机量由固定种子驱动，输出可复现。

覆盖的现场异常：
- 重复扫码（同一货位 1–3 秒内重复上报）
- 扫码器短时故障（某台设备 15 分钟无上报）
- 任务取消（发单前取消 / 取货中途取消）
- 长时间无扫描（休息，位置不可知）
- 拓扑改版（容量、区域归属、货位条码）
- 货位条码别名（改版后仍扫到旧条码）
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from .models import Task
from .topology import load_topology

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

AISLES = "ABCDEFGH"
RACK_W = 14.0
RACK_H = 10.0
BLOCK_X = (24.0, 42.0, 60.0, 78.0)
ROW_Y = {"fast": 8.0, "cold": 24.0}
SPINE_X = 17.0

ZONES = [
    {"id": "fast", "name": "快拣 A 区", "kind": "picking", "color": 1},
    {"id": "cold", "name": "冷链 B 区", "kind": "picking", "color": 2},
    {"id": "staging", "name": "复核打包区", "kind": "staging", "color": 3},
    {"id": "dock", "name": "越库 / 月台", "kind": "dock", "color": 0},
    {"id": "main", "name": "主通道", "kind": "buffer", "color": 0},
]

ZONE_OF_AISLE = {
    "v1": {"A": "fast", "B": "fast", "C": "fast", "D": "fast",
           "E": "cold", "F": "cold", "G": "cold", "H": "cold"},
    "v2": {"A": "fast", "B": "fast", "C": "fast", "D": "cold",
           "E": "cold", "F": "cold", "G": "cold", "H": "cold"},
}
CAPACITY = {
    "v1": {"A": 2, "B": 2, "C": 2, "D": 2, "E": 2, "F": 2, "G": 2, "H": 2},
    "v2": {"A": 3, "B": 2, "C": 1, "D": 2, "E": 2, "F": 2, "G": 2, "H": 2},
}
RACK_NAMES = {
    "v1": {a: f"{a} 通道" for a in AISLES},
    "v2": {
        "A": "A 通道",
        "B": "B 通道",
        "C": "C 通道（脚手架限行）",
        "D": "D 通道（冷链扩容）",
        "E": "E 通道",
        "F": "F 通道",
        "G": "G 通道",
        "H": "H 通道",
    },
}

BASE_DAY1 = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)
BASE_DAY2 = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)
WINDOW_SECONDS = 5400

PICKERS_DAY1 = ["P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08"]
PICKERS_DAY2 = ["P01", "P02", "P03", "P04", "P05", "P06", "P07"]
DEVICES = {p: f"HH-{100 + i + 1}" for i, p in enumerate(PICKERS_DAY1)}


def _aisle_xy(aisle: str) -> tuple[float, float]:
    idx = AISLES.index(aisle)
    row = "fast" if idx < 4 else "cold"
    col = idx % 4
    return BLOCK_X[col], ROW_Y[row]


def topology_v1() -> dict:
    entities = []
    nodes = []
    edges = []
    locations = []
    # 主通道纵向脊
    entities.append({
        "id": "MAIN",
        "name": "主通道",
        "kind": "cross",
        "zone": "main",
        "capacity": 6,
        "rect": {"x": 14.0, "y": 5.0, "w": 6.0, "h": 52.0},
    })
    for i, y in enumerate((7.0, 17.0, 28.0, 40.0, 53.0)):
        nodes.append({"id": f"S{i}", "x": SPINE_X, "y": y, "entity": "MAIN"})
        if i:
            edges.append({"a": f"S{i - 1}", "b": f"S{i}", "distance_m": abs(y - (7.0, 17.0, 28.0, 40.0, 53.0)[i - 1])})
    for i, aisle in enumerate(AISLES):
        x, y = _aisle_xy(aisle)
        zone = ZONE_OF_AISLE["v1"][aisle]
        entities.append({
            "id": f"AISLE_{aisle}",
            "name": RACK_NAMES["v1"][aisle],
            "kind": "rack",
            "zone": zone,
            "capacity": CAPACITY["v1"][aisle],
            "rect": {"x": x, "y": y, "w": RACK_W, "h": RACK_H},
        })
        node_id = f"BLK_{aisle}"
        nodes.append({"id": node_id, "x": x - 2.5, "y": y + RACK_H / 2, "entity": f"AISLE_{aisle}"})
        spine = "S1" if zone == "fast" else "S2"
        edges.append({"a": node_id, "b": spine, "distance_m": (x - 2.5) - SPINE_X})
        # 通道内部：4 排 × 2 面 × 3 位，按网格落在货架块内
        for row in range(1, 5):
            for side in (0, 1):
                for bay in range(1, 4):
                    loc_id = f"{aisle}{row}-{'L' if side == 0 else 'R'}{bay:02d}"
                    lx = x + 3.2 + (side * 3 + (bay - 1)) * 1.35
                    ly = y + 0.9 + (row - 1) * 2.25
                    locations.append({
                        "id": loc_id,
                        "entity": f"AISLE_{aisle}",
                        "zone": zone,
                        "x": round(lx, 2),
                        "y": round(ly, 2),
                        "node": node_id,
                        "local_m": 6.0,
                        "kind": "bin",
                    })
    entities.append({
        "id": "STAGE",
        "name": "复核打包区",
        "kind": "staging",
        "zone": "staging",
        "capacity": 5,
        "rect": {"x": 24.0, "y": 44.0, "w": 68.0, "h": 12.0},
    })
    entities.append({
        "id": "DOCK",
        "name": "越库 / 月台",
        "kind": "dock",
        "zone": "dock",
        "capacity": 3,
        "rect": {"x": 2.0, "y": 10.0, "w": 10.0, "h": 13.0},
    })
    nodes.append({"id": "STAGE_N", "x": 58.0, "y": 47.0, "entity": "STAGE"})
    nodes.append({"id": "DOCK_N", "x": 7.0, "y": 16.5, "entity": "DOCK"})
    edges.append({"a": "S4", "b": "STAGE_N", "distance_m": 42.0})
    edges.append({"a": "S1", "b": "DOCK_N", "distance_m": 10.0})
    for i in range(4):
        locations.append({
            "id": f"STG-{i + 1:02d}", "entity": "STAGE", "zone": "staging",
            "x": 32.0 + i * 16.0, "y": 50.0, "node": "STAGE_N", "local_m": 8.0,
            "kind": "stage",
        })
        locations.append({
            "id": f"DK-{i + 1:02d}", "entity": "DOCK", "zone": "dock",
            "x": 4.5, "y": 13.0 + i * 2.6, "node": "DOCK_N", "local_m": 4.0,
            "kind": "dock",
        })
    return {
        "version_id": "v1",
        "label": "改版前（11-11 08:00 起）",
        "effective_from": BASE_DAY1.timestamp(),
        "effective_to": BASE_DAY2.timestamp(),
        "zones": ZONES,
        "entities": entities,
        "nodes": nodes,
        "edges": edges,
        "locations": locations,
        "aliases": {},
        "changes": [],
    }


def topology_v2(v1: dict) -> dict:
    v2 = json.loads(json.dumps(v1))
    v2["version_id"] = "v2"
    v2["label"] = "改版后（11-12 14:00 起）"
    v2["effective_from"] = BASE_DAY2.timestamp()
    v2["effective_to"] = 9.9e12
    v2["changes"] = [
        "C 通道增加脚手架，同时容纳量 2 → 1",
        "A 通道扩容 2 → 3",
        "D 通道由快拣区划入冷链区",
        "STG-02 货位条码变更为 STG-02N，旧条码保留 30 天别名",
    ]
    aliases = {}
    for ent in v2["entities"]:
        if ent["id"] == "AISLE_C":
            ent["name"] = RACK_NAMES["v2"]["C"]
            ent["capacity"] = CAPACITY["v2"]["C"]
        elif ent["id"] == "AISLE_A":
            ent["capacity"] = CAPACITY["v2"]["A"]
        elif ent["id"] == "AISLE_D":
            ent["zone"] = ZONE_OF_AISLE["v2"]["D"]
    for loc in v2["locations"]:
        if loc["entity"] == "AISLE_D":
            loc["zone"] = "cold"
        if loc["id"] == "STG-02":
            aliases["STG-02"] = "STG-02N"
            loc["id"] = "STG-02N"
    v2["aliases"] = aliases
    return v2


# ---------------------------------------------------------------------------
# 作业单与发单方案
# ---------------------------------------------------------------------------

ZONE_WEIGHT = {"fast": 0.58, "cold": 0.42}


def _aisles_by_zone(topo: dict, zone: str) -> list[str]:
    zmap = ZONE_OF_AISLE[topo["version_id"]]
    return [a for a in AISLES if zmap[a] == zone]


def _pick_locations(rng: random.Random, topo: dict, zone: str) -> list[str]:
    aisles = _aisles_by_zone(topo, zone)
    count = rng.choice([1, 2, 2, 3])
    chosen = []
    for _ in range(count):
        aisle = rng.choice(aisles)
        row = rng.randint(1, 4)
        side = rng.choice(("L", "R"))
        bay = rng.randint(1, 3)
        chosen.append(f"{aisle}{row}-{side}{bay:02d}")
    return chosen


def make_tasks(
    rng: random.Random,
    topo: dict,
    base: datetime,
    count: int,
    prefix: str,
    cancel_rate: float = 0.09,
) -> list[Task]:
    t0 = base.timestamp()
    tasks = []
    for n in range(count):
        zone = "fast" if rng.random() < ZONE_WEIGHT["fast"] else "cold"
        locs = _pick_locations(rng, topo, zone)
        created = t0 - rng.uniform(300, 1800)
        orders = tuple(f"SO-{rng.randint(10000, 99999)}" for _ in range(rng.randint(1, 3)))
        cancel_mode = "none"
        cancel_offset = None
        reason = ""
        if rng.random() < cancel_rate:
            # 一半是发单前取消（从未开始拣货），一半是取货中途取消
            if len(locs) >= 2 and rng.random() < 0.5:
                cancel_mode = "mid"
                cancel_offset = rng.uniform(90, 240)
            else:
                cancel_mode = "pre"
                cancel_offset = -rng.uniform(60, 300)
            reason = rng.choice(["客户撤单", "缺货整单取消", "重复建单", "改约配送"])
        tasks.append(Task(
            id=f"{prefix}-{n + 1:03d}",
            wave_id="",
            zone=zone,
            created_at=created,
            released_at=0.0,
            due_at=None,
            cancelled_at=None,
            cancel_reason=reason,
            order_ids=orders,
            locations=tuple(locs),
            release_rank=n,
            priority="normal",
            cancel_mode=cancel_mode,
            cancel_offset_s=cancel_offset,
        ))
    return tasks


def schedule_tasks(
    tasks: list[Task],
    base: datetime,
    plan: str,
) -> list[Task]:
    """只改变发单顺序/节奏，不改动作业内容。"""
    t0 = base.timestamp()
    ordered = list(tasks)
    if plan == "wave":
        # 同区成组发单：每波 6 单，波内同区，波次交替快拣/冷链
        fast = [t for t in ordered if t.zone == "fast"]
        cold = [t for t in ordered if t.zone == "cold"]
        sequence = []
        while fast or cold:
            if fast:
                sequence.extend(fast[:6]); fast = fast[6:]
            if cold:
                sequence.extend(cold[:6]); cold = cold[6:]
        ordered = []
        wave_no = 0
        for idx, task in enumerate(sequence):
            if idx % 6 == 0:
                wave_no += 1
            ordered.append(task)
        release_slots = []
        for idx, task in enumerate(ordered):
            wave = idx // 6
            release_slots.append(t0 + wave * 240 + (idx % 6) * 6.0)
        result = []
        for rank, (task, rel) in enumerate(zip(ordered, release_slots)):
            result.append(_with_release(task, rel, f"W{rank // 6 + 1}", rank, plan))
        return result
    # 混流：快拣/冷链交替，每 20 秒释放一单
    fast = [t for t in ordered if t.zone == "fast"]
    cold = [t for t in ordered if t.zone == "cold"]
    sequence = []
    while fast or cold:
        if fast:
            sequence.append(fast.pop(0))
        if cold:
            sequence.append(cold.pop(0))
    result = []
    for rank, task in enumerate(sequence):
        rel = t0 + rank * 20.0 + (rank % 3) * 2.0
        result.append(_with_release(task, rel, f"W{rank // 8 + 1}", rank, plan))
    return result


def _with_release(task: Task, release: float, wave: str, rank: int, plan: str) -> Task:
    return Task(
        id=task.id,
        wave_id=wave,
        zone=task.zone,
        created_at=task.created_at,
        released_at=release,
        due_at=release + (1500 if task.zone == "fast" else 2100) + (rank % 4) * 120,
        cancelled_at=(release + task.cancel_offset_s) if task.cancel_offset_s is not None else None,
        cancel_reason=task.cancel_reason,
        order_ids=task.order_ids,
        locations=task.locations,
        release_rank=rank,
        priority="rush" if plan == "mixed" and rank % 7 == 0 else "normal",
        cancel_mode=task.cancel_mode,
        cancel_offset_s=task.cancel_offset_s,
    )


# ---------------------------------------------------------------------------
# 行走与扫码仿真
# ---------------------------------------------------------------------------


def _profiles(rng: random.Random, tasks: list[Task]) -> dict:
    profiles = {}
    for task in tasks:
        picks = []
        for _ in task.locations:
            picks.append({
                "dwell": rng.uniform(20, 46),
                "duplicate": rng.random() < 0.055,
                "confirm": rng.random() < 0.18,
                "skip": rng.random() < 0.028,
            })
        profiles[task.id] = {
            "picks": picks,
            "travel_jitter": rng.uniform(0.9, 1.18),
        }
    return profiles


def simulate(
    topo,
    tasks: list[Task],
    pickers: list[str],
    base: datetime,
    plan: str,
    profiles: dict,
    benches: dict | None = None,
    outage: tuple[str, float, float] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """按发单顺序模拟拣货。返回 scans / ground_truth / stats。"""
    from .topology import WalkGraph

    graph = WalkGraph(topo)
    t0 = base.timestamp()
    window_end = t0 + WINDOW_SECONDS
    picker_state = {}
    for idx, picker in enumerate(pickers):
        picker_state[picker] = {
            "free": t0 + idx * 9.0,
            "loc": f"STG-{idx % 4 + 1:02d}",
            "entity": "STAGE",
            "zone": "staging",
            "break_start": t0 + 1500 + (idx * 347) % 2800,
            "break_len": 540 + (idx * 173) % 420,
            "break_taken": False,
            "walk_m": 0.0,
        }
    scans: list[dict] = []
    truth: list[dict] = []
    scan_no = 0
    pending = sorted(tasks, key=lambda t: (t.released_at, t.release_rank))
    cancelled_pre = 0
    cancelled_mid = 0
    completed = 0

    def emit(picker, loc_id, ts, task_id, kind="pick", dup_of=None, note=""):
        nonlocal scan_no
        if outage and picker == outage[0] and outage[1] <= ts <= outage[2]:
            return None
        scan_no += 1
        loc = topo.locations[loc_id]
        if ts < window_end:
            scans.append({
                "id": f"S{scan_no:05d}",
                "ts": round(ts, 1),
                "picker": picker,
                "location": loc_id,
                "entity": loc.entity,
                "zone": loc.zone,
                "task": task_id,
                "device": DEVICES.get(picker, f"HH-{picker}"),
                "source": "handheld",
                "kind": kind,
                "duplicate_of": dup_of,
                "note": note,
            })
        return scan_no

    def travel(from_loc: str, to_loc: str, jitter: float) -> float:
        meters = graph.location_distance(from_loc, to_loc)
        return meters * jitter / 1.15

    def add_truth(picker, kind, start, end, entity, zone, location, task_id, conf="actual"):
        if end <= start or start >= window_end:
            return
        truth.append({
            "picker": picker, "kind": kind, "start": round(start, 1),
            "end": round(min(end, window_end), 1), "entity": entity, "zone": zone,
            "location": location, "task": task_id, "confidence": conf,
        })

    def take_break(picker, after_ts):
        st = picker_state[picker]
        if st["break_taken"] or after_ts < st["break_start"] - 60:
            return after_ts
        start = max(after_ts, st["break_start"])
        if start > window_end - 300:
            return after_ts
        st["break_taken"] = True
        add_truth(picker, "break", start, start + st["break_len"], "UNKNOWN", "unknown", "", "")
        return start + st["break_len"]

    while pending:
        task = pending[0]
        if task.cancelled_at is not None and task.cancelled_at <= task.released_at:
            pending.pop(0)
            cancelled_pre += 1
            continue
        # 最早可用的空闲人员
        free_pickers = [p for p, s in picker_state.items() if s["free"] <= task.released_at]
        earliest = min(s["free"] for s in picker_state.values())
        start = max(task.released_at, earliest)
        if start >= window_end:
            break
        if task.cancelled_at is not None and task.cancelled_at <= start:
            pending.pop(0)
            cancelled_pre += 1
            continue
        free_pickers = [p for p, s in picker_state.items() if s["free"] <= start + 1]
        if not free_pickers:
            continue
        same_zone = [p for p in free_pickers if picker_state[p]["zone"] == task.zone]
        cand = same_zone or free_pickers
        cand.sort(key=lambda p: (picker_state[p]["free"], picker_state[p]["loc"]))
        picker = cand[0]
        st = picker_state[picker]
        pending.pop(0)
        profile = profiles[task.id]
        start = max(st["free"], task.released_at, start)
        start = take_break(picker, start)
        if start >= window_end:
            break

        # 走到首货位
        first_loc = task.locations[0]
        first = topo.locations[first_loc]
        walk = travel(st["loc"], first_loc, profile["travel_jitter"])
        add_truth(picker, "walk", start, start + walk, first.entity, first.zone, first_loc, task.id)
        st["walk_m"] += graph.location_distance(st["loc"], first_loc)
        arrival = start + walk

        cancelled_mid_task = False
        for idx, loc_id in enumerate(task.locations):
            loc = topo.locations[loc_id]
            if idx > 0:
                walk_s = travel(task.locations[idx - 1], loc_id, profile["travel_jitter"])
                add_truth(picker, "walk", arrival, arrival + walk_s, loc.entity, loc.zone, loc_id, task.id)
                st["walk_m"] += graph.location_distance(task.locations[idx - 1], loc_id)
                arrival += walk_s
            pick = profile["picks"][idx]
            add_truth(picker, "dwell", arrival, arrival + pick["dwell"], loc.entity, loc.zone, loc_id, task.id)
            if not pick["skip"]:
                first_scan_no = emit(picker, loc_id, arrival, task.id)
                if first_scan_no is not None and pick["duplicate"]:
                    emit(picker, loc_id, arrival + 1.4 + (idx % 2), task.id,
                         kind="repeat", dup_of=f"S{first_scan_no:05d}", note="同货位重复上报")
                if pick["confirm"]:
                    emit(picker, loc_id, arrival + pick["dwell"] * 0.9, task.id, kind="confirm")
            arrival += pick["dwell"]
            # 中途取消：取完当前货位后停止，返回复核区
            if task.cancelled_at is not None and task.cancelled_at <= arrival:
                cancelled_mid_task = True
                break
        if cancelled_mid_task:
            cancelled_mid += 1
            back_loc = f"STG-{pickers.index(picker) % 4 + 1:02d}"
            back = travel(task.locations[min(len(task.locations) - 1, 0)], back_loc,
                          profile["travel_jitter"])
            add_truth(picker, "walk", arrival, arrival + back, "STAGE", "staging", back_loc, task.id)
            st["walk_m"] += graph.location_distance(task.locations[0], back_loc)
            st["loc"] = back_loc
            st["entity"] = "STAGE"
            st["zone"] = "staging"
            st["free"] = arrival + back + 12
        else:
            if task.cancelled_at is None:
                completed += 1
            last_loc = task.locations[-1]
            last = topo.locations[last_loc]
            st["loc"] = last_loc
            st["entity"] = last.entity
            st["zone"] = last.zone
            st["free"] = arrival + 10
        if st["free"] >= window_end:
            break

    scans.sort(key=lambda s: (s["ts"], s["picker"]))
    truth.sort(key=lambda s: (s["picker"], s["start"]))
    stats = {
        "completed_tasks": completed,
        "cancelled_pre_start": cancelled_pre,
        "cancelled_mid_task": cancelled_mid,
        "walk_m_by_picker": {p: round(s["walk_m"], 1) for p, s in picker_state.items()},
        "scan_count": len(scans),
        "sim_end": max([s["ts"] for s in scans], default=t0),
    }
    return scans, truth, stats


def _task_dict(t: Task) -> dict:
    return {
        "id": t.id,
        "wave": t.wave_id,
        "zone": t.zone,
        "created": t.created_at,
        "released": t.released_at,
        "due": t.due_at,
        "cancelled": t.cancelled_at,
        "cancel_reason": t.cancel_reason,
        "orders": list(t.order_ids),
        "locations": list(t.locations),
        "release_rank": t.release_rank,
        "priority": t.priority,
        "cancel_mode": t.cancel_mode,
    }


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def build_all() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261115)
    v1 = topology_v1()
    v2 = topology_v2(v1)
    _write(DATA_DIR / "topology_v1.json", v1)
    _write(DATA_DIR / "topology_v2.json", v2)
    topo1 = load_topology(DATA_DIR / "topology_v1.json")
    topo2 = load_topology(DATA_DIR / "topology_v2.json")

    # 11-11 改版前
    tasks1 = schedule_tasks(make_tasks(rng, v1, BASE_DAY1, 108, "D1"), BASE_DAY1, "wave")
    prof1 = _profiles(rng, tasks1)
    scans1, truth1, stats1 = simulate(topo1, tasks1, PICKERS_DAY1, BASE_DAY1, "wave", prof1)
    # 11-12 改版后，P05 手持设备 15 分钟离线（数据缺失，非无人）
    tasks2 = schedule_tasks(make_tasks(rng, v2, BASE_DAY2, 210, "D2"), BASE_DAY2, "mixed")
    prof2 = _profiles(rng, tasks2)
    scans2, truth2, stats2 = simulate(
        topo2, tasks2, PICKERS_DAY2, BASE_DAY2, "mixed", prof2,
        outage=("P05", BASE_DAY2.timestamp() + 2100, BASE_DAY2.timestamp() + 3000),
    )
    # 旧条码在改版后仍被扫到：加两笔 STG-02 旧条码事件（别名）
    for i, ts in enumerate((BASE_DAY2.timestamp() + 1500, BASE_DAY2.timestamp() + 2600)):
        scans2.append({
            "id": f"S9{i + 1:04d}",
            "ts": round(ts, 1),
            "picker": "P02",
            "location": "STG-02",
            "entity": "STAGE",
            "zone": "staging",
            "task": None,
            "device": "HH-102",
            "source": "handheld",
            "kind": "manual",
            "duplicate_of": None,
            "note": "旧条码（改版后别名）",
        })
    scans2.sort(key=lambda s: (s["ts"], s["picker"]))

    # 两套发单顺序：同一批作业、同一批人员，只有释放顺序/节奏不同
    shared_raw = make_tasks(rng, v1, BASE_DAY1, 54, "RP")
    shared_raw = [t for t in shared_raw if t.cancelled_at is None or t.cancelled_at > BASE_DAY1.timestamp()]
    plan_a_tasks = schedule_tasks(shared_raw, BASE_DAY1, "wave")
    plan_b_tasks = schedule_tasks(shared_raw, BASE_DAY1, "mixed")
    prof_shared = _profiles(rng, shared_raw)
    replays = {}
    for key, plan, tasks in (("a", "wave", plan_a_tasks), ("b", "mixed", plan_b_tasks)):
        scans, truth, stats = simulate(topo1, tasks, PICKERS_DAY1[:6], BASE_DAY1, plan, prof_shared)
        replays[key] = {
            "id": f"replay-{key}",
            "name": "方案 A：按波次集中发单" if key == "a" else "方案 B：跨区混流发单",
            "plan": plan,
            "topology": "v1",
            "start": BASE_DAY1.timestamp(),
            "end": BASE_DAY1.timestamp() + WINDOW_SECONDS,
            "pickers": PICKERS_DAY1[:6],
            "tasks": [_task_dict(t) for t in tasks],
            "scans": scans,
            "stats": stats,
        }

    def scenario(sid, label, topo, tasks, pickers, scans, truth, stats):
        return {
            "id": sid,
            "label": label,
            "topology": topo,
            "start": BASE_DAY1.timestamp() if topo == "v1" and sid == "day1" else BASE_DAY2.timestamp(),
            "end": (BASE_DAY1.timestamp() if sid == "day1" else BASE_DAY2.timestamp()) + WINDOW_SECONDS,
            "pickers": pickers,
            "tasks": [_task_dict(t) for t in tasks],
            "scans": scans,
            "ground_truth": truth,
            "sim_stats": stats,
        }

    payload = {
        "meta": {
            "warehouse": "合成仓 WH-SYN-01",
            "note": "全部数据为本地合成，仅用于方法演示，不含真实人员与定位硬件。",
            "generated_by": "src/warehouse/synth.py",
            "window_seconds": WINDOW_SECONDS,
        },
        "topologies": {"v1": v1, "v2": v2},
        "scenarios": {
            "day1": scenario("day1", "11-11 早班 · 改版前", "v1", tasks1, PICKERS_DAY1, scans1, truth1, stats1),
            "day2": scenario("day2", "11-12 午班 · 改版后", "v2", tasks2, PICKERS_DAY2, scans2, truth2, stats2),
        },
        "replays": replays,
    }
    _write(DATA_DIR / "dataset.json", payload)
    write_csv_samples(payload)
    return payload


def write_csv_samples(payload: dict) -> None:
    """导出与导入格式一致的 CSV 样例，供“导入数据”面板使用。"""
    import csv

    csv_dir = DATA_DIR / "csv"
    csv_dir.mkdir(exist_ok=True)
    sc = payload["scenarios"]["day1"]
    with (csv_dir / "scans.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["scan_id", "ts", "picker", "location", "task", "device", "source", "kind", "duplicate_of", "note"])
        for s in sc["scans"]:
            w.writerow([s["id"], s["ts"], s["picker"], s["location"], s["task"] or "", s["device"],
                        s["source"], s["kind"], s["duplicate_of"] or "", s["note"]])
    with (csv_dir / "tasks.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["task_id", "wave", "zone", "created_at", "released_at", "due_at",
                    "cancelled_at", "cancel_reason", "order_ids", "locations", "priority"])
        for t in sc["tasks"]:
            w.writerow([t["id"], t["wave"], t["zone"], t["created"], t["released"], t["due"] or "",
                        t["cancelled"] or "", t["cancel_reason"], "|".join(t["orders"]),
                        "|".join(t["locations"]), t["priority"]])
    with (csv_dir / "locations.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["location_id", "entity", "zone", "x", "y", "node", "local_m"])
        for loc in payload["topologies"]["v1"]["locations"]:
            w.writerow([loc["id"], loc["entity"], loc["zone"], loc["x"], loc["y"],
                        loc["node"], loc["local_m"]])
    (csv_dir / "topology.json").write_text(
        json.dumps(payload["topologies"]["v1"], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (csv_dir / "README.txt").write_text(
        "导入面板接受：topology.json + scans.csv + tasks.csv（locations 可选，用于覆盖货位坐标）。\n"
        "时间列可用 ISO8601（含时区）或 epoch 秒。\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    result = build_all()
    print(f"dataset written: {len(result['scenarios']['day1']['scans'])} + "
          f"{len(result['scenarios']['day2']['scans'])} scans")
