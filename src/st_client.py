"""
Sensor Tower 数据接入层。

API 模式：环境变量 SENSORTOWER_AUTH_TOKEN 存在时启用，直连 api.sensortower.com（本工具仅使用 Sensor Tower 真实数据，无手工/示例数据降级路径）。

所有 API 原始响应落盘到 data/cache/，便于复现与审计。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # requests 为可选依赖，缺失时无法直连 ST API
    requests = None  # type: ignore

BASE_URL = "https://api.sensortower.com"

# 端点表。不同 ST 套餐可能有差异，可在 configs 里用 st.endpoints 覆盖。
ENDPOINTS = {
    # 品类大盘（市场总量）。os: ios|android
    "games_breakdown": "/v1/{os}/games_breakdown",
    # 单品收入/下载，unified 会聚合所有区域 SKU
    "unified_sales": "/v1/unified/sales_report_estimates",
    "sales": "/v1/{os}/sales_report_estimates",
    # 榜单 + 富属性（含 aggregate_tags：留存 / DAU / RPD 等）
    "top_charts": "/v1/{os}/sales_report_estimates_comparison_attributes",
    # 活跃用户
    "active_users": "/v1/{os}/top_and_trending/active_users",
    # 按 custom fields filter 取 app 列表
    "app_tag": "/v1/app_tag/apps",
    # app 详情 / 搜索
    "app_details": "/v1/{os}/apps",
    "search": "/v1/{os}/search_entities",
}


class STError(RuntimeError):
    pass


class STClient:
    """Sensor Tower 客户端。无 token 时 api_available 为 False，调用方应走 CSV 分支。"""

    def __init__(
        self,
        token: str | None = None,
        cache_dir: str | Path = "data/cache",
        endpoints: dict[str, str] | None = None,
        min_interval: float = 0.35,
    ) -> None:
        self.token = token or os.environ.get("SENSORTOWER_AUTH_TOKEN") or ""
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.endpoints = {**ENDPOINTS, **(endpoints or {})}
        self.min_interval = min_interval
        self._last_call = 0.0
        # 代理支持：公司内网常需在代理后访问 api.sensortower.com。
        # requests 默认也会读系统/环境变量代理，这里显式收集便于统一与诊断。
        self._proxies: dict[str, str] = {}
        for _env in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            _v = os.environ.get(_env)
            if _v:
                self._proxies["http"] = _v
                self._proxies["https"] = _v
                break

    @property
    def api_available(self) -> bool:
        return bool(self.token) and requests is not None

    # ---------- 底层 ----------

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def _throttle(self) -> None:
        gap = time.time() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.time()

    def get(
        self,
        endpoint_key: str,
        os_name: str = "unified",
        params: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> Any:
        """调用一个 ST 端点。命中缓存则不消耗 API 配额。"""
        if not self.api_available:
            raise STError("未配置 SENSORTOWER_AUTH_TOKEN，无法调用 API")

        path = self.endpoints[endpoint_key].format(os=os_name)
        query = dict(params or {})
        query["auth_token"] = self.token

        cache_key = json.dumps(
            {"p": path, "q": {k: v for k, v in query.items() if k != "auth_token"}},
            sort_keys=True,
            ensure_ascii=False,
        )
        cache_file = self._cache_path(cache_key)
        if use_cache and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        self._throttle()
        try:
            resp = requests.get(
                BASE_URL + path, params=query, timeout=(10, 30), proxies=self._proxies or None
            )
        except requests.exceptions.ConnectionError as exc:
            raise STError(
                "无法连接到 api.sensortower.com（网络层失败，请求根本没发出去）。\n"
                "这说明不是 token 或接口问题，而是网络不通。常见两种原因：\n"
                "  ① 你跑这个命令的终端/机器当前没有外网（例如处于沙箱/内网隔离）；\n"
                "  ② 公司网络需要代理，但 Python 没有走代理。\n"
                "排查：在『同一个终端』里执行\n"
                "      python -c \"import requests;print(requests.get('https://api.sensortower.com',timeout=10).status_code)\"\n"
                "若仍失败且你在公司代理后，先设置：\n"
                "      set HTTPS_PROXY=http://你的代理地址:端口\n"
                "      set HTTP_PROXY=http://你的代理地址:端口\n"
                "然后重新运行本程序。\n"
                f"原始错误：{str(exc)[:300]}"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise STError(
                f"连接 api.sensortower.com 超时（60s）。可能是网络慢或被拦截。\n原始错误：{str(exc)[:300]}"
            ) from exc
        if resp.status_code != 200:
            hint = ""
            if resp.status_code == 401:
                hint = ("\n-> 401 认证失败：token 无效/过期/被吊销，或认证方式不对"
                        "（经典 API 用 ?auth_token=，企业版用 Authorization: Bearer 请求头）。"
                        "请到 sensortower.com/users/edit 重新复制完整 token，重设 SENSORTOWER_AUTH_TOKEN 再试。")
            elif resp.status_code == 403:
                hint = ("\n-> 403 无权限：token 有效，但你的套餐不含该接口。"
                        "需升级套餐以开通该接口（本工具仅使用 ST API 真实数据，无 CSV 降级路径）。")
            elif resp.status_code == 404:
                hint = ("\n-> 404 接口/路径不存在：该端点可能不在你的套餐，或路径已变更。"
                        "可忽略单个 404，不影响其他接口。")
            raise STError(
                f"ST API {resp.status_code} @ {path}{hint}\n"
                f"params={ {k: v for k, v in query.items() if k != 'auth_token'} }\n"
                f"body={resp.text[:400]}"
            )
        data = resp.json()
        cache_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return data

    # ---------- 语义化封装 ----------

    def discover_fields(self) -> Any:
        """自定义字段枚举在本 API 套餐下不可用（无公开列举端点）。
        替代做法：在 ST 网页端(app.sensortower.com)配置过滤器后，从 URL 复制
        custom_fields_filter_id 使用。"""
        raise STError(
            "自定义字段无法经 API 枚举：该端点在不同套餐下不可用。"
            "请在 ST 网页端创建过滤器，复制 URL 中的 custom_fields_filter_id 填入配置。"
        )

    def games_breakdown(
        self,
        categories: str | int,
        countries: list[str],
        date_from: str,
        date_to: str,
        os_name: str = "ios",
        granularity: str = "quarterly",
    ) -> Any:
        """品类大盘。注意：这是市场总量口径，不要用 Top-N 榜单加总来替代。"""
        return self.get(
            "games_breakdown",
            os_name=os_name,
            params={
                "categories": categories,
                "countries": ",".join(countries),
                "date_granularity": granularity,
                "start_date": date_from,
                "end_date": date_to,
            },
        )

    def unified_sales(
        self,
        unified_app_ids: list[str],
        countries: list[str],
        date_from: str,
        date_to: str,
        granularity: str = "monthly",
    ) -> Any:
        """单品收入/下载。unified 口径会聚合同一游戏的多区域 SKU，避免低估。"""
        return self.get(
            "unified_sales",
            os_name="unified",
            params={
                "app_ids": ",".join(unified_app_ids),
                "countries": ",".join(countries),
                "date_granularity": granularity,
                "start_date": date_from,
                "end_date": date_to,
            },
        )

    def top_charts(
        self,
        countries: list[str],
        date_from: str,
        date_to: str,
        category: str | int | None = None,
        custom_fields_filter_id: str | None = None,
        custom_tags_mode: str = "include_unified_apps",
        os_name: str = "unified",
        limit: int = 100,
    ) -> Any:
        """榜单 + 富属性。返回体里的 aggregate_tags 常含留存 / DAU / RPD，是打分的主要来源。"""
        params: dict[str, Any] = {
            "comparison_attribute": "absolute",
            "time_range": "quarter",
            "measure": "revenue",
            "regions": ",".join(countries),
            "date": date_from,
            "end_date": date_to,
            "limit": limit,
        }
        if custom_fields_filter_id:
            params["custom_fields_filter_id"] = custom_fields_filter_id
            if os_name == "unified":
                params["custom_tags_mode"] = custom_tags_mode
        if category is not None:
            params["category"] = category
        return self.get("top_charts", os_name=os_name, params=params)

    def fetch_tags_by_category(
        self,
        category: str | int,
        countries: list[str],
        date_from: str,
        date_to: str,
        limit: int = 200,
    ) -> dict[str, dict]:
        """按品类拉取榜单，返回 {unified_app_id: aggregate_tags}。

        Sensor Tower 的榜单接口（sales_report_estimates_comparison_attributes）
        每个 app 行里带 aggregate_tags，内含留存 / DAU / RPD 等富属性。
        我们主要取留存 D7（字段形如 retention_7d_ww / retention_7d_us）。

        关键修复：该端点**没有 unified 变体**（只有 ios / android），原代码用
        os_name="unified" 会 404 被静默吞掉，导致 aggregate_tags（留存）永远取不到。
        这里依次尝试 ios → android，命中即返回。

        失败（无套餐权限 / 网络）时返回 {}，调用方按缺失处理，不抛异常。
        """
        out: dict[str, dict] = {}
        last_err: str = ""
        for os_try in ("ios", "android"):
            try:
                data = self.top_charts(
                    countries, date_from, date_to, category=category,
                    os_name=os_try, limit=limit,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:140]
                continue
            rows = (
                data
                if isinstance(data, list)
                else (data.get("apps") or data.get("data") or data.get("results") or [])
            )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                aid = str(row.get("unified_app_id") or row.get("app_id") or "")
                tags = row.get("aggregate_tags") or {}
                if aid and isinstance(tags, dict):
                    out[aid] = tags
            if out:
                break
        return out

    def search_app(self, name: str, os_name: str = "unified", limit: int = 5) -> Any:
        """按名称找 app，用于把配置里的种子产品名解析成 unified_app_id。

        健壮性：与 discover_games 一致，依次尝试 unified → ios → android。
        ST 的 unified 搜索对很多短词/专名（如 "TFT"）返回为空，但 ios 能命中，
        若只查 unified 会导致种子名解析失败、app_ids 为空、整片「无相关数据」。
        返回结构统一为 {"apps": [{name, app_id}, ...]}，便于调用方 .get("apps") 解析。
        """
        last_err: str = ""
        for os_try in ("unified", "ios", "android"):
            try:
                data = self.get(
                    "search",
                    os_name=os_try,
                    params={"term": name, "entity_type": "app", "limit": limit},
                )
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:120]
                continue
            out = self._parse_apps(data)
            if out:
                return {"apps": out}
        return {"apps": []}

    def discover_games(
        self,
        keyword: str,
        category_id: int | None = None,
        countries: list[str] | None = None,
        limit: int = 20,
        os_name: str = "ios",
    ) -> list[dict]:
        """按关键词发现某机制/品类的真实游戏（用于「标杆竞品扫描」）。

        健壮性要点：
          - 优先用 unified 端点（直接给 unified_app_id，下游 unified_sales 更顺），
            失败再回退 ios；两类端点响应里取 app_id / unified_app_id 皆可。
          - 不再传 category 参数：ST search_entities 的 category 在多数套餐下会
            把结果压成 0（或 4xx），宁可多搜再在客户端按品类筛，也不要空手而归。
          - 响应结构按多种可能解析（apps / results / 直接列表 / 任意列表值），
            任一能取到就返回，避免「明明有数据却解析不出」导致的 0 游戏。
        """
        params: dict[str, Any] = {
            "term": keyword,
            "entity_type": "app",
            "limit": limit,
        }
        last_err: str = ""
        for os_try in ("unified", os_name if os_name != "unified" else "ios"):
            try:
                data = self.get("search", os_name=os_try, params=params, use_cache=True)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:120]
                continue
            out = self._parse_apps(data)
            if out:
                return out
        if last_err:
            # 仅用于诊断：调用方已吞异常，这里把原因记进返回不了的日志里没关系
            pass
        return []

    @staticmethod
    def _parse_apps(data: Any) -> list[dict]:
        """从 search_entities 响应里尽量取出 app 列表（健壮解析）。"""
        if isinstance(data, list):
            apps: Any = data
        elif isinstance(data, dict):
            apps = (
                data.get("apps")
                or data.get("results")
                or data.get("app_ids")
                or []
            )
            if not apps:
                # 兜底：响应里若有任意「列表且首元素是 dict」的字段，就用它
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        apps = v
                        break
        else:
            apps = []
        out: list[dict] = []
        for a in apps or []:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("unified_app_id") or a.get("app_id") or "")
            if aid:
                out.append(
                    {
                        "name": a.get("name", "") or a.get("title", "") or "",
                        "app_id": aid,
                    }
                )
        return out

    def sales_report(
        self,
        app_ids: list[str],
        countries: list[str],
        date_from: str,
        date_to: str,
        os_name: str = "ios",
        granularity: str = "monthly",
    ) -> list[dict]:
        """单品收入/下载（分平台口径）。当 unified_sales 拿不到数据时作兜底。"""
        return self.get(
            "sales",
            os_name=os_name,
            params={
                "app_ids": ",".join(app_ids),
                "countries": ",".join(countries),
                "date_granularity": granularity,
                "start_date": date_from,
                "end_date": date_to,
            },
        )

    def preflight(self) -> tuple[bool, float, str]:
        """采集前的快速连通性自检：发 1 个最便宜的请求并测往返时延。

        返回 (ok, 秒数, 说明)。网络不通 / token 失效时能在几秒内明确失败，
        避免傻等几百个请求却始终卡在第二步。
        """
        t0 = time.time()
        try:
            self.get(
                "search", os_name="unified",
                params={"term": "last war", "entity_type": "app", "limit": 3},
                use_cache=False,
            )
            secs = time.time() - t0
            return True, secs, f"可达，单请求约 {secs:.1f}s"
        except STError as exc:
            secs = time.time() - t0
            return False, secs, f"请求被拒（{secs:.1f}s）：{str(exc)[:240]}"
        except Exception as exc:  # noqa: BLE001
            secs = time.time() - t0
            return False, secs, f"未知错误（{secs:.1f}s）：{str(exc)[:240]}"

    def probe(self) -> dict[str, str]:
        """探测账号对核心数据端点的可用性。首次接入时先跑这个，避免逐个试错。"""
        results: dict[str, str] = {}
        probes = [
            ("games_breakdown", "ios", {
                "categories": 7001, "countries": "US",
                "date_granularity": "quarterly",
                "start_date": "2025-01-01", "end_date": "2025-03-31",
            }),
            ("unified_sales", "unified", {
                "app_ids": "64075e77537c41636a8e1c58",  # Last War:Survival 连通性样本
                "countries": "US",
                "date_granularity": "quarterly",
                "start_date": "2025-07-01", "end_date": "2026-06-30",
            }),
            ("search", "unified", {"term": "last war", "entity_type": "app", "limit": 3}),
        ]
        for key, os_name, params in probes:
            try:
                self.get(key, os_name=os_name, params=params, use_cache=False)
                results[key] = "OK"
            except Exception as exc:  # noqa: BLE001
                results[key] = f"FAIL — {str(exc)[:200]}"
        return results


# ---------------- CSV 降级模式 ----------------

def _num(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if text in {"", "-", "NA", "N/A", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None
