/** A separate document avoids inheriting the privileged renderer's script CSP. */
export const INLINE_WIDGET_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
export const INLINE_WIDGET_SHELL = `<!doctype html><meta charset="utf-8"><script>
function initialize(event) {
  if (event.source !== parent || event.data?.type !== 'ace-inline-init' || typeof event.data.html !== 'string') return;
  if (event.data.html.length > 200000) return;
  removeEventListener('message', initialize);
  document.open(); document.write(event.data.html); document.close();
}
addEventListener('message', initialize);
parent.postMessage({type:'ace-inline-shell-ready'}, '*');
</script>`;
