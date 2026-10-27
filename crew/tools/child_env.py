"""白名单派生环境：宿主密钥不进入 shell 子进程。

Ace 是多租户服务端，宿主进程持有的 API key / token 不得注入 model 可执行
的 shell。派生环境每次 spawn 重建：先丢弃 ambient ``ACE_*`` 覆盖，再从白名单
（路径/语言/终端/系统代理/平台必需项）挑选宿主变量，最后叠加受信覆盖值与
显式 pass-through 声明。词表取向与宿主密钥黑名单相反：白名单误伤可控。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# 白名单：无机密、子进程运行所必需的宿主变量。
_BASE_WHITELIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR",
    "LANG", "LANGUAGE",
    # 系统代理（大小写双写：不同工具读取不同写法）
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
})

# LC_* 全族放行（LC_ALL/LC_CTYPE/...）。
_LC_PREFIX = "LC_"

_WINDOWS_WHITELIST = frozenset({
    "SystemRoot", "SystemDrive", "WINDIR", "ComSpec", "PATHEXT",
    "TEMP", "TMP", "OS", "HOMEDRIVE", "HOMEPATH",
    "USERPROFILE", "USERNAME",
    "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
})

# ambient 覆盖一律丢弃的命名空间：子进程只接受注册表重建的快照。
_AMBIENT_DROP_PREFIXES = ("ACE_",)

_IS_WINDOWS = os.name == "nt"


def _fold(key: str) -> str:
    # Windows 环境变量名大小写不敏感：匹配与属主唯一性都按折叠键判定。
    return key.upper() if _IS_WINDOWS else key


def _valid_key(key: object) -> str:
    text = str(key or "").strip()
    if not text or "\x00" in text or "=" in text:
        raise ValueError(f"非法环境变量名: {key!r}")
    return text


_FOLDED_BASE = frozenset(_fold(key) for key in _BASE_WHITELIST)
_FOLDED_WINDOWS = frozenset(_fold(key) for key in _WINDOWS_WHITELIST)
_FOLDED_LC_PREFIX = _fold(_LC_PREFIX)


def _whitelisted(folded_key: str) -> bool:
    if _IS_WINDOWS and folded_key in _FOLDED_WINDOWS:
        return True
    return folded_key in _FOLDED_BASE or folded_key.startswith(_FOLDED_LC_PREFIX)


@dataclass(frozen=True)
class EnvPassThrough:
    """一条显式 pass-through 声明：某个宿主变量被授权进入派生环境。"""

    key: str
    owner: str


class PassThroughRegistry:
    """显式 env pass-through 声明表：键属主唯一，冲突即抛，可枚举。

    与受信覆盖值不同，pass-through 放行的是 *宿主 ambient* 变量；任何放宽
    白名单的需求都必须在这里登记属主，而不是散落的 dict 拷贝。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, EnvPassThrough] = {}

    def register(self, key: str, *, owner: str) -> Callable[[], None]:
        """登记一条 pass-through；返回注销闭包。键冲突立即抛错。"""
        text = _valid_key(key)
        folded = _fold(text)
        owner_name = str(owner or "").strip()
        if not owner_name:
            raise ValueError("pass-through 必须声明非空属主")
        entry = EnvPassThrough(key=text, owner=owner_name)
        with self._lock:
            existing = self._entries.get(folded)
            if existing is not None:
                raise ValueError(
                    f"env pass-through {text!r} 已属 {existing.owner!r}，"
                    f"{owner_name!r} 不能重复登记"
                )
            self._entries[folded] = entry

        def unregister() -> None:
            with self._lock:
                if self._entries.get(folded) == entry:
                    del self._entries[folded]

        return unregister

    def list(self) -> list[EnvPassThrough]:
        """按变量名排序枚举全部声明（不读取宿主值）。"""
        with self._lock:
            return sorted(self._entries.values(), key=lambda item: _fold(item.key))

    def folded_keys(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._entries)


pass_through_registry = PassThroughRegistry()


def build_spawn_env(
    overrides: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
    registry: PassThroughRegistry | None = None,
) -> dict[str, str]:
    """每次 spawn 重建派生环境：白名单宿主变量 + 受信覆盖值。

    - ambient ``ACE_*`` 一律丢弃，防止宿主启动配置被子进程继承；
    - 白名单与 pass-through 之外的宿主变量（API key/token/云凭据）不进入；
    - ``overrides`` 是受信注册表快照（如 CREW_* 运行时路径），最后叠加。
    """
    source = os.environ if base is None else base
    active = pass_through_registry if registry is None else registry
    pass_keys = active.folded_keys()
    env: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in source.items():
        if not isinstance(key, str) or key.startswith(_AMBIENT_DROP_PREFIXES):
            continue
        folded = _fold(key)
        if _IS_WINDOWS:
            # Windows env 块大小写不敏感：重复折叠键只保留首个，避免歧义。
            if folded in seen:
                continue
            seen.add(folded)
        if _whitelisted(folded) or folded in pass_keys:
            env[key] = value
    if overrides:
        if _IS_WINDOWS:
            # 覆盖键与既有键折叠后同键时，先移除宿主侧写法，避免 env 块歧义。
            override_folded = {_fold(key) for key in overrides}
            env = {
                key: value
                for key, value in env.items()
                if _fold(key) not in override_folded
            }
        env.update(overrides)
    return env
