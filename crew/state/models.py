"""状态层数据模型。"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Workspace:
    """工作空间：会话的上层容器，承载共享的空间级指令与可选本地根目录。"""

    name: str
    description: str = ""
    instructions: str = ""  # 注入该空间所有会话的系统提示
    root_path: str = ""  # 绑定的本地项目目录；空则回退 task_workspaces/{id}
    hidden: bool = False  # 隐藏后不展示在侧栏/选择器，会话保留
    id: str = field(default_factory=lambda: f"ws_{uuid.uuid4().hex[:8]}")
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
