"""
编排入口。

用法：
    python src/run.py --config configs/slg.yaml          # 生成报告
    python src/run.py --probe                            # 探测 ST 端点可用性
    python src/run.py --discover                         # 导出账号可用的 custom fields
    python src/run.py --config configs/slg.yaml --dry    # 只算分不调模型
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

# 安全网：在 GBK/GB2312 等中文控制台下，遇到无法编码的字符（如某些符号/emoji）
# 直接替换成 '?'，避免 UnicodeEncodeError 导致整个程序崩溃。中文本身在 GBK 内不受影响。
_enc = (getattr(sys.stdout, "encoding", "") or "").lower()
if _enc and _enc not in ("utf-8", "utf8", "utf_8", "utf-8-sig"):
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from collect import SRC_API, SRC_CFG, SRC_NONE, Collector
from narrative import (
    LIST_MODULES,
    MODULES,
    SYSTEM_PROMPT,
    _fallback,
    _normalize_module_value,
    generate_plans,
)
from scorecard import ASP_WEIGHTS, DIM_LABELS, WEIGHTS, Scorer, audit
from st_client import STClient

ROOT = Path(__file__).resolve().parent.parent


CONFIG_KEYS = {
    "SENSORTOWER_AUTH_TOKEN", "LLM_API_KEY", "LLM_BASE_URL",
    "LLM_MODEL", "LLM_TEMPERATURE", "LLM_MAX_WORKERS",
    "LLM_CONNECT_TIMEOUT", "LLM_READ_TIMEOUT", "LLM_RETRIES",
    "CONFIG_FILE", "HTTPS_PROXY", "HTTP_PROXY",
}

# 配置文件名（仅 VBS；旧的 .bat 已废弃，见 run.vbs）。
CONFIG_FILENAMES = ("config.user.vbs",)


def _read_text_any(path: Path) -> str:
    """读配置文本。VBS 文件常见三种编码：UTF-16LE(BOM) / UTF-8(BOM) / GBK。逐个试。"""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_config_file(path: Path) -> dict[str, str]:
    """解析密钥配置文件，同时兼容 BAT 与 VBS 两种写法。

    支持的行格式（大小写不敏感、可带引号、行尾注释按引号闭合处理）：
        set "KEY=VALUE"            <- 旧版 BAT 写法（兼容读取，推荐用下方 VBS 写法）
        SetEnv "KEY", "VALUE"      <- config.user.vbs 推荐写法
        KEY = "VALUE"              <- config.user.vbs 直接赋值
    以 ' 或 REM 开头的行视为注释。
    """
    out: dict[str, str] = {}
    try:
        text = _read_text_any(path)
    except Exception:  # noqa: BLE001
        return out

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("'") or line.lower().startswith("rem"):
            continue

        key = value = None
        low = line.lower()
        if low.startswith("set ") and "=" in line:          # BAT: set "K=V"
            body = line[3:].strip()
            key, value = body.split("=", 1)
        elif low.startswith("setenv") and "," in line:      # VBS: SetEnv "K", "V"
            body = line.split("(", 1)[1] if "(" in line.split(",", 1)[0] else line[6:]
            body = body.strip().rstrip(")")
            key, value = body.split(",", 1)
        elif "=" in line and not low.startswith(("if ", "for ", "while ")):
            key, value = line.split("=", 1)                 # VBS: K = "V"

        if key is None or value is None:
            continue
        key = key.strip().strip('"').strip()
        value = value.strip()
        if value.startswith('"'):                            # 取第一对引号内的内容
            end = value.find('"', 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split("'")[0].strip().strip('"')
        if key in CONFIG_KEYS and value:
            out[key] = value
    return out


def find_config_files(config_path: Path | None = None) -> list[Path]:
    """按优先级列出所有存在的密钥配置文件（后者覆盖前者）。

    查找目录：
      1) 技能/包根目录  <ROOT>
      2) --config 所在目录（用户研究工作区，最常见的放密钥位置）
      3) 当前工作目录    cwd
    每个目录内 config.user.vbs 都会被读取。
    """
    dirs: list[Path] = [ROOT]
    if config_path:
        try:
            dirs.append(Path(config_path).resolve().parent)
        except Exception:  # noqa: BLE001
            pass
    dirs.append(Path.cwd())

    found: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        for name in CONFIG_FILENAMES:
            p = d / name
            key = str(p).lower()
            if key in seen or not p.exists():
                continue
            seen.add(key)
            found.append(p)
    return found


def load_user_env(config_path: Path | None = None) -> None:
    """自动加载 config.user.vbs 里配置的环境变量。

    这样即便用户直接 `python src/run.py`（不走 run.vbs）也能用上配置好的
    Sensor Tower token 与 LLM API key，不会出现「之前能跑、直接跑就不走 API」。

    规则：仅当文件里写了「非空值」才设置/覆盖对应环境变量；
    空白值不覆盖，保留已有的系统或进程环境变量。
    """
    for env_file in find_config_files(config_path):
        for k, v in parse_config_file(env_file).items():
            os.environ[k] = v


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def serialize_scored(results, benchmarks, config, meta) -> dict:
    """把已打分的融合结果 + 标杆数据序列化成 JSON，交给 AI（WorkBuddy 自身）阅读并撰写方案卡。

    这样「叙事层」就不需要外部 LLM API：AI 读这份结构化数据，为每个方向产出 10 个模块的文字，
    写回 narrative.json，再由渲染步骤消费。所有数值仍来自评分层，AI 只碰文字。
    """
    fusion_meta = {f["id"]: f for f in config["fusions"]}
    fusions = []
    for r in results:
        by_market = {}
        for m, fs in r.by_market.items():
            by_market[m] = {
                "dims": {
                    k: {
                        "score": (v.score if v.score is not None else None),
                        "basis": v.basis,
                        "imputed": v.imputed,
                    }
                    for k, v in fs.dims.items()
                },
                "risk": fs.risk,
                "risk_reasons": fs.risk_reasons,
            }
        fm = fusion_meta.get(r.fusion_id, {})
        bk = (benchmarks or {}).get(r.fusion_id) or []
        fusions.append({
            "id": r.fusion_id,
            "name": r.fusion_name,
            "role": getattr(r, "fusion_role", fm.get("fusion_role", "")),
            "priority": r.priority,
            "combined_roi": r.combined_roi,
            "strong_side": r.strong_side,
            "coverage": r.coverage,
            "seeds": fm.get("seeds") or [],
            "hypothesis": fm.get("hypothesis") or "",
            "by_market": by_market,
            "benchmarks": [
                {"name": g["name"], "revenue": g["revenue"], "downloads": g["downloads"], "rpd": g["rpd"]}
                for g in bk
            ],
        })
    modules = []
    for k, n, d in MODULES:
        entry: dict[str, Any] = {"key": k, "name": n, "spec": d, "type": "text"}
        if k in LIST_MODULES:
            entry["type"] = "list"
            entry["fields"] = [
                {"key": fk, "name": fn, "spec": fs} for fk, fn, fs in LIST_MODULES[k]
            ]
        modules.append(entry)
    # AI 自评估风险：独立模块，输出对象而非段落
    modules.append({
        "key": "risk",
        "name": "融合风险",
        "spec": "对象 {score: 0-30 数值(越高越危险), reasons: [理由1, 理由2]}",
        "type": "risk",
    })

    return {
        "meta": meta,
        "modules": modules,
        "system_prompt": SYSTEM_PROMPT,
        "fusions": fusions,
        "instructions": (
            f"你是资深游戏立项分析师。请基于每个 fusion 的实测评分与标杆竞品数据，"
            f"为 modules 里列出的 {len(MODULES) + 1} 个 module 各写内容"
            "（严格遵循各自的字数与格式要求）。不得编造市场数字、下载量、收入。\n"
            "注意 module 的 type：type=text 的写一段文字；type=list 的必须写「对象数组」，"
            "数组每一项包含该 module 的 fields 里列出的全部字段"
            "（如 ref_games：name 游戏名 / gameplay 玩法详细介绍 / hook 吸量亮点 / status 现状）；"
            "type=risk 的（risk 模块）必须输出对象 {score: 0-30 数值, reasons: [理由列表]}。\n"
            f"最终输出一个 JSON 对象：键为 fusion_id（共 {len(fusions)} 个），"
            "值为该方向所有 module_key 到内容的映射，例如 "
            "{\"f1\": {\"concept\": \"...\", \"ref_games\": [{\"name\": \"...\", \"gameplay\": \"...\", "
            "\"hook\": \"...\", \"status\": \"...\"}], \"risk\": {\"score\": 12, \"reasons\": [\"...\", \"...\"]}, ...}}。"
            "把全部 fusion 的结果写入同一个文件（narrative.json）。"
        ),
    }


def load_narrative(path: Path, config: dict) -> tuple[dict, dict]:
    """读取 AI 写回的 narrative.json（{fusion_id: {module_key: value, risk: {...}}}），做结构校验。

    文字模块归一化为 str；结构化模块（如 ref_games）归一化为 list[dict]；
    risk 抽取为 {"score": float, "reasons": [str]}，供 apply_ai_risk 使用。
    缺字段先留空，由调用方用 _fallback 回填。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    valid = {f["id"] for f in config["fusions"]}
    out: dict = {}
    risks: dict = {}
    for fid in valid:
        entry = data.get(fid, {}) if isinstance(data, dict) else {}
        if not isinstance(entry, dict):
            entry = {}
        out[fid] = {
            k: _normalize_module_value(k, entry.get(k)) for k, _, _ in MODULES
        }
        raw_risk = entry.get("risk")
        if isinstance(raw_risk, dict):
            risks[fid] = {
                "score": raw_risk.get("score", raw_risk.get("value")),
                "reasons": list(raw_risk.get("reasons") or raw_risk.get("reason") or []),
            }
        elif isinstance(raw_risk, (int, float)):
            risks[fid] = {"score": float(raw_risk), "reasons": []}
    return out, risks


def fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1e8:
        return f"${value / 1e8:.2f}亿"
    if value >= 1e4:
        return f"${value / 1e4:.0f}万"
    return f"${value:,.0f}"


def fmt_count(value: float | None) -> str:
    """下载量等计数的中文量级格式化（万/亿），保持数字紧凑可读。"""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:,.0f}"


def build_matrix(results, bundles_by_key) -> dict:
    """四格判定矩阵：市场验证 / 受众契合 / 竞争格局 / 付费适配。"""
    matrix = {}
    for r in results:
        strong = r.by_market[r.strong_side]
        bundle = bundles_by_key.get((r.fusion_id, r.strong_side))

        seed_count = int(bundle.get("seed_count") or 0) if bundle else 0
        if seed_count >= 3:
            verify = {"tone": "g", "text": f"{seed_count}款已验证"}
        elif seed_count >= 1:
            verify = {"tone": "y", "text": f"{seed_count}款在跑"}
        else:
            verify = {"tone": "x", "text": "无案例"}

        overlap = bundle.get("audience_overlap") if bundle else None
        if overlap is None:
            fit = {"tone": "x", "text": "无相关数据"}
        elif overlap >= 0.4:
            fit = {"tone": "g", "text": f"重叠{overlap:.0%}"}
        elif overlap >= 0.25:
            fit = {"tone": "y", "text": f"重叠{overlap:.0%}"}
        else:
            fit = {"tone": "x", "text": f"重叠{overlap:.0%}"}

        comp_score = strong.dims["competition"].score
        comp = {
            "tone": "g" if (comp_score is not None and comp_score >= 6.5)
            else ("y" if (comp_score is not None and comp_score >= 4.5) else "x"),
            "text": f"{comp_score:.1f}" if comp_score is not None else "无",
        }
        pay_score = strong.dims["payment"].score
        pay = {
            "tone": "g" if (pay_score is not None and pay_score >= 6.5)
            else ("y" if (pay_score is not None and pay_score >= 4.5) else "x"),
            "text": f"{pay_score:.1f}" if pay_score is not None else "无",
        }
        matrix[r.fusion_id] = [verify, fit, comp, pay]
    return matrix


