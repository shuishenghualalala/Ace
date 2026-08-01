"""场景推荐模块：对话首页的场景化引导。

对外导出：
  - get_scenarios()      全部场景（含细分玩法）
  - recommend(count)     随机推荐 N 个场景（首页 / 换一换）
  - resolve_binding(id)  按细分玩法 id 反查需要的 skill / 注入提示词
  - get_intro_lines()    Crew 功能介绍话术
  - get_loading_statuses() 任务运行状态语
"""

from __future__ import annotations

from crew.scenarios.registry import (
    get_intro_lines,
    get_loading_statuses,
    get_scenarios,
    recommend,
    recommend_intro_lines,
    recommend_loading_statuses,
    resolve_binding,
)

__all__ = [
    "get_scenarios",
    "recommend",
    "resolve_binding",
    "get_intro_lines",
    "recommend_intro_lines",
    "get_loading_statuses",
    "recommend_loading_statuses",
]
