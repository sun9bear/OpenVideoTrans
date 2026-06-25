import { describe, expect, it } from "vitest";
import { resolveUploadType, unsupportedTypeWarning } from "./mime";

describe("mime — resolveUploadType", () => {
  it("passes an allowlisted browser MIME through", () => {
    expect(resolveUploadType({ name: "a.mp4", type: "video/mp4" })).toBe("video/mp4");
  });

  it("infers an allowlisted type by extension when the browser MIME is blank (.mkv on Firefox)", () => {
    expect(resolveUploadType({ name: "movie.mkv", type: "" })).toBe("video/x-matroska");
    expect(resolveUploadType({ name: "clip.avi", type: "" })).toBe("video/x-msvideo");
  });

  it("infers by extension when the browser MIME is octet-stream (treated as indeterminate)", () => {
    expect(resolveUploadType({ name: "clip.mov", type: "application/octet-stream" })).toBe("video/quicktime");
  });

  it("normalizes a non-canonical OS MIME alias to the canonical type by extension", () => {
    // .avi reported as "video/avi" / .m4a as "audio/x-m4a" -> the server allowlist only has the
    // canonical types, so the extension mapping must win to avoid a 415 for a supported file.
    expect(resolveUploadType({ name: "clip.avi", type: "video/avi" })).toBe("video/x-msvideo");
    expect(resolveUploadType({ name: "song.m4a", type: "audio/x-m4a" })).toBe("audio/mp4");
  });

  it("trusts a concrete browser MIME for an UNKNOWN extension (server is authoritative)", () => {
    // a runtime-expanded server allowlist (CFG-GUARD) may accept this; the client must not hard-block
    expect(resolveUploadType({ name: "x.3gp", type: "video/3gpp" })).toBe("video/3gpp");
  });

  it("returns null only when no declared_type can be formed (blank type + unknown extension)", () => {
    expect(resolveUploadType({ name: "noext", type: "" })).toBeNull();
    expect(resolveUploadType({ name: "data.bin", type: "application/octet-stream" })).toBeNull();
  });
});

describe("mime — unsupportedTypeWarning", () => {
  it("warns only when no declared_type can be formed (the sole hard gate)", () => {
    expect(unsupportedTypeWarning({ name: "movie.mkv", type: "" })).toBeNull();
    expect(unsupportedTypeWarning({ name: "clip.3gp", type: "video/3gpp" })).toBeNull(); // server decides
    expect(unsupportedTypeWarning({ name: "noext", type: "" })).toContain("无法识别");
  });
});
