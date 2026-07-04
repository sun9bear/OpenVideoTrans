import type { CreateJobBody, JobView } from "./types";

// A thin typed client over the control-plane /api surface (T2.1). The anon id rides X-OVT-Anon-Id on
// every /api call; fetch is injected so the whole flow is unit-testable without a network. baseUrl is
// "" for same-origin. We never log request bodies / urls beyond what the browser already exposes.
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface SignResult {
  upload_session_id: string;
  source_key: string;
  put_url: string;
  expires_at: number;
}

interface ErrorBody {
  error?: { code?: string; message?: string };
}

type FetchFn = typeof fetch;

export class ApiClient {
  private readonly baseUrl: string;
  constructor(
    baseUrl: string,
    private readonly anonId: string,
    // Default MUST be a wrapper, not the bare global `fetch`: this.fetchFn(...) is a METHOD call, so a
    // stored native fetch would be invoked with `this` = ApiClient and throw "TypeError: Illegal
    // invocation" (fetch must run with `this` = window) — before any request is even sent. The wrapper
    // calls the global fetch bare (correct `this`). Tests inject their own fetchFn, overriding this.
    private readonly fetchFn: FetchFn = (input, init) => fetch(input, init),
  ) {
    // Trim trailing slashes so a configured VITE_API_BASE like "https://cp.example/" does not produce
    // "https://cp.example//api/..." — the Worker router matches the exact "/api/..." path and would 404.
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  private headers(json: boolean): Record<string, string> {
    const h: Record<string, string> = { "X-OVT-Anon-Id": this.anonId };
    if (json) h["content-type"] = "application/json";
    return h;
  }

  private async parse<T>(res: Response): Promise<T> {
    const body = (await res.json().catch(() => null)) as (T & ErrorBody) | null;
    if (!res.ok) {
      const code = body?.error?.code ?? "http_error";
      const message = body?.error?.message ?? `请求失败（${res.status}）`;
      throw new ApiError(res.status, code, message);
    }
    return body as T;
  }

  // POST /api/uploads/sign — create a pending upload session + presigned R2 PUT.
  async signUpload(declaredBytes: number, declaredType: string): Promise<SignResult> {
    const res = await this.fetchFn(`${this.baseUrl}/api/uploads/sign`, {
      method: "POST",
      headers: this.headers(true),
      body: JSON.stringify({ declared_bytes: declaredBytes, declared_type: declaredType }),
    });
    return this.parse<SignResult>(res);
  }

  // Direct-to-R2 PUT of the file bytes (no anon header — the URL itself is the SigV4 capability).
  async putSource(putUrl: string, file: Blob, contentType: string): Promise<void> {
    const res = await this.fetchFn(putUrl, {
      method: "PUT",
      headers: { "content-type": contentType },
      body: file,
    });
    if (!res.ok) throw new ApiError(res.status, "upload_put_failed", `直传失败（${res.status}）`);
  }

  // POST /api/jobs — create the queued job (HEAD-verified server-side).
  async createJob(body: CreateJobBody): Promise<JobView> {
    const res = await this.fetchFn(`${this.baseUrl}/api/jobs`, {
      method: "POST",
      headers: this.headers(true),
      body: JSON.stringify(body),
    });
    return (await this.parse<{ job: JobView }>(res)).job;
  }

  // GET /api/jobs/:id — owner-scoped status poll.
  async getJob(jobId: string): Promise<JobView> {
    const res = await this.fetchFn(`${this.baseUrl}/api/jobs/${encodeURIComponent(jobId)}`, {
      headers: this.headers(false),
    });
    return (await this.parse<{ job: JobView }>(res)).job;
  }

  // GET /api/jobs/:id/download/:artifact — owner-scoped presigned GET (artifact = video | srt).
  async downloadUrl(jobId: string, artifact: "video" | "srt"): Promise<{ url: string; expires_at: number }> {
    const res = await this.fetchFn(
      `${this.baseUrl}/api/jobs/${encodeURIComponent(jobId)}/download/${artifact}`,
      { headers: this.headers(false) },
    );
    return this.parse<{ url: string; expires_at: number }>(res);
  }
}
