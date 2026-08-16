"""
叙事层：为每个融合方向生成方案卡的十一个模块。

设计原则 —— 模型只碰文字，不碰数字：
    传给模型的是已经算好的评分与实测指标，模型负责把它们组织成方案叙述。
    报告里所有数值仍从 scorecard 直出，模型改写不了。

无 API key 时降级为「数据陈述式」占位：只复述实测事实，不编造。
"""
from __future__ import annotations

import json
import os
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore

import concurrent.futures as _cf
import threading
import time as _time
import random

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    """线程安全的诊断输出（并行生成时避免多路打印互相穿插）。"""
    with _print_lock:
        print(msg, flush=True)


def _bar(done: int, total: int, width: int = 20) -> str:
    """纯 ASCII 进度条（避免中文 GBK 控制台下的 UnicodeEncodeError），如 [####------] 4/10。"""
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = max(0, min(width, int(round(width * done / total))))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _fmt_tokens(n) -> str:
    """token 数格式化：超过 1000 附 (x.xk) 简写，便于一眼看量级。"""
    n = int(n or 0)
    if n >= 1000:
        return f"{n:,} ({n / 1000:.1f}k)"
    return str(n)


def _llm_preflight(base_url, api_key, model, proxies, timeout=(10, 30)) -> tuple[bool, float, str]:
    """发一个最小请求，快速验证 LLM 端点连通性。返回 (是否成功, 耗时秒, 说明)。

    用于生成前几秒就暴露「连不上」的真实原因，避免 10 个方向各等几百秒才发现全挂。
    """
    if requests is None:
        return False, 0.0, "requests 未安装"
    try:
        t0 = _time.time()
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
            timeout=timeout,
            proxies=proxies or None,
        )
        secs = _time.time() - t0
        if resp.status_code == 200:
            return True, secs, f"正常（{secs:.1f}s）"
        return False, secs, f"HTTP {resp.status_code}：{(resp.text or '')[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, 0.0, f"{type(exc).__name__}：{str(exc)[:200]}"

# ── 结构化模块字段定义 ──
# 「可融合的非SLG游戏」不再是一段文字，而是一组游戏对象，渲染成与标杆竞品同款卡片：
# 每款游戏都有 玩法详细介绍 / 吸量亮点 / 现状 三段（不含「可结合玩法」，那是竞品扫描的专属维度）。
REF_GAME_FIELDS = [
    ("name", "游戏名", "真实存在的具体产品名（非 SLG），如「Candy Crush Saga」"),
    ("gameplay", "玩法详细介绍", "核心循环 + 关键机制怎么运转 + 一局多长，说清玩家实际在做什么，70-120字"),
    ("hook", "吸量亮点", "它靠什么抓量：首屏感受、素材钩子、可视化爽点，50-90字"),
    ("status", "现状", "当前市场表现与趋势：体量档位、增长还是内卷、玩法演化方向，40-80字"),
]

# key -> 字段表。出现在这里的模块，值是「对象数组」而非字符串。
LIST_MODULES: dict[str, list[tuple[str, str, str]]] = {"ref_games": REF_GAME_FIELDS}

MODULES = [
    ("concept", "概念", "一句话说清玩家在做什么、核心循环是什么，60-110字"),
    ("fit", "适配", "为什么这个玩法能嫁接到核心玩法上，引用实测数据佐证，60-110字"),
    ("acquisition", "吸量点", "3个吸量钩子，末尾附「吸量要素：」标签行，80-130字"),
    ("retention", "留存", "4条留存设计，用｜分隔，40-80字"),
    ("monetization", "付费", "参考产品（上线≤5年）+ 付费点 + 目标RPD，40-80字"),
    ("art", "美术", "2-3个美术参考锚点，用｜分隔，20-50字"),
    ("concept_art", "概念图", "3张概念图描述，玩法向不是氛围向，用｜分隔，40-80字"),
    ("status", "现状", "该方向标杆游戏的市场现状：头部产品、规模、涨/卷，60-110字"),
    ("success_logic", "成功逻辑", "头部产品为什么能成：核心循环/买量/留存设计，60-110字"),
    ("ref_mechanics", "可结合玩法", "这些机制里哪些能嫁接到核心玩法、怎么借，60-110字"),
    (
        "ref_games",
        "可融合的非SLG游戏",
        "【对象数组，不是字符串】3-5 个该融合方向对应品类的真实非SLG游戏，"
        "每个对象四个字段：name（游戏名）、gameplay（玩法详细介绍 70-120字）、"
        "hook（吸量亮点 50-90字）、status（现状 40-80字）；只列该品类的非SLG产品，不要列 SLG 竞品",
    ),
]

