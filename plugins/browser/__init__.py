"""内置浏览器插件：Browser 能力包与生命周期边界。

- 创建并持有 BrowserManager（crew/browser/ 作为安全运行时库，不搬迁）。
- 注册 browser_use 与按需加载的 browser_use_advanced（替代原 15 个 deferred browser_* 工具）。
- register_disposer 保证系统级卸载时关闭全部 Browser owner。
- 用户级热开关不走卸载：由 browser_use 的 permission_resolver 每次执行重查
  有效状态（crew.state.plugin_preferences），配合 BrowserManager.revoke_owner
  立即撤销在途能力。
"""

from __future__ import annotations

from crew.browser import BrowserManager
from crew.browser.tab_reading import (
    BROWSER_TAB_REFERENCE_RE,
    format_browser_tab_references,
    resolve_browser_tab_references,
)
from crew.core.envelope import Envelope
from crew.features import ContextContribution
from crew.state.logging import get_logger

from .compile_tool import register_record_compile_tool
from .tool import PLUGIN_KEY as PLUGIN_KEY, register_browser_use_tool

# PLUGIN_KEY 是给外部（权限判定、偏好键）读的，显式 re-export 而不是留成未用导入。
__all__ = ["PLUGIN_KEY", "manager", "register"]

log = get_logger("plugins.browser")

# build_app 在插件加载后从这里取回 manager，维持 app.browser_manager 引用点
# （startup/aclose、gateway 面板路由、会话关闭清理）不变。
manager: BrowserManager | None = None


async def register(ctx) -> None:
    global manager
    config = ctx.resolve_service("config")
    plugin_prefs = ctx.get_service("plugin_prefs")

    browser_manager = BrowserManager(config.browser)
    manager = browser_manager
    # Register cleanup before startup so every partial installation can be
    # rolled back by FeatureTransaction, including a failed driver startup.
    ctx.register_disposer(_manager_disposer(browser_manager))
    await browser_manager.startup()
    ctx.register_service("browser.manager", browser_manager)
    ctx.register_context_contributor(
        "browser.reference.tab",
        _browser_tab_contributor(browser_manager),
        priority=200,
        timeout_seconds=max(
            1.0,
            float(config.browser.command_timeout_seconds) + 1.0,
        ),
        predicate=lambda envelope: bool(
            BROWSER_TAB_REFERENCE_RE.search(str(envelope.query or ""))
        ),
        persistent=False,
        description="Read-only snapshots for explicit browser tab references",
    )
    ctx.register_skill_root("skills")
    browser_tool = register_browser_use_tool(
        ctx,
        browser_manager,
        config,
        plugin_prefs,
    )
    # 两阶段发布 + 独立回放：compile 只生成 owner-private immutable draft；
    # install 经一次性审批发布私有 executable plan 和全局 opaque entry；
    # replay 每次 mutation 再走动态审批与 exact session lease。
    register_record_compile_tool(
        ctx,
        browser_manager,
        capability_check=browser_tool.capability_denial,
    )
    log.info(
        "browser 插件已注册工具、Skill Root 与标签页 Context Contributor"
    )


def _browser_tab_contributor(browser_manager: BrowserManager):
    """Bind one contributor to the manager owned by its exact Generation."""

    async def contribute(envelope: Envelope) -> ContextContribution | None:
        refs = await resolve_browser_tab_references(
            envelope.query,
            manager=browser_manager,
            owner_account_id=envelope.user_id,
            session_id=envelope.session_id,
        )
        if not refs:
            return None
        prompt = format_browser_tab_references(refs)
        return ContextContribution(
            params={"browser_tab_references": refs},
            prompt_parts=(prompt,) if prompt else (),
        )

    return contribute


def _manager_disposer(browser_manager: BrowserManager):
    """Close exactly one Generation's manager and clear its compatibility slot."""

    async def _close_manager() -> None:
        global manager
        await browser_manager.aclose()
        if manager is browser_manager:
            manager = None

    return _close_manager
