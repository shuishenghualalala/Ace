"""配置加载：.env (敏感信息) + config.yaml (结构化配置)，env 优先。

不配 LLM key 时 has_llm_key=False，上层会自动回退到 FakeProvider，保证流程可跑通。
"""

from __future__ import annotations

import os
import shutil
import threading
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values, load_dotenv

from crew.security.outbound import NetworkConfig
from crew.state.access_control import AccessControlConfig
from crew.state.credentials import read_stored_key

from crew.state.logging import get_logger

import sys

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    ROOT = Path(sys._MEIPASS)
else:
    ROOT = Path(__file__).resolve().parents[2]
log = get_logger("config")
_CONFIG_WRITE_LOCK = threading.Lock()
_LEGACY_CRON_TICK_WARNING_EMITTED = False

def _bundled_config_template_path() -> Path:
    """Return the publishable config template, with legacy package fallback."""
    example = ROOT / "config" / "config.yaml.example"
    if example.is_file():
        return example
    return ROOT / "config" / "config.yaml"


def _get_user_config_dir() -> Path:
    """返回用户配置目录。

    - 冻结态（PyInstaller 打包后）：委托 get_crew_home()，与 CREW_HOME 保持一致
    - 开发态：项目根/config（config.yaml 是被 Git 忽略的本地配置）
    """
    if getattr(sys, "frozen", False):
        from crew.state.home import get_crew_home
        return get_crew_home()
    return ROOT / "config"


def _skills_copy_ignore(_dir: str, names: list[str]) -> list[str]:
    """copytree 忽略规则：跳过 node_modules 等运行时依赖目录，避免首装复制巨量无关文件。

    背景：html-to-pdf 这类 skill 自带 node_modules（含 puppeteer 等，~16MB），
    占内置 skills 体积的 ~80%。这些是 skill 运行时按需解析的依赖，不应进入
    用户配置目录（用户目录里只需 SKILL.md + 脚本本体即可被 scan_skills 发现与执行）。
    跳过后首装复制量从 ~20MB 降到 ~3MB。
    """
    _SKIP = {"node_modules", "__pycache__", ".pytest_cache", ".git", ".venv", "venv"}
    return [n for n in names if n in _SKIP]


def _init_user_config_dir() -> Path:
    """Ensure writable config exists, initialized from the publishable example."""
    user_dir = _get_user_config_dir()
    user_dir.mkdir(parents=True, exist_ok=True)

    # 开发态写 config/config.yaml，打包态写 Crew Home/config.yaml；两者都只在缺失时
    # 从可提交的 example 初始化，绝不覆盖用户已有配置。
    bundled_config = _bundled_config_template_path()
    user_config = user_dir / "config.yaml"
    if bundled_config.is_file() and bundled_config != user_config and not user_config.exists():
        shutil.copy2(bundled_config, user_config)
        log.info("首次运行：已从 config.yaml.example 复制本地配置到 %s", user_config)

    if getattr(sys, "frozen", False):
        # 1. 从空模板生成用户私有 .env；发布包绝不携带真实密钥
        bundled_env = ROOT / "config" / ".env.example"
        user_env = user_dir / ".env"
        if bundled_env.is_file() and not user_env.exists():
            shutil.copy2(bundled_env, user_env)
            log.info("首次运行：已从 .env.example 复制 .env 到 %s", user_env)

        # 2. 释放 skills 目录（跳过 node_modules 等运行时依赖，见 _skills_copy_ignore）
        bundled_skills = ROOT / "crew" / "skills"
        user_skills = user_dir / "skills"
        if bundled_skills.is_dir() and not user_skills.exists():
            shutil.copytree(bundled_skills, user_skills, ignore=_skills_copy_ignore)
            log.info("首次运行：已释放 skills 目录到 %s（已跳过 node_modules 等依赖）", user_skills)

        # 3. 释放 optional-skills 目录
        bundled_opt_skills = ROOT / "optional-skills"
        user_opt_skills = user_dir / "optional-skills"
        if bundled_opt_skills.is_dir() and not user_opt_skills.exists():
            shutil.copytree(bundled_opt_skills, user_opt_skills, ignore=_skills_copy_ignore)
            log.info("首次运行：已释放 optional-skills 目录到 %s", user_opt_skills)

    return user_dir


@dataclass
class ModelProfile:
    """一个模型配置档案。"""

    id: str
    name: str = ""
    api_key: str = ""
    api_key_env: str = "CREW_API_KEY"
    provider: str = "openai"
    base_url: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int | None = None
    context_window: int | None = None
    timeout: float = 60.0
    vision: bool = False
    loaded: bool = True
    builtin: bool = False
    capabilities: list[str] = field(default_factory=lambda: ["text", "tools"])

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @property
    def supports_vision(self) -> bool:
        """Whether this profile may send image inputs to its provider."""
        return "vision" in {
            str(item).strip().lower() for item in self.capabilities
        }

    @property
    def api_key_masked(self) -> str:
        """脱敏 api_key，仅供展示（前 4 + **** + 后 3；过短则全掩，无 key 为空）。"""
        k = self.api_key
        if not k:
            return ""
        return "****" if len(k) <= 8 else f"{k[:4]}****"  # 只露前缀，不晒末尾

    def public_dict(self) -> dict[str, Any]:
        """返回给前端的非敏感信息。"""
        return {
            "id": self.id,
            "name": self.label,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "provider": self.provider,
            "has_key": self.has_key,
            "api_key_masked": self.api_key_masked,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "timeout": self.timeout,
            "vision": self.supports_vision,
            "loaded": self.loaded,
            "builtin": self.builtin,
            "capabilities": list(self.capabilities),
        }


def is_placeholder_model_profile(profile: ModelProfile | None) -> bool:
    """判断 profile 是否仍是开源模板中的模型占位配置。"""
    if profile is None:
        return False
    model = str(profile.model or "").strip().lower()
    base_url = str(profile.base_url or "").strip().lower().rstrip("/")
    return model == "your-model-name" or base_url in {
        "https://api.example.com",
        "https://api.example.com/v1",
    }


