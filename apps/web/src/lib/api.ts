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

// Upload progress fraction in [0, 1], reported as bytes stream out.
export type UploadProgress = (fraction: number) => void;

// The direct-to-R2 PUT uploader. Split out (and injectable) because fetch() CANNOT report upload
// progress — only XMLHttpRequest exposes upload.onprogress. The default is the real XHR impl; tests
// inject a fake so the flow stays network-free. Resolves on a 2xx, rejects with ApiError otherwise
// (matching the prior fetch-based error contract: code "upload_put_failed").
export type Uploader = (
  url: string,
  body: Blob,
  contentType: string,
  onProgress?: UploadProgress,
) => Promise<void>;

const xhrUpload: Uploader = (url, body, contentType, onProgress) =>
  new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("content-type", contentType);
    if (onProgress) {
      xhr.upload.onprogress = (e: ProgressEvent) => {
        if (e.lengthComputable && e.total > 0) onProgress(Math.min(1, e.loaded / e.total));
      };
    }
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new ApiError(xhr.status, "upload_put_failed", `直传失败（${xhr.status}）`));
    xhr.onerror = () => reject(new ApiError(0, "upload_put_failed", "直传失败（网络错误）"));
    xhr.ontimeout = () => reject(new ApiError(0, "upload_put_failed", "直传超时"));
    xhr.send(body);
  });

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
    // Injectable so tests stay network-free; the default is the real XHR uploader (progress-capable).
    private readonly uploadFn: Uploader = xhrUpload,
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
  // Delegates to the XHR uploader so `onProgress` can report the completion fraction as bytes stream
  // out (fetch cannot); rejects with ApiError("upload_put_failed") on a non-2xx / network error.
  async putSource(
    putUrl: string,
    file: Blob,
    contentType: string,
    onProgress?: UploadProgress,
  ): Promise<void> {
    await this.uploadFn(putUrl, file, contentType, onProgress);
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
