// Cloudflare Turnstile (the T2.4 abuse gate). When VITE_TURNSTILE_SITE_KEY is configured, the UI MUST
// render the widget and include the resulting token as `turnstile_token` in POST /api/jobs — the
// control-plane admitJob rejects the submission with 403 challenge_required otherwise. When no site
// key is set (dev / self-host without Turnstile), the server gate is inert and we send no token.
//
// Note: the Turnstile SITE key is PUBLIC (it ships in client HTML by design); the verification SECRET
// (TURNSTILE_SECRET_KEY) lives only in the control-plane and never touches the browser.
const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js";

// Read fresh (not a module-load const) so it reflects the build env and is stubbable in tests.
function siteKey(): string {
  return import.meta.env.VITE_TURNSTILE_SITE_KEY ?? "";
}

// True iff a site key is configured — i.e. the deployment expects the gate to be on.
export function turnstileEnabled(): boolean {
  return siteKey() !== "";
}

interface TurnstileApi {
  render(
    el: HTMLElement,
    opts: {
      sitekey: string;
      callback: (token: string) => void;
      "error-callback"?: () => void;
      "expired-callback"?: () => void;
    },
  ): string;
  reset(widgetId?: string): void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

let scriptPromise: Promise<void> | null = null;
function loadScript(): Promise<void> {
  if (window.turnstile) return Promise.resolve();
  if (scriptPromise) return scriptPromise;
  scriptPromise = new Promise<void>((resolve, reject) => {
    const s = document.createElement("script");
    s.src = SCRIPT_SRC;
    s.async = true;
    s.defer = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("turnstile script failed to load"));
    document.head.appendChild(s);
  });
  return scriptPromise;
}

export interface TurnstileHandle {
  reset(): void;
}

// Render the widget into `el`; onToken fires with a fresh token when solved, onClear when it expires
// or errors (the token is single-use). No-op (returns null) when Turnstile is not configured.
export async function renderTurnstile(
  el: HTMLElement,
  onToken: (token: string) => void,
  onClear: () => void,
): Promise<TurnstileHandle | null> {
  if (!turnstileEnabled()) return null;
  await loadScript();
  const api = window.turnstile;
  if (!api) return null;
  const widgetId = api.render(el, {
    sitekey: siteKey(),
    callback: onToken,
    "expired-callback": onClear,
    "error-callback": onClear,
  });
  return { reset: () => api.reset(widgetId) };
}
