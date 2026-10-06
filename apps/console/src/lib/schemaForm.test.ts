import { describe, expect, it } from "vitest";
import { coerce, fieldsOf } from "./schemaForm";
import { errorText } from "./api";

const schema = {
  type: "object", required: ["agent_name"],
  properties: {
    agent_name: { type: "string", title: "에이전트 이름", pattern: "^[a-z][a-z0-9-]{2,40}$" },
    tone: { type: "string", enum: ["formal", "friendly"], default: "formal" },
    api_key: { type: "string", format: "password", "x-atlas-secret": true },
    top_k: { type: "integer", default: 5 },
    rerank: { type: "boolean" },
  },
};

describe("설치 폼 (X-MKT-02)", () => {
  it("스키마 → 위젯 (enum=select · 비밀=password · 숫자 · 체크)", () => {
    const f = fieldsOf(schema, { api_key: { "ui:widget": "password" } });
    expect(f.map((x) => [x.name, x.kind, x.required])).toEqual([
      ["agent_name", "text", true], ["tone", "select", false], ["api_key", "password", false], ["top_k", "number", false], ["rerank", "checkbox", false]]);
    expect(f[1].initial).toBe("formal");
  });

  it("값 변환·필수·패턴 검사", () => {
    const f = fieldsOf(schema);
    expect(coerce(f, { agent_name: "", tone: "formal" }).errors).toEqual(["에이전트 이름 필수"]);
    expect(coerce(f, { agent_name: "X!" }).errors[0]).toContain("형식");
    expect(coerce(f, { agent_name: "faq-bot", top_k: "3", rerank: true, api_key: "" }).params).toEqual({ agent_name: "faq-bot", top_k: 3, rerank: true });
  });

  it("FastAPI 오류 본문을 사람이 읽게", () => {
    expect(errorText({ detail: { message: "공급망 검증 실패", verification: { reasons: ["서명 없음"] } } }, 403)).toBe("공급망 검증 실패 — 서명 없음");
    expect(errorText({ detail: { message: "파라미터 오류", errors: [{ path: "agent_name", message: "bad" }] } }, 422)).toBe("파라미터 오류 — agent_name: bad");
    expect(errorText({ detail: "없음" }, 404)).toBe("없음");
    expect(errorText({}, 500)).toBe("HTTP 500");
  });
});
