// Zero-dependency AWS SigV4 query-string presigner for the Cloudflare R2 S3 API, built on Web Crypto
// (available identically in the Workers runtime and in Node 18+/vitest). It signs a time-limited
// PUT (direct browser upload) or GET (artifact download) URL scoped to one bucket/key. The R2 access
// keys are wrangler secrets injected at deploy; this module only ever receives them as arguments and
// never logs them. Size is NOT constrained by the presign — the cap is enforced by the HEAD-after-PUT
// check on the object (see uploads.ts).

const enc = new TextEncoder();

function hex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

export async function sha256Hex(data: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", enc.encode(data));
  return hex(new Uint8Array(digest));
}

async function hmac(key: ArrayBuffer | Uint8Array, msg: string): Promise<ArrayBuffer> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return crypto.subtle.sign("HMAC", cryptoKey, enc.encode(msg));
}

export async function hmacHex(key: string, msg: string): Promise<string> {
  return hex(new Uint8Array(await hmac(enc.encode(key), msg)));
}

// RFC 3986 / AWS canonical URI encoding. Slashes in a path are preserved (encodeSlash=false);
// everything in a query value is encoded (encodeSlash=true).
function uriEncode(str: string, encodeSlash: boolean): string {
  let out = "";
  for (const ch of str) {
    if (/[A-Za-z0-9\-_.~]/.test(ch)) {
      out += ch;
    } else if (ch === "/") {
      out += encodeSlash ? "%2F" : "/";
    } else {
      for (const b of enc.encode(ch)) {
        out += "%" + b.toString(16).toUpperCase().padStart(2, "0");
      }
    }
  }
  return out;
}

function amzDates(now: number): { amzDate: string; dateStamp: string } {
  const d = new Date(now);
  const pad = (n: number): string => String(n).padStart(2, "0");
  const amzDate =
    `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;
  return { amzDate, dateStamp: amzDate.slice(0, 8) };
}

export interface PresignOpts {
  method: "GET" | "PUT";
  accountId: string;
  bucket: string;
  key: string;
  accessKeyId: string;
  secretAccessKey: string;
  now: number;
  expiresSec: number;
  region?: string;
}

export async function presignR2Url(o: PresignOpts): Promise<string> {
  const region = o.region ?? "auto";
  const service = "s3";
  const host = `${o.accountId}.r2.cloudflarestorage.com`;
  const { amzDate, dateStamp } = amzDates(o.now);
  const credentialScope = `${dateStamp}/${region}/${service}/aws4_request`;
  const canonicalUri = `/${uriEncode(o.bucket, false)}/${uriEncode(o.key, false)}`;

  const params: Record<string, string> = {
    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
    "X-Amz-Credential": `${o.accessKeyId}/${credentialScope}`,
    "X-Amz-Date": amzDate,
    "X-Amz-Expires": String(o.expiresSec),
    "X-Amz-SignedHeaders": "host",
  };
  const canonicalQuery = Object.keys(params)
    .sort()
    .map((k) => `${uriEncode(k, true)}=${uriEncode(params[k]!, true)}`)
    .join("&");

  const canonicalRequest = [
    o.method,
    canonicalUri,
    canonicalQuery,
    `host:${host}\n`,
    "host",
    "UNSIGNED-PAYLOAD",
  ].join("\n");

  const stringToSign = [
    "AWS4-HMAC-SHA256",
    amzDate,
    credentialScope,
    await sha256Hex(canonicalRequest),
  ].join("\n");

  const kDate = await hmac(enc.encode(`AWS4${o.secretAccessKey}`), dateStamp);
  const kRegion = await hmac(kDate, region);
  const kService = await hmac(kRegion, service);
  const kSigning = await hmac(kService, "aws4_request");
  const signature = hex(new Uint8Array(await hmac(kSigning, stringToSign)));

  return `https://${host}${canonicalUri}?${canonicalQuery}&X-Amz-Signature=${signature}`;
}
