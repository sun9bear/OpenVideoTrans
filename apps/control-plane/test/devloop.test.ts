import { afterEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/core";
import { mediaDelete, mediaHead } from "../src/media";
import { presignR2Url } from "../src/sigv4";
import { makeEnv } from "./helpers/bindings";

// DEVLOOP (#25): the R2_S3_ENDPOINT override that lets the control plane presign for, and HEAD/delete
// against, a LOCAL S3 stub instead of the real R2 host — the seam that makes `just dev` work with no
// cloud. ABSENT (prod) every object op is byte-identical to before; these assert both branches.

const PRESIGN_BASE = {
  accountId: "acct-dev",
  bucket: "ovt-media",
  key: "uploads/us_abc",
  accessKeyId: "AKIDEV",
  secretAccessKey: "secret-dev",
  now: 1_700_000_000_000,
  expiresSec: 900,
} as const;

describe("presignR2Url endpoint override", () => {
  it("targets the real R2 host when no endpoint is given (prod)", async () => {
    const url = await presignR2Url({ method: "PUT", ...PRESIGN_BASE });
    expect(
      url.startsWith("https://acct-dev.r2.cloudflarestorage.com/ovt-media/uploads/us_abc?"),
    ).toBe(true);
    expect(url).toContain("X-Amz-Signature=");
  });

  it("targets the local stub (scheme+host) path-style when endpoint is set (dev)", async () => {
    const url = await presignR2Url({
      method: "GET",
      ...PRESIGN_BASE,
      endpoint: "http://127.0.0.1:9000",
    });
    expect(url.startsWith("http://127.0.0.1:9000/ovt-media/uploads/us_abc?")).toBe(true);
    // Still a real signed URL — the path/host are signed, the stub just ignores the signature.
    expect(url).toContain("X-Amz-Signature=");
    expect(url).toContain("X-Amz-Credential=");
  });
});

describe("mediaHead / mediaDelete dev branch (R2_S3_ENDPOINT)", () => {
  afterEach(() => vi.unstubAllGlobals());

  function devEnv(): Env {
    // MEDIA must be present for the type but must NOT be touched on the dev path — make it throw.
    const media = new Proxy(
      {},
      {
        get() {
          throw new Error("MEDIA binding must not be used when R2_S3_ENDPOINT is set");
        },
      },
    );
    return {
      DB: {} as Env["DB"],
      MEDIA: media as Env["MEDIA"],
      CONFIG: {} as Env["CONFIG"],
      R2_BUCKET: "ovt-media",
      R2_S3_ENDPOINT: "http://127.0.0.1:9000",
    } as Env;
  }

  it("HEAD returns size + content-type from the stub", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(null, {
            status: 200,
            headers: { "content-length": "2048", "content-type": "video/mp4" },
          }),
      ),
    );
    const head = await mediaHead(devEnv(), "uploads/us_abc");
    expect(head).toEqual({ size: 2048, contentType: "video/mp4" });
    expect(fetch).toHaveBeenCalledWith("http://127.0.0.1:9000/ovt-media/uploads/us_abc", {
      method: "HEAD",
    });
  });

  it("HEAD returns null for a missing object (404)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 404 })));
    expect(await mediaHead(devEnv(), "uploads/missing")).toBeNull();
  });

  it("delete issues a DELETE to the stub", async () => {
    const f = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", f);
    await mediaDelete(devEnv(), "uploads/us_abc");
    expect(f).toHaveBeenCalledWith("http://127.0.0.1:9000/ovt-media/uploads/us_abc", {
      method: "DELETE",
    });
  });
});

describe("mediaHead / mediaDelete prod branch (MEDIA binding)", () => {
  it("uses the R2 binding when no endpoint is set", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true });
    r2.putSized("uploads/us_abc", 4096, "video/quicktime");
    expect(await mediaHead(env, "uploads/us_abc")).toEqual({
      size: 4096,
      contentType: "video/quicktime",
    });
    await mediaDelete(env, "uploads/us_abc");
    expect(r2.has("uploads/us_abc")).toBe(false);
    expect(await mediaHead(env, "uploads/us_abc")).toBeNull();
  });
});
