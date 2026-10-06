// JSON Schema → 설치 폼 필드 (FR-X-MKT-02, react-jsonschema-form 의 최소 부분집합: string·enum·number·integer·boolean · 비밀은 password)
export type JSONSchema = {
  type?: string; title?: string; required?: string[]; default?: unknown; enum?: unknown[]; format?: string; pattern?: string;
  properties?: Record<string, JSONSchema>; "x-atlas-secret"?: boolean;
};

export type Field = {
  name: string; label: string; kind: "text" | "password" | "select" | "number" | "checkbox";
  required: boolean; options: string[]; initial: string | boolean; pattern?: string;
};

export function fieldsOf(schema: JSONSchema, uiSchema: Record<string, { "ui:widget"?: string }> = {}): Field[] {
  const req = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {}).map(([name, p]) => {
    const secret = uiSchema[name]?.["ui:widget"] === "password" || p.format === "password" || p["x-atlas-secret"] === true;
    const kind: Field["kind"] = p.enum ? "select" : p.type === "boolean" ? "checkbox" : p.type === "number" || p.type === "integer" ? "number" : secret ? "password" : "text";
    const initial = kind === "checkbox" ? Boolean(p.default) : p.default === undefined ? "" : String(p.default);
    return { name, label: p.title ?? name, kind, required: req.has(name), options: (p.enum ?? []).map(String), initial, pattern: p.pattern };
  });
}

/** 폼 값 → 스키마 타입으로 변환. 빈 선택 항목은 보내지 않는다(서버 기본값 적용). 필수 누락은 errors 로. */
export function coerce(fields: Field[], values: Record<string, string | boolean>): { params: Record<string, unknown>; errors: string[] } {
  const params: Record<string, unknown> = {};
  const errors: string[] = [];
  for (const f of fields) {
    const v = values[f.name];
    if (f.kind === "checkbox") { params[f.name] = Boolean(v); continue; }
    const s = typeof v === "string" ? v.trim() : "";
    if (!s) { if (f.required) errors.push(`${f.label} 필수`); continue; }
    if (f.pattern && !new RegExp(f.pattern).test(s)) { errors.push(`${f.label} 형식 (${f.pattern})`); continue; }
    if (f.kind === "number") {
      const n = Number(s);
      if (Number.isNaN(n)) { errors.push(`${f.label} 숫자`); continue; }
      params[f.name] = n;
    } else params[f.name] = s;
  }
  return { params, errors };
}