MODULE_KEYS = [k for k, _, _ in MODULES]

SYSTEM_PROMPT = """你是资深游戏立项分析师，正在撰写「核心玩法 × 融合玩法」的立项方案卡。核心玩法固定为「SLG」（策略战争）。

硬性要求：
1. 只输出 JSON，不要 markdown 代码块包裹，不要任何解释。
2. 忠实于给定的实测数据与评分，不得编造市场数字、下载量、收入。
3. 融合方案要以「SLG 为根、融合玩法为壳」——一眼能认出是哪两个玩法的结合，不要把所有玩法都往同一个套路上靠。
4. 判断融合玩法形态后再决定怎么融：非对抗/单人/休闲/解谜/建造类要保持其原有体验节奏，不要强行加对抗和社交。
5. 付费参考产品必须是上线不超过5年的真实产品。
6. 「可融合的非SLG游戏」(ref_games) 必须输出**对象数组**，每项 4 个字段：
   name（真实存在的非SLG具体产品名）、gameplay（玩法详细介绍：核心循环+关键机制+一局时长）、
   hook（吸量亮点：靠什么抓量、素材钩子、首屏爽点）、status（现状：体量档位、涨还是卷、演化方向）。
   必须是该融合方向对应品类的产品（「三消」方向列三消游戏、「塔防」方向列塔防游戏），**不要列 SLG 竞品**，
   也不要在这里写「怎么和 SLG 结合」——那是「可结合玩法」模块的内容。
7. 语言精炼，不写空话套话。
8. 标杆竞品真实性：只有当输入里明确给出「该方向标杆竞品（ST 真实游戏）」列表时，才能写「现状 / 成功逻辑 / 可结合玩法」；
   若未给出任何标杆竞品（即该方向在 Sensor Tower 里搜不到对应真实游戏），这三个字段必须统一写成「目前没有竞品」，
   绝对禁止编造竞品名称、收入、下载量或市场数字。
9. 融合风险自评估：综合「是否有已上线案例/竞品、受众契合度、品类成熟度、竞争烈度、数据可信度」评估该方向立项风险，
   在返回 JSON 中额外输出 risk 字段：{"score": 0-30 的数值（越高越危险，0=几乎无风险，30=极高风险）,
   "reasons": ["理由1", "理由2"]}。基于实测与你的市场判断独立给出，不要照抄下方系统初步风险参考。
10. 标杆竞品（benchmark）的语义边界——这是避免与「种子产品」章节自相矛盾的关键：
    输入里给的「标杆竞品」是「该品类的头部纯玩法真实游戏」，仅用于佐证「这个品类自身有市场盘子」，
    它们【本身不是】SLG×本玩法 的已上线组合产品。
    融合路线是否已被验证，以「已上线种子产品（seed_count）」为准——seed_count=0 即无直接组合案例。
    撰写「现状 / 成功逻辑 / 可结合玩法」时，请基于这些品类标杆客观分析，但必须明确区分：
    它们是品类对标参照，不代表「SLG×本玩法」融合路线已被市场验证，也绝对不要把它们写成 SLG 组合产品。
    若 seed_count=0（种子产品章节写的是「无已上线组合案例 / 同类标杆代理」），更要在文中点明
    「该品类有成熟标杆，但 SLG 与该玩法的直接组合尚未被市场验证」。"""


def _normalize_module_value(key: str, value):
    """把模型/AI 写回的模块值归一化：文字模块 -> str；结构化模块 -> list[dict]。

    对结构化模块做足容错：接受对象数组、字符串数组、{游戏名: {...}} 字典，
    甚至旧版的一整段文字（此时原样保留字符串，模板会退化成段落渲染）。
    """
    fields = LIST_MODULES.get(key)
    if not fields:
        return str(value or "").strip()

    keys = [f for f, _, _ in fields]

    def _row(name: str, src: dict | None = None) -> dict:
        src = src or {}
        row = {k: str(src.get(k, "") or "").strip() for k in keys}
        row["name"] = str(name or row.get("name") or "").strip()
        return row

    out: list[dict] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                row = _row(item.get("name", ""), item)
                if row["name"]:
                    out.append(row)
            elif isinstance(item, str) and item.strip():
                out.append(_row(item.strip()))
    elif isinstance(value, dict):
        for name, item in value.items():
            if isinstance(item, dict):
                out.append(_row(name, item))
            elif isinstance(item, str):
                out.append(_row(name, {"gameplay": item}))
    elif isinstance(value, str) and value.strip():
        return value.strip()  # 兼容旧格式：整段文字
    return out


