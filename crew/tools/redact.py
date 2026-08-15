"""基于正则的密钥脱敏，用于日志和工具输出（复用自 Hermes agent/redact.py）。

在命令输出/日志进入模型上下文或落盘前，匹配并打码 API key、token、凭据等。

短 token（< 18 字符）整体打码；长 token 保留前 6 / 后 4 字符便于调试。

默认开启（安全默认）。需要看原始密钥值时可设 `CREW_REDACT_SECRETS=false`。
"""

import logging
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# 敏感 query 参数名（大小写不敏感精确匹配）。
_SENSITIVE_QUERY_PARAMS = frozenset({
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "ticket",
    "api_key",
    "apikey",
    "client_secret",
    "password",
    "auth",
    "jwt",
    "session",
    "secret",
    "key",
    "code",           # OAuth authorization codes
    "signature",      # pre-signed URL signatures
    "x-amz-signature",
})

# 敏感 form/JSON body 字段名（大小写不敏感精确匹配，非子串）。
_SENSITIVE_BODY_KEYS = frozenset({
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "ticket",
    "api_key",
    "apikey",
    "client_secret",
    "password",
    "auth",
    "jwt",
    "secret",
    "private_key",
    "authorization",
    "key",
})

# import 时快照，运行期 env 变更（如模型 export CREW_REDACT_SECRETS=false）
# 无法中途关闭脱敏。默认开启。
_REDACT_ENABLED = os.getenv("CREW_REDACT_SECRETS", "true").lower() in {"1", "true", "yes", "on"}

# 已知 API key 前缀 —— 匹配前缀 + 连续 token 字符
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth access token
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server token
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server token
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack tokens
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",           # Perplexity
    r"fal_[A-Za-z0-9_-]{10,}",          # Fal.ai
    r"fc-[A-Za-z0-9]{10,}",             # Firecrawl
    r"bb_live_[A-Za-z0-9_-]{10,}",      # BrowserBase
    r"gAAAA[A-Za-z0-9_=-]{20,}",        # Codex encrypted tokens
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe secret key (live)
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe secret key (test)
    r"rk_live_[A-Za-z0-9]{10,}",        # Stripe restricted key
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid API key
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace token
    r"r8_[A-Za-z0-9]{10,}",             # Replicate API token
    r"npm_[A-Za-z0-9]{10,}",            # npm access token
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI API token
    r"dop_v1_[A-Za-z0-9]{10,}",         # DigitalOcean PAT
    r"doo_v1_[A-Za-z0-9]{10,}",         # DigitalOcean OAuth
    r"am_[A-Za-z0-9_-]{10,}",           # AgentMail API key
    r"sk_[A-Za-z0-9_]{10,}",            # ElevenLabs TTS key (sk_ underscore, not sk- dash)
    r"tvly-[A-Za-z0-9]{10,}",           # Tavily search API key
    r"exa_[A-Za-z0-9]{10,}",            # Exa search API key
    r"gsk_[A-Za-z0-9]{10,}",            # Groq Cloud API key
    r"syt_[A-Za-z0-9]{10,}",            # Matrix access token
    r"retaindb_[A-Za-z0-9]{10,}",       # RetainDB API key
    r"hsk-[A-Za-z0-9]{10,}",            # Hindsight API key
    r"mem0_[A-Za-z0-9]{10,}",           # Mem0 Platform API key
    r"brv_[A-Za-z0-9]{10,}",            # ByteRover API key
    r"xai-[A-Za-z0-9]{30,}",            # xAI (Grok) API key
]

# ENV 赋值：KEY=value，KEY 含密钥类名字
_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_ENV_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+)\2",
)

# JSON 字段："apiKey": "value", "token": "value" 等
_JSON_KEY_NAMES = r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|bearer|secret_value|raw_secret|secret_input|key_material)"
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

# Authorization headers
_AUTH_HEADER_RE = re.compile(
    r"(Authorization:\s*Bearer\s+)(\S+)",
    re.IGNORECASE,
)

# Telegram bot tokens: bot<digits>:<token> 或 <digits>:<token>
_TELEGRAM_RE = re.compile(
    r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})",
)

# 私钥块：-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)

