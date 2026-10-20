"""web_search 的 provider seam：注册、可用性探测与显式有序降级。

工具层只消费 ``search_with_fallback``，不感知具体引擎；新增搜索引擎时
实现 ``SearchProvider`` 并 ``register_search_provider`` 即可被链路消费。

降级语义：配置 ``providers`` 列表（config.yaml tools.web_search 节）即
显式 fallback 链——按序逐个尝试，全部失败抛出携带已尝试链路的结构化错误；
未配置时按注册顺序使用所有可用 provider。纯 API provider，无 HTML 刮取
兜底：一个都不可用时 fail-closed，错误文案引导用户完成配置。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from crew.core.errors import ToolError


@dataclass(frozen=True)
class SearchResult:
    """归一化搜索结果；title/url 之外的字段一律 optional，不造假。"""

    title: str
    url: str
    snippet: str | None = None


@dataclass(frozen=True)
class SearchContext:
    """工具执行上下文，provider 用它完成逐跳授权的网络访问。"""

    workspace_store: Any | None = None
    security_service: Any | None = None


@dataclass(frozen=True)
class SearchProvider:
    id: str
    available: Callable[[], bool]
    search: Callable[[str, int, SearchContext], Awaitable[list[SearchResult]]]


@dataclass(frozen=True)
class SearchOutcome:
    provider_id: str
    results: list[SearchResult]
    #: 成功之前被跳过/失败的链路记录（"id(原因)"），供结构化错误使用。
    attempted: tuple[str, ...] = ()


_PROVIDERS: dict[str, SearchProvider] = {}
_PROVIDER_ORDER: list[str] = []
_CHAIN: list[str] | None = None
_CONFIG: dict[str, Any] = {}


def register_search_provider(provider: SearchProvider) -> Callable[[], None]:
    """注册搜索 provider，返回 disposer（释放即注销，幂等）。

    重复注册同 id 以新注册为准，旧 disposer 随之失效——释放只移除仍由
    自己持有的条目，不会误删后来者。该 seam 迁移到 Feature Scope /
    Service Registry 时，disposer 即 Registration Token 的语义载体。
    """
    if provider.id not in _PROVIDERS:
        _PROVIDER_ORDER.append(provider.id)
    _PROVIDERS[provider.id] = provider
    return _disposer_for(provider)


def _disposer_for(provider: SearchProvider) -> Callable[[], None]:
    def dispose() -> None:
        if _PROVIDERS.get(provider.id) is not provider:
            return
        del _PROVIDERS[provider.id]
        if provider.id in _PROVIDER_ORDER:
            _PROVIDER_ORDER.remove(provider.id)

    return dispose


def reset_search_providers() -> None:
    """清空注册表与配置（测试隔离用）。"""
    _PROVIDERS.clear()
    _PROVIDER_ORDER.clear()
    global _CHAIN, _CONFIG
    _CHAIN = None
    _CONFIG = {}


def configure_search(raw: dict[str, Any] | None) -> None:
    """注入 config.yaml tools.web_search 节；providers 为空列表等同未配置。"""
    global _CHAIN, _CONFIG
    _CONFIG = dict(raw or {})
    providers = _CONFIG.get("providers")
    if isinstance(providers, list):
        chain = [str(item).strip() for item in providers if str(item).strip()]
        _CHAIN = chain or None
    else:
        _CHAIN = None


def search_config(key: str, default: str = "") -> str:
    value = _CONFIG.get(key)
    text = str(value).strip() if value is not None else ""
    return text or default


def _resolve_chain() -> list[str]:
    if _CHAIN is None:
        return [pid for pid in _PROVIDER_ORDER if _PROVIDERS[pid].available()]
    unknown = [pid for pid in _CHAIN if pid not in _PROVIDERS]
    if unknown:
        registered = ", ".join(_PROVIDER_ORDER) or "(无)"
        raise ToolError(
            f"搜索源配置无效: 未注册的 provider {', '.join(unknown)}（已注册: {registered}）"
        )
    return list(_CHAIN)


async def search_with_fallback(
    query: str,
    limit: int,
    ctx: SearchContext,
) -> SearchOutcome:
    """按解析出的链路逐个降级，全败抛出携带已尝试链路的 ToolError。"""
    chain = _resolve_chain()
    if not chain:
        raise ToolError(
            "没有可用的搜索源：exa 托管 MCP 无需 key、默认可用，请检查网络或代理"
            "（network.upstream_proxy）；自建实例可在 config.yaml tools.web_search "
            "配置 searxng_base_url"
        )
    attempted: list[str] = []
    for pid in chain:
        provider = _PROVIDERS[pid]
        if not provider.available():
            attempted.append(f"{pid}(不可用)")
            continue
        try:
            results = await provider.search(query, limit, ctx)
        except Exception as exc:  # noqa: BLE001 - 单源失败不阻断降级链路
            reason = str(exc) or type(exc).__name__
            attempted.append(f"{pid}({reason[:120]})")
            continue
        return SearchOutcome(
            provider_id=pid,
            results=results,
            attempted=tuple(attempted),
        )
    raise ToolError("所有搜索源均失败: " + "; ".join(attempted))
