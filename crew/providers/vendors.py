"""LLM 厂商档案注册表。

"OpenAI 兼容"与"Anthropic Messages"两种 wire 协议之下，各厂商还有一层
专属差异：思考参数格式、reasoning 字段名、assistant 消息回传要求等。
本模块把这些差异收口成显式数据（档案表），Provider 内核只按档案开关
分叉，不再在 wire 层散落对 base_url / model 名的字符串嗅探。

档案内容（模型清单、context_window、reasoning/vision 标记）取自
models.dev 公开目录的当日快照；未收录字段落留 None，宁缺勿猜。

扩展点：
- 新增厂商 = 在 VENDORS 里加一条 VendorProfile；
- 新增差异维度 = 先在 VendorCompat 加字段并给默认值（默认 = 通用行为），
  再让 openai_provider 消费该开关。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

# 统一思考等级（LLMProvider.reasoning_mode 的取值；"off"/"disabled" 表示关闭）
REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class VendorModel:
    """厂商官方目录里的一个模型。"""

    id: str
    reasoning: bool = False
    vision: bool = False
    context_window: int | None = None
    max_tokens: int | None = None
    # 统一思考等级 -> 厂商 effort 值；缺项 = 原样透传等级名，None = 该档不支持
    thinking_level_map: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class VendorCompat:
    """OpenAI 兼容协议下的厂商差异开关（默认 = 通用 OpenAI 行为）。

    thinking_format：
      none     不发思考参数，模型自行决定（如 Moonshot/Kimi 的 OpenAI 端点）
      deepseek thinking: {type: enabled|disabled}，可附 reasoning_effort
      zai      thinking: {type: enabled|disabled}，不支持 reasoning_effort
      qwen     enable_thinking: bool，可附 reasoning_effort
    """

    thinking_format: str = "none"
    supports_reasoning_effort: bool = True
    # DeepSeek 要求历史 assistant 消息携带 reasoning_content（缺失时补空串）
    requires_reasoning_echo: bool = False
    # 当前模型的思考等级映射（由 compat_for_model 从 VendorModel 填入）
    thinking_level_map: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class VendorProfile:
    """一个厂商的接入档案。"""

    id: str
    name: str
    protocol: str  # "openai" | "anthropic"
    base_url: str
    api_key_env: str
    models: tuple[VendorModel, ...]
    compat: VendorCompat = field(default_factory=VendorCompat)

    def model(self, model_id: str) -> VendorModel | None:
        for m in self.models:
            if m.id == model_id:
                return m
        return None


# ---------------------------------------------------------------------------
# 模型清单（models.dev 快照；同一份清单被同厂商多端点复用）
# ---------------------------------------------------------------------------

_DEEPSEEK_MODELS = (
    VendorModel("deepseek-v4-flash", reasoning=True, context_window=1_000_000, max_tokens=384_000),
    VendorModel("deepseek-v4-pro", reasoning=True, context_window=1_000_000, max_tokens=384_000),
    VendorModel("deepseek-v4-flash-vision-exp", reasoning=True, vision=True, context_window=1_000_000, max_tokens=384_000),
    # 官方稳定别名（不带上下文元数据，仅用于能力标记与厂商识别）
    VendorModel("deepseek-chat"),
    VendorModel("deepseek-reasoner", reasoning=True),
)

_ZAI_MODELS = (
    VendorModel("glm-4.6", reasoning=True, context_window=204_800, max_tokens=131_072),
    VendorModel("glm-4.5-flash", reasoning=True, context_window=131_072, max_tokens=98_304),
    VendorModel("glm-5v-turbo", reasoning=True, vision=True, context_window=200_000, max_tokens=131_072),
    VendorModel("glm-4.7", reasoning=True, context_window=204_800, max_tokens=131_072),
    VendorModel("glm-4.5v", reasoning=True, vision=True, context_window=64_000, max_tokens=16_384),
    VendorModel("glm-4.7-flashx", reasoning=True, context_window=200_000, max_tokens=131_072),
    VendorModel("glm-5", reasoning=True, context_window=204_800, max_tokens=131_072),
    VendorModel("glm-5-turbo", reasoning=True, context_window=200_000, max_tokens=131_072),
    VendorModel("glm-5.3", reasoning=True, context_window=1_000_000, max_tokens=131_072),
    VendorModel("glm-5.2", reasoning=True, context_window=1_000_000, max_tokens=131_072),
    VendorModel("glm-4.6v", reasoning=True, vision=True, context_window=128_000, max_tokens=32_768),
    VendorModel("glm-4.5-air", reasoning=True, context_window=131_072, max_tokens=98_304),
    VendorModel("glm-5.1", reasoning=True, context_window=200_000, max_tokens=131_072),
    VendorModel("glm-4.7-flash", reasoning=True, context_window=200_000, max_tokens=131_072),
    VendorModel("glm-4.5", reasoning=True, context_window=131_072, max_tokens=98_304),
    VendorModel("glm-5.3-flash", reasoning=True, vision=True, context_window=1_000_000, max_tokens=131_072),
)

_MOONSHOT_MODELS = (
    VendorModel("kimi-k2.7-code", reasoning=True, vision=True, context_window=262_144, max_tokens=262_144),
    VendorModel("kimi-k3", reasoning=True, vision=True, context_window=1_048_576, max_tokens=131_072),
    VendorModel("kimi-k2-0711-preview", context_window=131_072, max_tokens=16_384),
    VendorModel("kimi-k2-thinking-turbo", reasoning=True, context_window=262_144, max_tokens=262_144),
    VendorModel("kimi-k2.5", reasoning=True, vision=True, context_window=262_144, max_tokens=262_144),
    VendorModel("kimi-k2-0905-preview", context_window=262_144, max_tokens=262_144),
    VendorModel("kimi-k2-turbo-preview", context_window=262_144, max_tokens=262_144),
    VendorModel("kimi-k2.7-code-highspeed", reasoning=True, vision=True, context_window=262_144, max_tokens=262_144),
    VendorModel("kimi-k2-thinking", reasoning=True, context_window=262_144, max_tokens=262_144),
    VendorModel("kimi-k2.6", reasoning=True, vision=True, context_window=262_144, max_tokens=262_144),
)

_MINIMAX_MODELS = (
    VendorModel("MiniMax-M2.7", reasoning=True, context_window=204_800, max_tokens=131_072),
    VendorModel("MiniMax-M2.7-highspeed", reasoning=True, context_window=204_800, max_tokens=131_072),
    VendorModel("MiniMax-M2.1", reasoning=True, context_window=204_800, max_tokens=131_072),
    VendorModel("MiniMax-M2", reasoning=True, context_window=204_800, max_tokens=131_072),
    VendorModel("MiniMax-M2.5-highspeed", reasoning=True, context_window=204_800, max_tokens=131_072),
    VendorModel("MiniMax-M3", reasoning=True, context_window=1_048_576, max_tokens=512_000),
    VendorModel("MiniMax-M2.5", reasoning=True, context_window=204_800, max_tokens=131_072),
)

# Qwen 3.8 系列的思考等级映射：不支持的档位显式置 None（不发 reasoning_effort）
_QWEN38_LEVEL_MAP = {
    "minimal": None,
    "low": "low",
    "medium": "medium",
    "high": None,
    "xhigh": "xhigh",
    "max": None,
}

_QWEN_TOKEN_PLAN_MODELS = (
    VendorModel("deepseek-v4-flash-0731", reasoning=True, context_window=1_000_000, max_tokens=384_000),
    VendorModel("deepseek-v4-pro", reasoning=True, context_window=1_000_000, max_tokens=384_000),
    VendorModel("deepseek-v4-pro-0813", reasoning=True, context_window=1_000_000, max_tokens=384_000),
    VendorModel("glm-5.2", reasoning=True, context_window=1_000_000, max_tokens=131_072),
    VendorModel("qwen3.6-flash", reasoning=True),
    VendorModel("qwen3.7-max", reasoning=True),
    VendorModel("qwen3.7-plus", reasoning=True),
    VendorModel("qwen3.8-max", reasoning=True, thinking_level_map=dict(_QWEN38_LEVEL_MAP)),
)

# Kimi For Coding 订阅端点（Anthropic 协议）；kimi-for-coding* 与 k2.7-code 同族
_KIMI_CODING_MODELS = (
    VendorModel("kimi-k3", reasoning=True, vision=True, context_window=1_048_576, max_tokens=131_072),
    VendorModel("kimi-for-coding", reasoning=True, vision=True, context_window=262_144, max_tokens=262_144),
    VendorModel("kimi-for-coding-highspeed", reasoning=True, vision=True, context_window=262_144, max_tokens=262_144),
)

# ---------------------------------------------------------------------------
# 厂商档案（声明顺序 = 自动识别优先级）
# ---------------------------------------------------------------------------

VENDORS: dict[str, VendorProfile] = {
    "deepseek": VendorProfile(
        id="deepseek",
        name="DeepSeek",
        protocol="openai",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        models=_DEEPSEEK_MODELS,
        compat=VendorCompat(
            thinking_format="deepseek",
            requires_reasoning_echo=True,
        ),
    ),
    "zai": VendorProfile(
        id="zai",
        name="Z.AI",
        protocol="openai",
        base_url="https://api.z.ai/api/coding/paas/v4",
        api_key_env="ZAI_API_KEY",
        models=_ZAI_MODELS,
        compat=VendorCompat(thinking_format="zai", supports_reasoning_effort=False),
    ),
    "zai-coding-cn": VendorProfile(
        id="zai-coding-cn",
        name="Z.AI Coding CN",
        protocol="openai",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        api_key_env="ZAI_CODING_CN_API_KEY",
        models=_ZAI_MODELS,
        compat=VendorCompat(thinking_format="zai", supports_reasoning_effort=False),
    ),
    "moonshotai": VendorProfile(
        id="moonshotai",
        name="Moonshot AI",
        protocol="openai",
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        models=_MOONSHOT_MODELS,
    ),
    "moonshotai-cn": VendorProfile(
        id="moonshotai-cn",
        name="Moonshot AI CN",
        protocol="openai",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        models=_MOONSHOT_MODELS,
    ),
    "kimi-coding": VendorProfile(
        id="kimi-coding",
        name="Kimi For Coding",
        protocol="anthropic",
        base_url="https://api.kimi.com/coding",
        api_key_env="KIMI_API_KEY",
        models=_KIMI_CODING_MODELS,
    ),
    "minimax": VendorProfile(
        id="minimax",
        name="MiniMax",
        protocol="anthropic",
        base_url="https://api.minimax.io/anthropic",
        api_key_env="MINIMAX_API_KEY",
        models=_MINIMAX_MODELS,
    ),
    "minimax-cn": VendorProfile(
        id="minimax-cn",
        name="MiniMax CN",
        protocol="anthropic",
        base_url="https://api.minimaxi.com/anthropic",
        api_key_env="MINIMAX_CN_API_KEY",
        models=_MINIMAX_MODELS,
    ),
    "qwen-token-plan": VendorProfile(
        id="qwen-token-plan",
        name="Qwen Token Plan",
        protocol="openai",
        base_url="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        api_key_env="QWEN_TOKEN_PLAN_API_KEY",
        models=_QWEN_TOKEN_PLAN_MODELS,
        compat=VendorCompat(thinking_format="qwen"),
    ),
}


# ---------------------------------------------------------------------------
# 对外暴露（前端厂商目录）
# ---------------------------------------------------------------------------

def vendor_public_dict(vendor: VendorProfile) -> dict:
    """厂商档案的公开视图：仅前端选型所需字段，不含 compat 内部开关与密钥。"""
    return {
        "id": vendor.id,
        "name": vendor.name,
        "protocol": vendor.protocol,
        "base_url": vendor.base_url,
        "api_key_env": vendor.api_key_env,
        "models": [
            {
                "id": m.id,
                "context_window": m.context_window,
                "max_tokens": m.max_tokens,
                "reasoning": m.reasoning,
                "vision": m.vision,
            }
            for m in vendor.models
        ],
    }


# ---------------------------------------------------------------------------
# 解析函数
# ---------------------------------------------------------------------------

def get_vendor(provider_id: str) -> VendorProfile | None:
    """按配置里的 provider id 精确取厂商档案。"""
    return VENDORS.get(str(provider_id or "").strip().lower())


def _vendor_host_matches(vendor: VendorProfile, base_url: str) -> bool:
    """base_url 的 host 与厂商官方 host 一致即视为该厂商。"""
    if not base_url:
        return False
    try:
        return urlsplit(base_url).netloc.lower() == urlsplit(vendor.base_url).netloc.lower()
    except ValueError:
        return False


def match_vendor(
    base_url: str,
    model: str,
    *,
    protocol: str | None = None,
) -> VendorProfile | None:
    """按 base_url host 或模型 id 自动识别厂商（通用 openai/anthropic 配置的兜底）。

    base_url host 精确匹配官方域名优先（URL 即端点身份，不受 protocol 过滤，
    指到某厂商官方域名即按该厂商装配）；否则看模型 id 是否在某个厂商目录里
    （覆盖"反代官方模型"的场景，仅在 protocol 指定协议的厂商里匹配，避免把
    显式声明的通用协议劫持成另一种协议）。声明顺序即优先级。
    """
    for vendor in VENDORS.values():
        if _vendor_host_matches(vendor, base_url):
            return vendor
    model_id = str(model or "").strip()
    if model_id:
        for vendor in VENDORS.values():
            if protocol is not None and vendor.protocol != protocol:
                continue
            if vendor.model(model_id) is not None:
                return vendor
    return None


def resolve_vendor(provider_id: str, base_url: str = "", model: str = "") -> VendorProfile | None:
    """解析厂商档案：provider id 精确匹配优先；通用 openai/anthropic 配置按
    URL/模型名兜底识别；其余未知 id 返回 None（由装配层按配置错误抛出）。"""
    vendor = get_vendor(provider_id)
    if vendor is not None:
        return vendor
    pid = str(provider_id or "").strip().lower()
    if pid in ("openai", "anthropic", ""):
        return match_vendor(base_url, model, protocol=pid or None)
    return None


def compat_for_model(vendor: VendorProfile, model_id: str) -> VendorCompat:
    """按模型修正 compat：填入等级映射；不支持思考的模型收掉思考参数与回传要求。"""
    compat = vendor.compat
    vm = vendor.model(model_id)
    if vm is None:
        return compat
    updates: dict[str, object] = {"thinking_level_map": dict(vm.thinking_level_map)}
    if not vm.reasoning:
        updates["thinking_format"] = "none"
        updates["requires_reasoning_echo"] = False
    return replace(compat, **updates)  # type: ignore[arg-type]