# 数据库连接串：protocol://user:PASSWORD@host
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)",
    re.IGNORECASE,
)

# JWT：header.payload[.signature]，恒以 "eyJ" 开头（base64 的 "{"）
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}"           # Header (always starts with eyJ)
    r"(?:\.[A-Za-z0-9_=-]{4,}){0,2}"   # Optional payload and/or signature
)

# E.164 电话号：+<country><number>，7-15 位
_SIGNAL_PHONE_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")

# 带 query 的 URL：scheme://...?...
_URL_WITH_QUERY_RE = re.compile(
    r"(https?|wss?|ftp)://"          # scheme
    r"([^\s/?#\"'<>]+)"              # authority (may include userinfo)
    r"([^\s?#\"'<>]*)"               # path
    r"\?([^\s#\"'<>]+)"              # query (required)
    r"(#[^\s\"'<>]*)?",              # optional fragment
)

# 带 userinfo 的 URL：scheme://user[:password]@host。
_URL_USERINFO_RE = re.compile(
    r"(https?|wss?|ftp)://[^/\s@]+@",
)

# HTTP 访问日志的相对请求目标："POST /webhook?password=... HTTP/1.1"
_HTTP_REQUEST_TARGET_QUERY_RE = re.compile(
    r"\b((?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)\s+[^ \t\r\n\"']*?)"
    r"\?([^ \t\r\n\"']+)",
    re.IGNORECASE,
)

# form-urlencoded body：仅当整段文本就是 k=v&k=v（无换行）才触发
_FORM_BODY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*(?:&[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*)+$"
)
_SENSITIVE_CLI_OPTIONS = frozenset(
    {
        "api-key",
        "apikey",
        "auth",
        "authorization",
        "client-secret",
        "credential",
        "password",
        "passwd",
        "proxy-password",
        "secret",
        "token",
    }
)
_SENSITIVE_CLI_TEXT_RE = re.compile(
    r"(?i)(?:--?(?:api[-_]?key|auth(?:orization)?|client[-_]?secret|credential|"
    r"password|passwd|proxy[-_]?password|secret|token))(?:\s+|=)"
    r"(?:\"[^\"]+\"|'[^']+'|[^\s]+)"
)
_SENSITIVE_HEADER_TEXT_RE = re.compile(
    r"(?i)(?:authorization|cookie|proxy-authorization|x-api-key|x-auth-token)"
    r"\s*:\s*\S+"
)

# 已知前缀合并成一个 alternation。
# ponytail: 不用 lookbehind/lookaround -- 贪婪量词 {10,} 已保证匹配到 token 边界，
# 而 lookbehind (?<![A-Za-z0-9_-]) 会在 ANSI 转义包裹密钥时漏报（[31m 的 m、
# 033[31m 的 1 都是 alphanumeric，阻断匹配）。安全脱敏宁可多打码也不漏报。
_PREFIX_RE = re.compile(
    r"(" + "|".join(_PREFIX_PATTERNS) + r")"
)


def mask_secret(
    value: str,
    *,
    head: int = 4,
    tail: int = 4,
    floor: int = 12,
    placeholder: str = "***",
    empty: str = "",
) -> str:
    """打码密钥用于展示，保留 head / tail 字符。

    长度小于 floor 的整体打码（返回 placeholder）；空值返回 empty。

    Examples:
        >>> mask_secret("sk-proj-abcdef1234567890")
        'sk-p...7890'
        >>> mask_secret("short")
        '***'
    """
    if not value:
        return empty
    if len(value) < floor:
        return placeholder
    return f"{value[:head]}...{value[-tail:]}"


def _mask_token(token: str) -> str:
    """日志 token 打码 —— 18 字符 floor，保留前 6 / 后 4。"""
    if not token:
        return "***"
    return mask_secret(token, head=6, tail=4, floor=18)


def _redact_query_string(query: str) -> str:
    """打码 URL query 中的敏感参数值（k=v&k=v，大小写不敏感）。"""
    if not query:
        return query
    parts = []
    for pair in query.split("&"):
        if "=" not in pair:
            parts.append(pair)
            continue
        key, _, value = pair.partition("=")
        if key.lower() in _SENSITIVE_QUERY_PARAMS:
            parts.append(f"{key}=***")
        else:
            parts.append(pair)
    return "&".join(parts)


