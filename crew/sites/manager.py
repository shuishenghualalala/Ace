"""显式发布本地站点、生成预览版本和便携分享包。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import zipfile
from html import escape
from pathlib import Path
from typing import Any

from crew.sites.blueprint import BlueprintManager, BlueprintStore
from crew.sites.store import SQLiteSiteStore
from crew.state.home import get_owner_runtime_home, safe_path_segment


class SiteBuildError(RuntimeError):
    pass


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class SiteManager:
    def __init__(self, store: SQLiteSiteStore) -> None:
        self.store = store
        self.blueprint = BlueprintManager(
            BlueprintStore(str(store.db_path), wal_enabled=store.wal_enabled)
        )

    async def start(self) -> None:
        await self.blueprint.start()

    async def stop(self) -> None:
        await self.blueprint.stop()

    def _root(self, owner: str) -> Path:
        root = get_owner_runtime_home(owner, create=True) / "sites"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _resolve_source(source_path: str, workspace_root: str) -> Path:
        root = Path(workspace_root).expanduser().resolve()
        raw = Path(source_path).expanduser()
        source = (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
        if not root.is_dir() or not source.is_dir() or not _within(source, root):
            raise ValueError("站点源码目录必须位于当前 Workspace 内")
        return source

    @staticmethod
    def _defaults(source: Path, build_command: str, output_directory: str) -> tuple[list[str], str]:
        command = build_command.strip()
        output = output_directory.strip()
        if not command and (source / "package.json").is_file():
            try:
                package = json.loads((source / "package.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SiteBuildError(f"package.json 无法读取: {exc}") from exc
            if "build" in (package.get("scripts") or {}):
                command = "npm run build"
            if not output:
                output = "dist"
        if not command:
            if not (source / "index.html").is_file():
                raise SiteBuildError("未找到 index.html，也没有可用的 build 命令")
            return [], output or "."
        argv = shlex.split(command)
        if not argv or argv[0] not in {"npm", "pnpm", "yarn", "bun"}:
            raise SiteBuildError("第一版仅允许 npm、pnpm、yarn 或 bun 构建命令")
        return argv, output or "dist"

    @staticmethod
    def _copy_release(source_dir: Path, release_dir: Path) -> dict[str, Any]:
        if not source_dir.is_dir() or not (source_dir / "index.html").is_file():
            raise SiteBuildError("构建输出目录缺少 index.html")
        if release_dir.exists():
            shutil.rmtree(release_dir)
        shutil.copytree(source_dir, release_dir, symlinks=False)
        # 便携分享包通过 file:// 打开，根绝对资源路径必须改成相对路径。
        # 只处理 HTML/CSS 中的站内根路径；协议相对 URL（//host）保持不变。
        for html_path in release_dir.rglob("*.html"):
            text = html_path.read_text(encoding="utf-8")
            text = re.sub(r'((?:src|href)=["\'])/(?!/)', r'\1./', text, flags=re.IGNORECASE)
            html_path.write_text(text, encoding="utf-8")
        for css_path in release_dir.rglob("*.css"):
            text = css_path.read_text(encoding="utf-8")
            text = re.sub(r'url\((["\']?)/(?!/)', r'url(\1../', text, flags=re.IGNORECASE)
            css_path.write_text(text, encoding="utf-8")
        files: list[dict[str, Any]] = []
        for path in sorted(release_dir.rglob("*")):
            if path.is_symlink():
                path.unlink()
                continue
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append({"path": path.relative_to(release_dir).as_posix(), "sha256": digest, "size": path.stat().st_size})
        return {"entry": "index.html", "files": files, "portable": True}

    def publish(
        self, *, owner: str, workspace_id: str, session_id: str, workspace_root: str,
        source_path: str, name: str, build_command: str = "", output_directory: str = "",
        site_id: str = "", description: str = "",
    ) -> dict[str, Any]:
        source = self._resolve_source(source_path, workspace_root)
        argv, output = self._defaults(source, build_command, output_directory)
        is_new = not site_id.strip()
        site = self.store.upsert_site(
            owner=owner, workspace_id=workspace_id, session_id=session_id,
            name=name.strip() or source.name, description=description.strip(), source_path=str(source),
            build_command=" ".join(argv), output_directory=output, site_id=site_id,
        )
        release = self.store.create_release(owner, site["id"])
        try:
            if argv:
                result = subprocess.run(
                    argv, cwd=source, capture_output=True, text=True, timeout=180,
                    env={**os.environ, "CI": "1"}, check=False,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "构建失败")[-6000:]
                    raise SiteBuildError(detail)
            output_path = (source / output).resolve()
            if not _within(output_path, source):
                raise SiteBuildError("构建输出目录不能位于源码目录之外")
            release_path = self._root(owner) / safe_path_segment(site["id"], "site") / "releases" / release["id"] / "site"
            manifest = self._copy_release(output_path, release_path)
            manifest.update({"site_id": site["id"], "release_id": release["id"]})
            self.store.finish_release(owner, site["id"], release["id"], status="ready", release_path=str(release_path), manifest=manifest)
        except Exception as exc:
            self.store.finish_release(owner, site["id"], release["id"], status="failed", error=str(exc))
            if is_new:
                self.store.delete_site(owner, site["id"])
            raise
        return {"site": self.store.get_site(owner, site["id"]), "release": self.store.get_release(owner, release["id"])}

    def delete(self, owner: str, site_id: str) -> None:
        self.store.get_site(owner, site_id)
        self.store.delete_site(owner, site_id)
        target = self._root(owner) / safe_path_segment(site_id, "site")
        if target.exists():
            shutil.rmtree(target)

    def release_file(self, owner: str, site_id: str, relative_path: str) -> Path:
        site = self.store.get_site(owner, site_id)
        if not site["active_release_id"]:
            raise KeyError("站点尚无可预览版本")
        release = self.store.get_release(owner, site["active_release_id"])
        root = Path(release["release_path"]).resolve()
        requested = (root / (relative_path or "index.html")).resolve()
        if not _within(requested, root):
            raise ValueError("无效的站点资源路径")
        if requested.is_file():
            return requested
        return root / "index.html"

    def export(self, owner: str, site_id: str) -> Path:
        site = self.store.get_site(owner, site_id)
        if not site["active_release_id"]:
            raise KeyError("站点尚无可分享版本")
        release = self.store.get_release(owner, site["active_release_id"])
        root = Path(release["release_path"]).resolve()
        export_dir = self._root(owner) / safe_path_segment(site_id, "site") / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive = export_dir / f"{safe_path_segment(site['name'], 'site')}-{release['id']}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    zf.write(path, path.relative_to(root).as_posix())
        return archive

    @staticmethod
    def _site_inspiration(site: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": site["id"], "kind": "site", "title": site["name"],
            "description": site.get("description") or "", "workspaceId": site["workspace_id"],
            "sessionId": site["session_id"], "createdAt": site["created_at"],
            "updatedAt": site["updated_at"],
        }

    @staticmethod
    def _canvas_inspiration(canvas: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": canvas["id"], "kind": "canvas", "title": canvas["title"],
            "description": canvas.get("purpose") or "", "workspaceId": canvas["workspaceId"],
            "sessionId": canvas["sessionId"], "createdAt": canvas["createdAt"],
            "updatedAt": canvas["updatedAt"],
        }

    def list_inspirations(self, owner: str) -> list[dict[str, Any]]:
        items = [self._site_inspiration(item) for item in self.store.list_sites(owner)]
        items.extend(self._canvas_inspiration(item) for item in self.blueprint.store.list_canvases(owner))
        return sorted(items, key=lambda item: float(item["updatedAt"]), reverse=True)

    def get_inspiration(self, owner: str, inspiration_id: str) -> dict[str, Any]:
        if inspiration_id.startswith("site_"):
            site = self.store.get_site(owner, inspiration_id)
            return {**self._site_inspiration(site), "site": site,
                    "annotations": self.store.list_inspiration_annotations(owner, inspiration_id)}
        if inspiration_id.startswith("canvas_"):
            canvas = self.blueprint.store.get_canvas(owner, inspiration_id)
            widgets = {
                placement["widgetId"]: self.blueprint.store.get_widget(owner, placement["widgetId"])
                for placement in canvas.get("placements", [])
            }
            return {**self._canvas_inspiration(canvas), "canvas": canvas, "widgets": widgets,
                    "annotations": self.store.list_inspiration_annotations(owner, inspiration_id)}
        raise KeyError("灵感不存在")

    def _canvas_document(self, owner: str, canvas_id: str, *, offline: bool = False) -> str:
        canvas = self.blueprint.store.get_canvas(owner, canvas_id)
        cards: list[str] = []
        for placement in canvas.get("placements", []):
            widget = self.blueprint.store.get_widget(owner, placement["widgetId"])
            layout = placement["layout"]
            src = (
                f"widgets/{widget['id']}/index.html"
                if offline else
                f"ace-site://{widget['id']}/?mount_id={placement['mountId']}"
            )
            cards.append(
                f'<section class="widget" data-widget-id="{escape(widget["id"])}" '
                f'data-mount-id="{escape(placement["mountId"])}" '
                f'style="grid-column:{int(layout["x"]) + 1}/span {int(layout["w"])};'
                f'grid-row:{int(layout["y"]) + 1}/span {int(layout["h"])}">'
                f'<iframe title="{escape(widget["title"])}" src="{escape(src)}" '
                'sandbox="allow-scripts allow-forms allow-modals allow-same-origin"></iframe></section>'
            )
        empty = '<p class="empty">这个 App 还没有内容。</p>' if not cards else ""
        bridge = "" if offline else """
