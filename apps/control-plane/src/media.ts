import type { Env } from "./core";

// DEVLOOP (#25) object-store seam. In prod the control plane reads/deletes objects through the R2
// `MEDIA` binding (Miniflare-local under wrangler, the real bucket in prod). For the local dev loop
// there is no R2 binding the external worker + browser can also reach, so when env.R2_S3_ENDPOINT is
// set these route to the SAME local S3 stub the presigned upload/download URLs target — giving one
// object store all three actors (browser PUT, worker GET/PUT, control-plane HEAD/delete) agree on.
//
// Absent R2_S3_ENDPOINT (prod / tests) ⇒ the MEDIA binding, byte-identical to before. Only HEAD +
// delete are routed (verifyUpload's needs); bulk bytes never flow through the control plane — the
// browser and worker hit the stub directly, exactly mirroring the prod S3 data plane.

export interface MediaHead {
  size: number;
  contentType: string | undefined;
}

function devObjectUrl(env: Env, key: string): string {
  const base = env.R2_S3_ENDPOINT!.replace(/\/+$/, "");
  const bucket = env.R2_BUCKET ?? "ovt-media";
  // Path-style {bucket}/{key}, matching the presigned URLs (sigv4.ts) and the worker's S3 client.
  return `${base}/${bucket}/${key}`;
}

// HEAD an object: its size + declared content-type, or null if absent (404). Mirrors the subset of
// R2Object that verifyUpload uses (size + httpMetadata.contentType).
export async function mediaHead(env: Env, key: string): Promise<MediaHead | null> {
  if (env.R2_S3_ENDPOINT) {
    const res = await fetch(devObjectUrl(env, key), { method: "HEAD" });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`dev S3 HEAD failed: ${res.status}`);
    const len = res.headers.get("content-length");
    return {
      size: len === null ? 0 : Number(len),
      contentType: res.headers.get("content-type") ?? undefined,
    };
  }
  const obj = await env.MEDIA.head(key);
  if (!obj) return null;
  return { size: obj.size, contentType: obj.httpMetadata?.contentType };
}

// Delete an object (idempotent — deleting a gone key is a no-op on both paths).
export async function mediaDelete(env: Env, key: string): Promise<void> {
  if (env.R2_S3_ENDPOINT) {
    await fetch(devObjectUrl(env, key), { method: "DELETE" });
    return;
  }
  await env.MEDIA.delete(key);
}
