import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, type Uploader } from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("ApiClient", () => {
  it("signUpload posts to /api/uploads/sign with the anon header + json body", async () => {
    const fetchFn = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ upload_session_id: "us_1", source_key: "uploads/us_1", put_url: "https://r2/put", expires_at: 1 }),
    );
    const api = new ApiClient("", "anon_abc", fetchFn as unknown as typeof fetch);
    const r = await api.signUpload(2048, "video/mp4");
    expect(r.upload_session_id).toBe("us_1");
    const [url, init] = fetchFn.mock.calls[0]!;
    expect(url).toBe("/api/uploads/sign");
    expect(init!.method).toBe("POST");
    expect((init!.headers as Record<string, string>)["X-OVT-Anon-Id"]).toBe("anon_abc");
    expect(JSON.parse(init!.body as string)).toEqual({ declared_bytes: 2048, declared_type: "video/mp4" });
  });

  it("normalizes a trailing slash in the API base so routes are not doubled", async () => {
    const fetchFn = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ upload_session_id: "us", source_key: "k", put_url: "u", expires_at: 1 }),
    );
    const api = new ApiClient("https://cp.example/", "anon_abc", fetchFn as unknown as typeof fetch);
    await api.signUpload(1, "video/mp4");
    expect(fetchFn.mock.calls[0]![0]).toBe("https://cp.example/api/uploads/sign"); // not //api
  });

  it("createJob returns the job and sends the full body", async () => {
    const fetchFn = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ job: { job_id: "job_1", status: "queued", output_mode: "subtitle_only", error_code: null, artifacts: {} } }),
    );
    const api = new ApiClient("https://cp.test", "anon_abc", fetchFn as unknown as typeof fetch);
    const job = await api.createJob({
      upload_session_id: "us_1",
      target_lang: "zh-Hans",
      output_mode: "subtitle_only",
      subtitle_delivery: "srt",
      subtitle_lang: "target",
    });
    expect(job.job_id).toBe("job_1");
    expect(fetchFn.mock.calls[0]![0]).toBe("https://cp.test/api/jobs");
  });

  it("getJob / downloadUrl issue owner-scoped GETs with the anon header", async () => {
    const fetchFn = vi.fn(async (url: string, _init?: RequestInit) =>
      url.includes("/download/")
        ? jsonResponse({ url: "https://r2/get", expires_at: 9 })
        : jsonResponse({ job: { job_id: "job_1", status: "done", output_mode: "subtitle_only", error_code: null, artifacts: { srt_key: "k" } } }),
    );
    const api = new ApiClient("", "anon_abc", fetchFn as unknown as typeof fetch);
    expect((await api.getJob("job_1")).status).toBe("done");
    expect((await api.downloadUrl("job_1", "srt")).url).toBe("https://r2/get");
    expect(fetchFn.mock.calls[1]![0]).toBe("/api/jobs/job_1/download/srt");
    expect((fetchFn.mock.calls[0]![1]!.headers as Record<string, string>)["X-OVT-Anon-Id"]).toBe("anon_abc");
  });

  it("throws ApiError carrying the server error code on a non-2xx", async () => {
    const fetchFn = vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({ error: { code: "daily_cap_reached", message: "额度已用尽" } }, 429));
    const api = new ApiClient("", "anon_abc", fetchFn as unknown as typeof fetch);
    await expect(api.getJob("job_1")).rejects.toMatchObject({ code: "daily_cap_reached", status: 429 });
  });

  it("putSource delegates to the (injected) uploader, forwards progress, and throws on failure", async () => {
    const okUp = vi.fn<Uploader>(async (_url, _body, _ct, onProgress) => {
      onProgress?.(0.5);
      onProgress?.(1);
    });
    const seen: number[] = [];
    const api = new ApiClient("", "anon_abc", undefined, okUp);
    await api.putSource("https://r2/put", new Blob(["x"]), "video/mp4", (f) => seen.push(f));
    expect(okUp.mock.calls[0]![0]).toBe("https://r2/put");
    expect(okUp.mock.calls[0]![2]).toBe("video/mp4");
    expect(seen).toEqual([0.5, 1]); // progress fractions are forwarded to the caller

    const badUp: Uploader = async () => {
      throw new ApiError(403, "upload_put_failed", "直传失败（403）");
    };
    const api2 = new ApiClient("", "anon_abc", undefined, badUp);
    await expect(api2.putSource("https://r2/put", new Blob(["x"]), "video/mp4")).rejects.toMatchObject({
      code: "upload_put_failed",
    });
  });
});
