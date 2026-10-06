import { describe, expect, it } from "vitest";
import { decodeJwt, pkcePair } from "./auth";

describe("console SSO (PKCE)", () => {
  it("S256 challenge = base64url(sha256(verifier)), 길이 43", async () => {
    const { verifier, challenge } = await pkcePair();
    expect(verifier).toMatch(/^[A-Za-z0-9_-]{64}$/);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    const expected = btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    expect(challenge).toBe(expected);
    expect(challenge).toHaveLength(43);
  });
  it("JWT 페이로드 디코드 (서명 검증은 Manager 몫)", () => {
    const payload = btoa(JSON.stringify({ preferred_username: "alice", atlas_org: "acme" })).replace(/=+$/, "");
    expect(decodeJwt(`x.${payload}.y`)).toMatchObject({ preferred_username: "alice", atlas_org: "acme" });
    expect(decodeJwt("garbage")).toEqual({});
  });
});