def redact_url_for_display(value: str, *, strip_fragment: bool = True) -> str:
    """Return a URL that is safe to put in UI, logs, or model metadata.

    General text redaction intentionally does not rewrite arbitrary URL query
    parameters because doing so can break URLs that must remain usable.  At a
    display boundary we have the opposite requirement: credentials must never
    be shown, while ordinary parameters such as ``keywords`` must remain
    intact.  Query *names* are therefore matched against the exact canonical
    set above instead of a broad substring regex.

    Userinfo and fragments are omitted.  Non-sensitive values still pass
    through the canonical secret-shape redactor so a key such as ``sk-...`` is
    hidden even when it appears under an innocuous parameter name.
    """
    raw = str(value or "")
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        if port is not None:
            host = f"{host}:{port}"
        query = urlencode(
            [
                (
                    key,
                    "[REDACTED]"
                    if key.lower() in _SENSITIVE_QUERY_PARAMS
                    else redact_sensitive_text(item, force=True),
                )
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        safe = urlunsplit(
            (
                parsed.scheme,
                host,
                parsed.path,
                query,
                "" if strip_fragment else redact_sensitive_text(parsed.fragment, force=True),
            )
        )
        return redact_sensitive_text(safe, force=True)
    except (TypeError, ValueError, UnicodeError):
        return "[invalid-url]"


def redact_sensitive_display_text(value: str) -> str:
    """Redact secrets in untrusted text that is about to be displayed.

    Unlike :func:`redact_sensitive_text`, this explicit display-boundary helper
    also masks exact sensitive URL query parameters and URL userinfo.  It must
    not be used on a URL that still needs to be navigated to or requested.
    """
    text = redact_sensitive_text(value, force=True)
    if "://" in text:
        text = _redact_url_userinfo(text)
        text = _redact_url_query_params(text)
    if "?" in text and _has_http_method_substring(text):
        text = _redact_http_request_target_query_params(text)
    return text


_DISPLAY_HOST_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\|/(?:private|tmp|home|Users|var|workspace|AppData|Windows)/|"
    r"(?:^|[\s\"'(=])/(?:[^/\s]+/)+[^/\s]*)"
)


def safe_public_error(
    value: BaseException | object,
    fallback: str = "请求失败",
    *,
    limit: int = 500,
) -> str:
    """Bound a user-visible diagnostic without exposing secrets or host paths."""
    text = redact_sensitive_display_text(str(value or "")).strip()
    if not text or _DISPLAY_HOST_PATH_RE.search(text):
        return fallback
    return text[: max(1, limit)]


def argv_contains_sensitive_value(argv) -> bool:
    """Detect credential values that must never cross an argv boundary."""

    values = [str(item) for item in argv]
    redact_next = False
    for index, token in enumerate(values):
        if redact_next:
            if token and not token.startswith("-"):
                return True
            redact_next = False

        if _PREFIX_RE.search(token) or _JWT_RE.search(token) or _PRIVATE_KEY_RE.search(token):
            return True
        if _SENSITIVE_CLI_TEXT_RE.search(token) or _SENSITIVE_HEADER_TEXT_RE.search(token):
            return True

        option, separator, option_value = token.partition("=")
        normalized = option.lstrip("-").lower().replace("_", "-")
        if normalized in _SENSITIVE_CLI_OPTIONS:
            if separator and option_value:
                return True
            redact_next = True
        elif normalized in {"u", "user", "proxy-user"}:
            if separator and ":" in option_value:
                return True
            if index + 1 < len(values) and ":" in values[index + 1]:
                return True

        if "://" not in token:
            continue
        try:
            parsed = urlsplit(token)
            if parsed.username is not None or parsed.password is not None:
                return True
            for encoded_fields in (parsed.query, parsed.fragment):
                for name, value in parse_qsl(
                    encoded_fields,
                    keep_blank_values=True,
                ):
                    if name.lower() in _SENSITIVE_QUERY_PARAMS and value:
                        return True
        except (TypeError, ValueError, UnicodeError):
            continue
    return False


def _redact_url_query_params(text: str) -> str:
    """扫描带 query 的 URL 并打码敏感参数。"""
    def _sub(m: re.Match) -> str:
        scheme = m.group(1)
        authority = m.group(2)
        path = m.group(3)
        query = _redact_query_string(m.group(4))
        fragment = m.group(5) or ""
        if fragment:
            # OAuth implicit-flow responses commonly put credentials after
            # ``#`` instead of ``?``. Preserve ordinary anchors while applying
            # the same exact-key masking to query-shaped fragments.
            fragment = "#" + _redact_query_string(fragment[1:])
        return f"{scheme}://{authority}{path}?{query}{fragment}"
    return _URL_WITH_QUERY_RE.sub(_sub, text)


def _redact_url_userinfo(text: str) -> str:
    """去掉 HTTP/WS/FTP URL 里的 userinfo（DB 协议由 _DB_CONNSTR_RE 处理）。"""
    return _URL_USERINFO_RE.sub(
        lambda m: f"{m.group(1)}://",
        text,
    )


def _redact_http_request_target_query_params(text: str) -> str:
    """打码 HTTP 访问日志请求目标里的敏感 query 参数。"""
    def _sub(m: re.Match) -> str:
        prefix = m.group(1)
        query = _redact_query_string(m.group(2))
        return f"{prefix}?{query}"
    return _HTTP_REQUEST_TARGET_QUERY_RE.sub(_sub, text)


def _redact_form_body(text: str) -> str:
    """打码 form-urlencoded body 里的敏感值（仅纯 k=v&k=v 才触发）。"""
    if not text or "\n" in text or "&" not in text:
        return text
    if not _FORM_BODY_RE.match(text.strip()):
        return text
    return _redact_query_string(text.strip())


def redact_sensitive_text(text: str, *, force: bool = False, code_file: bool = False) -> str:
    """对一段文本应用全部脱敏规则。

    任意字符串均可安全调用，不匹配的原样返回。默认开启，可通过
    CREW_REDACT_SECRETS=false 关闭。force=True 用于必须永不返回原始密钥的安全边界。

    code_file=True 时跳过 ENV 赋值与 JSON 字段规则（源码里的 MAX_TOKENS=*** 常量、
    "apiKey": "test" 测试桩等易误判）；前缀、auth header、私钥、DB 连接串、JWT 仍打码。

    每条规则前都有廉价子串预检（如 "=" in text），干净文本几乎零开销。
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    if not (force or _REDACT_ENABLED):
        return text

    # 已知前缀（sk-, ghp_ 等）—— 子串预检
    if _has_known_prefix_substring(text):
        text = _PREFIX_RE.sub(lambda m: _mask_token(m.group(1)), text)

    # ENV 赋值：OPENAI_API_KEY=***（源码跳过，避免误判）
    if not code_file:
        if "=" in text:
            def _redact_env(m):
                name, quote, value = m.group(1), m.group(2), m.group(3)
                return f"{name}={quote}{_mask_token(value)}{quote}"
            text = _ENV_ASSIGN_RE.sub(_redact_env, text)

        # JSON 字段："apiKey": "***"（源码跳过，避免误判）
        if ":" in text and '"' in text:
            def _redact_json(m):
                key, value = m.group(1), m.group(2)
                return f'{key}: "{_mask_token(value)}"'
            text = _JSON_FIELD_RE.sub(_redact_json, text)

    # Authorization header（"uthorization" 是覆盖大小写的最廉价子串门）
    if "uthorization" in text or "UTHORIZATION" in text:
        text = _AUTH_HEADER_RE.sub(
            lambda m: m.group(1) + _mask_token(m.group(2)),
            text,
        )

    # Telegram bot token
    if ":" in text:
        def _redact_telegram(m):
            prefix = m.group(1) or ""
            digits = m.group(2)
            return f"{prefix}{digits}:***"
        text = _TELEGRAM_RE.sub(_redact_telegram, text)

    # 私钥块
    if "BEGIN" in text and "-----" in text:
        text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)

    # 数据库连接串密码
    if "://" in text:
        text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)

    # JWT（eyJ...）
    if "eyJ" in text:
        text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)

    # 注意：Web-URL 脱敏（query 参数 + userinfo + 访问日志请求目标）默认关闭。
    # 很多正常流程会把 opaque token 放在 query 里（magic-link、OAuth 回调、
    # 预签名 URL），按名字盲打码会破坏这些流程。URL 里的已知凭据形状
    # （sk-、ghp_、JWT 等）仍被上面的 _PREFIX_RE / _JWT_RE 捕获，DB 连接串
    # 密码仍被 _DB_CONNSTR_RE 捕获。

    # form-urlencoded body（仅纯 k=v&k=v 才触发）
    if "&" in text and "=" in text:
        text = _redact_form_body(text)

    # E.164 电话号（Signal / WhatsApp）
    if "+" in text:
        def _redact_phone(m):
            phone = m.group(1)
            if len(phone) <= 8:
                return phone[:2] + "****" + phone[-2:]
            return phone[:4] + "****" + phone[-4:]
        text = _SIGNAL_PHONE_RE.sub(_redact_phone, text)

    return text


# 用于门控 _PREFIX_RE 的子串。输入中若不含任一子串，前缀正则不可能匹配，
# 直接跳过。从 _PREFIX_PATTERNS 自动推导，保证无漏报（false negative）。

def _extract_literal_prefix(pattern: str) -> str:
    """返回正则前缀的开头字面量（遇第一个元字符停止）。"""
    meta = "[(\\.?*+|{^$"
    for i, ch in enumerate(pattern):
        if ch in meta:
            return pattern[:i]
    return pattern


_PREFIX_SUBSTRINGS = tuple(
    _extract_literal_prefix(p) for p in _PREFIX_PATTERNS
)


def _has_known_prefix_substring(text: str) -> bool:
    """text 是否含任一已知凭据前缀子串（_PREFIX_RE 前的廉价预检）。"""
    return any(p in text for p in _PREFIX_SUBSTRINGS)


_HTTP_METHOD_SUBSTRINGS = (
    "GET ",
    "POST ",
    "PUT ",
    "PATCH ",
    "DELETE ",
    "HEAD ",
    "OPTIONS ",
    "TRACE ",
    "CONNECT ",
)


def _has_http_method_substring(text: str) -> bool:
    """扫描访问日志请求目标前的廉价预检。"""
    upper = text.upper()
    return any(method in upper for method in _HTTP_METHOD_SUBSTRINGS)


# Env var keys whose VALUES are treated as secrets for precise-value redaction.
# Matches the key-name classes spec §7.3/§37 says to strip from child envs.
# Stems are matched as full ``_``/``-``-delimited components so that unrelated keys
# containing a substring (AUTHOR, AUTHORITY, KEYCLOAK, MANAGER) are NOT over-redacted.
_SENSITIVE_ENV_KEY_RE = re.compile(
    r"(?:^|[_-])(pass(word|wd)?|secret|token|ticket|api[_-]?key|auth|credential|private[_-]?key|"
    r"access[_-]?key|client[_-]?secret|refresh[_-]?token|bearer)(?:$|[_-])",
    re.IGNORECASE,
)


def sensitive_env_values(env: dict[str, str] | None) -> list[str]:
    """Return the VALUES of env entries whose KEY names a secret class.

    These are the precise secret values injected into a task's child process; they are
    redacted verbatim from process output regardless of shape (spec §7.3/§109).
    """
    if not env:
        return []
    out: list[str] = []
    for key, value in env.items():
        value = value if isinstance(value, str) else str(value)
        if value and len(value) >= 4 and _SENSITIVE_ENV_KEY_RE.search(str(key)):
            out.append(value)
    return out


def redact_secret_values(text: str | None, values) -> str | None:
    """Always-on exact-match redaction of explicit secret values.

    Unlike :func:`redact_sensitive_text`, this is NOT disable-able via
    ``CREW_REDACT_SECRETS`` — it is a hard security boundary for values the host
    knowingly injected into a task (spec §109 "项目环境不能关闭"). Any exact
    occurrence of a supplied value is replaced; non-matching text is untouched.
    """
    if not text or not values:
        return text
    if not isinstance(text, str):
        text = str(text)
    for value in values:
        if not value or len(value) < 4:
            continue
        text = text.replace(value, "***REDACTED***")
    return text
