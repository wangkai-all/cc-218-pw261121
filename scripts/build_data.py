#!/usr/bin/env python3
"""重新生成全部合成数据（拓扑、场景、回放与 CSV 样例）。

用法：
    python3 scripts/build_data.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.warehouse import synth  # noqa: E402


def main() -> int:
    dataset = synth.build_all()
    meta = dataset["meta"]
    print(f"仓库：{meta['warehouse']} · 场景 {len(dataset['scenarios'])} 个 · "
          f"回放 {len(dataset['replays'])} 套")
    for sid, sc in dataset["scenarios"].items():
        print(f"  {sid}: {len(sc['tasks'])} 单 / {len(sc['scans'])} 扫描 / {sc['label']}")
    print(f"输出目录：{synth.DATA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
