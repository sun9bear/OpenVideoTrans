import { describe, expect, it } from "vitest";
import { signAnonId, verifyAnonId } from "../src/anon";
import { handle } from "../src/router";
import { call, makeClock, makeEnv } from "./helpers/bindings";

// M2-CLOSE PR-B (#26) — server-side anon-id HMAC: mint + verify, the dev-accept / prod-fail-closed
// posture, and BASE-id ownership (stable across a key rollout, SEC-2).

const KEY = "test-hmac-key";
const SIGN_BODY = { declared_bytes: 1000, declared_type: "video/mp4" } as const;

describe("anon HMAC sign/verify", () => {
  it("round-trips: a signed id verifies to its base", async () => {
    const signed = await signAnonId(KEY, "anon_abc");
    expect(signed.startsWith("anon_abc.")).toBe(true);
    expect(await verifyAnonId(KEY, signed)).toBe("anon_abc");
  });

  it("rejects forged / wrong-key / unsigned / malformed ids", async () => {
    const signed = await signAnonId(KEY, "anon_abc");
    expect(await verifyAnonId(KEY, "anon_abc.deadbeef")).toBeNull(); // bad signature
    expect(await verifyAnonId("other-key", signed)).toBeNull(); // wrong key
    expect(await verifyAnonId(KEY, "anon_abc")).toBeNull(); // unsigned (no separator)
    expect(await verifyAnonId(KEY, ".sig")).toBeNull(); // empty base
    expect(await verifyAnonId(KEY, "anon_abc.")).toBeNull(); // empty signature
  });
});

describe("POST /api/anon (mint)", () => {
  it("mints a signed id + Set-Cookie (Secure in prod) when the key is configured", async () => {
    const { env } = makeEnv({ anonHmacKey: KEY, ovtEnv: "prod" });
    const { deps } = makeClock(1000);
    const res = await handle(new Request("https://cp.test/api/anon", { method: "POST" }), env, deps);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { anon_id: string };
    expect(await verifyAnonId(KEY, body.anon_id)).toBe("anon_00000000"); // verifies to the minted base
    const cookie = res.headers.get("set-cookie") ?? "";
    expect(cookie).toContain("ovt_anon=");
    expect(cookie).toContain("SameSite=Strict");
    expect(cookie).toContain("Secure"); // prod
  });

  it("mints an UNSIGNED id when no key (dev posture)", async () => {
    const { env } = makeEnv();
    const { deps } = makeClock(1000);
    const res = await handle(new Request("https://cp.test/api/anon", { method: "POST" }), env, deps);
    const body = (await res.json()) as { anon_id: string };
    expect(body.anon_id).toBe("anon_00000000"); // bare base, no signature
  });
});

describe("getActor posture (via an actor-authed route)", () => {
  it("key set: a valid signed actor passes; forged + unsigned ids 401", async () => {
    const { env } = makeEnv({ r2Creds: true, anonHmacKey: KEY });
    const { deps } = makeClock(1000);
    const signed = await signAnonId(KEY, "anon_u1");
    expect((await call(env, deps, "POST", "/api/uploads/sign", { actor: signed, body: SIGN_BODY })).status).toBe(200);
    expect((await call(env, deps, "POST", "/api/uploads/sign", { actor: "anon_u1.deadbeef", body: SIGN_BODY })).status).toBe(401);
    expect((await call(env, deps, "POST", "/api/uploads/sign", { actor: "anon_u1", body: SIGN_BODY })).status).toBe(401);
  });

  it("ownership keys on the BASE id, stable across the key rollout (SEC-2)", async () => {
    const { env, r2 } = makeEnv({ r2Creds: true, anonHmacKey: KEY });
    const { deps } = makeClock(1000);
    const signed = await signAnonId(KEY, "anon_owner");
    const s = await call(env, deps, "POST", "/api/uploads/sign", { actor: signed, body: SIGN_BODY });
    r2.putSized((s.json as { source_key: string }).source_key, 2048);
    const created = await call(env, deps, "POST", "/api/jobs", {
      actor: signed,
      body: {
        upload_session_id: (s.json as { upload_session_id: string }).upload_session_id,
        target_lang: "zh-Hans",
        output_mode: "subtitle_only",
        subtitle_delivery: "srt",
        subtitle_lang: "target",
      },
    });
    expect(created.status).toBe(201);
    expect(created.json.job.anon_or_user_id).toBe("anon_owner"); // stored owner is the BASE, not base.sig
    const jobId = created.json.job.job_id;
    expect((await call(env, deps, "GET", `/api/jobs/${jobId}`, { actor: signed })).status).toBe(200);
    // a different base cannot see it (ownership is the base, and a different id verifies to a different base)
    const other = await signAnonId(KEY, "anon_other");
    expect((await call(env, deps, "GET", `/api/jobs/${jobId}`, { actor: other })).status).toBe(404);
  });

  it("no key + OVT_ENV=prod -> 503 fail-closed (a misconfigured prod never forges identities)", async () => {
    const { env } = makeEnv({ r2Creds: true, ovtEnv: "prod" });
    const { deps } = makeClock(1000);
    const r = await call(env, deps, "POST", "/api/uploads/sign", { actor: "anon_u1", body: SIGN_BODY });
    expect(r.status).toBe(503);
    expect(r.json.error.code).toBe("anon_unconfigured");
  });

  it("no key + OVT_ENV omitted (forgotten deploy var) -> 503 fail-closed BY DEFAULT (CodeX R1 #4)", async () => {
    const { env } = makeEnv({ r2Creds: true, ovtEnv: "none" }); // neither the key nor OVT_ENV set
    const { deps } = makeClock(1000);
    const r = await call(env, deps, "POST", "/api/uploads/sign", { actor: "anon_u1", body: SIGN_BODY });
    expect(r.status).toBe(503); // default is fail-closed, NOT raw-accept
    expect(r.json.error.code).toBe("anon_unconfigured");
  });

  it("no key + explicit OVT_ENV=dev -> raw id accepted (DEVLOOP / tests opt-in only)", async () => {
    const { env } = makeEnv({ r2Creds: true, ovtEnv: "dev" });
    const { deps } = makeClock(1000);
    expect((await call(env, deps, "POST", "/api/uploads/sign", { actor: "anon_raw", body: SIGN_BODY })).status).toBe(200);
  });

  it("rotation overlap: an id signed with the PREVIOUS key still verifies (zero-downtime, CodeX R5)", async () => {
    const PREV = "old-hmac-key";
    const { env } = makeEnv({ r2Creds: true, anonHmacKey: KEY, anonHmacKeyPrevious: PREV });
    const { deps } = makeClock(1000);
    // an id minted under the OLD key (before the rotation) still validates against the previous key ...
    const oldSigned = await signAnonId(PREV, "anon_old");
    expect((await call(env, deps, "POST", "/api/uploads/sign", { actor: oldSigned, body: SIGN_BODY })).status).toBe(200);
    // ... and a current-key id validates too.
    const newSigned = await signAnonId(KEY, "anon_new");
    expect((await call(env, deps, "POST", "/api/uploads/sign", { actor: newSigned, body: SIGN_BODY })).status).toBe(200);
  });

  it("rotation complete: with no previous key configured, an old-key id no longer verifies (401)", async () => {
    const { env } = makeEnv({ r2Creds: true, anonHmacKey: KEY }); // previous dropped
    const { deps } = makeClock(1000);
    const oldSigned = await signAnonId("old-hmac-key", "anon_old");
    expect((await call(env, deps, "POST", "/api/uploads/sign", { actor: oldSigned, body: SIGN_BODY })).status).toBe(401);
  });
});