def build_summary(results, meta, priority) -> dict:
    if not results:
        return {"headline": "无候选方向", "p2_count": 0, "top_roi": 0, "top_name": "—", "callouts": []}

    top = results[0]
    p2 = [r for r in results if r.priority in ("P1", "P2")]
    p1 = [r for r in results if r.priority == "P1"]

    core = meta["core_genre"]
    headline = (
        f"{core} 融合扫描共产出 {len(results)} 个方向"
        f"（{len(p1)}个P1 + {len(p2) - len(p1)}个P2 + "
        f"{len([r for r in results if r.priority == 'P3'])}个P3）；"
        f"最优方向「{core} × {top.fusion_name}」综合 ROI {top.combined_roi:.2f}。"
    )
    if not p1:
        headline += f" 无方向达到 P1（≥{priority['P1']}），说明该核心玩法的融合机会属于「中体量做增量」而非「重仓赌爆款」。"

    callouts = []
    if p2:
        parts = [
            f"①{core}×{r.fusion_name}（{r.combined_roi:.2f}）= {r.fusion_role or '待定位'}"
            for r in p2[:4]
        ]
        callouts.append(
            f"<b>{len(p2)} 个 P2 及以上方向的差异化定位：</b>" + "；".join(parts) + "。"
        )

    spread = results[0].combined_roi - results[-1].combined_roi
    callouts.append(
        f"<b>方向间区分度：</b>最高与最低 ROI 相差 {spread:.2f}。"
        + ("区分度充分，排序可直接用于立项优先级决策。" if spread >= 1.5
           else "区分度偏小，说明候选玩法的差异未被现有指标充分捕捉，建议增加维度或细化种子产品。")
    )
    return {
        "headline": headline,
        "p2_count": len(p2),
        "top_roi": top.combined_roi,
        "top_name": f"{core}×{top.fusion_name}",
        "callouts": callouts,
    }