def _parse_risk(data: dict) -> dict | None:
    """从模型返回的 JSON 里抽取融合风险。支持 {"score":..,"reasons":[..]} 或直接数值。

    返回 {"score": float, "reasons": [str]} 或 None（模型未给有效风险时由确定性兜底接管）。
    """
    if not isinstance(data, dict):
        return None
    raw = data.get("risk")
    if isinstance(raw, dict):
        score = raw.get("score", raw.get("value"))
        reasons = raw.get("reasons") or raw.get("reason") or []
    elif isinstance(raw, (int, float)):
        score = raw
        reasons = []
    else:
        return None
    try:
        score = float(score)
    except (TypeError, ValueError):
        return None
    if not (0 <= score <= 100):
        return None
    reasons = [str(x) for x in reasons if str(x).strip()][:4]
    return {"score": score, "reasons": reasons}


def _payload_for(
    fusion: dict, result: Any, meta: dict, benchmarks: dict | None = None
) -> str:
    lines = [
        f"核心玩法：{meta['core_genre']}（{meta.get('core_genre_desc','')}）",
        f"融合玩法：{fusion['name']}",
        f"融合角色：{fusion.get('fusion_role','未指定')}",
        f"综合 ROI：{result.combined_roi}（优先级 {result.priority}，强侧 {result.strong_side}）",
    ]
    bk = (benchmarks or {}).get(fusion.get("id"))
    if fusion.get("seeds"):
        names = "、".join(s["name"] for s in fusion["seeds"])
        if any(s.get("benchmark_backed") for s in fusion["seeds"]):
            lines.append(
                f"已上线种子产品（同类标杆代理，非直接组合案例）：{names}；"
                f"这些是该品类在 Sensor Tower 检索到的真实上架游戏，代表『品类盘子』，"
                f"并不代表 SLG×本玩法 组合已被市场验证。"
            )
        else:
            lines.append(f"已上线种子产品：{names}")
    elif bk:
        lines.append(
            f"已上线种子产品：暂无人工整理的直接组合（SLG×本玩法）案例，"
            f"但本方向在 Sensor Tower 检索到 {len(bk)} 款同类标杆竞品"
            f"（即该品类的真实已上线案例），详见标杆竞品扫描。"
        )
    else:
        lines.append("已上线种子产品：无（该组合尚无验证案例）")
    if fusion.get("hypothesis"):
        lines.append(f"立项假设：{fusion['hypothesis']}")
    lines.append(
        f"「可融合的非SLG游戏」(ref_games) 输出**对象数组**：3-5 个体现「{fusion['name']}」品类的真实非SLG游戏，"
        "每项含 name / gameplay（玩法详细介绍）/ hook（吸量亮点）/ status（现状）四个字段；"
        "只列该品类的非SLG游戏，不要列 SLG 竞品，也不要在这里写与 SLG 的结合方式。"
    )

    for market, fs in result.by_market.items():
        dims = "、".join(
            f"{k}{v.score}" for k, v in fs.dims.items()
        )
        lines.append(f"[{market}] 五维：{dims}；系统初步风险参考 {fs.risk:.0f}%（请基于全部事实独立重新评估，勿照抄）")
        for dim in fs.dims.values():
            if not dim.imputed:
                lines.append(f"  - {dim.key} 依据：{dim.basis}")

    # 标杆竞品扫描：把 ST 搜到的真实游戏 + 指标喂给模型，让它产出
    # 现状 / 成功逻辑 / 可结合玩法（需求②）。
    bk = (benchmarks or {}).get(fusion.get("id"))
    if bk:
        lines.append(
            "\n【重要语义提示】下面列出的「标杆竞品」是该品类的头部纯玩法真实游戏（同类标杆），"
            "仅用于佐证「这个品类自身有市场盘子」。它们【不是】SLG×本玩法 的已上线组合产品。"
            "融合路线是否已被验证，请以本prompt上方的「已上线种子产品」为准（seed_count=0 即无直接组合案例）。"
            "撰写「现状 / 成功逻辑 / 可结合玩法」时请客观分析这些品类标杆，但务必说明："
            "它们是品类对标参照，不代表「SLG×本玩法」融合路线已被市场验证，也不要把它们写成 SLG 组合产品。"
        )
        lines.append(f"该方向同类品类标杆（ST 真实游戏，Top {len(bk)}，按收入）：")
        for g in bk:
            lines.append(
                f"  - {g['name']}：收入 ${g['revenue']:.0f}，"
                f"下载 {g['downloads']:.0f}，RPD ${g['rpd']:.2f}"
            )
        lines.append("请基于上述真实品类标杆，撰写「现状 / 成功逻辑 / 可结合玩法」三段分析，忠实于数据、不编造。")
    else:
        lines.append(
            "\n本方向暂未检索到任何标杆竞品（Sensor Tower 无对应真实游戏数据）。"
            "请将 status / success_logic / ref_mechanics 三个字段统一写成「目前没有竞品」，"
            "不要编造任何竞品名称、收入或市场数字。"
        )
    lines.append(
        "最后务必在返回 JSON 中额外输出 risk 字段："
        "{\"score\": 0-30 的数值（越高越危险）, \"reasons\": [\"理由1\", \"理由2\"]}。"
    )
    return "\n".join(lines)


