"""
采集层：把 Sensor Tower 的原始数据归一化成"每个融合方向 × 每个市场"的指标包。

核心方法论 —— 种子产品法：
    Sensor Tower 的分类体系是单标签（Primary Genre + Sub-genre），
    查不到"SLG × 三消"这类组合品类。因此对每个候选组合，先圈定已上线的
    代表产品（种子产品），再用 ST 拉这些产品的真实指标聚合成"组合大盘"。
    找不到种子产品的格子 = 无验证案例，走蓝海/伪需求判定分支。

每个指标都带 provenance（来源标记），报告的审计章节据此自动生成。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from st_client import STClient

# provenance 取值
SRC_API = "sensortower_api"
SRC_CFG = "config"
SRC_NONE = "missing"

# 留存 D7 在 ST aggregate_tags 里的候选字段名（沙箱无 token 无法实测真实键名，
# 故多候选 + 诊断日志；命中后以「比例」归一：>1 视为百分比）。
# 依据 ST 富属性文档，主用 retention_7d_ww / retention_7d_us（第 7 日留存）。
_RETENTION_D7_KEYS = (
    "retention_7d_ww", "retention_7d_us", "retention_7d", "retention_7day",
    "retention_d7", "d7_retention", "retain_d7", "retention_d7_pct",
    "retention_7d_pct", "day7_retention", "day_7_retention", "retention_7",
    "d7", "retention_rate_d7", "ret_7d", "d7_ret", "seventh_day_retention",
    "retention_d7_ww", "retention_d7_us", "retention_7d_ww_pct", "retention_7d_us_pct",
)


def _extract_retention_d7(tags: dict, unmatched: set[str]) -> float | None:
    """从 aggregate_tags 里捞 D7 留存；返回 0-1 比例或 None。

    两道兜底：
      1) 直接候选键（_RETENTION_D7_KEYS）；
      2) 扫描所有键里含 retention / retain / _d7 / 结尾 d7 字样的数值（兼容未知命名）。
    命中后以「比例」归一：>1 视为百分比。全部未命中则记录所有键供诊断。
    """
    if not isinstance(tags, dict):
        return None
    # 1) 直接候选键
    for k in _RETENTION_D7_KEYS:
        if k in tags and tags[k] is not None:
            try:
                v = float(tags[k])
            except (TypeError, ValueError):
                continue
            if v > 1:  # 百分比 -> 比例
                v = v / 100.0
            if 0 < v <= 1:
                return v
    # 2) 兜底：扫描任意疑似留存键
    for k, v in tags.items():
        kl = str(k).lower()
        if ("retain" in kl) or kl.endswith("d7") or ("_d7" in kl) or kl == "d7":
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            if val > 1:
                val = val / 100.0
            if 0 < val <= 1:
                return val
    if tags:
        unmatched.update(tags.keys())
    return None


@dataclass
class MetricBundle:
    """一个融合方向在一个市场下的全部指标。"""

    fusion_id: str
    market: str
    values: dict[str, float] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def put(self, key: str, value: float | None, source: str) -> None:
        if value is None:
            self.provenance[key] = SRC_NONE
            return
        self.values[key] = float(value)
        self.provenance[key] = source

    def get(self, key: str, default: float | None = None) -> float | None:
        return self.values.get(key, default)

    @property
    def coverage(self) -> float:
        """指标覆盖率，用于报告里标注该方向的数据可信度。"""
        total = len(self.provenance)
        if not total:
            return 0.0
        known = sum(1 for s in self.provenance.values() if s != SRC_NONE)
        return known / total


# 打分依赖的指标清单。缺失的会在报告里显式标注，不会静默当 0。
REQUIRED_METRICS = [
    "seed_count",            # 已上线的该组合产品数 —— 竞争 & 验证度
    "seed_downloads_12m",    # 种子产品近 12 月下载合计 —— 吸量
    "seed_revenue_12m",      # 种子产品近 12 月收入合计 —— 付费 & 空间
    "seed_rpd",              # 收入 / 下载 —— 付费强度
    "seed_retention_d7",     # 加权 D7 留存 —— 留存
    "top3_share",            # 头部三款收入占比 —— 竞争集中度
    "genre_revenue_12m",     # 该融合玩法自身品类大盘 —— 空间上限
    "genre_revenue_yoy",     # 品类同比 —— 增速信号
    "audience_overlap",      # 与核心玩法的受众重叠率 —— 融合风险
]


class Collector:
    def __init__(self, config: dict[str, Any], root: Path) -> None:
        self.cfg = config
        self.root = root
        st_cfg = config.get("st", {})
        self.client = STClient(
            cache_dir=root / "data" / "cache",
            endpoints=st_cfg.get("endpoints"),
        )
        self.markets: dict[str, list[str]] = st_cfg.get(
            "markets", {"CN": ["CN"], "GL": ["US"]}
        )
        self.date_from = st_cfg.get("date_from", "2025-01-01")
        self.date_to = st_cfg.get("date_to", "2026-06-30")
        # 核心玩法品类 id：SLG→7017(Strategy)，RPG→7014(Role Playing)。
        # 用于「核心玩法基本盘」与作为融合方向「空间」维度的默认品类。
        # 每个融合方向可在 fusions[].category_id 覆盖为自己玩法对应的 ST 品类 id，
        # 从而让「空间」维度按各自市场天花板区分（避免全部顶到 10）。
        self.games_category_id = st_cfg.get("games_category_id", 7017)
        self._genre: dict[tuple, dict] = {}       # (category_id, market) -> {revenue_12m, yoy}
        self.core_market: dict[str, dict] = {}     # market -> {revenue_12m, yoy}

    # ---------- 采集 ----------

    def collect(self) -> list[MetricBundle]:
        use_api = self.client.api_available
        # 先按 (品类id, 市场) 把品类天花板抓好并缓存；核心玩法品类单独存一份给「基本盘」
        if use_api:
            cats: set[int] = {self.games_category_id}
            for fusion in self.cfg["fusions"]:
                raw = str(fusion.get("category_id", "")).strip()
                if raw:
                    try:
                        cats.add(int(raw))
                    except ValueError:
                        pass
            for cat in cats:
                for market in self.markets:
                    self._genre[(cat, market)] = self._compute_genre(market, cat)
            for market in self.markets:
                self.core_market[market] = self._genre.get(
                    (self.games_category_id, market)
                ) or self._compute_genre(market, self.games_category_id)
        bundles: list[MetricBundle] = []
        total = len(self.cfg["fusions"]) * len(self.markets)
        done = 0
        for fusion in self.cfg["fusions"]:
            for market in self.markets:
                bundles.append(self._collect_one(fusion, market))
                done += 1
                print(f"      · 指标包 {done}/{total}：{fusion.get('name', '?')} × {market}")
        return bundles

    def _compute_genre(self, market: str, category_id: int) -> dict[str, float]:
        """用 games_breakdown 抓某品类大盘，作为「空间」维度（或核心基本盘）的天花板。

        一次拉取「上一周期 + 当前周期」的扩展区间，再拆分出当前与同比，省一半请求。
        返回 {revenue_12m(USD), yoy(小数)}；失败或为空返回 {}（交给 CSV / 标记缺失）。
        ST 该端点 ar/ir 为分，需 /100 换算成美元；os=ios 也会同时返回 android+ios。
        """
        countries = self.markets[market]
        try:
            df = datetime.date.fromisoformat(self.date_from)
            dt = datetime.date.fromisoformat(self.date_to)
            span = (dt - df).days
            prev_from = (df - datetime.timedelta(days=span)).isoformat()
            rows = self.client.games_breakdown(
                category_id, countries, prev_from, self.date_to, "ios", "quarterly"
            )
            rows = rows if isinstance(rows, list) else rows.get("data", [])
        except Exception:  # noqa: BLE001
            return {}

        def _in(r: dict, a: datetime.date, b: datetime.date) -> bool:
            try:
                d = datetime.date.fromisoformat(str(r.get("d", ""))[:10])
            except Exception:  # noqa: BLE001
                return False
            return a <= d <= b

        cur = sum(
            (float(r.get("ar") or 0) + float(r.get("ir") or 0)) / 100.0
            for r in rows if _in(r, df, dt)
        )
        prev = sum(
            (float(r.get("ar") or 0) + float(r.get("ir") or 0)) / 100.0
            for r in rows
            if _in(r, datetime.date.fromisoformat(prev_from), df - datetime.timedelta(days=1))
        )
        if cur <= 0:
            return {}
        out: dict[str, float] = {"revenue_12m": cur}
        if prev > 0:
            out["yoy"] = (cur - prev) / prev
        return out

    def _fill_genre_from_api(self, bundle: MetricBundle, market: str, category_id: int) -> None:
        g = self._genre.get((category_id, market)) or {}
        if g.get("revenue_12m"):
            bundle.put("genre_revenue_12m", g["revenue_12m"], SRC_API)
        if g.get("yoy") is not None:
            bundle.put("genre_revenue_yoy", g["yoy"], SRC_API)

    def _collect_one(self, fusion: dict[str, Any], market: str) -> MetricBundle:
        fid = fusion["id"]
        bundle = MetricBundle(fusion_id=fid, market=market)
        for key in REQUIRED_METRICS:
            bundle.provenance.setdefault(key, SRC_NONE)

        use_api = self.client.api_available

        seeds = fusion.get("seeds", []) or []
        cat = int(fusion["category_id"]) if str(fusion.get("category_id", "")).strip() else self.games_category_id
        # 留存标签：每个 fusion 抓一次（按品类 + 全市场国家），供各市场复用，避免重复打榜接口
        tags_map: dict[str, dict] = {}
        if use_api:
            all_countries = sorted({c for m in self.markets.values() for c in m})
            tags_map = self._get_tags_for_cat(cat, all_countries)
        if use_api:
            self._fill_from_api(bundle, fusion, market, seeds, cat, tags_map)
            self._fill_genre_from_api(bundle, market, cat)
        self._fill_from_config(bundle, fusion, market)
        self._derive(bundle, seeds)
        return bundle

    def _get_tags_for_cat(self, cat: int, all_countries: list[str]) -> dict[str, dict]:
        """按 (品类, 全市场国家) 缓存榜单 aggregate_tags，避免同一 fusion 多市场重复拉取。"""
        key = (cat, tuple(all_countries))
        if getattr(self, "_tags_cache", None) is None:
            self._tags_cache: dict[tuple, dict] = {}
        if key in self._tags_cache:
            return self._tags_cache[key]
        tags = self.client.fetch_tags_by_category(cat, all_countries, self.date_from, self.date_to)
        self._tags_cache[key] = tags
        return tags

    def _fill_from_api(
        self,
        bundle: MetricBundle,
        fusion: dict[str, Any],
        market: str,
        seeds: list[dict[str, Any]],
        cat: int,
        tags_map: dict[str, dict],
    ) -> None:
        """从 ST API 拉种子产品指标并聚合。失败时不中断，留给 CSV / 配置兜底。"""
        countries = self.markets[market]
        app_ids: list[str] = []
        for seed in seeds:
            aid = str(seed.get("st_app_id") or "").strip()
            if not aid and seed.get("name"):
                try:
                    hit = self.client.search_app(seed["name"])
                    apps = hit.get("apps", hit) if isinstance(hit, dict) else hit
                    if isinstance(apps, list) and apps:
                        # 优先精确同名，否则取首个命中
                        name_l = str(seed["name"]).strip().lower()
                        match = next(
                            (a for a in apps if str(a.get("name", "")).strip().lower() == name_l),
                            apps[0],
                        )
                        aid = str(match.get("app_id") or match.get("unified_app_id") or "")
                except Exception as exc:  # noqa: BLE001
                    bundle.notes.append(f"搜索 {seed.get('name')} 失败：{str(exc)[:80]}")
            if aid:
                app_ids.append(aid)

        if not app_ids:
            return

        rows = self._fetch_unified(app_ids, countries)
        revenue = 0.0
        downloads = 0.0
        per_app: dict[str, float] = {}
        for row in rows:
            rev = row.get("revenue") or row.get("unified_revenue") or 0
            dl = row.get("downloads") or row.get("unified_units") or 0
            # unified 端点返回的是分，统一换算成美元
            rev = float(rev) / 100.0 if rev and float(rev) > 1e6 else float(rev or 0)
            revenue += rev
            downloads += float(dl or 0)
            key = str(row.get("unified_app_id") or row.get("app_id") or "?")
            per_app[key] = per_app.get(key, 0.0) + rev

        if revenue:
            bundle.put("seed_revenue_12m", revenue, SRC_API)
        if downloads:
            bundle.put("seed_downloads_12m", downloads, SRC_API)
        if per_app and revenue > 0:
            top3 = sum(sorted(per_app.values(), reverse=True)[:3])
            bundle.put("top3_share", top3 / revenue, SRC_API)

        # 留存 D7：从 ST aggregate_tags（top_charts 返回）捞取种子产品真实留存，按收入加权。
        # 沙箱无 token 无法实测真实键名，未命中时记录 aggregate_tags 全部键到 notes 便于真机核对。
        if tags_map and app_ids:
            rets: list[float] = []
            weights: list[float] = []
            unmatched: set[str] = set()
            for aid in app_ids:
                tags = tags_map.get(aid)
                if not tags:
                    continue
                d7 = _extract_retention_d7(tags, unmatched)
                if d7 is not None:
                    rev = per_app.get(aid, 0.0)
                    rets.append(d7)
                    weights.append(rev if rev > 0 else 1.0)
            if unmatched:
                bundle.notes.append(
                    f"aggregate_tags 含字段 {sorted(unmatched)}，未匹配留存D7键，请反馈真实键名以补全"
                )
            if rets:
                wsum = sum(weights)
                seed_ret = (
                    sum(r * w for r, w in zip(rets, weights)) / wsum if wsum > 0
                    else sum(rets) / len(rets)
                )
                bundle.put("seed_retention_d7", seed_ret, SRC_API)

    def _fetch_unified(self, app_ids: list[str], countries: list[str]) -> list[dict]:
        """拉单品销售；先尝试合并调用，失败/空则逐 app 重试，提升健壮性。"""

        def _call(ids: list[str]) -> list[dict]:
            data = self.client.unified_sales(
                ids, countries, self.date_from, self.date_to, "quarterly"
            )
            return data if isinstance(data, list) else data.get("data", [])

        try:
            rows = _call(app_ids)
            if rows:
                return rows
        except Exception:  # noqa: BLE001
            pass
        rows = []
        for aid in app_ids:
            try:
                rows.extend(_call([aid]))
            except Exception:  # noqa: BLE001
                continue
        return rows

    def _fill_from_config(
        self, bundle: MetricBundle, fusion: dict[str, Any], market: str
    ) -> None:
        """配置里的 assumptions 是最后兜底，会在报告里标为"人工估值"。"""
        assumptions = (fusion.get("assumptions") or {}).get(market, {})
        for key, value in assumptions.items():
            if key in REQUIRED_METRICS and key not in bundle.values:
                bundle.put(key, float(value), SRC_CFG)

    def _derive(self, bundle: MetricBundle, seeds: list[dict[str, Any]]) -> None:
        """派生指标 + 兜底。"""
        # seed_count = 直接组合（SLG×本玩法）的已上线验证案例数。
        # benchmark_backed 的种子是同类品类标杆的代理，不算「直接组合案例」，不计入，
        # 避免把品类盘子误读成已验证融合产品（风险模型照常给无案例惩罚）。
        real_seeds = [s for s in seeds if not s.get("benchmark_backed")]
        if "seed_count" not in bundle.values:
            bundle.put("seed_count", float(len(real_seeds)), SRC_CFG)

        # 注意：受众重叠（audience_overlap）目前仍无 API 来源，保留 SRC_NONE（显示「无相关数据」）。
        # 留存 D7 已接入 ST aggregate_tags（见 _fill_from_api），命中即填充 seed_retention_d7。
        # 两者缺失时均不参与评分，绝不编造兜底估值。

        revenue = bundle.get("seed_revenue_12m")
        downloads = bundle.get("seed_downloads_12m")
        if "seed_rpd" not in bundle.values and revenue and downloads:
            bundle.put("seed_rpd", revenue / downloads, bundle.provenance.get(
                "seed_revenue_12m", SRC_CFG
            ))

        if not bundle.get("seed_count"):
            tag = "（标杆为同类品类代理）" if any(s.get("benchmark_backed") for s in seeds) else ""
            bundle.notes.append(f"无已上线组合产品{tag} —— 蓝海或伪需求，需人工判定")

    # ---------- 标杆竞品扫描（需求②）----------

    def _load_library(self) -> dict[str, Any]:
        """加载 configs/fusion_library.yaml，按 id 与中文名建索引，便于给原配置补英文搜索词。"""
        if getattr(self, "_library", None) is not None:
            return self._library
        self._library: dict[str, Any] = {}
        p = self.root / "configs" / "fusion_library.yaml"
        try:
            import yaml

            if p.exists():
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                for entry in data.get("library", []):
                    if entry.get("id"):
                        self._library[entry["id"]] = entry
                    if entry.get("name"):
                        self._library[entry["name"]] = entry
        except Exception:  # noqa: BLE001
            pass
        return self._library

    def _benchmark_terms(self, fusion: dict[str, Any], lib: dict[str, Any]) -> list[str]:
        """为某个 fusion 构造 ST 搜索词：自身关键词 + 融合库英文词/exemplars + 子品类名。

        关键：ST search_entities 按英文 app 名匹配，中文词几乎必 0 命中。所以原配置
        （如 slg.yaml）即使没写 benchmark_keywords，也能靠融合库的英文词搜到真实游戏。
        排序把纯英文放前面（命中率高），中文兜底。
        """
        terms: list[str] = list(fusion.get("benchmark_keywords") or [])
        entry = lib.get(fusion.get("id")) or lib.get(fusion.get("name", "")) or {}
        if entry:
            terms += list(entry.get("benchmark_keywords") or [])
            terms += list(entry.get("exemplars") or [])
        terms += [fusion.get("st_subgenre", "")]
        # 去重保序
        seen: set[str] = set()
        out: list[str] = []
        for t in terms:
            t = str(t).strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        # 英文优先（any(ord>127) 为非 ASCII 标记，False 排前面），但保留中文词——
        # 中文词能搜到国服/ localized 游戏，是 CN 市场补齐吸量/付费数据的关键来源。
        out.sort(key=lambda t: any(ord(c) > 127 for c in t))
        return out[:10]

    def _fetch_benchmark_sales(
        self, app_ids: list[str], countries: list[str]
    ) -> tuple[list[dict], str]:
        """拉候选游戏规模：先 unified，拿不到（多为 id 口径不匹配）再回退分平台 ios。"""
        rows = self._fetch_unified(app_ids, countries)
        if rows:
            return rows, "unified"
        try:
            data = self.client.sales_report(
                app_ids, countries, self.date_from, self.date_to, "ios", "quarterly"
            )
            rows = data if isinstance(data, list) else data.get("data", [])
        except Exception:  # noqa: BLE001
            rows = []
        return (rows or []), ("ios" if rows else "none")

    def collect_benchmarks(
        self,
        config: dict[str, Any],
        top_n: int = 8,
        search_limit: int = 20,
    ) -> dict[str, list[dict]]:
        """发现每个融合方向里的真实标杆游戏（纯玩法向），供叙事层做竞品扫描。

        流程：搜索词（英文优先）→ discover_games 取候选 app_id → 拉规模 → 按收入排 Top-N。
        仅在 API 模式可用；无 token 时返回 {}（模板显示「需配置 token」），不报错。
        每个融合都打印诊断（命中数 / 有规模数据数），万一仍 0 能从日志快速定位卡在哪一步。
        """
        out: dict[str, list[dict]] = {}
        use_api = self.client.api_available
        if not use_api:
            return out

        all_countries = sorted({c for m in self.markets.values() for c in m})
        lib = self._load_library()

        for idx, fusion in enumerate(config["fusions"], 1):
            fid = fusion["id"]
            terms = self._benchmark_terms(fusion, lib)
            print(
                f"      · 标杆扫描 {idx}/{len(config['fusions'])}："
                f"{fusion.get('name', '?')}（关键词 {len(terms)} 个）"
            )

            app_ids: list[str] = []
            name_map: dict[str, str] = {}
            for term in terms:
                try:
                    hits = self.client.discover_games(term, None, None, search_limit)
                except Exception:  # noqa: BLE001
                    hits = []
                for h in hits:
                    aid = h.get("app_id", "")
                    if aid and aid not in app_ids:
                        app_ids.append(aid)
                        name_map[aid] = h.get("name", aid)
                if len(app_ids) >= search_limit * 2:
                    break
            print(f"        搜索命中 {len(app_ids)} 个候选 app_id")

            if not app_ids:
                out[fid] = []
                continue

            rows, tag = self._fetch_benchmark_sales(app_ids, all_countries)
            print(f"        有规模数据的 {len(rows)} 条（口径 {tag}）")

            games: list[dict] = []
            for row in rows:
                rev = row.get("revenue") or row.get("unified_revenue") or 0
                dl = row.get("downloads") or row.get("unified_units") or 0
                # unified / ios 端点返回分，超 1e6 视为分需 /100 换算成美元
                rev = float(rev) / 100.0 if rev and float(rev) > 1e6 else float(rev or 0)
                dl = float(dl or 0)
                aid = str(row.get("unified_app_id") or row.get("app_id") or "")
                rpd = (rev / dl) if dl > 0 else 0.0
                games.append(
                    {
                        "name": name_map.get(aid, aid) or aid,
                        "app_id": aid,
                        "revenue": rev,
                        "downloads": dl,
                        "rpd": rpd,
                    }
                )
            games.sort(key=lambda g: g["revenue"], reverse=True)
            out[fid] = games[:top_n]
        return out

    def backfill_seeds(
        self, config: dict[str, Any], benchmarks: dict[str, list[dict]]
    ) -> None:
        """对 seeds 为空的融合方向，把 collect_benchmarks 已检索到的同类标杆 app_id 回填成种子。

        - 目的：让空种子的创新组合方向也能以**完全相同口径**拿到吸量/付费/留存
          （走 _fill_from_api 同一套 ST 接口 + 同一套换算/求和），不再整片「无相关数据」。
        - 诚实性：回填的种子标记 benchmark_backed=True；_derive 里 seed_count 只统计「直接组合案例」，
          故标杆代理不计入（仍显示种子数为 0，风险模型照常给无案例惩罚），
          避免把品类盘子误读成已验证融合产品。
        - 性能：collect_benchmarks 已为这些 app_id 拉过销售（all_countries），此处 _fill_from_api 再按市场拉取；
          st_client 默认缓存，重复请求命中 data/cache，无额外成本。
        """
        for fusion in config["fusions"]:
            if fusion.get("seeds"):
                continue
            bk = benchmarks.get(fusion["id"]) or []
            injected = [
                {
                    "name": g.get("name", ""),
                    "st_app_id": g.get("app_id", ""),
                    "benchmark_backed": True,
                }
                for g in bk
                if g.get("app_id")
            ]
            if injected:
                fusion["seeds"] = injected
                fusion["_benchmark_seeded"] = True