def build_core_kpis(config, core_data, core_genre, gl_countries) -> list:
    """核心玩法基本盘 KPI：优先用 ST games_breakdown 真实数据，缺失则显示「无相关数据」。"""
    kpis = []
    if core_data:
        gl = core_data.get("GL") or next(iter(core_data.values()))
        if gl and gl.get("revenue_12m"):
            kpis.append({
                "value": fmt_money(gl["revenue_12m"]),
                "label": f"{core_genre} 主要市场年收入（ST games_breakdown：{' / '.join(gl_countries)}）",
            })
            if gl.get("yoy") is not None:
                kpis.append({"value": f"{gl['yoy']:+.0%}", "label": "同比增速 YoY"})
        cn = core_data.get("CN")
        if cn and cn.get("revenue_12m"):
            kpis.append({"value": fmt_money(cn["revenue_12m"]), "label": "中国区年收入"})
    if not kpis:
        kpis = [{"value": "无相关数据", "label": f"{core_genre} 核心大盘（ST games_breakdown 未取到）"}]
    return kpis


def build_provenance_stats(bundles) -> list[dict]:
    counter = Counter()
    for b in bundles:
        for source in b.provenance.values():
            counter[source] += 1
    labels = {
        SRC_API: ("Sensor Tower API", "直连实测，最高可信度"),
        SRC_CFG: ("配置人工估值", "需复核，报告中以 * 标注"),
        SRC_NONE: ("缺失（无相关数据）", "该维度未参与评分，报告中显示「无相关数据」"),
    }
    return [
        {"label": labels[k][0], "count": v, "note": labels[k][1]}
        for k, v in counter.most_common()
        if k in labels
    ]