def _fallback(
    fusion: dict, result: Any, meta: dict, benchmarks: dict | None = None
) -> dict[str, str]:
    """无模型时的数据陈述式占位 —— 只讲事实，把创作留白交给人。"""
    core = meta["core_genre"]
    name = fusion["name"]
    seeds = fusion.get("seeds") or []
    benchmark_proxy = any(s.get("benchmark_backed") for s in seeds)
    seed_text = "、".join(s["name"] for s in seeds) if seeds else "无"
    if benchmark_proxy:
        seed_text = f"{seed_text}（同类标杆代理，非直接组合案例）"

    strong = result.by_market.get(result.strong_side)
    dim_text = (
        "、".join(f"{k} {v.score}" for k, v in strong.dims.items()) if strong else "—"
    )

    bk = (benchmarks or {}).get(fusion.get("id"))
    if bk:
        glist = "、".join(g["name"] for g in bk[:5])
        status_txt = (
            f"已检索到真实竞品：{glist}。"
            "（当前为无模型占位态，需配置 LLM_API_KEY 由 AI 撰写「现状 / 成功逻辑 / 可结合玩法」分析。）"
        )
        sl_txt = "（同上，需配置 LLM_API_KEY 由 AI 撰写）"
        rm_txt = "（同上，需配置 LLM_API_KEY 由 AI 撰写）"
    else:
        status_txt = "目前没有竞品"
        sl_txt = "目前没有竞品"
        rm_txt = "目前没有竞品"

    return {
        "concept": fusion.get("hypothesis")
        or f"【待模型或人工填写】{core} × {name} 的核心循环设计。",
        "fit": (
            f"该组合已上线案例：{seed_text}。强侧市场 {result.strong_side} 五维表现：{dim_text}。"
            f"综合 ROI {result.combined_roi}，优先级 {result.priority}。"
        ),
        "acquisition": "【待填写】吸量钩子。<b>吸量要素：</b>需结合买量素材分析补充",
        "retention": "【待填写】留存设计",
        "monetization": "【待填写】付费设计（参考产品需上线≤5年）",
        "art": "【待填写】美术参考",
        "concept_art": "【待填写】概念图 3 张",
        "status": status_txt,
        "success_logic": sl_txt,
        "ref_mechanics": rm_txt,
        # 结构化模块：空数组 -> 模板显示「待补充」，不编造游戏名
        "ref_games": [],
    }


