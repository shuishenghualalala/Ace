"""场景推荐（scenarios）数据加载、推荐与绑定解析。

数据源（两层，用户层覆盖内置层同 id 的场景）：
  1. 内置：<repo>/crew/scenarios/scenarios.yaml   随仓库发布
  2. 用户：get_crew_home()/scenarios.yaml         用户自定义/追加

每个顶层场景含若干 items（细分玩法）。前端拿 query 预填输入框；
发送时携带细分玩法 id，后端经 resolve_binding 反查需要的 skill / 注入提示词。
"""

from __future__ import annotations

import logging
import random
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 仓库根目录（crew/scenarios 的上两层），兼容 PyInstaller 冻结环境。
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _REPO_ROOT = Path(sys._MEIPASS)
else:
    _REPO_ROOT = Path(__file__).resolve().parents[2]

# 缓存：(mtime_ns 元组) → 解析后的场景列表
_cache: list[dict] = []
_cache_key: tuple = ()
_intro_cache: list[str] = []
_intro_cache_key: tuple = ()
_loading_status_cache: list[str] = []
_loading_status_cache_key: tuple = ()


def get_builtin_scenarios_file() -> Path:
    """内置场景数据文件：<repo>/crew/scenarios/scenarios.yaml。"""
    return _REPO_ROOT / "crew" / "scenarios" / "scenarios.yaml"


def get_builtin_intro_lines_file() -> Path:
    """内置 Crew 功能介绍话术文件。"""
    return _REPO_ROOT / "crew" / "scenarios" / "crew_intro_lines.yaml"


def get_builtin_loading_status_file() -> Path:
    """内置任务运行状态语文件。"""
    return _REPO_ROOT / "crew" / "scenarios" / "crew_loading_status.yaml"


def get_user_scenarios_file() -> Path:
    """用户场景数据文件：get_crew_home()/scenarios.yaml。"""
    from crew.state.home import get_crew_home
    return get_crew_home() / "scenarios.yaml"


def get_user_intro_lines_file() -> Path:
    """用户 Crew 功能介绍话术覆盖文件：get_crew_home()/crew_intro_lines.yaml。"""
    from crew.state.home import get_crew_home
    return get_crew_home() / "crew_intro_lines.yaml"


def get_user_loading_status_file() -> Path:
    """用户任务运行状态语追加文件：get_crew_home()/crew_loading_status.yaml。"""
    from crew.state.home import get_crew_home
    return get_crew_home() / "crew_loading_status.yaml"


def _data_files() -> list[Path]:
    """按优先级返回存在的数据文件（内置在前，用户在后覆盖）。"""
    return [p for p in (get_builtin_scenarios_file(), get_user_scenarios_file()) if p.exists()]


def _intro_line_files() -> list[Path]:
    """按优先级返回话术文件（内置在前，用户在后追加）。"""
    return [p for p in (get_builtin_intro_lines_file(), get_user_intro_lines_file()) if p.exists()]


def _loading_status_files() -> list[Path]:
    """按优先级返回任务运行状态语文件（内置在前，用户在后追加）。"""
    return [p for p in (get_builtin_loading_status_file(), get_user_loading_status_file()) if p.exists()]


def _cache_signature(files: list[Path]) -> tuple:
    return tuple((str(p), p.stat().st_mtime_ns) for p in files)


def _load_file(path: Path) -> list[dict]:
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("场景数据解析失败 %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("场景数据顶层应为列表：%s", path)
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id")]


def _load_string_list(files: list[Path], label: str) -> list[str]:
    lines: list[str] = []
    for path in files:
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 解析失败 %s: %s", label, path, exc)
            continue
        if not isinstance(data, list):
            logger.warning("%s 顶层应为列表：%s", label, path)
            continue
        for item in data:
            text = str(item or "").strip()
            if text:
                lines.append(text)
    return lines


def get_scenarios() -> list[dict]:
    """加载全部场景（内置 + 用户覆盖），带 mtime 缓存。

    用户层与内置层 id 相同的场景，用户层整体覆盖内置层。
    """
    global _cache, _cache_key
    files = _data_files()
    signature = _cache_signature(files)
    if signature == _cache_key and _cache:
        return _cache

    merged: dict[str, dict] = {}
    for path in files:
        for scenario in _load_file(path):
            merged[str(scenario["id"])] = scenario

    _cache = list(merged.values())
    _cache_key = signature
    return _cache


def get_intro_lines() -> list[str]:
    """加载任务运行中展示的 Crew 功能介绍话术。

    用户级文件采用追加策略，便于在保留内置话术的同时增加团队自定义文案。
    """
    global _intro_cache, _intro_cache_key
    files = _intro_line_files()
    signature = _cache_signature(files)
    if signature == _intro_cache_key and _intro_cache:
        return _intro_cache

    _intro_cache = _load_string_list(files, "Crew 介绍话术")
    _intro_cache_key = signature
    return _intro_cache


def recommend_intro_lines(count: int = 8) -> list[str]:
    """随机返回 count 条 Crew 功能介绍话术，供前端 loading 轮播。"""
    lines = get_intro_lines()
    if count <= 0 or count >= len(lines):
        result = list(lines)
        random.shuffle(result)
        return result
    return random.sample(lines, count)


def get_loading_statuses() -> list[str]:
    """加载任务运行中展示的活泼状态语。"""
    global _loading_status_cache, _loading_status_cache_key
    files = _loading_status_files()
    signature = _cache_signature(files)
    if signature == _loading_status_cache_key and _loading_status_cache:
        return _loading_status_cache

    _loading_status_cache = _load_string_list(files, "任务运行状态语")
    _loading_status_cache_key = signature
    return _loading_status_cache


def recommend_loading_statuses(count: int = 8) -> list[str]:
    """随机返回 count 条任务运行状态语，供前端 loading 轮播。"""
    lines = get_loading_statuses()
    if count <= 0 or count >= len(lines):
        result = list(lines)
        random.shuffle(result)
        return result
    return random.sample(lines, count)


def recommend(count: int = 4) -> list[dict]:
    """随机推荐 count 个顶层场景（含 items），供首页展示 / 换一换。"""
    scenarios = get_scenarios()
    if count <= 0 or count >= len(scenarios):
        result = list(scenarios)
        random.shuffle(result)
        return result
    return random.sample(scenarios, count)


def _iter_items() -> list[tuple[dict, dict]]:
    """展平为 (顶层场景, 细分玩法) 列表。"""
    pairs: list[tuple[dict, dict]] = []
    for scenario in get_scenarios():
        for item in scenario.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                pairs.append((scenario, item))
    return pairs


def resolve_binding(sub_id: str) -> Optional[dict[str, Any]]:
    """按细分玩法 id 反查绑定信息。

    Returns dict（未找到返回 None）：
      {
        "skills": [...],          # 需可用的 skill slug 列表
        "inject": str | "",       # 手写注入提示词（原样透传）
        "mode": str | "",
      }
    """
    if not sub_id:
        return None
    for _scenario, item in _iter_items():
        if str(item["id"]) != str(sub_id):
            continue
        skills = item.get("skills") or []
        if not isinstance(skills, list):
            skills = [skills]
        return {
            "skills": [str(s) for s in skills if s],
            "inject": str(item.get("inject") or "").strip(),
            "mode": str(item.get("mode") or "").strip(),
        }
    return None