def build_opportunities(results, config) -> dict:
    """「寻找新机会」版块：从已算好的融合结果中捞出蓝海 / 黑马 / 空白方向。

    只用既有五维分数与 ROI，不引入新假设；所需维度缺失的方向不参与对应榜单，避免编造。
    """
    fusion_meta = {f["id"]: f for f in config["fusions"]}
    items = []
    for r in results:
        strong = r.by_market.get(r.strong_side)
        if not strong:
            continue
        d = strong.dims
        items.append({
            "id": r.fusion_id,
            "name": r.fusion_name,
            "role": r.fusion_role,
            "roi": r.combined_roi,
            "priority": r.priority,
            "coverage": r.coverage,
            "space": d["space"].score,
            "comp": d["competition"].score,
            "traffic": d["traffic"].score,
            "retention": d["retention"].score,
            "payment": d["payment"].score,
            "category_id": fusion_meta.get(r.fusion_id, {}).get("category_id"),
        })
    if not items:
        return {"available": False, "blue_ocean": [], "dark_horse": [], "whitespace": []}

    def both(i, a, b):
        return i[a] is not None and i[b] is not None

    rois = [i["roi"] for i in items]
    covs = [i["coverage"] for i in items]
    med_roi = sorted(rois)[len(rois) // 2]
    med_cov = sorted(covs)[len(covs) // 2]

    # ① 蓝海方向：空间高且竞争低（市场大、尚未拥挤）
    blue = [i for i in items if both(i, "space", "comp")]
    blue.sort(key=lambda i: (i["space"] - i["comp"], i["roi"]), reverse=True)

    # ② 高潜力黑马：综合 ROI 进前列但种子覆盖低（被低估、未充分验证）
    horses = [i for i in items if i["roi"] >= med_roi and i["coverage"] <= med_cov]
    horses.sort(key=lambda i: i["roi"], reverse=True)

    # ③ 空白赛道：空间天花板可观且竞争极低（核心玩法几乎未渗透）
    white = [i for i in items if both(i, "space", "comp") and i["space"] >= 5 and i["comp"] <= 4]
    white.sort(key=lambda i: (i["space"], i["roi"]), reverse=True)

    return {
        "available": True,
        "blue_ocean": blue[:5],
        "dark_horse": horses[:5],
        "whitespace": white[:5],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="融合玩法立项机会报告生成器")
    parser.add_argument("--config", type=str, help="配置文件路径")
    parser.add_argument("--out", type=str, default=None, help="输出 HTML 路径")
    parser.add_argument("--probe", action="store_true", help="探测 ST 端点可用性")
    parser.add_argument("--discover", action="store_true", help="导出可用 custom fields")
    parser.add_argument("--dry", action="store_true", help="跳过 LLM，只算分并用占位方案卡")
    parser.add_argument("--dump-scored", type=str, default=None,
                        help="把已打分结果+标杆数据导出为 JSON，交给 AI（WorkBuddy 自身）撰写方案卡")
    parser.add_argument("--narrative-json", type=str, default=None,
                        help="读取 AI 写回的方案卡 JSON（narrative.json），替代外部 LLM 生成")
    args = parser.parse_args()
    load_user_env(args.config)  # 自动注入 config.user.vbs（包根 / 配置目录 / cwd 均可）

    if args.probe:
        client = STClient(cache_dir=ROOT / "data" / "cache")
        if not client.api_available:
            print("未检测到 SENSORTOWER_AUTH_TOKEN，无法探测。请先设置环境变量。")
            return 1
        print("探测 Sensor Tower 端点可用性：\n")
        for key, status in client.probe().items():
            print(f"  {key:20s} {status}")
        return 0

    if args.discover:
        client = STClient(cache_dir=ROOT / "data" / "cache")
        if not client.api_available:
            print("未检测到 SENSORTOWER_AUTH_TOKEN，无法探测。请先设置环境变量。")
            return 1
        print("注意：Sensor Tower 没有公开的「列出全部自定义字段」接口（不同套餐下该端点不可用），")
        print("所以无法用 API 自动枚举 Game Sub-genre / Art Style 等字段。")
        print("正确做法：在 ST 网页端(app.sensortower.com)的数据板块手动配置过滤器，")
        print("从 URL 复制 custom_fields_filter_id，填入配置里对应 fusion 的 filter_id 字段。\n")
        print("下面用你的 token 实测核心数据端点的连通性：\n")
        for key, status in client.probe().items():
            print(f"  {key:18s} {status}")
        print("\n若核心端点显示 OK，即可用 python src/run.py --config configs/slg.yaml 跑真实数据。")
        return 0

    if not args.config:
        parser.error("需要 --config，或使用 --probe / --discover")

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    config = load_config(config_path)

    print(f"[1/5] 读取配置 {config_path.name}")
    collector = Collector(config, ROOT)

    # ── token 门禁：数据源仅限 Sensor Tower 真实 API，拒绝任何示例/占位数据 ──
    has_token = collector.client.api_available
    if not has_token:
        print()
        print("=" * 64)
        print("  未检测到 Sensor Tower token —— 本工具仅使用 ST 真实数据，无法生成报告。")
        print()
        print("  配置方式（二选一）：")
        print("    1) 双击 run.vbs -> 选 4，填入 SENSORTOWER_AUTH_TOKEN")
        print("    2) 复制 config.user.vbs.example 为 config.user.vbs 并填入 token")
        print()
        print("  填好后在可访问 api.sensortower.com 的机器上重跑")
        print("  （国内直连受限时，先在 config.user.vbs 里配置 HTTPS_PROXY）。")
        print("=" * 64)
        return 1

    mode = "Sensor Tower API"
    print(f"[2/5] 采集数据 —— 模式：{mode}")

    # 连通性自检：API 模式下先发 1 个最快的请求，连不上就几秒明确报错并中止，
    # 避免傻等几百个请求却始终卡在第二步（常见于国内直连 ST 受限 / 代理缺失 / token 失效）。
    if has_token:
        print("      正在做连通性自检（1 个最快的请求）...")
        ok, secs, msg = collector.client.preflight()
        if not ok:
            print(f"      [X] ST 连接自检失败：{msg}")
            print("      -> 网络连不上 Sensor Tower（多为国内直连受限或需代理），或 token 失效。")
            print("        排查：1) 此机器能访问 https://api.sensortower.com 吗？")
            print("              2) 若在公司代理后，先设 set HTTPS_PROXY=http://代理地址:端口 再跑；")
            print("              3) 到 sensortower.com/users/edit 重新复制完整 token。")
            return 1
        print(f"      [OK] ST 连通：{msg}")
        if secs > 5:
            print(f"      [!] ST 响应偏慢（{secs:.1f}s/请求）。整步可能要几分钟，请耐心等待；")
            print("        结果会缓存到 data/cache，重跑会快很多。下方会逐个方向显示进度。")

    # 标杆竞品扫描前置：先检索各方向同类真实游戏，再把其 app_id 回填为种子，
    # 使空种子的创新组合方向也能以完全相同口径拿到吸量/付费/留存（同类标杆代理）。
    benchmarks = collector.collect_benchmarks(config)
    if benchmarks:
        total_games = sum(len(v) for v in benchmarks.values())
        print(f"      标杆竞品扫描：{len(benchmarks)} 方向共发现 {total_games} 款真实游戏")
    else:
        print("      标杆竞品扫描：无 API token，跳过（报告中显示「需配置 token」）")
    collector.backfill_seeds(config, benchmarks)

    bundles = collector.collect()
    print(f"      产出 {len(bundles)} 个指标包（{len(config['fusions'])} 方向 × {len(collector.markets)} 市场）")

    print("[3/5] 五维打分（初步，使用确定性风险基线）")
    scorer = Scorer(config)
    results = scorer.score(bundles)

    meta = dict(config["meta"])
    meta.setdefault("date", date.today().isoformat())
    meta.setdefault("methodology", "genre-fusion-research v1.0 + 立项线索评估 v3.1.4")
    meta["markets"] = list(collector.markets.keys())
    meta["config_file"] = str(config_path.relative_to(ROOT)).replace("\\", "/")
    meta["data_source"] = "Sensor Tower"

    print("[4/5] 生成方案卡（AI 评估风险 + 文案）")
    fusion_meta_map = {f["id"]: f for f in config["fusions"]}
    if args.narrative_json:
        # AI 手桥：直接读回 WorkBuddy 自身（或人工）写好的方案卡，不经外部 LLM
        agent_plans, agent_risks = load_narrative(Path(args.narrative_json), config)
        plans = {}
        risks = agent_risks
        missing: list[str] = []
        for r in results:
            fid = r.fusion_id
            raw = agent_plans.get(fid, {})
            fb = _fallback(fusion_meta_map[fid], r, meta, benchmarks)
            full = {}
            for k, n, d in MODULES:
                val = raw.get(k)
                if isinstance(val, str):
                    val = val.strip()
                if not val:                       # 空字符串 / 空数组 -> 用占位回填
                    val = fb.get(k, "")
                    missing.append(f"{r.fusion_name}.{k}")
                full[k] = val
            plans[fid] = full
        print(f"      已从 {args.narrative_json} 载入 AI 撰写的方案卡（{len(plans)} 个方向）")
        if missing:
            print(f"      [!] {len(missing)} 个模块缺失，已用占位回填：{'、'.join(missing[:8])}"
                  + ("…" if len(missing) > 8 else ""))
    elif args.dry:
        plans = {r.fusion_id: _fallback(fusion_meta_map[r.fusion_id], r, meta, benchmarks) for r in results}
        risks = {}
    else:
        # 叙事层对全部 15 个候选生成方案卡并自评估风险（满足「15 候选取 10 最优」）
        plans, risks = generate_plans(config, results, meta, benchmarks)

    # 用 AI 自评估风险覆盖确定性风险，再重排、取 top10
    scorer.apply_ai_risk(results, risks)
    results.sort(key=lambda r: r.combined_roi, reverse=True)
    scorer.assign_priorities(results)  # 按相对分位重算 P1-P4（保证四档都有）
    plan_results = results[:10]
    audit_result = audit(results)
    opportunities = build_opportunities(results, config)
    for r in results:
        print(f"      {r.priority}  {r.combined_roi:5.2f}  {config['meta']['core_genre']} × {r.fusion_name}")

    # 导出供 AI（WorkBuddy 自身）阅读的结构化数据，用于「无外部 LLM」的方案卡生成流程。
    if args.dump_scored:
        scored = serialize_scored(results, benchmarks, config, meta)
        dp = Path(args.dump_scored)
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"      已导出供 AI 阅读的数据 -> {dp}")

    # 预生成方案卡里的「数据依据」字符串（缺失维度显示「无相关数据」）
    for r in results:
        r.market_dim_basis = {
            m: "/".join(
                f"{DIM_LABELS[k]}{('无相关数据' if fs.dims[k].score is None else f'{fs.dims[k].score:.1f}')}"
                for k in WEIGHTS
            )
            for m, fs in r.by_market.items()
        }

    print("[5/5] 渲染 HTML")
    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = fmt_money
    env.filters["count_cn"] = fmt_count
    template = env.get_template("report.html.j2")

    bundles_by_key = {(b.fusion_id, b.market): b for b in bundles}
    scoring = scorer.scoring
    dim_specs = [
        {
            "key": k,
            "label": DIM_LABELS[k],
            "weight": f"{WEIGHTS[k]:.0%}",
            "metric": scoring[k]["metric"],
            "mapping": "绝对分档（对数插值）" if "anchors" in scoring[k] else "相对分位",
        }
        for k in WEIGHTS
    ]

    core_kpis = build_core_kpis(
        config, getattr(collector, "core_market", {}),
        meta["core_genre"], collector.markets.get("GL", []),
    )
    html = template.render(
        meta=meta,
        summary=build_summary(results, meta, getattr(scorer, "_priority_bands", scorer.thresholds)),
        results=results,
        fusion_meta={f["id"]: f for f in config["fusions"]},
        dim_specs=dim_specs,
        core_kpis=core_kpis,
        core_notes=config.get("core_market", {}).get("notes", ""),
        matrix=build_matrix(results, bundles_by_key),
        plans=plans,
        plans_results=plan_results,
        benchmarks=benchmarks,
        opportunities=opportunities,
        audit=audit_result,
        sources=config.get("sources", []),
        provenance_stats=build_provenance_stats(bundles),
        priority=scorer.thresholds,
    )

    out_path = Path(args.out) if args.out else ROOT / "out" / (
        f"{meta['core_genre']}_玩法融合立项机会研究.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"\n完成 -> {out_path}")
    print(f"候选方向 {len(results)} 个，方向方案 {len(plan_results)} 个；"
          f"数学校验 {'通过' if audit_result['passed'] else '未通过'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
