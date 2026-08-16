"""
计算层：把归一化指标转成五维评分与 ROI。

沿用「立项线索评估 v3.1.4」公式，只改语义（题材→融合玩法）：
    raw  = 吸量×0.25 + 空间×0.20 + 竞争×0.15 + 留存×0.15 + 付费×0.25
    roi_raw = raw × (1 - risk/100)
    ROI  = clamp(1, 10, 5.5 + (roi_raw - 5.5) × 2.0)
    组合 = 0.90 × max(CN, GL) + 0.10 × min(CN, GL)
    ASP  = 吸量×35.7% + 空间×28.6% + 付费×35.7%

指标 → 分数的映射不拍脑袋，两种方式：
    anchors     绝对分档（推荐），配置里给 [[阈值, 分数], ...]，对数插值
    percentile  相对分位，在本次全部候选中排序
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from collect import MetricBundle

WEIGHTS = {
    "traffic": 0.25,
    "space": 0.20,
    "competition": 0.15,
    "retention": 0.15,
    "payment": 0.25,
}
ASP_WEIGHTS = {"traffic": 0.357, "space": 0.286, "payment": 0.357}
DISPLAY_CENTER = 5.5
DISPLAY_GAIN = 2.0
MARKET_ALPHA = 0.90

DIM_LABELS = {
    "traffic": "吸量",
    "space": "空间",
    "competition": "竞争",
    "retention": "留存",
    "payment": "付费",
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def anchor_score(value: float | None, anchors: list[list[float]]) -> float | None:
    """对数插值分档。anchors 需按阈值升序，形如 [[5e7, 2], [2e8, 4], ...]"""
    if value is None or not anchors:
        return None
    pts = sorted(anchors, key=lambda a: a[0])
    if value <= pts[0][0]:
        return float(pts[0][1])
    if value >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= value <= x1:
            if x0 <= 0 or x1 <= 0:
                ratio = (value - x0) / (x1 - x0) if x1 != x0 else 0
            else:
                ratio = (math.log(value) - math.log(x0)) / (
                    math.log(x1) - math.log(x0)
                )
            return float(y0 + ratio * (y1 - y0))
    return float(pts[-1][1])


def percentile_score(
    value: float | None, population: list[float], reverse: bool = False
) -> float | None:
    """在参照池中的分位 → 1..10。reverse=True 表示值越小越好。"""
    if value is None or not population:
        return None
    pool = sorted(population)
    below = sum(1 for v in pool if v < value)
    pct = below / max(1, len(pool) - 1) if len(pool) > 1 else 0.5
    if reverse:
        pct = 1 - pct
    return 1 + pct * 9


@dataclass
class DimScore:
    key: str
    score: float | None # None 表示该维度无对应数据
    basis: str          # 依据说明，直接印在报告里
    source: str         # provenance
    imputed: bool = False
    missing: bool = False


@dataclass
class FusionScore:
    fusion_id: str
    fusion_name: str
    market: str
    dims: dict[str, DimScore] = field(default_factory=dict)
    risk: float = 0.0
    risk_reasons: list[str] = field(default_factory=list)
    raw: float = 0.0
    roi: float = 0.0
    asp: float = 0.0
    coverage: float = 0.0

    def dim(self, key: str) -> float | None:
        d = self.dims.get(key)
        return d.score if d else None


@dataclass
class FusionResult:
    fusion_id: str
    fusion_name: str
    fusion_role: str
    by_market: dict[str, FusionScore] = field(default_factory=dict)
    combined_roi: float = 0.0
    asp: float = 0.0
    strong_side: str = ""
    priority: str = ""
    coverage: float = 0.0
    notes: list[str] = field(default_factory=list)


DEFAULT_SCORING: dict[str, Any] = {
    "traffic": {
        "metric": "seed_downloads_12m",
        "anchors": [[2e6, 2], [1e7, 4], [4e7, 6], [1.2e8, 8], [4e8, 10]],
    },
    "space": {
        "metric": "genre_revenue_12m",
        # 锚点按「品类市场真实量级」设（GL 主要市场年化收入常见 $2B–$40B），
        # 上限抬到 $80B 避免大品类被一刀切顶到 10。若你的真实数据仍聚集在 10，
        # 调高这里或配置 scoring.space.anchors 即可。
        "anchors": [[2e9, 2], [8e9, 4], [2e10, 6], [4e10, 8], [8e10, 10]],
        "yoy_metric": "genre_revenue_yoy",
        "yoy_bonus": [[-0.15, -1.0], [0.0, 0.0], [0.25, 0.8], [0.6, 1.5]],
    },
    "competition": {
        "metric": "seed_count",
        "anchors": [[0, 8.5], [1, 8.0], [3, 6.5], [6, 5.0], [12, 3.2], [25, 2.0]],
        "concentration_metric": "top3_share",
        "concentration_penalty": [[0.4, 0.0], [0.7, -0.8], [0.9, -1.6]],
    },
    "retention": {
        "metric": "seed_retention_d7",
        "anchors": [[0.10, 2], [0.18, 4], [0.25, 6], [0.32, 8], [0.42, 10]],
    },
    "payment": {
        "metric": "seed_rpd",
        "anchors": [[0.5, 2], [2.0, 4], [5.0, 6], [12.0, 8], [30.0, 10]],
    },
}

DEFAULT_RISK = {
    "no_seed_penalty": 12.0,        # 无已上线验证案例
    "low_overlap_threshold": 0.25,  # 受众重叠低于此值视为融合风险
    "low_overlap_penalty": 10.0,
    "max_risk": 30.0,
}


class Scorer:
    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = config
        self.scoring = {**DEFAULT_SCORING}
        for key, override in (config.get("scoring") or {}).items():
            self.scoring[key] = {**self.scoring.get(key, {}), **override}
        self.risk_cfg = {**DEFAULT_RISK, **(config.get("risk") or {})}
        self.fusion_meta = {f["id"]: f for f in config["fusions"]}
        self.thresholds = config.get("priority", {"P1": 7.5, "P2": 6.0, "P3": 4.0})

    # ---------- 维度打分 ----------

    def _score_dim(
        self, key: str, bundle: MetricBundle, population: dict[str, list[float]]
    ) -> DimScore:
        spec = self.scoring[key]
        metric = spec["metric"]
        value = bundle.get(metric)
        source = bundle.provenance.get(metric, "missing")

        score = None
        if "anchors" in spec:
            score = anchor_score(value, spec["anchors"])
        if score is None and value is not None:
            score = percentile_score(
                value, population.get(metric, []), reverse=spec.get("reverse", False)
            )

        missing = False
        if score is None:
            missing = True
            basis = f"{metric} 无对应数据，不参与评分"
        else:
            basis = f"{metric}={_fmt(value)}"

        # 空间维度叠加增速修正（仅在有效打分时）
        if key == "space" and not missing:
            yoy = bundle.get(spec.get("yoy_metric", ""))
            bonus = anchor_score(yoy, spec.get("yoy_bonus", [])) if yoy is not None else None
            if bonus is not None:
                score = clamp(score + bonus, 1, 10)
                basis += f"，YoY={yoy:+.0%} 修正{bonus:+.1f}"

        # 竞争维度叠加集中度惩罚
        if key == "competition" and not missing:
            share = bundle.get(spec.get("concentration_metric", ""))
            penalty = (
                anchor_score(share, spec.get("concentration_penalty", []))
                if share is not None
                else None
            )
            if penalty is not None:
                score = clamp(score + penalty, 1, 10)
                basis += f"，Top3占比{share:.0%} 修正{penalty:+.1f}"

        return DimScore(
            key=key,
            score=None if missing else round(clamp(score, 1, 10), 2),
            basis=basis,
            source=source,
            imputed=False,
            missing=missing,
        )

    def _risk(self, bundle: MetricBundle) -> tuple[float, list[str]]:
        risk = 0.0
        reasons: list[str] = []

        fusion = self.fusion_meta[bundle.fusion_id]
        base = float(fusion.get("risk_base", 0) or 0)
        if base:
            risk += base
            reasons.append(f"配置基础风险 {base:.0f}%")

        if not bundle.get("seed_count"):
            penalty = self.risk_cfg["no_seed_penalty"]
            risk += penalty
            reasons.append(f"无已上线验证案例 +{penalty:.0f}%")

        overlap = bundle.get("audience_overlap")
        if overlap is not None and overlap < self.risk_cfg["low_overlap_threshold"]:
            penalty = self.risk_cfg["low_overlap_penalty"]
            risk += penalty
            reasons.append(f"受众重叠仅 {overlap:.0%} +{penalty:.0f}%")

        return min(risk, self.risk_cfg["max_risk"]), reasons

    # ---------- 主流程 ----------

    def score(self, bundles: list[MetricBundle]) -> list[FusionResult]:
        population: dict[str, list[float]] = {}
        for b in bundles:
            for key, value in b.values.items():
                population.setdefault(key, []).append(value)

        per_market: dict[tuple[str, str], FusionScore] = {}
        for bundle in bundles:
            meta = self.fusion_meta[bundle.fusion_id]
            fs = FusionScore(
                fusion_id=bundle.fusion_id,
                fusion_name=meta["name"],
                market=bundle.market,
                coverage=bundle.coverage,
            )
            for key in WEIGHTS:
                fs.dims[key] = self._score_dim(key, bundle, population)
            fs.risk, fs.risk_reasons = self._risk(bundle)

            # 缺失维度从加权和中剔除，并对剩余权重做归一化（避免缺失被静默当 0/5）
            avail = [(k, w) for k, w in WEIGHTS.items() if fs.dims[k].score is not None]
            wsum = sum(w for _, w in avail)
            fs.raw = (
                sum(fs.dims[k].score * w for k, w in avail) / wsum if wsum else 0.0
            )
            roi_raw = fs.raw * (1 - fs.risk / 100.0)
            fs.roi = round(
                clamp(DISPLAY_CENTER + (roi_raw - DISPLAY_CENTER) * DISPLAY_GAIN, 1, 10),
                2,
            )
            avail_asp = [
                (k, w) for k, w in ASP_WEIGHTS.items() if fs.dims[k].score is not None
            ]
            wsum_asp = sum(w for _, w in avail_asp)
            fs.asp = (
                sum(fs.dims[k].score * w for k, w in avail_asp) / wsum_asp
                if wsum_asp
                else 0.0
            )
            per_market[(bundle.fusion_id, bundle.market)] = fs

        results: list[FusionResult] = []
        for fusion in self.cfg["fusions"]:
            fid = fusion["id"]
            markets = {
                m: fs for (f, m), fs in per_market.items() if f == fid
            }
            if not markets:
                continue
            rois = {m: fs.roi for m, fs in markets.items()}
            hi_market = max(rois, key=lambda m: rois[m])
            lo_market = min(rois, key=lambda m: rois[m])
            combined = (
                MARKET_ALPHA * rois[hi_market] + (1 - MARKET_ALPHA) * rois[lo_market]
                if len(rois) > 1
                else rois[hi_market]
            )
            combined = round(combined, 2)

            result = FusionResult(
                fusion_id=fid,
                fusion_name=fusion["name"],
                fusion_role=fusion.get("fusion_role", ""),
                by_market=markets,
                combined_roi=combined,
                asp=round(
                    sum(fs.asp for fs in markets.values()) / len(markets), 2
                ),
                strong_side=hi_market,
                priority=self._priority(combined),
                coverage=round(
                    sum(fs.coverage for fs in markets.values()) / len(markets), 3
                ),
            )
            results.append(result)

        results.sort(key=lambda r: r.combined_roi, reverse=True)
        self.assign_priorities(results)
        return results

    def assign_priorities(self, results: list[FusionResult]) -> None:
        """按综合 ROI 相对分位把候选分到 P1-P4（各约 1/4），保证四档都有且区分清晰。

        为什么用相对分位而非写死的绝对阈值：综合 ROI 受 AI 风险、指标缺失等影响，
        绝对阈值（如 7.5/6.0/4.0）极易让全部候选塌缩到同一档（本项目就出现过
        「全 P3/P4」）。相对四分位保证 P1~P4 各占约 1/4，既能拉开区分度、又能直接用于
        立项排序决策。切分点写回 self._priority_bands 供报告摘要引用。
        """
        if not results:
            return
        rois = sorted((r.combined_roi for r in results), reverse=True)
        n = len(rois)

        def q(p: float) -> float:
            if n == 1:
                return rois[0]
            idx = (n - 1) * p
            lo = int(idx)
            frac = idx - lo
            if lo + 1 < n:
                return rois[lo] + (rois[lo + 1] - rois[lo]) * frac
            return rois[lo]

        t1, t2, t3 = q(0.25), q(0.50), q(0.75)
        for r in results:
            roi = r.combined_roi
            if roi >= t1:
                r.priority = "P1"
            elif roi >= t2:
                r.priority = "P2"
            elif roi >= t3:
                r.priority = "P3"
            else:
                r.priority = "P4"
        self._priority_bands = {"P1": round(t1, 2), "P2": round(t2, 2), "P3": round(t3, 2)}

    def _priority(self, roi: float) -> str:
        if roi >= self.thresholds["P1"]:
            return "P1"
        if roi >= self.thresholds["P2"]:
            return "P2"
        if roi >= self.thresholds["P3"]:
            return "P3"
        return "P4"

    def apply_ai_risk(self, results: list[FusionResult], risks: dict) -> None:
        """用 AI 自评估的风险覆盖确定性风险，并重算 roi / combined_roi / priority。

        risks: {fusion_id: {"score": float(0-30), "reasons": [str]}}
        仅覆盖 AI 给出有效值的融合；其余保留确定性风险（无 AI 时的兜底）。
        调用前 score() 已算好各维度分数与 raw（不含风险），这里只重算风险相关部分。
        """
        for r in results:
            ai = (risks or {}).get(r.fusion_id)
            if not ai or ai.get("score") is None:
                continue
            try:
                score = float(ai["score"])
            except (TypeError, ValueError):
                continue
            score = max(0.0, min(self.risk_cfg["max_risk"], score))
            reasons = [str(x) for x in (ai.get("reasons") or []) if str(x).strip()][:4]
            for fs in r.by_market.values():
                fs.risk = score
                if reasons:
                    fs.risk_reasons = reasons
                roi_raw = fs.raw * (1 - score / 100.0)
                fs.roi = round(
                    clamp(DISPLAY_CENTER + (roi_raw - DISPLAY_CENTER) * DISPLAY_GAIN, 1, 10),
                    2,
                )
            rois = {m: fs.roi for m, fs in r.by_market.items()}
            if len(rois) > 1:
                hi = max(rois, key=lambda m: rois[m])
                lo = min(rois, key=lambda m: rois[m])
                combined = MARKET_ALPHA * rois[hi] + (1 - MARKET_ALPHA) * rois[lo]
            else:
                combined = next(iter(rois.values()))
            r.combined_roi = round(combined, 2)
            # 优先级在 run.py 重新排序后由 assign_priorities 统一按相对分位重算，这里不单独赋值


def audit(results: list[FusionResult]) -> dict[str, Any]:
    """数学校验 + 数据质量统计，直接渲染进报告的审计章节。"""
    checks: list[dict[str, str]] = []
    ok = True

    for r in results:
        rois = [fs.roi for fs in r.by_market.values()]
        if rois and r.combined_roi > max(rois) + 1e-6:
            ok = False
            checks.append(
                {"item": f"{r.fusion_name} 组合ROI ≤ 单市场最大值", "status": "FAIL"}
            )
    checks.append(
        {
            "item": f"组合ROI ≤ max(单市场) 校验 {len(results)}/{len(results)}",
            "status": "PASS" if ok else "FAIL",
        }
    )

    missing = sum(
        1
        for r in results
        for fs in r.by_market.values()
        for d in fs.dims.values()
        if d.missing
    )
    total_dims = sum(len(fs.dims) for r in results for fs in r.by_market.values())
    coverage = 1 - missing / total_dims if total_dims else 0

    checks.append(
        {
            "item": f"指标覆盖率 {coverage:.0%}（{total_dims - missing}/{total_dims} 个维度有实测数据）",
            "status": "PASS" if coverage >= 0.6 else "WARN",
        }
    )

    spread = (
        f"{min(r.combined_roi for r in results):.2f} – {max(r.combined_roi for r in results):.2f}"
        if results
        else "N/A"
    )
    checks.append({"item": f"ROI 分布区间 {spread}", "status": "INFO"})

    return {
        "checks": checks,
        "coverage": coverage,
        "imputed_dims": missing,
        "total_dims": total_dims,
        "passed": ok,
    }


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.1f}万"
    if abs(value) < 1:
        return f"{value:.1%}" if abs(value) < 1 else f"{value:.2f}"
    return f"{value:.2f}"
