"""契约层：全局共享的数据类型、消息信封、接口定义。

【重要】所有业务模块只 import `crew.core`，模块之间不直接 import 对方实现。
这是"并行开发 + 零冲突合并"的基础。改动本层需经架构师评审。
"""

from crew.core.types import (
    Role,
    ToolCall,
    Message,
    ToolResult,
    ChatResponse,
    StreamChunk,
)
from crew.core.envelope import Envelope, ResponseChunk, ChunkKind, Status
from crew.core.errors import (
    CrewError,
    ProviderError,
    ToolError,
    ToolNotFoundError,
    ConfigError,
)
from crew.core.interfaces import (
    LLMProvider,
    Tool,
    ToolRegistry,
    Agent,
    SessionStore,
    WorkspaceStore,
    MemoryProvider,
    Plugin,
    Channel,
    TeamManager,
    TaskManager,
    Scheduler,
)

__all__ = [
    # types
    "Role",
    "ToolCall",
    "Message",
    "ToolResult",
    "ChatResponse",
    "StreamChunk",
    # envelope
    "Envelope",
    "ResponseChunk",
    "ChunkKind",
    "Status",
    # errors
    "CrewError",
    "ProviderError",
    "ToolError",
    "ToolNotFoundError",
    "ConfigError",
    # interfaces
    "LLMProvider",
    "Tool",
    "ToolRegistry",
    "Agent",
    "SessionStore",
    "WorkspaceStore",
    "MemoryProvider",
    "Plugin",
    "Channel",
    "TeamManager",
    "TaskManager",
    "Scheduler",
]
