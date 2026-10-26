// Reload the Android Control panel script on every open (A0 caches component
// HTML by path). Widening is handled in CSS (see modal-shell-end).
export default async function(ctx) {
  const path = ctx && ctx.modalPath;
  if (typeof path === 'string' && path.includes('droidclaw/webui/device-panel.html') && !path.includes('dc=')) {
    ctx.modalPath = path + (path.includes('?') ? '&' : '?') + 'dc=' + Date.now();
  }
}