<script>
addEventListener('message', event => {
  if (event.data?.type === 'ace-inspiration-annotation-mode') {
    document.querySelectorAll('iframe').forEach(frame => frame.contentWindow?.postMessage(
      {type:'ace-blueprint-annotation-mode',enabled:Boolean(event.data.enabled)}, '*'));
  }
  if (event.data?.type === 'ace-blueprint-element-selected') parent.postMessage(event.data, '*');
  if (event.data?.type === 'ace-widget-emit') parent.postMessage(event.data, '*');
  if (event.data?.type === 'ace-widget-view-state') parent.postMessage({...event.data,canvasId:'%s'}, '*');
});
parent.postMessage({type:'ace-site-preview-ready'}, '*');
</script>""" % canvas_id
        canvas_class = "canvas canvas--single" if len(cards) == 1 else "canvas"
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(canvas['title'])}</title><style>
:root{{color-scheme:light dark;font-family:system-ui,sans-serif}}*{{box-sizing:border-box}}html,body{{margin:0;width:100%;height:100%;min-height:100%;overflow:hidden;background:#f5f6f8}}
.canvas{{display:grid;width:100%;height:100%;grid-template-columns:repeat(12,minmax(0,1fr));grid-auto-rows:clamp(28px,3.2vw,42px);gap:10px;overflow:auto;padding:10px}}
.widget{{min-width:0;min-height:0;overflow:hidden;border:1px solid rgba(127,127,127,.2);border-radius:16px;background:#fff;box-shadow:0 4px 18px rgba(0,0,0,.06)}}
.canvas--single{{display:block;padding:0}}.canvas--single .widget{{width:100%;height:100%;border:0;border-radius:0;box-shadow:none}}
iframe{{display:block;width:100%;height:100%;border:0;background:#fff}}.empty{{grid-column:1/-1;margin:auto;color:#737b8c}}
@media(prefers-color-scheme:dark){{html,body{{background:#111318}}.widget{{background:#191c22}}}}
</style></head><body><main class="{canvas_class}">{''.join(cards)}{empty}</main>{bridge}</body></html>"""

    def canvas_html(self, owner: str, canvas_id: str) -> str:
        return self._canvas_document(owner, canvas_id)

    def export_canvas(self, owner: str, canvas_id: str) -> Path:
        canvas = self.blueprint.store.get_canvas(owner, canvas_id)
        export_root = self._root(owner) / safe_path_segment(canvas_id, "canvas") / "exports"
        export_root.mkdir(parents=True, exist_ok=True)
        archive = export_root / f"{safe_path_segment(canvas['title'], 'app')}-{canvas_id}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("index.html", self._canvas_document(owner, canvas_id, offline=True))
            manifest = {"format": "ace-inspiration-offline-v1", "title": canvas["title"],
                        "createdAt": canvas["createdAt"], "updatedAt": canvas["updatedAt"]}
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            copied: set[str] = set()
            for placement in canvas.get("placements", []):
                widget_id = placement["widgetId"]
                if widget_id in copied:
                    continue
                copied.add(widget_id)
                widget = self.blueprint.store.get_widget(owner, widget_id)
                root = Path(widget["workspacePath"]).resolve()
                if not root.is_dir():
                    continue
                for path in sorted(root.rglob("*")):
                    if not path.is_file() or path.is_symlink() or path.suffix.lower() == ".map":
                        continue
                    relative = path.relative_to(root).as_posix()
                    target = f"widgets/{widget_id}/{relative}"
                    if relative == "index.html":
                        html = self.blueprint.runtime_html(widget, placement)
                        html = html.replace(str(root), "")
                        zf.writestr(target, html)
                    else:
                        zf.write(path, target)
        return archive

    def export_inspiration(self, owner: str, inspiration_id: str) -> Path:
        if inspiration_id.startswith("site_"):
            return self.export(owner, inspiration_id)
        if inspiration_id.startswith("canvas_"):
            return self.export_canvas(owner, inspiration_id)
        raise KeyError("灵感不存在")

    def delete_inspiration(self, owner: str, inspiration_id: str) -> None:
        if inspiration_id.startswith("site_"):
            self.delete(owner, inspiration_id)
            self.store.delete_inspiration_annotations(owner, inspiration_id)
            return
        if not inspiration_id.startswith("canvas_"):
            raise KeyError("灵感不存在")
        canvas = self.blueprint.store.get_canvas(owner, inspiration_id)
        widget_ids = {item["widgetId"] for item in canvas.get("placements", [])}
        candidate_automations: set[str] = set()
        for widget_id in widget_ids:
            candidate_automations.update(
                binding["automationId"]
                for binding in self.blueprint.store.list_bindings(owner, widget_id=widget_id)
            )
        self.blueprint.store.delete_canvas(owner, inspiration_id)
        remaining_widget_ids = {
            placement["widgetId"]
            for remaining in self.blueprint.store.list_canvases(owner)
            for placement in self.blueprint.store.list_placements(owner, remaining["id"])
        }
        for widget_id in widget_ids - remaining_widget_ids:
            self.blueprint.store.delete_widget(owner, widget_id)
        for automation_id in candidate_automations:
            if not self.blueprint.store.list_bindings(owner, automation_id=automation_id):
                self.blueprint.remove_schedule(owner, automation_id)
                self.blueprint.store.delete_automation(owner, automation_id)
        self.store.delete_inspiration_annotations(owner, inspiration_id)
        export_root = self._root(owner) / safe_path_segment(inspiration_id, "canvas")
        if export_root.exists():
            shutil.rmtree(export_root)
