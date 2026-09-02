"""模型 API Key 凭证库（本地 JSON 存储）。

设计要点：
- 键 = 模型 profile id（owner 作用域内唯一）。凭证的身份由系统定义，不由用户
  起名——从结构上消除"多个模型共用一个环境变量名、新 key 覆盖旧 key"的串 key
  问题（旧缺陷：add/update 默认把 key 写进共享的 CREW_API_KEY）。
- 解析链：凭证库 → api_key_env 环境变量 →（仅共享层）CREW_API_KEY 兜底。
  环境变量路径完整保留，存量 .env 部署零迁移。
- 文件：全局 ``{CREW_HOME}/credentials.json``；owner 私有在 owner runtime home。
- 写入 = 读全量 → 内存合并 → 临时文件 + ``os.replace`` 原子替换；POSIX 下 0600
  （目录由 home 层保证）。Windows 无 POSIX 权限位，依赖用户目录 ACL；``os.chmod``
  在 Windows 只影响只读位，此处安全无副作用。
- 并发：单进程 FastAPI + 原子替换已足够；刻意不引入文件锁依赖。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_CREDENTIALS_FILENAME = "credentials.json"


def credentials_path(owner_account_id: str = "", *, create: bool = False) -> Path:
    """返回凭证库文件路径。owner 传入时为该账号私有库，否则为全局库。"""
    owner = str(owner_account_id or "").strip()
    if owner:
        from crew.state.home import get_owner_runtime_home

        return get_owner_runtime_home(owner, create=create) / _CREDENTIALS_FILENAME
    from crew.state.home import get_crew_home

    return get_crew_home() / _CREDENTIALS_FILENAME


def _read_map(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("凭证库读取失败，按空库处理: %s", path)
        return {}
    if not isinstance(raw, dict):
        log.warning("凭证库格式异常（顶层非对象），按空库处理: %s", path)
        return {}
    return {str(k): str(v) for k, v in raw.items() if k and v}


def _restrict_permissions(path_str: str) -> None:
    try:
        os.chmod(path_str, 0o600)
    except OSError:
        log.debug("设置凭证文件权限失败（Windows 下可忽略）: %s", path_str)


def _write_map(path: Path, mapping: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".credentials-",
        suffix=".tmp",
        delete=False,
    )
    tmp_name = fd.name
    try:
        with fd:
            json.dump(mapping, fd, ensure_ascii=False, indent=2, sort_keys=True)
            fd.flush()
            os.fsync(fd.fileno())
        _restrict_permissions(tmp_name)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def read_stored_key(owner_account_id: str, profile_id: str) -> str:
    """读某个模型 profile 的存储 key；无条目返回空串。"""
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return ""
    return _read_map(credentials_path(owner_account_id)).get(profile_id, "")


def store_key(owner_account_id: str, profile_id: str, api_key: str) -> None:
    """写入/更新某个模型 profile 的 key；空串等价于删除条目。"""
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        raise ValueError("profile id 不能为空")
    path = credentials_path(owner_account_id)
    mapping = _read_map(path)
    api_key = str(api_key or "")
    if api_key:
        mapping[profile_id] = api_key
    else:
        mapping.pop(profile_id, None)
    _write_map(path, mapping)


def delete_stored_key(owner_account_id: str, profile_id: str) -> None:
    """删除某个模型 profile 的存储 key（无条目时静默）。"""
    store_key(owner_account_id, profile_id, "")