@dataclass
class Config:
    # --- LLM ---
    api_key: str = ""
    api_key_env: str = "CREW_API_KEY"
    provider: str = "openai"
    base_url: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int | None = None
    context_window: int | None = None
    timeout: float = 60.0
    vision: bool = True
    active_model_id: str = "default"
    default_model_id: str = ""  # 用户设定的默认模型；空=回落到 active_model_id
    model_profiles: dict[str, ModelProfile] = field(default_factory=dict)
    # 加载 config.yaml 的实际路径（load_config 填充），用于运行时 CRUD 写回。
    # 空串表示未通过 yaml 加载（例如纯测试构造的 Config），此时 persist 会拒绝写。
    config_path: str = ""

    # --- 运行时 ---
    db_path: str = "crew_data/crew.db"
    memory_db_path: str = "crew_data/memory.db"  # SQLiteMemory 独立路径，便于测试隔离
    # 内置记忆默认关闭：SQLiteMemory 的关键词朴素召回是 demo 级，「有记忆但召回差」
    # 比没有更伤产品观感。显式开启：runtime.memory_enabled: true（中期路线是接入
    # MCP memory server，见 docs/todo/ace-weaknesses-dsh-remediation-plan.md §7.3）。
    memory_enabled: bool = False
    # cron Feature 独立库（ADR-0038 拆库试点）：与主库分文件，回退时把本键指回 crew.db 即可
    cron_db_path: str = "crew_data/cron.db"
    # work Feature 独立库（ADR-0038 拆库第二批）：与主库分文件，回退时把本键指回 crew.db 即可
    work_db_path: str = "crew_data/work.db"
    # dynamic-kanban Feature 独立库（ADR-0038 拆库第三批）：与主库分文件，回退时把本键指回 crew.db 即可
    kanban_db_path: str = "crew_data/kanban.db"
    # external-agents / team Feature 独立库（ADR-0038 拆库第四批，一次双库）：
    # external_* 4 表归 external.db、external_team* 2 表归 team.db；
    # 回退时把对应键指回 crew.db 即可
    external_db_path: str = "crew_data/external.db"
    team_db_path: str = "crew_data/team.db"
    # sites / tasks / notifications Feature 独立库（ADR-0038 拆库收尾批，三小域）：
    # sites 域 10 表归 sites.db、runtime_tasks 归 tasks.db、notifications 归
    # notifications.db；回退时把对应键指回 crew.db 即可
    sites_db_path: str = "crew_data/sites.db"
    tasks_db_path: str = "crew_data/tasks.db"
    notifications_db_path: str = "crew_data/notifications.db"
    # channels Feature 独立库（ADR-0038 拆库最后一批，随 P2-7 归属迁移）：
    # channel_bindings / channel_session_routes 2 表归 channels.db；
    # 回退时把本键指回 crew.db 即可
    channels_db_path: str = "crew_data/channels.db"
    # wiki_learning 目录插件自有库（ADR-0038 清单外补充批）：插件 6 表归
    # crew_data/wiki_learning.db；装配点在插件 register（消费侧归一），
    # 回退时把本键指回 crew.db 即可
    wiki_learning_db_path: str = "crew_data/wiki_learning.db"
    log_level: str = "INFO"
    log_file: str = ""  # 空=不写文件；填路径则同时写文件（支持 ~ 展开）
    llm_trace: bool = True  # 是否把每次 LLM 收发全量写入 {crew_home}/logs/llm.jsonl，便于排查
    max_iterations: int = 0  # 0=无限，靠 auto-compact + guardrail 防失控
    dk_task_timeout_seconds: float = 3600.0  # Dynamic Kanban 单个任务执行超时（秒），0=不限
    # 外部 Runtime 的统一空闲/硬截止/交互等待策略；空映射保持现有协议兼容默认。
    timeout_policy: dict[str, Any] = field(default_factory=dict)
    dk_verification_gate_enabled: bool = True  # Dynamic Kanban 是否启用 LLM verification gate
    crew_home: str = ""  # 空=使用默认（冻结态 ~/DEFAULT_HOME_DIRNAME，开发态 ROOT/.crew/），否则使用指定路径
    task_workspace_root: str = ""  # 空={crew_home}/task_workspaces；否则作为任务产物根目录
    sqlite_wal: bool = True  # 是否启用 SQLite WAL + 写锁重试

    # --- agent 执行层 ---
    agent_executor: str = "builtin"  # builtin | client | external (acp is legacy)
    compaction_enabled: bool = True
    compaction_token_budget: int = 0  # 0=按 ratio × context_window 动态计算；>0 则绝对值优先
    compaction_token_budget_ratio: float = 0.75  # 触发摘要的窗口比例
    compaction_keep_recent: int = 8
    compaction_keep_recent_tools: int = 6  # L1：保留最近 N 个工具结果，更早的清理
    compaction_l2_incremental: bool = True  # L2：增量摘要缓存（复用旧摘要）
    compaction_l2_delta_threshold: int = 5000  # L2：新增低于此 token 时纯规则复用、零 LLM
    compaction_post_compact_files: int = 3  # 压缩后恢复最近 N 个文件内容（按 path 去重，从磁盘重读）
    compaction_post_compact_max_chars_per_file: int = 5000  # 单个恢复文件最大字符数
    compaction_post_compact_max_instructions: int = 5  # 压缩后最多恢复的指令条数
    compaction_post_compact_max_important: int = 8  # 压缩后最多恢复的重要结论条数
    compaction_post_compact_max_instruction_chars: int = 20000  # 单条恢复指令最大字符数
    compaction_post_compact_max_important_chars: int = 5000  # 单条恢复重要结论最大字符数
    compaction_post_compact_max_total_chars: int = 140000  # 恢复附件总字符闸口
    compaction_max_tool_result_chars: int = 20000  # 单条 tool result 最大字符数，超长截断
    compaction_max_overflow_retries: int = 1  # 上下文 overflow 后 compact-retry 次数上限（每次溢出序列一次）
    compaction_summary_max_tokens: int = 8192  # 摘要调用 maxTokens 封顶（截断视为失败）
    compaction_summary_prefix_reuse: bool = True  # 摘要调用是否允许 KV 前缀复用（按 provider 能力决策）
    retry_max: int = 2
    retry_backoff: float = 1.0
    title_auto: bool = True
    evolution_auto_trigger: bool = False  # 每轮交互结束后自动触发 evolution 轨迹提取
    evolution_auto_full_cycle: bool = False  # 自动触发时是否执行完整周期（优化+生成），False=仅提取轨迹
    evolution_visible: bool = False  # Demo 模式：前台可见地执行 evolution（输出状态帧，同步等待）
    agent_client_config: dict[str, Any] = field(default_factory=dict)  # client 执行器配置
    agent_acp_config: dict[str, Any] = field(default_factory=dict)     # acp 执行器配置

    # --- agent loop 鲁棒性/可控性 ---
    parallel_tools: bool = True          # 只读工具批次是否并行执行
    max_parallel_tool_calls: int = 8      # 工具调用默认并发上限
    empty_retry_max: int = 2             # 空响应最多重试次数
    continuation_max: int = 2            # 截断续写最多次数
    stream_read_timeout: float = 120.0    # 流式 read timeout（秒）
    stream_retry_jitter: bool = True      # LLM 重试 backoff 是否加随机抖动
    stream_stale_timeout: float = 0.0     # 流 stale 检测（秒，0=关闭）
    stream_continuation_max: int = 2      # 流式中断续写最多次数
    fallback_models: list[str] = field(default_factory=list)  # 主 provider 失败时依次切换的 model_profile id
    # 工具防循环 guardrail
    guardrail_enabled: bool = True               # 总开关（warn 始终开；下面控制 hard-stop）
    guardrail_hard_stop: bool = False            # 默认关：日常靠 warn 引导模型调整，hard-stop 仅 opt-in 兜底
    guardrail_exact_failure_block_after: int = 5 # 同参工具失败 N 次后拦截（需 hard_stop 开启）
    guardrail_same_tool_failure_halt_after: int = 8  # 同名工具失败 N 次后硬停（需 hard_stop 开启）
    guardrail_no_progress_block_after: int = 5   # 只读工具返回相同结果 N 次后拦截（需 hard_stop 开启）

    # --- gateway ---
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000
    gateway_busy_mode: str = "queue"      # queue | interrupt | steer — 忙时策略
    gateway_push_min_interval: float = 0.05  # WS 推送最小间隔（秒），0=不限流
    gateway_admin_accounts: list[str] = field(default_factory=list)
    gateway_dev_mode: bool = False  # 开发态旁路：loopback 请求放行开发账号身份（勿用于生产）
    gateway_dev_account: str = "dev:dev"  # 开发环境 owner ID，dev 模式下自动 admin
    gateway_max_active_runs: int = 4      # 不同 session 同时运行的全局上限
    gateway_max_queue_depth_per_session: int = 20  # 单 session 等待队列上限
    health_port: int = 0   # 独立线程 health 服务端口（runtime.health_port）；0 = 主端口+1

    # --- security ---
    # 默认关闭：工具以当前宿主用户权限运行，不启用沙箱或审批链路。
    security_enabled: bool = False

    # --- authentication ---
    # local：本机免登录；email：本机邮箱租户入口；remote：通过用户配置的认证服务登录。
    # 显式 remote 优先于 gateway.dev_mode，便于在开发启动方式下联调登录。
    auth_mode: str = "local"
    auth_provider_id: str = "custom"
    auth_base_url: str = ""
    auth_send_code_path: str = "/auth/send-code"
    auth_login_path: str = "/auth/login-by-code"
    auth_timeout_seconds: float = 10.0
    auth_session_ttl_seconds: int = 7 * 24 * 60 * 60
    channels: dict[str, Any] = field(default_factory=dict)  # 外部通道配置
    platforms: dict[str, Any] = field(default_factory=dict) # 平台插件配置
    raw_config: dict[str, Any] = field(default_factory=dict)

    # --- team ---
    team_config: dict[str, Any] = field(default_factory=dict)
    team_max_concurrent_children: int = 3
    # 外部 ACP 智能体与外部 Team 的产品开关；不影响 Dynamic Kanban 和默认主智能体。
    external_agents_enabled: bool = True
    # 外援安全边界开关；默认关闭，外援走旧 runtime 直联，内建工具仍按会话安全边界执行。
    external_security_enabled: bool = False

    # --- subagent（主 agent 通过 delegate_task / run_agent 调用子 agent）---
    subagent_max_concurrent: int = 3      # delegate_task 批量子任务的最大并发数
    subagent_max_tasks: int = 8           # delegate_task 单次最多委派的子任务数（防失控）
    subagent_max_iterations: int = 200    # 子 agent 单轮工具迭代上限；主 agent 可配置为无限
    subagent_idle_timeout_seconds: float = 120.0  # 子 agent 空闲（无活动）超时：N 秒零输出才中止（防卡死），0=不限
    subagent_timeout_seconds: float = 1800.0  # 子 agent 绝对运行上限（全局兜底），0=不限

    # --- unified long-task runtime ---
    tasks_auto_background_after_seconds: float = 15.0
    tasks_heartbeat_interval_seconds: float = 10.0
    tasks_monitor_interval_seconds: float = 5.0
    tasks_wait_timeout_seconds: float = 30.0
    tasks_finished_retention_days: int = 7
    tasks_shell_inactivity_timeout_seconds: float = 600.0
    tasks_shell_execution_timeout_seconds: float = 0.0
    tasks_subagent_inactivity_timeout_seconds: float = 120.0
    tasks_subagent_execution_timeout_seconds: float = 1800.0
    tasks_agent_turn_inactivity_timeout_seconds: float = 600.0
    tasks_agent_turn_execution_timeout_seconds: float = 3600.0
    # 单工具执行段看门狗：超时取消该工具并合成 "timed out after X.Xs" 作为正常
    # tool output 回灌模型（tool_call/tool output 配对完整，回合继续推进），0=关闭。
    tool_execution_timeout_seconds: float = 600.0
    # 整回合 deadline（秒）：回合累计时长上限。到点走与 interrupt 相同的优雅收尾
    # （中断标记 + 回合正常结束，历史/输出含 "deadline exceeded" 标记），在途工具
    # 走 aborted 语义；0 = 不限（默认关闭）。
    turn_deadline_seconds: float = 0.0
    # 追问/权限确认等待上限（秒）：wait_for_answer 调用方未显式传 timeout 时的
    # 默认有界等待；超时按「用户未回答」收尾（返回空答案）。0 = 默认不限
    # （维持旧的不限语义）。显式传 timeout（含 None/0）始终优先于该默认值。
    interaction_timeout_seconds: float = 3600.0

    # --- mcp / cron ---
    mcp_servers: dict[str, Any] = field(default_factory=dict)  # 外部 MCP server 配置
    cron_enabled: bool = True          # 是否启动 cron 引擎
    cron_max_parallel_jobs: int = 2

    # --- plugins ---
    plugins_enabled: list[str] | None = None
    plugins_disabled: list[str] = field(default_factory=list)

    # --- session ---
    session_idle_timeout: int = 0      # 会话空闲超时（分钟），0=不自动过期

    # --- wiki ---
    # Feature 原始配置透传（与 team_config 同一模式）：core 只保存 config.yaml
    # 的 wiki 节，WikiConfig 的解析与默认值由 wiki Feature 侧负责。
    wiki_config: dict[str, Any] = field(default_factory=dict)
    # /api/config 能力上报用的轻量开关（取自 wiki 节的 enabled，默认开启）。
    wiki_enabled: bool = True

    access_control: AccessControlConfig = field(default_factory=AccessControlConfig)
    # Feature 原始配置透传（与 wiki_config/team_config 同一模式）：core 只保存
    # config.yaml 的 tools.browser 节，BrowserConfig 的解析与默认值由 browser
    # Feature 侧装配时负责。
    browser_config: dict[str, Any] = field(default_factory=dict)
    # tools.web_search 节透传：provider 有序降级链、API key 环境变量名等，
    # 解析由 web_search_service 负责（加载时经 configure_search 注入）。
    web_search_config: dict[str, Any] = field(default_factory=dict)
    # 进程内 HTTP 边界（web_search/web_extract/Wiki）上游代理；空=读环境变量
    network: NetworkConfig = field(default_factory=NetworkConfig)

    @property
    def has_llm_key(self) -> bool:
        return bool(self.api_key)

    @property
    def active_model(self) -> ModelProfile:
        if not self.model_profiles:
            capabilities = ["text", "tools"]
            if self.vision:
                capabilities.append("vision")
            self.model_profiles["default"] = ModelProfile(
                id="default",
                api_key=self.api_key,
                api_key_env=self.api_key_env,
                provider=self.provider,
                base_url=self.base_url,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                context_window=self.context_window,
                timeout=self.timeout,
                vision=self.vision,
                capabilities=capabilities,
            )
        return self.model_profiles.get(self.active_model_id) or next(iter(self.model_profiles.values()))

    def activate_model(self, model_id: str) -> ModelProfile:
        if model_id not in self.model_profiles:
            raise KeyError(model_id)
        if not self.model_profiles[model_id].loaded:
            raise ValueError(f"模型未加载，不能用于对话: {model_id}")
        self.active_model_id = model_id
        profile = self.model_profiles[model_id]
        self.api_key = profile.api_key
        self.api_key_env = profile.api_key_env
        self.provider = profile.provider
        self.base_url = profile.base_url
        self.model = profile.model
        self.temperature = profile.temperature
        self.max_tokens = profile.max_tokens
        self.context_window = profile.context_window
        self.timeout = profile.timeout
        # capabilities 是模型能力的唯一运行时来源；vision 仅保留为旧配置兼容字段。
        self.vision = profile.supports_vision
        return profile

    def public_model_options(self) -> list[dict[str, Any]]:
        """返回给前端对话可用的模型列表：必须 loaded 且已配置 API Key。"""
        options = [profile.public_dict() for profile in self.model_profiles.values() if profile.loaded and profile.has_key]
        # 如果过滤后为空，至少保留当前激活模型，避免前端无选项可选
        if not options and self.active_model_id in self.model_profiles:
            options = [self.model_profiles[self.active_model_id].public_dict()]
        return options

    def owner_overlay_data(self, owner_account_id: str | None = None) -> dict[str, Any]:
        """读取 owner 私有 overlay 配置。"""
        return _apply_features_namespace(_read_yaml_file(owner_overlay_config_path(owner_account_id)))

    def owner_env_map(self, owner_account_id: str | None = None) -> dict[str, str]:
        """读取 owner 私有 .env，不污染全局进程环境。"""
        return _load_env_map(resolve_writable_env_path(owner_account_id))

    def _owner_builtin_allows_global_key_fallback(self, owner_account_id: str | None = None) -> bool:
        """本地 owner 与隔离开发 owner 可读取进程环境中的模型 Key。"""
        owner = str(owner_account_id or "").strip()
        if owner == "local":
            return True
        if not self.gateway_dev_mode:
            return False
        dev_account = str(self.gateway_dev_account or "").strip()
        return bool(owner and dev_account and owner == dev_account)

    def owner_model_profiles(self, owner_account_id: str | None = None) -> dict[str, ModelProfile]:
        """返回 owner 可见的模型视图：全局共享模型 + owner 私有模型。

        这里的 builtin/shared 只由“配置来源”决定：
        - 基础 ``config.yaml`` 中加载的模型始终视为共享内置模型
        - owner overlay 中的模型始终视为 owner 私有模型

        内置模型的 API Key 优先从 owner 私有 ``.env`` 解析；单用户本地 owner
        与 dev 开发 owner也可读取进程环境，方便通过环境变量直接配置开源版。
        """
        env_map = self.owner_env_map(owner_account_id)
        fallback_global = self._owner_builtin_allows_global_key_fallback(owner_account_id)
        profiles: dict[str, ModelProfile] = {}
        for model_id, profile in self.model_profiles.items():
            if not profile.builtin:
                continue
            builtin = replace(profile)
            # 内置模型是全局共享层：key 解析走全局凭证库 + owner .env / 进程环境兜底
            builtin.api_key = resolve_profile_api_key(
                model_id,
                builtin.api_key_env,
                env_map=env_map,
                fallback_global=fallback_global,
                owner_account_id=owner_account_id,
            )
            profiles[model_id] = builtin

        overlay = self.owner_overlay_data(owner_account_id)
        models = (overlay.get("llm") or {}).get("models")
        if isinstance(models, dict):
            for model_id, raw in models.items():
                if not isinstance(raw, dict):
                    continue
                profile = _build_owner_model_profile(str(model_id), raw, env_map, owner_account_id or "")
                profiles[str(model_id)] = profile
        return profiles

    def owner_active_model_id(self, owner_account_id: str | None = None) -> str:
        """解析 owner 的默认兜底模型。

        ``llm.active`` 是早期配置字段；设置页已经把它呈现为“默认模型”。
        新配置同时写入 ``llm.default``，读取时仍兼容已有 owner overlay。
        Session 若有显式模型绑定，不受这里的默认值影响。
        """
        profiles = self.owner_model_profiles(owner_account_id)
        if not profiles:
            return self.active_model_id

        # 开源模板自带的 default 只用于说明配置结构。owner 已经配置真实可用
        # 模型后，不能再让这个占位项因为 CREW_API_KEY 的兼容回落而被误判为
        # 可用默认模型，否则辅助规划会请求 api.example.com/your-model-name。
        ready_model_id = next(
            (
                model_id
                for model_id in sorted(profiles)
                if profiles[model_id].loaded
                and profiles[model_id].has_key
                and not is_placeholder_model_profile(profiles[model_id])
            ),
            "",
        )

        def _resolved_candidate(model_id: str) -> str:
            profile = profiles.get(model_id)
            if profile is None or not profile.loaded:
                return ""
            if is_placeholder_model_profile(profile) and ready_model_id:
                return ready_model_id
            return model_id

        overlay = self.owner_overlay_data(owner_account_id)
        llm = overlay.get("llm") if isinstance(overlay.get("llm"), dict) else {}
        candidate = str((llm or {}).get("default") or (llm or {}).get("active") or "").strip()
        resolved = _resolved_candidate(candidate)
        if resolved:
            return resolved
        global_default = str(self.default_model_id or "").strip()
        resolved = _resolved_candidate(global_default)
        if resolved:
            return resolved
        resolved = _resolved_candidate(self.active_model_id)
        if resolved:
            return resolved
        if ready_model_id:
            return ready_model_id
        for model_id in sorted(profiles):
            profile = profiles[model_id]
            if profile.loaded and profile.has_key:
                return model_id
        for model_id in sorted(profiles):
            if profiles[model_id].loaded:
                return model_id
        return sorted(profiles)[0]

    def owner_active_model_profile(self, owner_account_id: str | None = None) -> ModelProfile | None:
        """返回 owner 默认兜底模型 profile（名称保留用于 API 兼容）。"""
        profiles = self.owner_model_profiles(owner_account_id)
        profile = profiles.get(self.owner_active_model_id(owner_account_id))
        # Programmatic/legacy configurations may not mark any global profile
        # as builtin, leaving the owner-visible map empty.  Keep the effective
        # profile aligned with the provider's global fallback in that case.
        return profile or self.active_model

    def owner_default_model_id(self, owner_account_id: str | None = None) -> str:
        """语义化别名：owner 默认兜底模型 id。"""
        return self.owner_active_model_id(owner_account_id)

    def owner_default_model_profile(self, owner_account_id: str | None = None) -> ModelProfile | None:
        """语义化别名：owner 默认兜底模型 profile。"""
        return self.owner_active_model_profile(owner_account_id)

    def owner_public_model_options(self, owner_account_id: str | None = None) -> list[dict[str, Any]]:
        """返回 owner 对话可选模型列表。"""
        profiles = self.owner_model_profiles(owner_account_id)
        options = [profile.public_dict() for profile in profiles.values() if profile.loaded and profile.has_key]
        active_id = self.owner_active_model_id(owner_account_id)
        if not options and active_id in profiles:
            options = [profiles[active_id].public_dict()]
        return options

    def owner_visible_model_profiles(
        self,
        owner_account_id: str | None = None,
        *,
        include_builtin_profiles: bool = True,
    ) -> list[ModelProfile]:
        """返回设置页可见模型列表。"""
        profiles = self.owner_model_profiles(owner_account_id)
        return [
            profile
            for profile in profiles.values()
            if include_builtin_profiles or not profile.builtin
        ]

    def persist_owner_model_profiles(
        self,
        owner_account_id: str,
        model_profiles: dict[str, ModelProfile],
        *,
        active_model_id: str | None = None,
    ) -> Path:
        """把 owner 私有模型视图写回 owner overlay。"""
        owner = str(owner_account_id or "").strip()
        if not owner:
            raise ValueError("owner_account_id 不能为空")
        with _CONFIG_WRITE_LOCK:
            yaml_path = owner_overlay_config_path(owner)
            data = _read_yaml_file(yaml_path)
            llm = data.get("llm")
            if not isinstance(llm, dict):
                llm = {}
                data["llm"] = llm
            active = str(active_model_id or llm.get("active") or "").strip()
            llm["active"] = active or self.active_model_id
            llm["default"] = llm["active"]
            llm["models"] = {
                model_id: _serialize_profile_for_yaml(profile)
                for model_id, profile in model_profiles.items()
                if not profile.builtin
            }
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = yaml_path.with_suffix(yaml_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(
                    data,
                    f,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
            tmp_path.replace(yaml_path)
            return yaml_path

    def persist_channel_config(
        self,
        name: str,
        config_data: dict[str, Any],
        *,
        owner_account_id: str | None = None,
    ) -> Path:
        """把单个平台配置写回全局或 owner overlay。"""
        platform = str(name or "").strip().lower()
        if not platform:
            raise ValueError("platform name 不能为空")
        owner = str(owner_account_id or "").strip()
        with _CONFIG_WRITE_LOCK:
            if owner:
                return self._persist_owner_channel_config_locked(owner, platform, dict(config_data))
            return self._persist_channel_config_locked(platform, dict(config_data))

    def _persist_owner_channel_config_locked(self, owner_account_id: str, name: str, config_data: dict[str, Any]) -> Path:
        yaml_path = owner_overlay_config_path(owner_account_id)
        data = _read_yaml_file(yaml_path)
        _write_channel_config(data, name, config_data, include_platforms=False)

        _atomic_write_yaml(yaml_path, data)
        return yaml_path

    # ---- 模型 profile 配置事务（候选值 → 持久化 → 发布）----
    #
    # 顺序契约（与 _commit_mcp_servers 一致，app.py 等调用方共用单一入口）：
    #   1. 候选值：在 model_profiles 的副本上计算新增/更新/删除后的完整映射，
    #      不触碰已发布的内存状态；
    #   2. 校验：存在性/唯一性前置条件不满足时抛 ValueError/KeyError，
    #      磁盘与内存均不变；
    #   3. 持久化：读盘 → 用候选值整体替换 llm 段 → _atomic_write_yaml 原子替换
    #      （失败清理残留 tmp 并重抛，目标文件保持旧内容）；
    #   4. 发布：磁盘提交成功后才把候选值发布为 self.model_profiles / active /
    #      default_model_id，并刷新 raw_config。
    #
    # owner 作用域：模型配置分两层——全局共享层（config.yaml 的 llm 段，本事务
    # 的唯一写入范围）与 owner 私有层（每个 owner 独立的 overlay yaml，经
    # persist_owner_model_profiles 按候选值写入）。本事务不触碰任何 owner
    # overlay，owner 之间按文件天然隔离，一个 owner 的操作不会影响其他 owner；
    # owner 私有模型的失败语义由其独立事务（persist_owner_model_profiles）保证。
    #
    # 失败语义：与 _commit_mcp_servers 一致——持久化失败（步骤 3 抛错）时内存与
    # 磁盘一致保留旧值，直接重试本入口即可，不丢更新；调用方收到异常后不得重建
    # Provider、不得切换激活、不得清理凭证/env、不得失效任何会话缓存。持久化
    # 成功后的运行资源操作失败（如 Provider 构建/退役失败）属于"已保存但切换
    # 失败"：磁盘与内存保持新值，调用方必须如实上报，不得回滚磁盘与内存伪装成
    # 保存失败。
    #
    # 其余设计要点：
    # 1. yaml 写回：使用 PyYAML 整体重写 config.yaml。这会丢失原注释（PyYAML 固有限制），
    #    但保证结构稳定、字段顺序可读。引入 ruamel.yaml 仅为此功能会破坏最小依赖原则。
    # 2. yaml 中只写非敏感字段（name/api_key_env/base_url/model/temperature/max_tokens/
    #    context_window/timeout）。API Key 明文不会进入 yaml，只会被写入 .env。
    # 3. .env 写入：单独函数处理（_write_env_key），按"已存在则替换该行，否则追加"策略，
    #    写完同步 os.environ 让当前进程立即可用。
    # 4. 边界：删除最后一个模型禁止（409）；删除激活模型由调用方负责切换激活。
    def _commit_model_profiles(
        self,
        candidate: dict[str, ModelProfile],
        *,
        active_model_id: str | None = None,
        default_model_id: str | None = None,
    ) -> Path:
        """统一写入边界：候选值 → 持久化 → 发布（顺序契约见上）。"""
        with _CONFIG_WRITE_LOCK:
            if not self.config_path:
                raise RuntimeError("config_path 未设置，无法写回（Config 不是从 yaml 加载的）")
            normalized = {str(pid): replace(p) for pid, p in candidate.items()}
            yaml_path = Path(self.config_path)
            data = _read_config_yaml_for_write(yaml_path)

            # 仅重写 llm 段，保留其它段（runtime/agent/gateway 等）原样
            llm = data.get("llm")
            if not isinstance(llm, dict):
                llm = {}
                data["llm"] = llm
            llm["active"] = active_model_id if active_model_id is not None else self.active_model_id
            default_id = default_model_id if default_model_id is not None else self.default_model_id
            if default_id:
                llm["default"] = default_id
            llm["models"] = {
                pid: _serialize_profile_for_yaml(p) for pid, p in normalized.items()
            }

            # 磁盘提交成功后才发布内存状态：写回失败时内存与磁盘一致保留旧值。
            _atomic_write_yaml(yaml_path, data)
            self.model_profiles = normalized
            if active_model_id is not None:
                self.active_model_id = active_model_id
            if default_model_id is not None:
                self.default_model_id = default_model_id
            self.raw_config = data
            return yaml_path

    def add_model(self, profile_data: dict[str, Any]) -> ModelProfile:
        """新增一个模型 profile：候选值 → 持久化 → 发布（顺序契约见上）。

        Args:
            profile_data: 必须含 id；其它字段缺省时取 ModelProfile 默认值。
                          可选 api_key（明文）：若提供则写入 .env（按 api_key_env 名）。

        Returns:
            新建的 ModelProfile。

        Raises:
            ValueError: id 为空或已存在（磁盘与内存均不变）。
            RuntimeError: config_path 为空（未通过 yaml 加载）。
            Exception: 持久化失败（内存与磁盘一致保留旧值，可直接重试）。
        """
        model_id = str(profile_data.get("id") or "").strip()
        if not model_id:
            raise ValueError("model id 不能为空")
        if model_id in self.model_profiles:
            raise ValueError(f"模型 id 已存在: {model_id}")

        # 构建 profile（不含 api_key；key 由调用方处理 env 写入）
        profile = _build_profile_from_payload(model_id, profile_data, owner_account_id="")
        self._commit_model_profiles({**self.model_profiles, model_id: profile})
        return profile

    def update_model(self, model_id: str, profile_data: dict[str, Any]) -> ModelProfile:
        """更新已存在的模型 profile：候选值 → 持久化 → 发布（顺序契约见上）。

        支持部分更新：未传入的字段保留原值。id 不可变（来自 path 参数）。

        Args:
            model_id: 目标 profile id（必须已存在）。
            profile_data: 待覆盖字段。

        Raises:
            KeyError: model_id 不存在（磁盘与内存均不变）。
            Exception: 持久化失败（内存与磁盘一致保留旧值，可直接重试）。
        """
        if model_id not in self.model_profiles:
            raise KeyError(model_id)

        current = self.model_profiles[model_id]
        # 合并：原值 + 新值（dataclass 字段集合作为白名单，防止脏字段写入）
        merged = {
            "id": model_id,
            "name": profile_data.get("name", current.name),
            "api_key_env": profile_data.get("api_key_env", current.api_key_env),
            "provider": profile_data.get("provider", current.provider),
            "base_url": profile_data.get("base_url", current.base_url),
            "model": profile_data.get("model", current.model),
            "temperature": profile_data.get("temperature", current.temperature),
            "max_tokens": profile_data.get("max_tokens", current.max_tokens),
            "context_window": profile_data.get("context_window", current.context_window),
            "timeout": profile_data.get("timeout", current.timeout),
            "loaded": profile_data.get("loaded", current.loaded),
            "builtin": profile_data.get("builtin", current.builtin),
            "capabilities": profile_data.get("capabilities", list(current.capabilities)),
        }
        # _build_profile_from_payload 按 key 解析链取值（凭证库 → env → 全局兜底）：
        # - 调用方先写凭证库/env → 取到新 key
        # - api_key_env 改到不存在的变量 → 取到空串，has_key=False（反映真实状态）
        # - 什么都不改 → 取到原值（os.environ 在 load_config 时已设置）
        profile = _build_profile_from_payload(model_id, merged, owner_account_id="")
        self._commit_model_profiles({**self.model_profiles, model_id: profile})
        return profile

    def remove_model(self, model_id: str) -> ModelProfile:
        """删除一个模型 profile：候选值 → 持久化 → 发布（顺序契约见上）。

        Args:
            model_id: 待删除的 profile id。

        Raises:
            KeyError: model_id 不存在（磁盘与内存均不变）。
            ValueError: 试图删除最后一个模型（至少保留一个）。
            Exception: 持久化失败（内存与磁盘一致保留旧值，可直接重试）。
        """
        if model_id not in self.model_profiles:
            raise KeyError(model_id)
        if len(self.model_profiles) <= 1:
            raise ValueError("至少保留一个模型配置，禁止删除最后一个")

        removed = self.model_profiles[model_id]
        self._commit_model_profiles(
            {pid: p for pid, p in self.model_profiles.items() if pid != model_id}
        )
        return removed

    def set_active_model(self, model_id: str) -> ModelProfile:
        """切换激活（默认兜底）模型：候选值 → 持久化 → 发布（顺序契约见上）。

        与 activate_model（纯内存发布）的区别：磁盘提交成功后才发布内存，
        持久化失败时 active/default 与磁盘一致保留旧值。use_model（app 层）与
        删除激活模型后的自动切换共用本入口。

        Raises:
            KeyError: model_id 不存在。
            ValueError: 模型未加载，不能用于对话。
            Exception: 持久化失败（内存与磁盘一致保留旧值，可直接重试）。
        """
        if model_id not in self.model_profiles:
            raise KeyError(model_id)
        if not self.model_profiles[model_id].loaded:
            raise ValueError(f"模型未加载，不能用于对话: {model_id}")
        self._commit_model_profiles(
            dict(self.model_profiles),
            active_model_id=model_id,
            default_model_id=model_id,
        )
        # 激活派生字段（api_key/provider/base_url/model/...）在磁盘提交成功后
        # 发布；activate_model 的存在性与 loaded 校验在此必然已通过。
        return self.activate_model(model_id)

    def persist_model_profiles(self) -> Path:
        """把当前 model_profiles 写回 config.yaml（遗留入口，失败语义见上）。

        注意：使用 PyYAML 整体重写，原注释会丢失。备份建议在 UI/CLI 提示用户。

        Returns:
            实际写入的 yaml 文件路径。

        Raises:
            RuntimeError: config_path 为空（未通过 yaml 加载）或写回失败。
        """
        return self._commit_model_profiles(dict(self.model_profiles))

    # ---- evolution 配置事务（候选值 → 持久化 → 发布）----
    #
    # 顺序契约与失败语义同模型 profile 事务：候选值只在副本上计算，磁盘提交
    # 成功后才发布内存；持久化失败时内存与磁盘一致保留旧值，可直接重试。
    # evolution 是全局开关（无 owner 维度），gateway misc 与 CLI knowledge
    # 共用本入口。
    def set_evolution_config(
        self,
        *,
        auto_trigger: bool | None = None,
        auto_full_cycle: bool | None = None,
        visible: bool | None = None,
    ) -> Path:
        """更新 evolution 开关并持久化到 config.yaml 的 agent.evolution 段。

        Args:
            auto_trigger / auto_full_cycle / visible: None 表示保持原值。

        Raises:
            RuntimeError: config_path 为空（未通过 yaml 加载）。
            Exception: 持久化失败（内存与磁盘一致保留旧值，可直接重试）。
        """
        candidate = {
            "auto_trigger": self.evolution_auto_trigger if auto_trigger is None else bool(auto_trigger),
            "auto_full_cycle": (
                self.evolution_auto_full_cycle if auto_full_cycle is None else bool(auto_full_cycle)
            ),
            "visible": self.evolution_visible if visible is None else bool(visible),
        }
        with _CONFIG_WRITE_LOCK:
            if not self.config_path:
                raise RuntimeError("config_path 未设置，无法写回（Config 不是从 yaml 加载的）")
            yaml_path = Path(self.config_path)
            data = _read_config_yaml_for_write(yaml_path)

            agent = data.get("agent")
            if not isinstance(agent, dict):
                agent = {}
                data["agent"] = agent
            agent["evolution"] = candidate

            # 磁盘提交成功后再发布内存状态：写回失败时内存与磁盘一致保留旧值。
            _atomic_write_yaml(yaml_path, data)
            self.evolution_auto_trigger = candidate["auto_trigger"]
            self.evolution_auto_full_cycle = candidate["auto_full_cycle"]
            self.evolution_visible = candidate["visible"]
            self.raw_config = data
            return yaml_path

    def persist_evolution_config(self) -> Path:
        """把当前 evolution 配置写回 config.yaml（遗留入口，失败语义见 set_evolution_config）。"""
        return self.set_evolution_config()

    # ---- MCP server 配置事务（候选值 → 持久化 → 发布）----
    #
    # 顺序契约（与 _atomic_write_yaml 配合，gateway Router 与 CLI 共用单一入口）：
    #   1. 候选值：在 mcp_servers 的副本上计算新增/更新/删除后的完整映射，
    #      不触碰已发布的内存状态；
    #   2. 校验：存在性前置条件不满足时抛 ValueError/KeyError，磁盘与内存均不变；
    #   3. 持久化：读盘 → 用候选值整体替换 mcp_servers 段 → _atomic_write_yaml
    #      原子替换（失败清理残留 tmp 并重抛，目标文件保持旧内容）；
    #   4. 发布：磁盘提交成功后才把候选值发布为 self.mcp_servers 并刷新
    #      raw_config。
    #
    # 失败语义：
    # - 持久化失败（步骤 3 抛错）时内存与磁盘一致保留旧值，直接重试本方法即可，
    #   不丢更新；调用方收到异常后不得启动/重载/移除任何运行资源。
    # - 持久化成功后的运行资源操作（连接/断开 MCP 子进程）失败属于
    #   "已保存但连接失败"：磁盘与内存保持新值，调用方必须如实上报错误，
    #   不得回滚磁盘与内存伪装成保存失败。
    #
    # 边界：本边界只保证单个 config.yaml 的原子替换与内存发布的先后一致，
    # 不承诺多文件与外部运行资源（MCP 子进程、连接池）的分布式原子性。
    def _commit_mcp_servers(self, candidate: dict[str, Any]) -> Path:
        """统一写入边界：候选值 → 持久化 → 发布（顺序契约见上）。"""
        normalized = {
            str(key): dict(value) if isinstance(value, dict) else {}
            for key, value in candidate.items()
        }
        with _CONFIG_WRITE_LOCK:
            if not self.config_path:
                raise RuntimeError("config_path 未设置，无法写回（Config 不是从 yaml 加载的）")
            yaml_path = Path(self.config_path)
            data = _read_config_yaml_for_write(yaml_path)
            servers = data.get("mcp_servers")
            if not isinstance(servers, dict):
                servers = {}
                data["mcp_servers"] = servers
            servers.clear()
            servers.update(normalized)
            _atomic_write_yaml(yaml_path, data)
            self.mcp_servers = normalized
            self.raw_config = data
            return yaml_path

    def add_mcp_server(self, name: str, server_cfg: dict[str, Any]) -> Path:
        """新增 MCP server：候选值 → 持久化 → 发布。

        Raises:
            ValueError: server 已存在（磁盘与内存均不变）。
            Exception: 持久化失败（内存与磁盘一致保留旧值，可直接重试）。
        """
        servers = self.mcp_servers if isinstance(self.mcp_servers, dict) else {}
        key = str(name)
        if key in servers:
            raise ValueError(f"MCP server 已存在: {key}")
        return self._commit_mcp_servers({**servers, key: dict(server_cfg)})

    def update_mcp_server(self, name: str, server_cfg: dict[str, Any]) -> Path:
        """更新 MCP server：候选值 → 持久化 → 发布。

        Raises:
            KeyError: server 不存在（磁盘与内存均不变）。
            Exception: 持久化失败（内存与磁盘一致保留旧值，可直接重试）。
        """
        servers = self.mcp_servers if isinstance(self.mcp_servers, dict) else {}
        key = str(name)
        if key not in servers:
            raise KeyError(f"MCP server 不存在: {key}")
        return self._commit_mcp_servers({**servers, key: dict(server_cfg)})

    def delete_mcp_server(self, name: str) -> Path:
        """删除 MCP server：候选值 → 持久化 → 发布。

        Raises:
            KeyError: server 不存在（磁盘与内存均不变）。
            Exception: 持久化失败（内存与磁盘一致保留旧值，可直接重试）。
        """
        servers = self.mcp_servers if isinstance(self.mcp_servers, dict) else {}
        key = str(name)
        if key not in servers:
            raise KeyError(f"MCP server 不存在: {key}")
        return self._commit_mcp_servers(
            {key_: value for key_, value in servers.items() if key_ != key}
        )

    def set_mcp_server(self, name: str, cfg: dict[str, Any]) -> None:
        """在运行时更新 mcp_servers 配置（不自动持久化）。

        遗留入口：先改内存、由调用方再调 persist_mcp_servers，两步之间失败
        会造成内存与磁盘分叉。管理 API / CLI 必须改用 add/update/
        delete_mcp_server 候选值事务；本方法仅为未迁移的内部调用方保留
        （如 cua-setup）。
        """
        if not isinstance(self.mcp_servers, dict):
            self.mcp_servers = {}
        self.mcp_servers[str(name)] = dict(cfg)

    def remove_mcp_server(self, name: str) -> None:
        """在运行时移除 mcp_servers 配置（不自动持久化）。遗留入口，见 set_mcp_server。"""
        if isinstance(self.mcp_servers, dict) and name in self.mcp_servers:
            self.mcp_servers.pop(name, None)

    def persist_mcp_servers(self) -> Path:
        """把当前 mcp_servers 写回 config.yaml（遗留入口，失败语义见 set_mcp_server）。"""
        servers = self.mcp_servers if isinstance(self.mcp_servers, dict) else {}
        return self._commit_mcp_servers(dict(servers))

    def _persist_channel_config_locked(self, name: str, config_data: dict[str, Any]) -> Path:
        if not self.config_path:
            raise RuntimeError("config_path 未设置，无法写回（Config 不是从 yaml 加载的）")

        yaml_path = Path(self.config_path)
        data = _read_config_yaml_for_write(yaml_path)

        merged = _write_channel_config(data, name, config_data)

        # 磁盘提交成功后再发布内存状态：写回失败时内存与磁盘一致保留旧值。
        _atomic_write_yaml(yaml_path, data)
        self.channels[name] = dict(merged)
        platforms = data.get("platforms")
        self.platforms = platforms if isinstance(platforms, dict) else {}
        self.raw_config = data
        return yaml_path

    def channel_config(self, name: str, owner_account_id: str | None = None) -> dict[str, Any]:
        """Return merged channel/platform config for *name*.

        ``channels.<name>`` wins over the legacy ``platforms.<name>`` layout
        because current channel plugins use the former layout.
        """
        owner = str(owner_account_id or "").strip()
        if owner:
            # owner overlay 只认 channels 布局（含归一后的 features.channels），
            # 不回落 platforms 旧别名，避免全局渠道凭据经 overlay 复活。
            overlay = self.owner_overlay_data(owner)
            channels = overlay.get("channels")
            channel_raw = channels.get(name) if isinstance(channels, dict) else {}
            if isinstance(channel_raw, dict):
                return deepcopy(channel_raw)
            return {}
        platform_raw = self.platforms.get(name) if isinstance(self.platforms, dict) else {}
        channel_raw = self.channels.get(name) if isinstance(self.channels, dict) else {}
        merged: dict[str, Any] = {}
        if isinstance(platform_raw, dict):
            merged.update(deepcopy(platform_raw))
        if isinstance(channel_raw, dict):
            merged.update(deepcopy(channel_raw))
        return merged

    def apply_platform_config_bridges(self, entries: list[Any]) -> None:
        """Apply plugin-owned YAML bridges after plugin discovery.

        Platform plugins may own translation from YAML to env vars and extra
        fields. Running this once during app assembly keeps status reads
        side-effect free while preserving env > YAML precedence inside plugins.
        """
        for entry in entries:
            bridge = getattr(entry, "apply_yaml_config_fn", None)
            if bridge is None:
                continue
            platform_cfg = self._raw_platform_config(getattr(entry, "name", ""))
            if not platform_cfg:
                continue
            try:
                seeded = bridge(self.raw_config, platform_cfg)
            except Exception as exc:  # noqa: BLE001
                log.warning("平台 %s YAML 配置桥接失败: %s", getattr(entry, "name", ""), exc)
                continue
            if isinstance(seeded, dict) and seeded:
                platform_cfg.setdefault("extra", {}).update(seeded)
            self._merge_platform_config(getattr(entry, "name", ""), platform_cfg)

    def _raw_platform_config(self, name: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        top_level = self.raw_config.get(name)
        if isinstance(top_level, dict):
            merged.update(top_level)
        platform_raw = self.platforms.get(name) if isinstance(self.platforms, dict) else None
        if isinstance(platform_raw, dict):
            merged.update(platform_raw)
        channel_raw = self.channels.get(name) if isinstance(self.channels, dict) else None
        if isinstance(channel_raw, dict):
            merged.update(channel_raw)
        return merged

    def _merge_platform_config(self, name: str, platform_cfg: dict[str, Any]) -> None:
        target = self.channels.get(name)
        if not isinstance(target, dict):
            target = self.platforms.get(name)
        if not isinstance(target, dict):
            target = {}
            self.platforms[name] = target
        extra = platform_cfg.pop("extra", None)
        target.update(platform_cfg)
        if isinstance(extra, dict) and extra:
            existing = target.get("extra")
            if not isinstance(existing, dict):
                existing = {}
            existing.update(extra)
            target["extra"] = existing


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _as_flag(value: Any, default: bool) -> bool:
    """宽松开关解析：bool 直取；字符串只认常见开/关字面量；其余一律回退默认值。

    与各 Feature config 的 from_raw 布尔规则一致（非 bool/str 的值不猜测，
    保持默认），避免 core 侧轻量字段与 Feature 侧解析结果分叉。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _model_capabilities(raw: dict[str, Any]) -> list[str]:
    value = raw.get("capabilities")
    if isinstance(value, list):
        items = [str(item).strip().lower() for item in value if str(item).strip()]
        return list(dict.fromkeys(items or ["text", "tools"]))

    # 旧配置没有 capabilities 时，用 legacy vision 完成一次兼容迁移；新配置一旦
    # 提供 capabilities，它就是唯一事实来源，避免 UI 与 Provider 各读一套开关。
    items = ["text", "tools"]
    if _as_bool(raw.get("vision", True), True):
        items.append("vision")
    return items


def _build_model_profile(model_id: str, raw: dict[str, Any]) -> ModelProfile:
    api_key_env = str(raw.get("api_key_env") or "CREW_API_KEY")
    api_key = resolve_profile_api_key(
        model_id,
        api_key_env,
        fallback_global=True,
        owner_account_id="",
    )
    capabilities = _model_capabilities(raw)

    return ModelProfile(
        id=model_id,
        name=str(raw.get("name") or model_id),
        api_key=api_key,
        api_key_env=api_key_env,
        provider=str(raw.get("provider") or "openai").strip().lower() or "openai",
        base_url=str(raw.get("base_url") or ""),
        model=str(raw.get("model") or "gpt-4o-mini"),
        temperature=_as_float(raw.get("temperature", 0.7), 0.7),
        max_tokens=_as_int_or_none(raw.get("max_tokens")),
        context_window=_as_int_or_none(raw.get("context_window")),
        timeout=_as_float(raw.get("timeout", 60.0), 60.0),
        vision="vision" in capabilities,
        loaded=_as_bool(raw.get("loaded", True), True),
        # 基础 config.yaml 是共享配置层；是否内置不信任用户填写字段，而由来源决定。
        builtin=True,
        capabilities=capabilities,
    )


def _build_owner_model_profile(
    model_id: str,
    raw: dict[str, Any],
    env_map: dict[str, str],
    owner_account_id: str,
) -> ModelProfile:
    api_key_env = str(raw.get("api_key_env") or "CREW_API_KEY")
    api_key = resolve_profile_api_key(
        model_id,
        api_key_env,
        env_map=env_map,
        fallback_global=False,
        owner_account_id=owner_account_id,
    )
    capabilities = _model_capabilities(raw)
    return ModelProfile(
        id=model_id,
        name=str(raw.get("name") or model_id),
        api_key=api_key,
        api_key_env=api_key_env,
        provider=str(raw.get("provider") or "openai").strip().lower() or "openai",
        base_url=str(raw.get("base_url") or ""),
        model=str(raw.get("model") or "gpt-4o-mini"),
        temperature=_as_float(raw.get("temperature", 0.7), 0.7),
        max_tokens=_as_int_or_none(raw.get("max_tokens")),
        context_window=_as_int_or_none(raw.get("context_window")),
        timeout=_as_float(raw.get("timeout", 60.0), 60.0),
        vision="vision" in capabilities,
        loaded=_as_bool(raw.get("loaded", True), True),
        builtin=False,
        capabilities=capabilities,
    )


def _build_profile_from_payload(
    model_id: str,
    payload: dict[str, Any],
    *,
    owner_account_id: str,
    env_map: dict[str, str] | None = None,
) -> ModelProfile:
    """从 CRUD payload 构建 ModelProfile。

    与 _build_model_profile 的区别：后者从 yaml+env 加载；前者从用户输入构建。
    key 走 resolve_profile_api_key 解析链（凭证库 → env → 全局兜底）：
    - 全局作用域（owner_account_id 为空）：env 兜底 CREW_API_KEY，与存量行为一致；
    - owner 作用域：env_map 为该 owner 的 .env（写入不同步进程环境），无全局兜底。
    """
    api_key_env = str(payload.get("api_key_env") or "CREW_API_KEY").strip() or "CREW_API_KEY"
    api_key = resolve_profile_api_key(
        model_id,
        api_key_env,
        env_map=env_map,
        fallback_global=not bool(owner_account_id),
        owner_account_id=owner_account_id,
    )
    capabilities = _model_capabilities(payload)
    return ModelProfile(
        id=model_id,
        name=str(payload.get("name") or model_id),
        api_key=api_key,
        api_key_env=api_key_env,
        provider=str(payload.get("provider") or "openai").strip().lower() or "openai",
        base_url=str(payload.get("base_url") or ""),
        model=str(payload.get("model") or "gpt-4o-mini"),
        temperature=_as_float(payload.get("temperature", 0.7), 0.7),
        max_tokens=_as_int_or_none(payload.get("max_tokens")),
        context_window=_as_int_or_none(payload.get("context_window")),
        timeout=_as_float(payload.get("timeout", 60.0), 60.0),
        vision="vision" in capabilities,
        loaded=_as_bool(payload.get("loaded", True), True),
        builtin=_as_bool(payload.get("builtin", False), False),
        capabilities=capabilities,
    )


def _serialize_profile_for_yaml(profile: ModelProfile) -> dict[str, Any]:
    """把 ModelProfile 序列化为 yaml 安全的 dict（不含 api_key 明文）。

    明确不写 api_key 字段，让 .env 成为唯一 secret 存储位置。
    """
    data: dict[str, Any] = {
        "name": profile.name or profile.id,
        "api_key_env": profile.api_key_env,
        "provider": profile.provider,
        "base_url": profile.base_url,
        "model": profile.model,
        "temperature": profile.temperature,
        "timeout": profile.timeout,
        "loaded": profile.loaded,
        "builtin": profile.builtin,
        "capabilities": list(profile.capabilities),
    }
    if profile.max_tokens is not None:
        data["max_tokens"] = profile.max_tokens
    if profile.context_window is not None:
        data["context_window"] = profile.context_window
    if not profile.supports_vision:
        data["vision"] = False
    return data


def _lookup_api_key(
    api_key_env: str,
    env_map: dict[str, str] | None,
    *,
    fallback_global: bool,
) -> str:
    env_name = str(api_key_env or "CREW_API_KEY").strip() or "CREW_API_KEY"
    if env_map is not None:
        local = str(env_map.get(env_name, "") or "")
        if local:
            return local
        if env_name != "CREW_API_KEY":
            fallback = str(env_map.get("CREW_API_KEY", "") or "")
            if fallback:
                return fallback
        if not fallback_global:
            return ""
    value = os.getenv(env_name, "") or ""
    if not value and env_name != "CREW_API_KEY" and fallback_global:
        value = os.getenv("CREW_API_KEY", "") or ""
    return value


def resolve_profile_api_key(
    profile_id: str,
    api_key_env: str,
    env_map: dict[str, str] | None = None,
    *,
    fallback_global: bool,
    owner_account_id: str,
) -> str:
    """模型 API Key 解析链（单一事实来源，所有 profile 构建点都走这里）。

    凭证库[profile_id]（owner 作用域，见 crew.state.credentials）
    → api_key_env 环境变量（env_map 优先于进程环境）
    → fallback_global 时回落 CREW_API_KEY。

    凭证库条目由 CRUD 写路径维护：默认按 profile id 存储，从结构上避免
    "多模型共用一个环境变量名、新 key 覆盖旧 key"的串 key 问题；环境变量
    路径完整保留，存量 .env 部署零迁移。
    """
    stored = read_stored_key(owner_account_id, profile_id)
    if stored:
        return stored
    return _lookup_api_key(api_key_env, env_map, fallback_global=fallback_global)


def _load_env_map(env_path: Path) -> dict[str, str]:
    if not env_path.is_file():
        return {}
    raw = dotenv_values(env_path)
    return {str(k): str(v) for k, v in raw.items() if k and v not in (None, "")}


def owner_overlay_config_path(owner_account_id: str | None = None) -> Path:
    from crew.state.home import get_owner_runtime_home

    return get_owner_runtime_home(owner_account_id) / "config.yaml"


def _read_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"config.yaml 解析失败: {path}") from exc
    return data if isinstance(data, dict) else {}


def resolve_writable_env_path(owner_account_id: str | None = None) -> Path:
    """返回适合写入的 .env 路径。

    用户在运行期保存的 key 写入 Crew Home，而不是系统默认配置目录。
    这样 `config/.env` 仅作为本地开发层，用户保存的凭据走
    `{CREW_HOME}/.env` 覆盖层，删除模型或渠道账号时也只清理用户层。
    """
    from crew.state.home import get_owner_runtime_home

    return get_owner_runtime_home(owner_account_id) / ".env"


def _replace_text_file(tmp_path: Path, target_path: Path, content: str) -> None:
    """写入临时文件并替换目标；Windows 锁文件时退回直接覆盖。"""
    tmp_path.write_text(content, encoding="utf-8")
    try:
        tmp_path.replace(target_path)
    except PermissionError:
        # Windows 上杀毒/索引器偶发短暂占用 .env 时，os.replace 可能失败。
        # 直接覆盖仍保持目标内容正确，且比把运行期配置保存中断更可接受。
        target_path.write_text(content, encoding="utf-8")
        tmp_path.unlink(missing_ok=True)


def write_env_key(env_path: Path, var_name: str, value: str, *, sync_process_env: bool = True) -> None:
    """把 key=value 写入指定 .env 文件（按行匹配：已存在则替换，否则追加）。

    写入后同步到 os.environ，让当前进程立即可用。

    Args:
        env_path: 目标 .env 文件路径（不存在会创建）。
        var_name: 环境变量名（必须是合法标识符，由调用方保证）。
        value: 变量值（明文，写入文件时不再转义）。
    """
    with _CONFIG_WRITE_LOCK:
        lines: list[str] = []
        prefix = f"{var_name}="
        replaced = False

        if env_path.exists():
            try:
                content = env_path.read_text(encoding="utf-8")
                lines = content.splitlines()
            except OSError as exc:
                raise RuntimeError(f"读取 .env 失败: {env_path}: {exc}") from exc

            for i, line in enumerate(lines):
                # 跳过注释行；匹配以 `var=` 开头的非注释行
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith(prefix):
                    lines[i] = f"{var_name}={value}"
                    replaced = True
                    break

        if not replaced:
            # 文件末尾保证有空行分隔
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(f"{var_name}={value}")

        env_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = env_path.with_suffix(env_path.suffix + ".tmp")
        _replace_text_file(tmp_path, env_path, "\n".join(lines) + "\n")

    if sync_process_env:
        os.environ[var_name] = value


def remove_env_key(env_path: Path, var_name: str, *, sync_process_env: bool = True) -> None:
    """从 .env 文件和当前进程环境中移除一个变量。"""
    with _CONFIG_WRITE_LOCK:
        lines: list[str] = []
        prefix = f"{var_name}="
        changed = False
        if env_path.exists():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    stripped = line.lstrip()
                    if not stripped.startswith("#") and stripped.startswith(prefix):
                        changed = True
                        continue
                    lines.append(line)
            except OSError as exc:
                raise RuntimeError(f"读取 .env 失败: {env_path}: {exc}") from exc
        if changed:
            tmp_path = env_path.with_suffix(env_path.suffix + ".tmp")
            _replace_text_file(tmp_path, env_path, "\n".join(lines).rstrip() + ("\n" if lines else ""))
    if sync_process_env:
        os.environ.pop(var_name, None)


def _load_env_files() -> None:
    """按优先级顺序加载 .env 文件，后者覆盖前者。

    关键场景：PyInstaller --onedir 打包后，源码里的 .env 不会自动进入 _internal/。
    此时用户把 .env 放在 .exe 同级（EXE_DIR）就能生效，无需重打包。

    顺序设计：EXE_DIR 故意放在 cwd 之后，确保用户"打好的包旁边再添加"具备最高优先级。
    """
    candidates: list[Path] = [
        ROOT / "config" / ".env",
        ROOT / ".env",
        Path.cwd() / ".env",
        ROOT / ".crew" / ".env",
    ]
    # 用户配置目录（冻结态 ~/.crew，开发态 ROOT/config）
    user_env = _get_user_config_dir() / ".env"
    if user_env not in candidates:
        candidates.append(user_env)
    # PyInstaller 冻结态：把 .exe 同级路径追加在最后，最高优先级
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / ".env")
        from crew.state.home import get_crew_home
        candidates.append(get_crew_home() / ".env")
    env_home = os.getenv("CREW_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser() / ".env")

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            load_dotenv(resolved, override=True)


def _load_crew_home_env_file(crew_home: str | Path | None) -> None:
    """加载 config.yaml runtime.crew_home 指向的 .env。"""
    if not crew_home:
        return
    path = Path(crew_home).expanduser() / ".env"
    if path.is_file():
        load_dotenv(path, override=True)


def _refresh_model_profile_keys(cfg: Config) -> None:
    """在额外加载 .env 后刷新已构建 ModelProfile 的 api_key。"""
    for profile in cfg.model_profiles.values():
        api_key = os.getenv(profile.api_key_env, "") or ""
        if not api_key and profile.api_key_env != "CREW_API_KEY":
            api_key = os.getenv("CREW_API_KEY", "") or ""
        profile.api_key = api_key


def _resolve_active_model_id(cfg: Config) -> str:
    """选择启动时可用于对话的 active profile。"""
    if not cfg.model_profiles:
        raise ValueError("没有可用的模型配置")
    active = cfg.model_profiles.get(cfg.active_model_id)
    if active is not None and active.loaded:
        return active.id
    for model_id in sorted(cfg.model_profiles):
        profile = cfg.model_profiles[model_id]
        if profile.loaded and profile.has_key:
            log.warning("激活模型 %s 未加载，已回退到 %s", cfg.active_model_id, model_id)
            return model_id
    for model_id in sorted(cfg.model_profiles):
        if cfg.model_profiles[model_id].loaded:
            log.warning("激活模型 %s 未加载，已回退到 %s", cfg.active_model_id, model_id)
            return model_id
    raise ValueError("没有已加载的模型配置，不能启动对话")


# --- features.* 命名空间（命名审计 P2-1 · 双读渐进第一步）---
#
# 目标形态：每个 Feature 的配置收敛到 features.<name>.* 子树（约定见
# docs/backend/modules/naming-audit.html §1）。迁移期 loader 同时读旧节与
# features.<name>：读取各节前先把 features 子树按下面两张显式映射表合并回
# 旧位置（同名键 features 侧获胜、旧节兜底），此后所有既有读取代码零改动，
# 下游与 Feature 侧解析均不感知双读。没有 features 节时原样返回，旧配置
# 文件的解析结果逐字节不变。
#
# 映射表分两类：
# - SECTION_MAP：features.<name> 整节 → 旧顶层节（或某节下的子节），
#   节内容与旧节逐字兼容；
# - FLAT_MAP：features.<name>.<key> → 旧平铺键。dynamic-kanban 历史上
#   平铺在 runtime.dk_*（无独立顶层节），目标子键去掉 dk_ 前缀。
#
# 库路径（runtime.<feature>_db_path）不进 features.*：db 文件由 core 装配层
# 消费并注入各 Store（Feature 自身不读自己的库路径），按"谁消费谁归属"留在
# runtime.*，属于终态而非过渡态。platforms 是 channels 的更旧别名（读取侧
# 已在 channel_config 合并），不单独映射；P2-1 第二步起写侧统一生产
# features.channels（见 _write_channel_config），旧节仅保留读取兼容、不再由
# 模板或运行时写回产出。
_FEATURES_SECTION_MAP: dict[str, tuple[str, ...]] = {
    "wiki": ("wiki",),
    "cron": ("cron",),
    "team": ("team",),
    "tasks": ("tasks",),
    "external_agents": ("external_agents",),
    "channels": ("channels",),
    "browser": ("tools", "browser"),
}
_FEATURES_FLAT_MAP: dict[tuple[str, str], tuple[str, ...]] = {
    ("dynamic_kanban", "task_timeout_seconds"): ("runtime", "dk_task_timeout_seconds"),
    ("dynamic_kanban", "verification_gate_enabled"): ("runtime", "dk_verification_gate_enabled"),
}


def _read_config_yaml_for_write(yaml_path: Path) -> dict[str, Any]:
    """读盘待写回：文件缺失按空 dict 构建；解析失败拒绝写回以保护原文件。"""
    if not yaml_path.exists():
        return {}
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"config.yaml 解析失败，拒绝写回以保护原文件: {yaml_path}") from exc
    return data


def _atomic_write_yaml(yaml_path: Path, data: dict[str, Any]) -> None:
    """把 data 原子写入 yaml_path：先写同目录 tmp 文件，成功后原子替换。

    中途失败（写 tmp 或替换）时清理残留 tmp 并原样重抛异常，目标文件保持
    旧内容不动；内存状态（channels/platforms/raw_config 等）的发布由调用方
    放在本函数成功返回之后，保证失败时内存与磁盘一致保留旧值。
    """
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = yaml_path.with_suffix(yaml_path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                data, f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        tmp_path.replace(yaml_path)
    except BaseException:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个配置 dict：override 同名键获胜，两侧 dict 值逐层合并。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _write_channel_config(
    data: dict[str, Any],
    name: str,
    config_data: dict[str, Any],
    *,
    include_platforms: bool = True,
) -> dict[str, Any]:
    """Materialize one channel into the canonical ``features.channels`` namespace.

    写侧终态契约（P2-1 第二步）：通道配置一律写回 features.channels，不再
    生产旧顶层 channels 节。磁盘上同名条目的旧位置（platforms/channels）仍
    作为本次生效值的合并来源参与计算，写回后按通道名逐个清理（legacy
    positions cleaned per channel）。remove-keys 与合并语义保持不变。
    """
    platforms = data.get("platforms")
    platform_map = platforms if isinstance(platforms, dict) else {}
    channels = data.get("channels")
    channel_map = channels if isinstance(channels, dict) else {}
    features = data.get("features")
    if not isinstance(features, dict):
        if features is not None:
            log.warning("features 节必须是键值映射，通道写回时已重置为空映射")
        features = {}
        data["features"] = features
    target = features.get("channels")
    if not isinstance(target, dict):
        if target is not None:
            log.warning("features.channels 必须是键值映射，通道写回时已重置为空映射")
        target = {}
        features["channels"] = target
    fallback_maps = [platform_map, channel_map] if include_platforms else [channel_map]

    payload = deepcopy(config_data)
    raw_remove = payload.pop("_remove_keys", [])
    if isinstance(raw_remove, (str, bytes)):
        remove_keys = {str(raw_remove)}
    else:
        try:
            remove_keys = {str(key) for key in (raw_remove or [])}
        except TypeError:
            remove_keys = set()
    platform_value = platform_map.get(name) if include_platforms else {}
    channel_value = channel_map.get(name)
    target_value = target.get(name) if isinstance(target.get(name), dict) else {}
    channel_effective = _deep_merge_dicts(
        channel_value if isinstance(channel_value, dict) else {}, target_value
    )
    # 与源数据深拷贝脱钩：platform 值与渠道有效合并结果的嵌套 dict 可能经
    # YAML anchor / 复用入参在渠道间共享引用，下方 remove-keys 会原地 pop，
    # 浅拷贝会把删除泄漏到兄弟渠道条目（或调用方对象）上。
    current = deepcopy(platform_value) if isinstance(platform_value, dict) else {}
    current.update(deepcopy(channel_effective))
    for key in remove_keys:
        current.pop(key, None)
        extra = current.get("extra")
        if isinstance(extra, dict):
            extra.pop(key, None)
            if not extra:
                current.pop("extra", None)
    merged = {**current, **deepcopy(payload)}
    target[name] = merged
    for fallback in fallback_maps:
        fallback.pop(name, None)
    return merged


def _ensure_container(data: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """沿 path 逐层取（必要时创建）dict 容器，返回 path 末段所在的父节点。"""
    node = data
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    return node


def _apply_features_namespace(data: dict[str, Any]) -> dict[str, Any]:
    """把 features.* 子树按映射表合并回旧配置位置（新节优先、旧节兜底）。

    直接在读入的 data 上原地合并并返回同一对象；features 节本身保留在
    结果里，raw_config 仍能观察完整原始配置。features 节缺失或不是映射
    时原样返回；单个子节类型不合法时跳过该节并告警，不影响其余配置。
    """
    features = data.get("features")
    if not isinstance(features, dict):
        return data

    for name, target_path in _FEATURES_SECTION_MAP.items():
        section = features.get(name)
        if section is None:
            continue
        if not isinstance(section, dict):
            log.warning("features.%s 必须是键值映射，已忽略该节", name)
            continue
        parent = _ensure_container(data, target_path)
        leaf_key = target_path[-1]
        leaf = parent.get(leaf_key)
        parent[leaf_key] = _deep_merge_dicts(leaf if isinstance(leaf, dict) else {}, section)

    for (name, key), target_path in _FEATURES_FLAT_MAP.items():
        section = features.get(name)
        if not isinstance(section, dict) or key not in section:
            continue
        parent = _ensure_container(data, target_path)
        parent[target_path[-1]] = section[key]

    return data


def load_config(config_path: str | Path | None = None) -> Config:
    """加载配置。顺序：默认值 < config.yaml < 环境变量(.env)。

    .env 查找顺序由 _load_env_files() 决定；后者覆盖前者。
    """
    _load_env_files()

    cfg = Config()

    # 1) config.yaml
    #    优先级：显式指定路径 > 用户配置目录（get_crew_home()/config.yaml）> 内置默认（ROOT/config/）
    if config_path:
        path = Path(config_path)
    else:
        user_dir = _init_user_config_dir()  # 首次运行自动从打包默认值复制
        user_yaml = user_dir / "config.yaml"
        bundled_yaml = _bundled_config_template_path()
        if user_yaml.is_file():
            path = user_yaml
        elif bundled_yaml.is_file():
            path = bundled_yaml
        else:
            path = user_yaml  # 不存在，后续 if path.exists() 会跳过
    if path.exists():
        # 记录加载路径，供运行时 CRUD 写回使用
        cfg.config_path = str(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            # features.* 命名空间双读（P2-1 渐进第一步）：先按映射表把
            # features 子树合并回旧节位置，再进入下方各节读取。
            data = _apply_features_namespace(data)
        cfg.raw_config = data if isinstance(data, dict) else {}
        llm = data.get("llm", {})
        cfg.active_model_id = str(llm.get("active", cfg.active_model_id) or cfg.active_model_id)
        cfg.default_model_id = str(llm.get("default", "") or "").strip()

        models = llm.get("models")
        if isinstance(models, dict) and models:
            for model_id, raw in models.items():
                if isinstance(raw, dict):
                    cfg.model_profiles[str(model_id)] = _build_model_profile(str(model_id), raw)
        else:
            cfg.api_key_env = llm.get("api_key_env", cfg.api_key_env)
            cfg.provider = str(llm.get("provider", cfg.provider) or cfg.provider).strip().lower()
            cfg.base_url = llm.get("base_url", cfg.base_url)
            cfg.model = llm.get("model", cfg.model)
            cfg.temperature = llm.get("temperature", cfg.temperature)
            cfg.max_tokens = _as_int_or_none(llm.get("max_tokens", cfg.max_tokens))
            cfg.context_window = _as_int_or_none(llm.get("context_window", cfg.context_window))
            cfg.timeout = _as_float(llm.get("timeout", cfg.timeout), cfg.timeout)
        runtime = data.get("runtime", {})
        cfg.db_path = runtime.get("db_path", cfg.db_path)
        cfg.memory_enabled = bool(runtime.get("memory_enabled", cfg.memory_enabled))
        cfg.cron_db_path = runtime.get("cron_db_path", cfg.cron_db_path)
        cfg.work_db_path = runtime.get("work_db_path", cfg.work_db_path)
        cfg.kanban_db_path = runtime.get("kanban_db_path", cfg.kanban_db_path)
        cfg.external_db_path = runtime.get("external_db_path", cfg.external_db_path)
        cfg.team_db_path = runtime.get("team_db_path", cfg.team_db_path)
        cfg.sites_db_path = runtime.get("sites_db_path", cfg.sites_db_path)
        cfg.tasks_db_path = runtime.get("tasks_db_path", cfg.tasks_db_path)
        cfg.notifications_db_path = runtime.get(
            "notifications_db_path", cfg.notifications_db_path
        )
        cfg.channels_db_path = runtime.get("channels_db_path", cfg.channels_db_path)
        cfg.wiki_learning_db_path = runtime.get(
            "wiki_learning_db_path", cfg.wiki_learning_db_path
        )
        cfg.log_level = runtime.get("log_level", cfg.log_level)
        cfg.log_file = runtime.get("log_file", cfg.log_file)
        cfg.crew_home = runtime.get("crew_home", cfg.crew_home)
        cfg.task_workspace_root = runtime.get("task_workspace_root", cfg.task_workspace_root)
        cfg.llm_trace = bool(runtime.get("llm_trace", cfg.llm_trace))
        cfg.max_iterations = runtime.get("max_iterations", cfg.max_iterations)
        cfg.dk_task_timeout_seconds = _as_float(
            runtime.get("dk_task_timeout_seconds", cfg.dk_task_timeout_seconds),
            cfg.dk_task_timeout_seconds,
        )
        cfg.dk_verification_gate_enabled = bool(
            runtime.get("dk_verification_gate_enabled", cfg.dk_verification_gate_enabled)
        )
        raw_timeout_policy = runtime.get("timeout_policy", cfg.timeout_policy)
        cfg.timeout_policy = raw_timeout_policy if isinstance(raw_timeout_policy, dict) else {}
        cfg.sqlite_wal = bool(runtime.get("sqlite_wal", cfg.sqlite_wal))
        cfg.tool_execution_timeout_seconds = _as_float(
            runtime.get("tool_execution_timeout_seconds", cfg.tool_execution_timeout_seconds),
            cfg.tool_execution_timeout_seconds,
        )
        cfg.turn_deadline_seconds = max(
            0.0,
            _as_float(
                runtime.get("turn_deadline_seconds", cfg.turn_deadline_seconds),
                cfg.turn_deadline_seconds,
            ),
        )
        cfg.interaction_timeout_seconds = max(
            0.0,
            _as_float(
                runtime.get("interaction_timeout_seconds", cfg.interaction_timeout_seconds),
                cfg.interaction_timeout_seconds,
            ),
        )
        health_port = _as_int_or_none(runtime.get("health_port", cfg.health_port))
        if health_port is not None:
            cfg.health_port = max(0, health_port)
        gw = data.get("gateway", {})
        cfg.gateway_host = gw.get("host", cfg.gateway_host)
        cfg.gateway_port = gw.get("port", cfg.gateway_port)
        # 环境变量 GATEWAY_PORT 优先级最高（供 Electron 托管子进程指定端口）
        _env_port = os.getenv("GATEWAY_PORT")
        if _env_port and _env_port.strip().isdigit():
            cfg.gateway_port = int(_env_port.strip())
        cfg.gateway_busy_mode = str(gw.get("busy_mode", cfg.gateway_busy_mode) or cfg.gateway_busy_mode)
        cfg.gateway_push_min_interval = _as_float(gw.get("push_min_interval", cfg.gateway_push_min_interval), cfg.gateway_push_min_interval)
        raw_admins = gw.get("admin_accounts", cfg.gateway_admin_accounts)
        if isinstance(raw_admins, list):
            cfg.gateway_admin_accounts = [str(item).strip() for item in raw_admins if str(item).strip()]
        cfg.gateway_dev_mode = bool(gw.get("dev_mode", False)) or os.getenv("CREW_GATEWAY_DEV", "").strip() in {"1", "true", "yes"}
        cfg.gateway_dev_account = str(gw.get("dev_account", cfg.gateway_dev_account) or "").strip() or cfg.gateway_dev_account
        cfg.gateway_max_active_runs = max(1, int(gw.get("max_active_runs", cfg.gateway_max_active_runs)))
        cfg.gateway_max_queue_depth_per_session = max(
            0,
            int(gw.get("max_queue_depth_per_session", cfg.gateway_max_queue_depth_per_session)),
        )
        security = data.get("security", {})
        if isinstance(security, dict):
            cfg.security_enabled = _as_bool(
                security.get("enabled"),
                cfg.security_enabled,
            )
        auth = data.get("auth", {})
        if isinstance(auth, dict):
            mode = str(auth.get("mode", cfg.auth_mode) or cfg.auth_mode).strip().lower()
            cfg.auth_mode = mode if mode in {"local", "email", "remote"} else "local"
            remote = auth.get("remote", {})
            if isinstance(remote, dict):
                cfg.auth_provider_id = (
                    str(remote.get("provider_id", cfg.auth_provider_id) or "").strip()
                    or cfg.auth_provider_id
                )
                cfg.auth_base_url = str(remote.get("base_url", cfg.auth_base_url) or "").strip()
                cfg.auth_send_code_path = str(
                    remote.get("send_code_path", cfg.auth_send_code_path)
                    or cfg.auth_send_code_path
                ).strip()
                cfg.auth_login_path = str(
                    remote.get("login_path", cfg.auth_login_path) or cfg.auth_login_path
                ).strip()
                cfg.auth_timeout_seconds = max(
                    1.0,
                    min(60.0, _as_float(
                        remote.get("timeout_seconds", cfg.auth_timeout_seconds),
                        cfg.auth_timeout_seconds,
                    )),
                )
                cfg.auth_session_ttl_seconds = max(
                    300,
                    min(
                        30 * 24 * 60 * 60,
                        int(remote.get("session_ttl_seconds", cfg.auth_session_ttl_seconds)),
                    ),
                )
        # 环境变量只覆盖认证服务地址，不改变 local/remote 模式。
        auth_base_url_env = os.getenv("CREW_AUTH_BASE_URL", "").strip()
        if auth_base_url_env:
            cfg.auth_base_url = auth_base_url_env
        channels = data.get("channels", {})
        cfg.channels = channels if isinstance(channels, dict) else {}
        platforms = data.get("platforms", {})
        cfg.platforms = platforms if isinstance(platforms, dict) else {}
        cfg.team_config = data.get("team", {})
        external_agents = data.get("external_agents", {})
        if isinstance(external_agents, dict):
            cfg.external_agents_enabled = _as_bool(
                external_agents.get("enabled"),
                cfg.external_agents_enabled,
            )
            cfg.external_security_enabled = _as_bool(
                external_agents.get("security_enabled"),
                cfg.external_security_enabled,
            )

        tasks = data.get("tasks", {})
        if isinstance(tasks, dict):
            cfg.tasks_auto_background_after_seconds = _as_float(
                tasks.get("auto_background_after_seconds", cfg.tasks_auto_background_after_seconds),
                cfg.tasks_auto_background_after_seconds,
            )
            cfg.tasks_heartbeat_interval_seconds = _as_float(
                tasks.get("heartbeat_interval_seconds", cfg.tasks_heartbeat_interval_seconds),
                cfg.tasks_heartbeat_interval_seconds,
            )
            cfg.tasks_monitor_interval_seconds = _as_float(
                tasks.get("monitor_interval_seconds", cfg.tasks_monitor_interval_seconds),
                cfg.tasks_monitor_interval_seconds,
            )
            cfg.tasks_wait_timeout_seconds = _as_float(
                tasks.get("wait_timeout_seconds", cfg.tasks_wait_timeout_seconds),
                cfg.tasks_wait_timeout_seconds,
            )
            cfg.tasks_finished_retention_days = int(
                tasks.get("finished_retention_days", cfg.tasks_finished_retention_days)
            )
            for prefix, attr_prefix in (
                ("shell", "tasks_shell"),
                ("subagent", "tasks_subagent"),
                ("agent_turn", "tasks_agent_turn"),
            ):
                section = tasks.get(prefix, {}) or {}
                if not isinstance(section, dict):
                    continue
                setattr(
                    cfg,
                    f"{attr_prefix}_inactivity_timeout_seconds",
                    _as_float(
                        section.get(
                            "inactivity_timeout_seconds",
                            getattr(cfg, f"{attr_prefix}_inactivity_timeout_seconds"),
                        ),
                        getattr(cfg, f"{attr_prefix}_inactivity_timeout_seconds"),
                    ),
                )
                setattr(
                    cfg,
                    f"{attr_prefix}_execution_timeout_seconds",
                    _as_float(
                        section.get(
                            "execution_timeout_seconds",
                            getattr(cfg, f"{attr_prefix}_execution_timeout_seconds"),
                        ),
                        getattr(cfg, f"{attr_prefix}_execution_timeout_seconds"),
                    ),
                )

        plugins = data.get("plugins", {})
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled")
            if isinstance(enabled, list):
                cfg.plugins_enabled = [str(item) for item in enabled]
            disabled = plugins.get("disabled")
            if isinstance(disabled, list):
                cfg.plugins_disabled = [str(item) for item in disabled]

        tools = data.get("tools", {})
        if isinstance(tools, dict):
            browser_raw = tools.get("browser") or {}
            if not isinstance(browser_raw, dict):
                browser_raw = {}
            cfg.browser_config = dict(browser_raw)
            web_search_raw = tools.get("web_search") or {}
            if not isinstance(web_search_raw, dict):
                web_search_raw = {}
            cfg.web_search_config = dict(web_search_raw)
            # 把 tools.web_search 节注入搜索 seam（provider 有序降级链与 key 环境变量名）。
            from crew.tools.web_search_service import configure_search

            configure_search(cfg.web_search_config)

        session_cfg = data.get("session", {})
        if isinstance(session_cfg, dict) and session_cfg:
            cfg.session_idle_timeout = int(session_cfg.get("idle_timeout_minutes", cfg.session_idle_timeout))

        wiki_raw = data.get("wiki") or {}
        if not isinstance(wiki_raw, dict):
            wiki_raw = {}
        cfg.wiki_config = dict(wiki_raw)
        cfg.wiki_enabled = _as_flag(wiki_raw.get("enabled"), cfg.wiki_enabled)
        cfg.network = NetworkConfig.from_raw(data.get("network", {}))
        # 把 config 的 network 段注入 outbound 作为进程级默认代理，
        # 供 web_search/web_extract/Wiki 等未显式传参的调用方使用。
        from crew.security.outbound import set_network_defaults

        set_network_defaults(
            upstream_proxy=cfg.network.upstream_proxy,
            allow_loopback_proxy=cfg.network.allow_loopback_proxy,
        )

        cfg.mcp_servers = data.get("mcp_servers", {}) or {}
        cron = data.get("cron", {})
        if isinstance(cron, dict) and cron:
            cfg.cron_enabled = bool(cron.get("enabled", cfg.cron_enabled))
            global _LEGACY_CRON_TICK_WARNING_EMITTED
            if "tick_seconds" in cron and not _LEGACY_CRON_TICK_WARNING_EMITTED:
                log.warning(
                    "已忽略废弃配置 cron.tick_seconds；APScheduler 是唯一生产调度器"
                )
                _LEGACY_CRON_TICK_WARNING_EMITTED = True
            cfg.cron_max_parallel_jobs = max(1, int(cron.get("max_parallel_jobs", cfg.cron_max_parallel_jobs)))

        agent = data.get("agent", {})
        if isinstance(agent, dict) and agent:
            cfg.agent_executor = str(agent.get("executor", cfg.agent_executor) or cfg.agent_executor)
            comp = agent.get("compaction", {}) or {}
            cfg.compaction_enabled = bool(comp.get("enabled", cfg.compaction_enabled))
            cfg.compaction_token_budget = int(comp.get("token_budget", cfg.compaction_token_budget))
            cfg.compaction_token_budget_ratio = _as_float(
                comp.get("token_budget_ratio", cfg.compaction_token_budget_ratio),
                cfg.compaction_token_budget_ratio,
            )
            cfg.compaction_keep_recent = int(comp.get("keep_recent", cfg.compaction_keep_recent))
            cfg.compaction_keep_recent_tools = int(comp.get("keep_recent_tools", cfg.compaction_keep_recent_tools))
            cfg.compaction_l2_incremental = bool(comp.get("l2_incremental", cfg.compaction_l2_incremental))
            cfg.compaction_l2_delta_threshold = int(comp.get("l2_delta_threshold", cfg.compaction_l2_delta_threshold))
            cfg.compaction_post_compact_files = int(comp.get("post_compact_files", cfg.compaction_post_compact_files))
            cfg.compaction_post_compact_max_chars_per_file = int(
                comp.get("post_compact_max_chars_per_file", cfg.compaction_post_compact_max_chars_per_file)
            )
            cfg.compaction_post_compact_max_instructions = int(
                comp.get("post_compact_max_instructions", cfg.compaction_post_compact_max_instructions)
            )
            cfg.compaction_post_compact_max_important = int(
                comp.get("post_compact_max_important", cfg.compaction_post_compact_max_important)
            )
            cfg.compaction_post_compact_max_instruction_chars = int(
                comp.get("post_compact_max_instruction_chars", cfg.compaction_post_compact_max_instruction_chars)
            )
            cfg.compaction_post_compact_max_important_chars = int(
                comp.get("post_compact_max_important_chars", cfg.compaction_post_compact_max_important_chars)
            )
            cfg.compaction_post_compact_max_total_chars = int(
                comp.get("post_compact_max_total_chars", cfg.compaction_post_compact_max_total_chars)
            )
            cfg.compaction_max_tool_result_chars = int(
                comp.get("max_tool_result_chars", cfg.compaction_max_tool_result_chars)
            )
            cfg.compaction_max_overflow_retries = max(
                0, int(comp.get("max_overflow_retries", cfg.compaction_max_overflow_retries))
            )
            cfg.compaction_summary_max_tokens = max(
                0, int(comp.get("summary_max_tokens", cfg.compaction_summary_max_tokens))
            )
            cfg.compaction_summary_prefix_reuse = bool(
                comp.get("summary_prefix_reuse", cfg.compaction_summary_prefix_reuse)
            )
            retry = agent.get("retry", {}) or {}
            cfg.retry_max = int(retry.get("max_retries", cfg.retry_max))
            cfg.retry_backoff = _as_float(retry.get("backoff_seconds", cfg.retry_backoff), cfg.retry_backoff)
            title = agent.get("title", {}) or {}
            cfg.title_auto = bool(title.get("auto", cfg.title_auto))
            evolution = agent.get("evolution", {}) or {}
            cfg.evolution_auto_trigger = bool(evolution.get("auto_trigger", cfg.evolution_auto_trigger))
            cfg.evolution_auto_full_cycle = bool(evolution.get("auto_full_cycle", cfg.evolution_auto_full_cycle))
            cfg.evolution_visible = bool(evolution.get("visible", cfg.evolution_visible))
            cfg.agent_client_config = agent.get("client", {}) or {}
            cfg.agent_acp_config = agent.get("acp", {}) or {}
            cfg.parallel_tools = bool(agent.get("parallel_tools", cfg.parallel_tools))
            cfg.max_parallel_tool_calls = max(
                1,
                int(agent.get("max_parallel_tool_calls", cfg.max_parallel_tool_calls)),
            )
            cfg.empty_retry_max = int(agent.get("empty_retry_max", cfg.empty_retry_max))
            cfg.continuation_max = int(agent.get("continuation_max", cfg.continuation_max))
            stream_resilience = agent.get("stream_resilience", {}) or {}
            cfg.stream_read_timeout = _as_float(
                stream_resilience.get("read_timeout", cfg.stream_read_timeout),
                cfg.stream_read_timeout,
            )
            cfg.stream_retry_jitter = bool(stream_resilience.get("retry_jitter", cfg.stream_retry_jitter))
            cfg.stream_stale_timeout = _as_float(
                stream_resilience.get("stale_timeout", cfg.stream_stale_timeout),
                cfg.stream_stale_timeout,
            )
            cfg.stream_continuation_max = int(stream_resilience.get("continuation_max", cfg.stream_continuation_max))
            fb = agent.get("fallback_models", cfg.fallback_models)
            cfg.fallback_models = list(fb) if isinstance(fb, (list, tuple)) else cfg.fallback_models
            guard = agent.get("guardrail", {}) or {}
            cfg.guardrail_enabled = bool(guard.get("enabled", cfg.guardrail_enabled))
            cfg.guardrail_hard_stop = bool(guard.get("hard_stop", cfg.guardrail_hard_stop))
            cfg.guardrail_exact_failure_block_after = int(
                guard.get("exact_failure_block_after", cfg.guardrail_exact_failure_block_after))
            cfg.guardrail_same_tool_failure_halt_after = int(
                guard.get("same_tool_failure_halt_after", cfg.guardrail_same_tool_failure_halt_after))
            cfg.guardrail_no_progress_block_after = int(
                guard.get("no_progress_block_after", cfg.guardrail_no_progress_block_after))

        ac = data.get("access_control", {})
        if isinstance(ac, dict):
            cfg.access_control = AccessControlConfig(
                user_type=str(ac.get("user_type", cfg.access_control.user_type) or cfg.access_control.user_type),
                external=ac.get("external", {}) or {},
                internal=ac.get("internal", {}) or {},
            )

    from crew.security.settings import configure_security

    configure_security(enabled=cfg.security_enabled)

    # config.yaml 里的 runtime.crew_home 需要等 yaml 解析后才知道；
    # 加载 {crew_home}/.env 后刷新已构建的模型 profile key。
    if cfg.crew_home and not os.getenv("CREW_HOME"):
        _load_crew_home_env_file(cfg.crew_home)
        _refresh_model_profile_keys(cfg)

    # 2) 环境变量覆盖（敏感信息只从 env 取）
    if not cfg.model_profiles:
        cfg.model_profiles["default"] = _build_model_profile(
            "default",
            {
                "name": "default",
                "api_key_env": cfg.api_key_env,
                "provider": cfg.provider,
                "base_url": cfg.base_url,
                "model": cfg.model,
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
                "context_window": cfg.context_window,
                "timeout": cfg.timeout,
                "vision": cfg.vision,
            },
        )

    if os.getenv("CREW_MODEL_PROFILE"):
        cfg.active_model_id = os.environ["CREW_MODEL_PROFILE"]
    if cfg.active_model_id not in cfg.model_profiles:
        cfg.active_model_id = sorted(cfg.model_profiles)[0]
    cfg.active_model_id = _resolve_active_model_id(cfg)

    profile = cfg.activate_model(cfg.active_model_id)

    # 旧式单模型配置保留 CREW_* 全局覆盖；多模型 profile 只读取自己的 api_key_env，
    # 避免 .env 里的 CREW_MODEL/CREW_BASE_URL 误覆盖已选择的命名模型。
    if profile.id == "default" and profile.api_key_env == "CREW_API_KEY":
        if os.getenv("CREW_API_KEY"):
            profile.api_key = os.environ["CREW_API_KEY"]
        if os.getenv("CREW_BASE_URL"):
            profile.base_url = os.environ["CREW_BASE_URL"]
        if os.getenv("CREW_MODEL"):
            profile.model = os.environ["CREW_MODEL"]
        if os.getenv("CREW_TEMPERATURE"):
            profile.temperature = _as_float(os.environ["CREW_TEMPERATURE"], profile.temperature)
        if os.getenv("CREW_MAX_TOKENS"):
            profile.max_tokens = _as_int_or_none(os.environ["CREW_MAX_TOKENS"])
        if os.getenv("CREW_CONTEXT_WINDOW"):
            profile.context_window = _as_int_or_none(os.environ["CREW_CONTEXT_WINDOW"])
        if os.getenv("CREW_TIMEOUT"):
            profile.timeout = _as_float(os.environ["CREW_TIMEOUT"], profile.timeout)
    elif os.getenv("CREW_API_KEY") and not profile.api_key:
        # 命名模型缺少专属 key 时，允许回退到全局 key。
        profile.api_key = os.environ["CREW_API_KEY"]
    cfg.activate_model(profile.id)
    if os.getenv("CREW_LOG_LEVEL"):
        cfg.log_level = os.environ["CREW_LOG_LEVEL"]
    if os.getenv("CREW_STREAM_READ_TIMEOUT"):
        cfg.stream_read_timeout = _as_float(
            os.environ["CREW_STREAM_READ_TIMEOUT"], cfg.stream_read_timeout
        )
    if os.getenv("CREW_LOG_FILE"):
        cfg.log_file = os.environ["CREW_LOG_FILE"]
    if os.getenv("CREW_HOME"):
        cfg.crew_home = os.environ["CREW_HOME"]
    if os.getenv("CREW_TASK_WORKSPACE_ROOT"):
        cfg.task_workspace_root = os.environ["CREW_TASK_WORKSPACE_ROOT"]
    # crew_home 桥接：config.yaml 的 runtime.crew_home 或 CREW_HOME 环境变量（env 优先）。
    # get_crew_home() 全局读 CREW_HOME 环境变量，故把最终值写回环境变量，使配置在所有
    # 调用 get_crew_home() 的地方（记忆/技能/计划/日志…）生效，而不只是环境变量能用。
    if cfg.crew_home:
        crew_home_path = Path(cfg.crew_home).expanduser()
        # 相对路径解析为相对于用户家目录（~），而非进程 CWD
        # 例：crew_home: "Crew" → ~/Crew
        if not crew_home_path.is_absolute():
            crew_home_path = Path.home() / crew_home_path
        cfg.crew_home = str(crew_home_path)
        os.environ["CREW_HOME"] = cfg.crew_home
        # 暴露跑 Crew 的 python 解释器路径，供 mcp_servers 配置 command: "${CREW_PYTHON}" 跨机器引用
        # PyInstaller 冻结态下 sys.executable 是 gateway 二进制本身，直接用于启动 MCP server
        # 脚本会导致 gateway 把脚本路径当 argv 递归重跑入口，繁殖出大量进程。
        # 故冻结态改用打包内嵌的 Python 解释器；开发态仍用 sys.executable。
        if not os.environ.get("CREW_PYTHON"):
            try:
                from crew.state.home import bundled_python_executable
                _py_exe = bundled_python_executable() or sys.executable
            except Exception:
                _py_exe = sys.executable
            os.environ["CREW_PYTHON"] = _py_exe

    from crew.state.home import export_crew_runtime_env, get_crew_home
    home = get_crew_home()
    runtime_env = export_crew_runtime_env(resolve_writable_env_path())
    home = Path(runtime_env["CREW_HOME"])
    cfg.crew_home = str(home)
    if cfg.task_workspace_root:
        task_root = Path(cfg.task_workspace_root).expanduser()
    else:
        task_root = home / "task_workspaces"
    if cfg.task_workspace_root and not task_root.is_absolute():
        task_root = home / task_root
    cfg.task_workspace_root = str(task_root)
    os.environ["CREW_TASK_WORKSPACE_ROOT"] = cfg.task_workspace_root
    # 相对路径的 db_path / log_file 统一落到 crew_home 下（跟随 crew_home，而非启动 cwd）。
    db_path = Path(cfg.db_path).expanduser()
    if not db_path.is_absolute():
        db_path = home / db_path
    cfg.db_path = str(db_path)
    if cfg.log_file:
        log_path = Path(cfg.log_file).expanduser()
        if not log_path.is_absolute():
            log_path = home / log_path
        cfg.log_file = str(log_path)
    if cfg.memory_db_path:
        mem_path = Path(cfg.memory_db_path).expanduser()
        if not mem_path.is_absolute():
            mem_path = home / mem_path
        cfg.memory_db_path = str(mem_path)
    if cfg.cron_db_path:
        cron_path = Path(cfg.cron_db_path).expanduser()
        if not cron_path.is_absolute():
            cron_path = home / cron_path
        cfg.cron_db_path = str(cron_path)
    if cfg.work_db_path:
        work_path = Path(cfg.work_db_path).expanduser()
        if not work_path.is_absolute():
            work_path = home / work_path
        cfg.work_db_path = str(work_path)
    if cfg.kanban_db_path:
        kanban_path = Path(cfg.kanban_db_path).expanduser()
        if not kanban_path.is_absolute():
            kanban_path = home / kanban_path
        cfg.kanban_db_path = str(kanban_path)
    if cfg.external_db_path:
        external_path = Path(cfg.external_db_path).expanduser()
        if not external_path.is_absolute():
            external_path = home / external_path
        cfg.external_db_path = str(external_path)
    if cfg.team_db_path:
        team_path = Path(cfg.team_db_path).expanduser()
        if not team_path.is_absolute():
            team_path = home / team_path
        cfg.team_db_path = str(team_path)
    for attr in (
        "sites_db_path",
        "tasks_db_path",
        "notifications_db_path",
        "channels_db_path",
        "wiki_learning_db_path",
    ):
        raw_value = getattr(cfg, attr)
        if not raw_value:
            continue
        raw = Path(raw_value).expanduser()
        if not raw.is_absolute():
            raw = home / raw
        setattr(cfg, attr, str(raw))

    return cfg
