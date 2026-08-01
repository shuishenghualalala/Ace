/** Pure chat-link classification. Security validation remains in the Gateway. */

function decoded(value: string): string {
  try { return decodeURIComponent(value); } catch { return value; }
}

export function htmlArtifactPathFromHref(value: string): string | null {
  const raw = String(value || '').trim();
  if (!raw || raw.startsWith('#') || /^https?:\/\//i.test(raw)) return null;
  let path = raw;
  if (/^file:/i.test(raw)) {
    try {
      const url = new URL(raw);
      if (url.protocol !== 'file:') return null;
      path = decoded(url.pathname);
      if (/^\/[A-Za-z]:\//.test(path)) path = path.slice(1);
    } catch {
      return null;
    }
  } else {
    path = decoded(raw.split(/[?#]/, 1)[0] || '');
  }
  return /\.html?$/i.test(path) ? path : null;
}

export function httpUrlFromHref(value: string): string | null {
  const raw = String(value || '').trim();
  if (!/^https?:\/\//i.test(raw)) return null;
  try {
    const parsed = new URL(raw);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.toString() : null;
  } catch {
    return null;
  }
}
