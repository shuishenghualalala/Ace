"""Configurable, provider-neutral login routes for local Crew clients."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.gateway.auth import REMOTE_AUTH_COOKIE, account_from_request, create_remote_session_token

_PLACEHOLDER_HOSTS = {"xxxxx", "xxxxx.example"}


def _effective_mode(config: Any) -> str:
    mode = str(getattr(config, "auth_mode", "local") or "local").strip().lower()
    if mode == "remote":
        return "remote"
    if bool(getattr(config, "gateway_dev_mode", False)):
        return "dev"
    return "local"


def _remote_base_url(config: Any) -> str:
    raw = str(getattr(config, "auth_base_url", "") or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or parsed.hostname.lower() in _PLACEHOLDER_HOSTS:
        return ""
    return raw


def _endpoint(base_url: str, path: str) -> str:
    normalized = "/" + str(path or "").strip().lstrip("/")
    return urljoin(f"{base_url}/", normalized.lstrip("/"))


def _message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return fallback


def _payload_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _remote_rejected(payload: Any) -> bool:
    return isinstance(payload, dict) and (
        payload.get("ok") is False or payload.get("success") is False
    )


async def _post_json(config: Any, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    base_url = _remote_base_url(config)
    if not base_url:
        return 503, {"ok": False, "error": "远程认证服务尚未配置"}
    timeout = max(1.0, min(60.0, float(getattr(config, "auth_timeout_seconds", 10.0))))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(_endpoint(base_url, path), json=body)
    except httpx.TimeoutException:
        return 504, {"ok": False, "error": "认证服务请求超时"}
    except httpx.TransportError:
        return 502, {"ok": False, "error": "无法连接认证服务"}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.is_success:
        return 502, {
            "ok": False,
            "error": _message(payload, f"认证服务返回 HTTP {response.status_code}"),
        }
    return 200, payload


def create_remote_auth_router(config: Any) -> APIRouter:
    """Expose the normalized Crew login contract without leaking provider details."""

    router = APIRouter()

    @router.get("/api/auth/config")
    async def auth_config() -> dict[str, Any]:
        mode = _effective_mode(config)
        return {
            "ok": True,
            "mode": mode,
            "configured": mode != "remote" or bool(_remote_base_url(config)),
            "providerId": str(getattr(config, "auth_provider_id", "custom") or "custom"),
        }

    @router.get("/api/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        account = account_from_request(request)
        mode = _effective_mode(config)
        user_id = account.user_id or account.owner_account_id
        provider_id = account.provider_id or ("dev" if mode == "dev" else "local")
        return {
            "ok": True,
            "mode": mode,
            "user": {"userId": user_id, "providerId": provider_id},
        }

    @router.post("/api/auth/send-code")
    async def send_code(request: Request) -> JSONResponse:
        if _effective_mode(config) != "remote":
            return JSONResponse(
                {"ok": False, "error": "当前未启用远程认证"},
                status_code=409,
            )
        try:
            body = await request.json()
        except ValueError:
            body = {}
        phone = str(body.get("phoneNumber") or "").strip() if isinstance(body, dict) else ""
        if not phone or len(phone) > 32:
            return JSONResponse({"ok": False, "error": "请输入有效手机号"}, status_code=400)
        status, payload = await _post_json(
            config,
            str(getattr(config, "auth_send_code_path", "/auth/send-code")),
            {"phoneNumber": phone},
        )
        if status != 200 or _remote_rejected(payload):
            return JSONResponse(
                {"ok": False, "error": _message(payload, "验证码发送失败")},
                status_code=status if status != 200 else 401,
            )
        return JSONResponse({"ok": True, "message": _message(payload, "验证码已发送")})

    @router.post("/api/auth/login")
    async def login(request: Request) -> JSONResponse:
        if _effective_mode(config) != "remote":
            return JSONResponse(
                {"ok": False, "error": "当前未启用远程认证"},
                status_code=409,
            )
        try:
            body = await request.json()
        except ValueError:
            body = {}
        phone = str(body.get("phoneNumber") or "").strip() if isinstance(body, dict) else ""
        code = str(body.get("code") or "").strip() if isinstance(body, dict) else ""
        if not phone or len(phone) > 32 or not code or len(code) > 32:
            return JSONResponse(
                {"ok": False, "error": "手机号和验证码不能为空"},
                status_code=400,
            )
        status, payload = await _post_json(
            config,
            str(getattr(config, "auth_login_path", "/auth/login-by-code")),
            {"phoneNumber": phone, "code": code},
        )
        if status != 200 or _remote_rejected(payload):
            return JSONResponse(
                {"ok": False, "error": _message(payload, "登录失败")},
                status_code=status if status != 200 else 401,
            )
        data = _payload_data(payload)
        user = data.get("user") if isinstance(data.get("user"), dict) else data
        user_id = str(user.get("userId") or "").strip()
        if not user_id:
            return JSONResponse(
                {"ok": False, "error": "认证服务未返回 userId"},
                status_code=502,
            )
        provider_id = str(getattr(config, "auth_provider_id", "custom") or "custom").strip()
        try:
            token = create_remote_session_token(
                provider_id,
                user_id,
                ttl_seconds=int(getattr(config, "auth_session_ttl_seconds", 604800)),
            )
        except (ValueError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        phone_number = str(user.get("phoneNumber") or phone).strip()
        display_name = str(user.get("displayName") or "").strip()
        response = JSONResponse(
            {
                "ok": True,
                "user": {
                    "userId": user_id,
                    "phoneNumber": phone_number,
                    **({"displayName": display_name} if display_name else {}),
                },
            }
        )
        response.set_cookie(
            REMOTE_AUTH_COOKIE,
            token,
            max_age=int(getattr(config, "auth_session_ttl_seconds", 604800)),
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    return router


__all__ = ["create_remote_auth_router"]
