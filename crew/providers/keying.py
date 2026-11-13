"""Provider 凭据解析 seam。

契约：引用 ≠ 值——API key 禁止跨请求缓存。装配层不传 key 字符串而传
resolver（每次调用重新走「凭证库 → env」解析链）；provider 在每次
chat/stream_chat 入口取一次快照写进请求头。飞行中的流在请求发起时
已固定凭据，配置变更只影响其后发起的请求。
"""

from __future__ import annotations

from typing import Callable

#: 每次调用返回当前应使用的 api_key；实现自身须无缓存，解析失败允许抛异常。
ApiKeyResolver = Callable[[], str]
