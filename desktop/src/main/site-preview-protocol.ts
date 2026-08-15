/** ace-site:// 本地站点预览协议。所有资源由主进程携带当前 Gateway 身份读取。 */

import { protocol } from 'electron';

export const SITE_PREVIEW_SCHEME = 'ace-site';

type GatewayResolver = () => Promise<{
  baseUrl: string;
  headers: (pathname: string) => Record<string, string>;
}>;

export interface SitePreviewRequest {
  kind: 'site' | 'canvas' | 'widget';
  assetId: string;
  siteId: string | undefined;
  canvasId: string | undefined;
  widgetId: string | undefined;
  assetPath: string;
  search: string;
}

function previewErrorDocument(message: string): string {
  const payload = JSON.stringify(message.slice(0, 1000)).replace(/</g, '\\u003c');
  return `<!doctype html><meta charset="utf-8"><title>站点预览失败</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:14px system-ui;color:#991b1b;background:#fff7f7}.box{max-width:560px;padding:24px;border:1px solid #fecaca;border-radius:12px;background:#fff}h1{font-size:17px;margin:0 0 10px}p{margin:0;line-height:1.6;white-space:pre-wrap}</style>
<div class="box"><h1>站点预览失败</h1><p id="message"></p></div>
<script>const message=${payload};document.getElementById('message').textContent=message;parent.postMessage({type:'ace-site-preview-error',message},'*');</script>`;
}

const PREVIEW_UPSTREAM_FAILURE = '站点预览请求失败';

export function parseSitePreviewUrl(rawUrl: string): SitePreviewRequest | null {
  if (rawUrl.length > 16_384) return null;
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== `${SITE_PREVIEW_SCHEME}:`) return null;
  const assetId = parsed.hostname;
  const kind = assetId.startsWith('site_') ? 'site'
    : assetId.startsWith('canvas_') ? 'canvas'
      : assetId.startsWith('widget_') ? 'widget' : null;
  if (!kind || !/^(?:site|canvas|widget)_[0-9a-f]{12}$/i.test(assetId)) return null;
  const segments = parsed.pathname.split('/').filter(Boolean);
  let assetPath: string;
  try {
    assetPath = segments.map((segment) => encodeURIComponent(decodeURIComponent(segment))).join('/');
  } catch {
    return null;
  }
  return {
    kind, assetId, assetPath, search: parsed.search,
    siteId: kind === 'site' ? assetId : undefined,
    canvasId: kind === 'canvas' ? assetId : undefined,
    widgetId: kind === 'widget' ? assetId : undefined,
  };
}

export function registerSitePreviewProtocol(resolveGateway: GatewayResolver): void {
  protocol.handle(SITE_PREVIEW_SCHEME, async (request) => {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method Not Allowed', { status: 405 });
    }
    const resolved = parseSitePreviewUrl(request.url);
    if (!resolved) return new Response('Not Found', { status: 404 });
    try {
      const gateway = await resolveGateway();
      const endpoint = new URL(gateway.baseUrl);
      endpoint.pathname = resolved.kind === 'site'
        ? `/api/sites/${resolved.siteId}/preview/${resolved.assetPath}`
        : resolved.kind === 'canvas'
          ? `/api/sites/canvases/${resolved.canvasId}/render`
          : `/api/sites/widgets/${resolved.widgetId}/render/${resolved.assetPath}`;
      endpoint.search = resolved.search;
      const upstream = await fetch(endpoint, {
        method: request.method,
        headers: gateway.headers(endpoint.pathname),
      });
      const contentType = upstream.headers.get('content-type') || 'application/octet-stream';
      if (!upstream.ok) {
        console.warn(
          `[ace-site] ${resolved.assetId}/${resolved.assetPath || 'index.html'} -> ` +
          `upstream status=${upstream.status}`,
        );
        if (!resolved.assetPath) {
          return new Response(previewErrorDocument(PREVIEW_UPSTREAM_FAILURE), {
            status: 200,
            headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
          });
        }
        return new Response(PREVIEW_UPSTREAM_FAILURE, {
          status: 502,
          headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
        });
      }
      if (!resolved.assetPath && !contentType.toLowerCase().includes('text/html')) {
        const message = `入口响应不是 HTML（${contentType}）`;
        console.warn(`[ace-site] ${resolved.assetId}/index.html -> ${message}`);
        return new Response(previewErrorDocument(message), {
          status: 200,
          headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
        });
      }
      console.log(`[ace-site] ${resolved.assetId}/${resolved.assetPath || 'index.html'} -> 200 ${contentType}`);
      const body = request.method === 'HEAD' ? null : await upstream.arrayBuffer();
      return new Response(body, {
        status: upstream.status,
        headers: {
          'Content-Type': contentType,
          'Cache-Control': 'no-store',
          'X-Content-Type-Options': 'nosniff',
        },
      });
    } catch (error) {
      console.error(
        `[ace-site] ${resolved.assetId}/${resolved.assetPath || 'index.html'} failed `
        + `type=${error instanceof Error ? error.name : 'unknown'}`,
      );
      return new Response(previewErrorDocument(PREVIEW_UPSTREAM_FAILURE), {
        status: 200,
        headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
      });
    }
  });
}
