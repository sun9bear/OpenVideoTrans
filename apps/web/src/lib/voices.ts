// P1d — the dub-voice picker data layer. Fetches the PUBLIC capability manifest (GET /api/tts/voices,
// P1c) and shapes it for the two-level engine→voice picker. Robust by construction (mirrors caps.ts):
// any network error / non-2xx / malformed payload yields an empty list — the picker then offers only
// "auto" (the server picks a commercial-safe voice), never blocking the flow.

export interface TtsVoice {
  provider: string;
  voice_id: string;
  target_lang: string;
  gender: string;
  label: string;
  commercial_safe: boolean;
  experimental: boolean;
}

// Human labels for the engine (provider) select. edge_tts is flagged experimental / non-commercial
// (AD-6): it is offered ONLY as an explicit user pick, never auto-routed (red line §1/§3). An
// unknown/new provider falls back to its raw name.
const PROVIDER_LABELS: Record<string, string> = {
  piper: "Piper（离线合成）",
  cloudflare: "Cloudflare MeloTTS",
  edge_tts: "微软 Edge 神经语音（实验 · 非商用）",
};

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

// A short gender tag for a voice option label ("" when unknown, so it just shows the label).
export function genderTag(gender: string): string {
  return gender === "male" ? "（男）" : gender === "female" ? "（女）" : "";
}

// Distinct providers present in a voice list, in a stable display order: commercial-safe engines
// first, the experimental edge_tts last, then alphabetical — for the engine select.
export function providersOf(voices: TtsVoice[]): string[] {
  const seen = new Set<string>();
  for (const v of voices) seen.add(v.provider);
  const rank = (p: string): number => (p === "edge_tts" ? 1 : 0);
  return [...seen].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

// Fetch the dub-voice options AVAILABLE for `targetLang` on the fleet (P1c public endpoint). NEVER
// throws and never returns a partial/garbled entry: a malformed item is skipped, and any error yields
// [] — the picker degrades to "auto" only. Fetch is injectable for tests.
export async function fetchTtsVoices(
  apiBase: string,
  targetLang: string,
  fetchFn: typeof fetch = (input, init) => fetch(input, init),
): Promise<TtsVoice[]> {
  try {
    const base = apiBase.replace(/\/+$/, "");
    const url = `${base}/api/tts/voices?target_lang=${encodeURIComponent(targetLang)}`;
    const res = await fetchFn(url, { headers: { accept: "application/json" } });
    if (!res.ok) return [];
    const body = (await res.json()) as { voices?: unknown };
    if (!Array.isArray(body.voices)) return [];
    const out: TtsVoice[] = [];
    for (const v of body.voices) {
      if (!v || typeof v !== "object") continue;
      const o = v as Record<string, unknown>;
      if (typeof o.provider !== "string" || o.provider === "") continue;
      if (typeof o.voice_id !== "string" || o.voice_id === "") continue;
      out.push({
        provider: o.provider,
        voice_id: o.voice_id,
        target_lang: typeof o.target_lang === "string" ? o.target_lang : targetLang,
        gender: typeof o.gender === "string" ? o.gender : "unknown",
        label: typeof o.label === "string" && o.label ? o.label : o.voice_id,
        commercial_safe: o.commercial_safe === true,
        experimental: o.experimental === true,
      });
    }
    return out;
  } catch {
    return []; // unreachable server / bad payload -> "auto" only, never block the flow
  }
}
