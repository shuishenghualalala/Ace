"""SkillCatalog：skill metadata 缓存与单调递增的失效计数器。

失效语义：revision + 1 并清空缓存。watcher 模式下 revision 即缓存键；
stat 回退模式下缓存键是 mtime 序列，revision 仍随失效递增。

缓存本体存放在包命名空间（``crew.agent.skills._cache`` 等模块属性），
保持历史契约：测试与宿主可直接读/重置这些属性。
"""

from __future__ import annotations

import sys


def _ns():
    return sys.modules.get("crew.agent.skills")


class SkillCatalog:
    """metadata 缓存守卫：存取都落在包命名空间的兼容属性上。"""

    def __init__(self) -> None:
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def stored(self) -> tuple[dict[str, dict], object]:
        """当前缓存的 (skills, key)；无缓存时 skills 为空 dict。"""
        ns = _ns()
        if ns is None:
            return {}, ()
        return (
            getattr(ns, "_cache", {}),
            getattr(ns, "_cache_key", ()),
        )

    def publish(
        self,
        *,
        skills: dict[str, dict],
        key: object,
        packages: dict[str, dict],
        package_members: dict[str, list[str]],
    ) -> None:
        ns = _ns()
        if ns is None:
            return
        ns._cache = skills
        ns._cache_key = key
        ns._packages = packages
        ns._package_members = package_members

    def invalidate(self) -> None:
        self._revision += 1
        ns = _ns()
        if ns is None:
            return
        ns._cache = {}
        ns._cache_key = ()
        ns._packages = {}
        ns._package_members = {}
