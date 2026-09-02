"""配置 + 模型 profile CRUD 路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.gateway.auth import AuthenticationError, account_from_request, require_admin
from crew.gateway.helpers import config_body
from crew.state.config import is_owner_overridable_model_profile


def create_config_router(crew, dispatcher=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/config")
    async def config(request: Request) -> JSONResponse:
        return JSONResponse(_visible_config_body(request))

    def _admin_or_403(request: Request) -> JSONResponse | None:
        try:
            require_admin(account_from_request(request), crew.config)
        except AuthenticationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        return None

    def _is_admin(request: Request) -> bool:
        return _admin_or_403(request) is None

    def _visible_config_body(request: Request) -> dict[str, Any]:
        is_admin = _is_admin(request)
        return config_body(
            crew,
            owner_account_id=account_from_request(request).owner_account_id,
            include_builtin_profiles=True,
            is_gateway_admin=is_admin,
        )

    @router.post("/api/config/model")
    async def switch_model(request: Request, payload: dict) -> JSONResponse:
        model_id = str(payload.get("model_id") or "")
        try:
            crew.use_model(
                model_id,
                owner_account_id=account_from_request(request).owner_account_id,
            )
        except KeyError:
            return JSONResponse({"ok": False, "error": f"未知模型配置: {model_id}"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return JSONResponse(_visible_config_body(request))

    # ---- 模型 profile CRUD：运行时增删改 + 持久化到 config.yaml / .env ----
    # 不直接改 yaml，统一走 CrewApp.add_model/update_model/remove_model（与 use_model
    # 共享副作用：重建 Provider + 清缓存）。API Key 明文仅接受在 body，由后端写 .env。
    def _config_response(request: Request, model_profile=None, status: int = 200) -> JSONResponse:
        """统一 CRUD 响应：models 列表 + 当前激活模型（+ 可选 profile）。"""
        body: dict[str, Any] = {"ok": True, **_visible_config_body(request)}
        if model_profile is not None:
            profile = model_profile.public_dict()
            profile["editable"] = (
                not model_profile.builtin
                or body["is_gateway_admin"]
                or is_owner_overridable_model_profile(model_profile)
            )
            body["profile"] = profile
        return JSONResponse(body, status_code=status)

    @router.post("/api/config/models")
    async def create_model(request: Request, payload: dict) -> JSONResponse:
        try:
            profile = crew.add_model(
                payload,
                owner_account_id=account_from_request(request).owner_account_id,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return _config_response(request, model_profile=profile, status=201)

    @router.put("/api/config/models/{model_id}")
    async def update_model(request: Request, model_id: str, payload: dict) -> JSONResponse:
        try:
            profile = crew.owner_model_profiles(account_from_request(request).owner_account_id).get(model_id)
            if profile is None:
                raise KeyError
            if profile.builtin and not is_owner_overridable_model_profile(profile):
                denied = _admin_or_403(request)
                if denied is not None:
                    return denied
            profile = crew.update_model(
                model_id,
                payload,
                owner_account_id=account_from_request(request).owner_account_id,
            )
        except KeyError:
            return JSONResponse({"ok": False, "error": f"模型配置不存在: {model_id}"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return _config_response(request, model_profile=profile)

    @router.delete("/api/config/models/{model_id}")
    async def delete_model(request: Request, model_id: str, force: bool = False) -> JSONResponse:
        from crew.state.session_model import rebind_sessions_from_model, sessions_using_model

        owner = account_from_request(request).owner_account_id
        hits = sessions_using_model(crew.session_store, model_id, owner_account_id=owner)
        busy_sessions: list[str] = []
        if dispatcher is not None:
            for hit in hits:
                sid = hit["session_id"]
                st = dispatcher.status(sid, owner_account_id=owner)
                if st.get("live") in ("running", "queued"):
                    busy_sessions.append(sid)
        if busy_sessions and not force:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "有会话正在使用该模型，请先停止或确认强制删除",
                    "busy_sessions": busy_sessions,
                },
                status_code=409,
            )
        if force and dispatcher is not None:
            for sid in busy_sessions:
                await dispatcher.stop(sid, owner_account_id=owner)

        visible_profiles = crew.owner_model_profiles(owner)
        fallback_model_id = crew.config.owner_default_model_id(owner)
        if fallback_model_id == model_id:
            fallback_model_id = next(
                (
                    candidate_id
                    for candidate_id, candidate in sorted(visible_profiles.items())
                    if candidate_id != model_id and candidate.loaded
                ),
                fallback_model_id,
            )
        rebound = rebind_sessions_from_model(
            crew.session_store,
            crew.config,
            visible_profiles,
            model_id,
            owner_account_id=owner,
            to_model_id=fallback_model_id,
            fallback_model_id=fallback_model_id,
        )
        for sid in rebound:
            crew.agents.drop(sid, owner_account_id=owner)

        try:
            profile = crew.owner_model_profiles(owner).get(model_id)
            if profile is None:
                raise KeyError
            if profile.builtin:
                denied = _admin_or_403(request)
                if denied is not None:
                    return denied
            result = crew.remove_model(
                model_id,
                owner_account_id=owner,
            )
        except KeyError:
            return JSONResponse({"ok": False, "error": f"模型配置不存在: {model_id}"}, status_code=404)
        except ValueError as exc:
            # 删除最后一个：业务规则不允许
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        body: dict[str, Any] = {
            "ok": True,
            **_visible_config_body(request),
            "removed": result["removed"].public_dict(),
            "rebound_sessions": rebound,
        }
        if result.get("switched_to"):
            body["switched_to"] = result["switched_to"]
        return JSONResponse(body)

    return router
