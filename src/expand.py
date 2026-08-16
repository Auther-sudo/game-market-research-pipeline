"""
展开器：把「融合玩法库」自动 × 核心玩法，生成尽量多的候选方向。

用法：
    python src/expand.py configs/slg.yaml
    → 写出 configs/slg_expanded.yaml（保留已策划方向 + 追加库里未覆盖的机制）

设计：
    - 已策划方向（yaml 里手写的 fusions）原样保留，其 seeds/hypothesis 不动；
    - 库里不在已策划集合中的机制，自动补成候选方向（seeds 取库里的 exemplars
      作为搜索种子，hypothesis 留空交给叙事层/人工）。
    这样「尽量多、能找到的都写进去」就变成一份库全列出来，用户再按需删减。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LIB_PATH = ROOT / "configs" / "fusion_library.yaml"


def expand(core_path: Path) -> Path:
    core = yaml.safe_load(core_path.read_text(encoding="utf-8"))
    lib = yaml.safe_load(LIB_PATH.read_text(encoding="utf-8"))
    library = {e["id"]: e for e in lib.get("library", [])}

    existing = {f["id"]: f for f in core.get("fusions", [])}
    fusions = []
    kept = {}
    for f in core.get("fusions", []):
        fid = f["id"]
        # 已策划方向：保留其 seeds/hypothesis/category_id 等手写内容，
        # 仅当缺失时用库里的 benchmark_keywords / category_id 补全（便于标杆扫描）。
        if fid in library:
            lib = library[fid]
            f.setdefault("benchmark_keywords", lib.get("benchmark_keywords", []))
            if not f.get("category_id"):
                f["category_id"] = lib.get("category_id")
        fusions.append(f)
        kept[fid] = f

    added = 0
    for eid, e in library.items():
        if eid in kept:
            continue
        fusions.append(
            {
                "id": e["id"],
                "name": e["name"],
                "fusion_role": e.get("fusion_role", "副玩法"),
                "st_subgenre": e.get("st_subgenre", ""),
                "category_id": e.get("category_id"),
                "benchmark_keywords": e.get("benchmark_keywords", []),
                "seeds": [{"name": n} for n in e.get("exemplars", [])],
                "hypothesis": "",
                "risk_base": 0,
            }
        )
        added += 1

    core["fusions"] = fusions
    out_path = core_path.with_name(core_path.stem + "_expanded.yaml")
    out_path.write_text(
        yaml.safe_dump(core, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(
        f"已写出 {out_path.name}：共 {len(fusions)} 个方向"
        f"（原有 {len(existing)} + 新增 {added}）"
    )
    return out_path


def main() -> int:
    if len(sys.argv) > 1:
        core_path = Path(sys.argv[1])
    else:
        core_path = ROOT / "configs" / "slg.yaml"
    if not core_path.exists():
        print(f"找不到配置：{core_path}")
        return 1
    expand(core_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