def generate_plans(
    config: dict,
    results: list,
    meta: dict,
    benchmarks: dict | None = None,
    verbose: bool = True,
) -> dict[str, dict[str, str]]:
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    base_url = (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"

    # 温度：不同模型约束不同（如 Moonshot kimi 系列只允许 1），故做成可配置项。
    # 默认值 0.7 对 OpenAI/DeepSeek 是稳妥取值；用 kimi 时请在配置里改成 1。
    try:
        temperature = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
    except (TypeError, ValueError):
        temperature = 0.7

    # 代理透传：与 ST 客户端一致，优先使用系统/环境变量里的代理（公司内网常需）。
    proxies: dict[str, str] = {}
    for _env in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        _v = os.environ.get(_env)
        if _v:
            proxies["http"] = _v
            proxies["https"] = _v
            break

    fusion_meta = {f["id"]: f for f in config["fusions"]}
    plans: dict[str, dict[str, str]] = {}
    risks: dict[str, dict] = {}

    if not api_key or requests is None:
        if verbose:
            _log("  [叙事层] 未配置 LLM_API_KEY，方案卡使用数据陈述式占位")
        for r in results:
            plans[r.fusion_id] = _fallback(fusion_meta[r.fusion_id], r, meta, benchmarks)
        return plans, {}

    # 并发度：默认 2 路同时。Moonshot/kimi 单次长文本生成慢，串行会累计很久；
    # 但并发太高会触发 429 限流，所以默认保守为 2，并在遇到 429 时**自动下调到 1**。
    # 可用环境变量 LLM_MAX_WORKERS 调整（1=完全串行最稳，3~5=更快但更易限流）。
    try:
        max_workers = int(os.environ.get("LLM_MAX_WORKERS", "2"))
    except (TypeError, ValueError):
        max_workers = 2
    max_workers = max(1, min(max_workers, len(results) or 1))

    # 超时/重试可调：握手超时 LLM_CONNECT_TIMEOUT（默认 30s）、
    # 读取超时 LLM_READ_TIMEOUT（默认 300s，给慢模型留足；若常超时可调大到 600）。
    # 重试次数 LLM_RETRIES（默认 5）。
    try:
        connect_timeout = float(os.environ.get("LLM_CONNECT_TIMEOUT", "30"))
    except (TypeError, ValueError):
        connect_timeout = 30
    try:
        read_timeout = float(os.environ.get("LLM_READ_TIMEOUT", "300"))
    except (TypeError, ValueError):
        read_timeout = 300
    try:
        retries = int(os.environ.get("LLM_RETRIES", "5"))
    except (TypeError, ValueError):
        retries = 5
    timeout = (connect_timeout, read_timeout)

    # 连通性自检：先发 1 个最小请求，连不上就几秒明确报错并给排查提示，
    # 避免傻等所有方向都超时（常见于网络/代理不通、key 失效、model 名写错、模型太慢）。
    if verbose:
        _log("  [叙事层] 正在做 LLM 连通性自检（1 个最小请求）...")
        pok, psecs, pmsg = _llm_preflight(base_url, api_key, model, proxies)
        if pok:
            _log(f"  [OK] LLM 连通：{pmsg}")
        else:
            _log(f"  [X] LLM 连通自检失败：{pmsg}")
            _log("      -> 常见原因：①本机连不上该域名（需代理/网络）；②API key 无效；③model 名写错；④该模型太慢触发读取超时。")
            _log("         排查：公司代理后请在 config.user.vbs 里设 HTTPS_PROXY=http://代理:端口；")
            _log("         或换更快的模型/base_url（如 DeepSeek：LLM_BASE_URL=https://api.deepseek.com/v1，LLM_MODEL=deepseek-chat，LLM_TEMPERATURE=0.7）。")

    schema = "、".join(f'"{k}"（{desc}）' for k, _, desc in MODULES)

    # 自适应并发控制：limit=当前允许同时在跑数，遇到 429 就 -1（最低 1）；
    # active=当前实际在跑数。两者配合实现"边跑边降速"——既不至于太慢，又不会持续 429。
    _lock = threading.Lock()
    _limit = {"n": max_workers}
    _active = {"c": 0}
    todo = list(results)

    def worker(r):
        fusion = fusion_meta[r.fusion_id]
        prompt = (
            f"{_payload_for(fusion, r, meta, benchmarks)}\n\n"
            f"请输出 JSON，键为：{schema}。"
        )
        # 超时/连接失败、以及 429 限流最多重试 retries 次；其余 HTTP 错误直接降级占位。
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": temperature,
                    },
                    timeout=timeout,
                    proxies=proxies or None,
                )
                resp.raise_for_status()
                payload = resp.json()
                text = payload["choices"][0]["message"]["content"].strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    text = text[4:].strip() if text.startswith("json") else text.strip()
                data = json.loads(text)
                usage = payload.get("usage") or {}
                plan = {k: _normalize_module_value(k, data.get(k)) for k, _, _ in MODULES}
                # 结构化模块若模型没给出合法数组，退回占位（空数组），不让脏数据进报告
                fb = _fallback(fusion, r, meta, benchmarks)
                for k in MODULE_KEYS:
                    if not plan.get(k):
                        plan[k] = fb.get(k, "")
                risk = _parse_risk(data)
                return fusion["id"], plan, True, usage, risk
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                # 区分连接被拒/DNS/握手超时/读取超时，便于定位"重连也连不上"的真实原因
                if isinstance(exc, requests.exceptions.ConnectionError):
                    reason = "连接失败(网络不可达/DNS/代理/被拒)"
                elif isinstance(exc, requests.exceptions.ConnectTimeout):
                    reason = f"连接超时(>{connect_timeout:.0f}s 未握手)"
                elif isinstance(exc, requests.exceptions.ReadTimeout):
                    reason = f"读取超时(>{read_timeout:.0f}s 未返回，模型可能太慢)"
                else:
                    reason = f"超时({type(exc).__name__})"
                if verbose:
                    _log(f"  [叙事层] {fusion['name']} {reason}（第{attempt}次），{'重试…' if attempt < retries else '放弃'}")
                _time.sleep(min(3 * attempt, 15))
                continue
            except requests.exceptions.HTTPError as exc:
                code = getattr(exc.response, "status_code", None)
                if code == 429:  # 限流：自动降速 + 退避后重试
                    with _lock:
                        if _limit["n"] > 1:
                            _limit["n"] -= 1
                        cur = _limit["n"]
                    wait = (4 * 2 ** (attempt - 1)) + random.uniform(0, 3)
                    if verbose:
                        _log(
                            f"  [叙事层] {fusion['name']} 触发限流(429)，"
                            f"并发自动下调至 {cur}，退避 {wait:.0f}s 后重试（第{attempt}次）"
                        )
                    _time.sleep(wait)
                    continue
                if verbose:
                    detail = ""
                    try:
                        detail = " | 服务端返回: " + (exc.response.text or "")[:300]
                    except Exception:  # noqa: BLE001
                        pass
                    _log(f"  [叙事层] {fusion['name']} 生成失败（HTTP {code}{detail}），改用占位")
                return fusion["id"], _fallback(fusion, r, meta, benchmarks), False, {}, None
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    _log(f"  [叙事层] {fusion['name']} 生成失败（{str(exc)[:80]}），改用占位")
                return fusion["id"], _fallback(fusion, r, meta, benchmarks), False, {}, None
        # retries 次均超时/限流
        if verbose:
            _log(f"  [叙事层] {fusion['name']} 多次超时/限流（{retries}次），改用占位")
        return fusion["id"], _fallback(fusion, r, meta, benchmarks), False, {}

    def run_one(r):
        try:
            return worker(r)
        finally:
            with _lock:
                _active["c"] -= 1

    if verbose:
        _log(f"  [叙事层] 生成 {len(results)} 个方向方案卡（起始并发 {max_workers}，遇 429 自动降速）")
    done = 0
    total = len(results)
    tot_prompt = tot_comp = tot_all = 0

    def try_submit(ex):
        while True:
            with _lock:
                if not todo or _active["c"] >= _limit["n"]:
                    break
                r = todo.pop(0)
                _active["c"] += 1
            f = ex.submit(run_one, r)
            futures_set.add(f)

    with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures_set = set()
        try_submit(ex)
        while futures_set:
            finished = _cf.wait(futures_set, return_when=_cf.FIRST_COMPLETED).done
            for f in finished:
                futures_set.discard(f)
                fid, plan, _ok, usage, _risk = f.result()
                plans[fid] = plan
                if _risk:
                    risks[fid] = _risk
                done += 1
                tot_prompt += int((usage or {}).get("prompt_tokens", 0) or 0)
                tot_comp += int((usage or {}).get("completion_tokens", 0) or 0)
                tot_all += int((usage or {}).get("total_tokens", 0) or 0)
                if verbose:
                    comp = int((usage or {}).get("completion_tokens", 0) or 0)
                    _log(
                        f"  {_bar(done, total)} {fusion_meta[fid]['name']} 完成"
                        + (f"  (本次输出 {_fmt_tokens(comp)} tok)" if comp else "  (占位)")
                    )
            try_submit(ex)
    if verbose:
        _log(
            f"  [叙事层] 全部完成：{total} 个方向 | "
            f"输入 {_fmt_tokens(tot_prompt)} + 输出 {_fmt_tokens(tot_comp)} "
            f"= 合计 {_fmt_tokens(tot_all)} tokens"
        )

    return plans, risks
