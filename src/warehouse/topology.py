"""拓扑加载、货位别名解析与行走距离图。

拓扑文件是 JSON（data/topology_v1.json / topology_v2.json）。加载后构建：
- 货位 -> 实体（货架通道/复核区/月台）
- 步道图最短路：任意货位/实体之间的最小步行米数
"""
from __future__ import annotations

import heapq
import json
from pathlib import Path

from .models import Edge, Entity, GraphNode, Location, Rect, Topology, Zone

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _load_zone(raw: dict) -> Zone:
    return Zone(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        kind=raw.get("kind", "picking"),
        color=int(raw.get("color", 0)),
    )


def load_topology(path: str | Path) -> Topology:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entities = {}
    for item in raw["entities"]:
        rect = item["rect"]
        entities[item["id"]] = Entity(
            id=item["id"],
            name=item.get("name", item["id"]),
            kind=item["kind"],
            zone=item["zone"],
            capacity=int(item["capacity"]),
            rect=Rect(float(rect["x"]), float(rect["y"]), float(rect["w"]), float(rect["h"])),
            aliases=tuple(item.get("aliases", [])),
        )
    nodes = {
        item["id"]: GraphNode(
            id=item["id"], x=float(item["x"]), y=float(item["y"]), entity=item["entity"]
        )
        for item in raw["nodes"]
    }
    edges = [
        Edge(a=item["a"], b=item["b"], distance_m=float(item["distance_m"]))
        for item in raw["edges"]
    ]
    locations = {}
    for item in raw["locations"]:
        locations[item["id"]] = Location(
            id=item["id"],
            entity=item["entity"],
            zone=item["zone"],
            x=float(item["x"]),
            y=float(item["y"]),
            node=item["node"],
            local_m=float(item.get("local_m", 0.0)),
        )
    aliases = {k: v for k, v in raw.get("aliases", {}).items()}
    for ent in entities.values():
        for old in ent.aliases:
            aliases.setdefault(old, ent.id)
    topo = Topology(
        version_id=raw["version_id"],
        label=raw.get("label", raw["version_id"]),
        effective_from=float(raw["effective_from"]),
        effective_to=float(raw.get("effective_to", 9.9e12)),
        entities=entities,
        nodes=nodes,
        edges=edges,
        locations=locations,
        aliases=aliases,
        changes=tuple(raw.get("changes", [])),
        zones={z["id"]: _load_zone(z) for z in raw.get("zones", [])},  # type: ignore[arg-type]
    )
    return topo


class WalkGraph:
    """无向加权图的 Dijkstra；权重为米，换算成秒由调用方乘速度参数。"""

    def __init__(self, topology: Topology):
        self.topology = topology
        self._adj: dict[str, list[tuple[str, float]]] = {}
        for edge in topology.edges:
            self._adj.setdefault(edge.a, []).append((edge.b, edge.distance_m))
            self._adj.setdefault(edge.b, []).append((edge.a, edge.distance_m))
        self._cache: dict[str, dict[str, float]] = {}

    def _dijkstra(self, start: str) -> dict[str, float]:
        if start in self._cache:
            return self._cache[start]
        dist = {start: 0.0}
        pq = [(0.0, start)]
        while pq:
            d, node = heapq.heappop(pq)
            if d > dist.get(node, float("inf")):
                continue
            for nxt, w in self._adj.get(node, ()):
                nd = d + w
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    heapq.heappush(pq, (nd, nxt))
        self._cache[start] = dist
        return dist

    def node_distance(self, a: str, b: str) -> float:
        if a == b:
            return 0.0
        dist = self._dijkstra(a)
        if b in dist:
            return dist[b]
        # 图不连通时给一个保守的大距离（不让路径判定误以为很近）
        return 999.0

    def resolve_location_id(self, loc_id: str) -> str:
        """旧货位条码 -> 新货位（别名表可能指向新货位或实体）。"""
        if loc_id in self.topology.locations:
            return loc_id
        target = self.topology.aliases.get(loc_id)
        if target and target in self.topology.locations:
            return target
        return loc_id

    def location_distance(self, a: str, b: str) -> float:
        a = self.resolve_location_id(a)
        b = self.resolve_location_id(b)
        la = self.topology.locations[a]
        lb = self.topology.locations[b]
        return (
            la.local_m
            + self.node_distance(la.node, lb.node)
            + lb.local_m
        )

    def entity_distance(self, a: str, b: str) -> float:
        """两个实体之间最短步行距离（分别取各自节点集合中的最近点）。"""
        if a == b:
            return 0.0
        best = float("inf")
        nodes_a = [n.id for n in self.topology.nodes.values() if n.entity == a]
        nodes_b = [n.id for n in self.topology.nodes.values() if n.entity == b]
        for na in nodes_a:
            dist = self._dijkstra(na)
            for nb in nodes_b:
                if nb in dist:
                    best = min(best, dist[nb])
        return best if best < float("inf") else 999.0
