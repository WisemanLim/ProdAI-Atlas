"""X-RPT 서버 렌더 — CSV · 최소 PDF(표·막대, 외부 의존 없음) · 월간 보고서 HTML(인라인 SVG 차트 4종)."""
from __future__ import annotations

import csv
import html
import io
from typing import Any


def to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _ascii(s: Any) -> str:
    t = str(s).encode("ascii", "replace").decode()      # 기본 14 글꼴(Helvetica)은 ASCII 만 — 한글 이름은 '?' (원문은 CSV·HTML)
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def to_pdf(title: str, rows: list[dict[str, Any]], bars: list[tuple[str, float]] | None = None) -> bytes:
    """한 쪽짜리 PDF 1.4 — 제목·표(최대 40행)·선택 막대 차트."""
    ops = ["BT /F1 14 Tf 40 800 Td (" + _ascii(title) + ") Tj ET"]
    y = 775
    if rows:
        cols = list(rows[0].keys())
        ops.append(f"BT /F1 8 Tf 40 {y} Td (" + _ascii(" | ".join(cols)) + ") Tj ET")
        for r in rows[:40]:
            y -= 12
            ops.append(f"BT /F1 8 Tf 40 {y} Td (" + _ascii(" | ".join(str(r[c]) for c in cols)) + ") Tj ET")
    if bars:
        y -= 30
        mx = max((v for _, v in bars), default=0) or 1
        for label, v in bars[:20]:
            y -= 14
            w = 300 * v / mx
            ops.append(f"0.2 0.4 0.8 rg 160 {y} {w:.1f} 9 re f 0 g")
            ops.append(f"BT /F1 8 Tf 40 {y + 1} Td (" + _ascii(label)[:30] + ") Tj ET")
    stream = "\n".join(ops).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def svg_line(points: list[tuple[str, float | None]], w: int = 320, h: int = 120) -> str:
    vals = [v for _, v in points if v is not None]
    if not vals:
        return f'<svg width="{w}" height="{h}" role="img"><text x="10" y="60">데이터 없음</text></svg>'
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = max(1, len(points) - 1)
    pts = " ".join(f"{10 + i * (w - 20) / n:.1f},{h - 15 - (v - lo) / span * (h - 30):.1f}" for i, (_, v) in enumerate(points) if v is not None)
    return (f'<svg width="{w}" height="{h}" role="img"><polyline fill="none" stroke="#2563eb" stroke-width="2" points="{pts}"/>'
            f'<text x="10" y="{h - 2}" font-size="10">{html.escape(points[0][0])}</text><text x="{w - 70}" y="{h - 2}" font-size="10">{html.escape(points[-1][0])}</text></svg>')


def svg_gauge(value: float | None, label: str, w: int = 320, h: int = 60) -> str:
    v = 0 if value is None else max(0.0, min(1.0, value))
    return (f'<svg width="{w}" height="{h}" role="img"><rect x="10" y="20" width="{w - 20}" height="16" fill="#e5e7eb"/>'
            f'<rect x="10" y="20" width="{(w - 20) * v:.1f}" height="16" fill="#16a34a"/>'
            f'<text x="10" y="54" font-size="11">{html.escape(label)}: {"—" if value is None else round(value, 4)}</text></svg>')


def monthly_html(d: dict[str, Any]) -> str:
    c = d["charts"]
    cards = [("비용/과업 추세 (cost_per_task)", svg_line([(p["day"], p["value"]) for p in c["cost_per_task"]])),
             ("과업 성공률 (task_success_rate)", svg_gauge(c["task_success_rate"], "성공률")),
             ("가드레일 차단율 (guardrail_block_rate)", svg_gauge(c["guardrail_block_rate"], "차단율")),
             ("MAT 성숙도 점수", svg_gauge((c["mat_score"] or 0) / 100 if c["mat_score"] is not None else None, f"L{c.get('mat_stage', '?')} 점수"))]
    s = d["summary"]
    body = "".join(f'<section><h2>{html.escape(t)}</h2>{svg}</section>' for t, svg in cards)
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>ProdAI Atlas 월간 보고서 {html.escape(d["month"])}</title>'
            '<style>body{font-family:system-ui,sans-serif;margin:24px;color:#111}section{display:inline-block;width:340px;vertical-align:top;margin:8px;'
            'padding:8px;border:1px solid #ddd;border-radius:6px}h2{font-size:14px}</style></head><body>'
            f'<h1>월간 비용·품질·안전·성숙도 — {html.escape(d["tenant"])}/{html.escape(d["project"])} {html.escape(d["month"])}</h1>'
            f'<p>총비용 ${s["cost_usd"]:.4f} · 요청 {s["requests"]} · 과업 {s["tasks"]} · 이상 탐지 {s["anomalies"]}건</p>{body}'
            '<p><small>"Better decisions. Lower cost. Faster wins." — X-RPT-06</small></p></body></html>')
