"""技能、插件、文件上传、路径补全等杂项路由。"""

from __future__ import annotations

import asyncio
import base64

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.agent.skills import (
    install_skill,
    list_local_skills,
    list_optional_skills,
    list_skills,
    uninstall_skill,
)
from crew.gateway.auth import AuthenticationError, account_from_request, require_admin
from crew.gateway.context import complete_path, save_upload
from crew.gateway.instance_auth import (
    GATEWAY_INSTANCE_CHALLENGE_HEADER,
    GATEWAY_INSTANCE_PROOF_FIELD,
    create_gateway_instance_proof,
    is_valid_gateway_instance_challenge,
)
from crew.gateway.helpers import safe_public_error
from crew.state.logging import get_logger
from crew.wiki.capture import capture_upload_to_wiki

log = get_logger("gateway.routers.misc")
_INSTANCE_VERIFICATION_FAILURE = "gateway instance verification failed"


def create_misc_router(crew) -> APIRouter:
    router = APIRouter()

    # 后台任务引用集合：防 GC 提前回收，完成回调里统一清理。
    _wiki_capture_tasks: set[asyncio.Task] = set()

    def _on_wiki_capture_done(task: asyncio.Task) -> None:
        _wiki_capture_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("聊天附件自动收入 Wiki 任务异常: %s", exc)

    def _schedule_wiki_capture(request: Request, filename: str, content: bytes) -> None:
        """上传成功后把附件后台收入 default wiki 知识库（wiki.capture_attachments 控制）。"""
        store = getattr(crew, "_wiki_store", None)
        wiki_cfg = getattr(getattr(crew, "config", None), "wiki", None)
        if store is None or wiki_cfg is None:
            return
        if not wiki_cfg.enabled or not wiki_cfg.capture_attachments:
            return
        task = asyncio.create_task(
            capture_upload_to_wiki(
                store,
                getattr(crew, "_wiki_compiler", None),
                wiki_cfg,
                filename,
                content,
                owner_account_id=account_from_request(request).owner_account_id,
                provider=getattr(crew, "provider", None),
            )
        )
        _wiki_capture_tasks.add(task)
        task.add_done_callback(_on_wiki_capture_done)

    def _components(request: Request) -> dict:
        startup_status = str(
            getattr(request.app.state, "deferred_startup_status", "starting") or "starting"
        )
        startup = {"status": startup_status}
        if startup_status == "failed":
            startup["message"] = "运行环境组件初始化失败，请查看 Gateway 日志"
        cron = getattr(crew, "cron_service", None)
        if cron is None:
            cron_status = {"status": "disabled"}
        elif bool(getattr(cron, "is_running", False)):
            cron_status = {"status": "ready"}
        elif str(getattr(cron, "start_error", "") or ""):
            cron_status = {
                "status": "failed",
                "message": "定时任务启动失败，请查看 Gateway 日志",
            }
        else:
            cron_status = {"status": "starting"}
        return {"startup": startup, "cron": cron_status}

    @router.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        """Public readiness plus an optional Desktop instance proof.

        Generic readiness probes remain compatible when no challenge is supplied. Desktop
        supplies a fresh challenge before trusting a loopback port; that branch fails closed
        unless this process can read the Desktop-created instance key.
        """

        challenge = request.headers.get(GATEWAY_INSTANCE_CHALLENGE_HEADER)
        if challenge is None:
            return JSONResponse({
                "ok": True,
                "service": "crew-gateway",
                "components": _components(request),
            })
        if not is_valid_gateway_instance_challenge(challenge):
            return JSONResponse(
                {"ok": False, "error": _INSTANCE_VERIFICATION_FAILURE},
                status_code=401,
            )
        proof = create_gateway_instance_proof(challenge)
        if proof is None:
            return JSONResponse(
                {"ok": False, "error": _INSTANCE_VERIFICATION_FAILURE},
                status_code=401,
            )
        return JSONResponse({
            "ok": True,
            "service": "crew-gateway",
            GATEWAY_INSTANCE_PROOF_FIELD: proof,
            "components": _components(request),
        })

    # ---- 技能 ----
    @router.get("/api/skills")
    async def skills() -> JSONResponse:
        return JSONResponse(list_skills())

    @router.get("/api/skills/store")
    async def skills_store() -> JSONResponse:
        cfg = crew.config
        return JSONResponse({
            "installed": list_skills(),
            "optional": list_optional_skills(),
            "local": list_local_skills(),
            "evolution": {
                "auto_trigger": cfg.evolution_auto_trigger,
                "auto_full_cycle": cfg.evolution_auto_full_cycle,
                "visible": cfg.evolution_visible,
            },
        })

    @router.put("/api/skills/evolution")
    async def skills_evolution(request: Request, payload: dict) -> JSONResponse:
        """切换自进化（evolution）配置并持久化到 config.yaml。

        body 可含任意子集: {auto_trigger?, auto_full_cycle?, visible?}。
        evolution 为全局开关（影响所有 owner），仅管理员可改。
        """
        cfg = crew.config
        try:
            require_admin(account_from_request(request), cfg)
        except AuthenticationError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "权限不足")}, status_code=403)
        changed = False
        if "auto_trigger" in payload:
            cfg.evolution_auto_trigger = bool(payload["auto_trigger"])
            changed = True
        if "auto_full_cycle" in payload:
            cfg.evolution_auto_full_cycle = bool(payload["auto_full_cycle"])
            changed = True
        if "visible" in payload:
            cfg.evolution_visible = bool(payload["visible"])
            changed = True
        if changed:
            try:
                cfg.persist_evolution_config()
            except RuntimeError as exc:
                return JSONResponse({"ok": False, "error": safe_public_error(exc, "技能操作失败")}, status_code=500)
        return JSONResponse({
            "ok": True,
            "evolution": {
                "auto_trigger": cfg.evolution_auto_trigger,
                "auto_full_cycle": cfg.evolution_auto_full_cycle,
                "visible": cfg.evolution_visible,
            },
        })

    @router.post("/api/skills/{slug}/install")
    async def skill_install(slug: str, request: Request) -> JSONResponse:
        account = account_from_request(request)
        try:
            require_admin(account, getattr(crew, "config", None))
        except AuthenticationError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "权限不足")}, status_code=403)
        owner = account.owner_account_id
        ok = install_skill(slug, operator_account_id=owner, source="desktop-api")
        if not ok:
            return JSONResponse({"ok": False, "error": "skill 不存在或已安装"}, status_code=400)
        return JSONResponse({"ok": True})

    @router.delete("/api/skills/{slug}")
    async def skill_uninstall(slug: str, request: Request) -> JSONResponse:
        account = account_from_request(request)
        try:
            require_admin(account, getattr(crew, "config", None))
        except AuthenticationError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "权限不足")}, status_code=403)
        owner = account.owner_account_id
        ok = uninstall_skill(slug, operator_account_id=owner, source="desktop-api")
        if not ok:
            return JSONResponse({"ok": False, "error": "skill 不存在或为内置（不可卸载）"}, status_code=400)
        return JSONResponse({"ok": True})

    # ---- 插件 ----
    @router.get("/api/plugins")
    async def list_plugins(request: Request) -> JSONResponse:
        """列出已发现/加载的 crew 插件（供技能页「插件」Tab 展示）。

        每项带五态：installed / system_allowed / role_allowed / user_enabled /
        effective_enabled（按当前登录账号计算；user_enabled 缺省按角色缺省：
        internal 开、external 关）。
        """
        from crew.state.plugin_preferences import (
            plugin_effective_enabled,
            plugin_role_allowed,
        )
        from crew.gateway.routers.plugins import browser_runtime_status

        mgr = getattr(crew, "plugins", None)
        if mgr is None:
            return JSONResponse([])
        owner = account_from_request(request).owner_account_id
        user_type = str(crew.config.access_control.user_type or "internal").strip().lower()
        ac = crew.config.access_control.resolve_for(user_type)
        prefs = getattr(crew, "plugin_prefs", None)
        items = []
        for loaded in mgr.loaded_plugins:
            manifest = loaded.manifest
            key = manifest.key or manifest.name
            system_allowed = bool(loaded.enabled)
            role_allowed = plugin_role_allowed(ac, key)
            user_enabled = None
            if prefs is not None and owner:
                try:
                    user_enabled = prefs.get_enabled(owner, key)
                except Exception:  # noqa: BLE001 - 读取失败 fail-closed
                    user_enabled = False
            policy_effective = plugin_effective_enabled(
                system_enabled=system_allowed,
                role_allowed=role_allowed,
                user_enabled=user_enabled,
                user_type=user_type,
            )
            runtime_state, runtime_error = browser_runtime_status(crew, owner, key)
            effective = policy_effective and runtime_state["ready"]
            errors = [str(item) for item in (loaded.error, runtime_error) if item]
            items.append({
                "name": manifest.name,
                "key": key,
                "label": manifest.label or manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "kind": manifest.kind,
                "enabled": loaded.enabled,
                "declarative_only": loaded.declarative_only,
                "execution_trusted": manifest.execution_trusted,
                "installed": True,
                "system_allowed": system_allowed,
                "role_allowed": role_allowed,
                "user_enabled": (
                    policy_effective if user_enabled is None else user_enabled
                ),
                "user_enabled_explicit": user_enabled is not None,
                "effective_enabled": effective,
                "runtime_ready": runtime_state["ready"],
                "runtime_state": runtime_state,
                "toggle_endpoint": (
                    str(manifest.ui_hints.get("toggle_endpoint") or "") or None
                ),
                "tools": loaded.tools_registered,
                "hooks": loaded.hooks_registered,
                "platforms": loaded.platforms_registered,
                "error": "；".join(errors) or None,
            })
        return JSONResponse(items)

    # ---- 工具 / 工具集目录 ----
    @router.get("/api/toolsets")
    async def toolsets() -> JSONResponse:
        registry = getattr(crew, "registry", None)
        if registry is None:
            return JSONResponse([])
        return JSONResponse(registry.toolsets())

    @router.get("/api/tools")
    async def tools() -> JSONResponse:
        registry = getattr(crew, "registry", None)
        if registry is None:
            return JSONResponse([])
        items = []
        for name in sorted(registry.names()):
            items.append({
                "name": name,
                "toolset": registry.toolset_for(name) or "default",
                "display_name": registry.ui_meta(name).get("display_name", ""),
            })
        return JSONResponse(items)

    # ---- 附件与路径 ----
    @router.post("/api/upload")
    async def upload_file(request: Request, payload: dict) -> JSONResponse:
        """上传文件附件。body: {filename, content(base64)}。"""
        from crew.gateway.auth import account_from_request

        filename = payload.get("filename", "untitled")
        content_b64 = payload.get("content", "")
        # 体积上限：解码后约 20 MiB（b64 ≈ 27 MiB）。在 b64decode 之前拦截，避免把
        # 超大 base64 字符串先解成内存 bytes 造成 RSS 尖峰。
        # 20 * 1024 * 1024 * 4/3 ≈ 27_962_026，向上取整到 28 MiB 留余量。
        max_b64_len = 28 * 1024 * 1024
        if len(content_b64) > max_b64_len:
            return JSONResponse({"ok": False, "error": "file too large"}, status_code=413)
        try:
            content_bytes = base64.b64decode(content_b64)
        except Exception:
            return JSONResponse({"ok": False, "error": "content 不是合法 base64"}, status_code=400)
        try:
            meta = save_upload(
                filename,
                content_bytes,
                owner_account_id=account_from_request(request).owner_account_id,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": safe_public_error(exc, "上传请求无效")}, status_code=400)
        if not meta.get("deduplicated"):
            _schedule_wiki_capture(request, filename, content_bytes)
        return JSONResponse(meta)

    @router.get("/api/complete")
    async def complete(
        request: Request,
        query: str = "",
        cwd: str | None = None,
        workspace_id: str | None = None,
    ) -> JSONResponse:
        """路径补全（@引用用）。未传 cwd 时按 workspace 的本地 root_path 或任务目录。"""
        from crew.gateway.auth import account_from_request

        owner = account_from_request(request).owner_account_id
        workspace_root_path: str | None = None
        if workspace_id and workspace_id != "default" and not cwd:
            try:
                ws = crew.workspace_store.get(workspace_id, owner_account_id=owner)
                root = str(ws.get("root_path") or "").strip()
                workspace_root_path = root or None
            except Exception:
                workspace_root_path = None
        return JSONResponse(
            complete_path(
                query,
                cwd,
                workspace_id=workspace_id,
                workspace_root_path=workspace_root_path,
                owner_account_id=owner,
            )
        )

    return router
