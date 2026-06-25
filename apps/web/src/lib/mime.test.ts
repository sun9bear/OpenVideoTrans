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

  it("infers by extension when the browser MIME is octet-stream (not allowlisted)", () => {
    expect(resolveUploadType({ name: "clip.mov", type: "application/octet-stream" })).toBe("video/quicktime");
  });

  it("returns null for a genuinely unsupported file", () => {
    expect(resolveUploadType({ name: "doc.pdf", type: "application/pdf" })).toBeNull();
    expect(resolveUploadType({ name: "noext", type: "" })).toBeNull();
  });
});

describe("mime — unsupportedTypeWarning", () => {
  it("warns only when the format cannot be resolved", () => {
    expect(unsupportedTypeWarning({ name: "movie.mkv", type: "" })).toBeNull();
    expect(unsupportedTypeWarning({ name: "doc.pdf", type: "application/pdf" })).toContain("暂不支持");
  });
});
