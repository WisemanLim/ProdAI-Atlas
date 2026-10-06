// 개요 맵 (SCR-B03) — 원본 도식 3 레이어 × 번호 카드, 카드를 누르면 개념 화면으로
import { useMemo } from "react";
import { fetchAgents, fetchDeployments, fetchGatewayUp, fetchUnits } from "../lib/api";
import { buildMap, coverage, type Chip, type ChipStatus } from "../lib/map";
import { svcGet } from "../lib/api";
import { gateLabel, phaseIndex, type Band } from "../lib/maturity";
import { CARD_SECTION, type Section } from "../lib/router";
import { ErrorLine, STATUS_LABEL, StatusDot, useData } from "../ui";

function UnitChip({ chip }: { chip: Chip }) {
  return (
    <li className={`chip s-${chip.status}`} title={`${chip.unitId} · ${STATUS_LABEL[chip.status]}`}>
      <StatusDot status={chip.status} />
      <span className="chip-name">{chip.name}</span>
      <span className="chip-id">{chip.unitId}</span>
    </li>
  );
}

export function Overview({ go }: { go: (s: Section) => void }) {
  const { data, error } = useData(
    async () => {
      const [units, agents, deps, gw] = await Promise.all([fetchUnits(), fetchAgents(), fetchDeployments(), fetchGatewayUp()]);
      return buildMap(units, agents, deps, gw);
    }, [], 5000);
  const layers = useMemo(() => data ?? [], [data]);
  const cov = coverage(layers);
  const band = useData(() => svcGet<Band>("mat", "/v1/band"), [], 30000);
  const here = band.data ? phaseIndex(band.data.stage) : 1;
  const BAND = [["SURFACE PROMPTS", "One-off chats. Short-term thinking."], ["PRODUCTION SYSTEMS", "Reliable, observable, cost-aware, safe."],
                ["COMPOUND ADVANTAGE", "Better decisions. Lower cost. Faster wins."]];
  return (
    <>
      <div className="title-row">
        <h1>개요 맵 — The map behind production AI systems</h1>
        <div className="meta"><span>가동 단위 <b>{cov.live}</b> / {cov.total}</span><span>5초 갱신</span></div>
      </div>
      <ErrorLine error={error} />
      <ul className="legend" aria-label="상태 범례">
        {(["ready", "service", "starting", "degraded", "failed", "none"] as ChipStatus[]).map((s) => <li key={s}><StatusDot status={s} /> {STATUS_LABEL[s]}</li>)}
      </ul>
      {layers.map((layer, i) => (
        <section key={layer.id} className={`layer ${layer.id}`} aria-labelledby={`layer-${layer.id}`}>
          <div className="layer-head">
            <span className="layer-tag">LAYER {i + 1}</span>
            <h2 id={`layer-${layer.id}`}>{layer.title}</h2>
            <span className="layer-tagline">{layer.tagline}</span>
          </div>
          <div className="cards">
            {layer.cards.map((card) => {
              const live = card.chips.filter((c) => c.status === "ready" || c.status === "service").length;
              return (
                <button type="button" key={card.concept} className={`card${card.supplement ? " supplement" : ""}`} onClick={() => go(CARD_SECTION[card.concept] ?? "overview")}>
                  <header><span className="no">{card.no}</span><h3>{card.title}</h3><span className="adopt">{live > 0 ? "● 도입" : "○ 미도입"}</span></header>
                  <p className="tagline">{card.tagline}</p>
                  <ul className="chips">{card.chips.map((c) => <UnitChip key={c.unitId} chip={c} />)}</ul>
                </button>
              );
            })}
          </div>
        </section>
      ))}
      <section className="band" aria-label="진화 띠">
        {BAND.map(([t, d], i) => (
          <div key={t} className={i === here ? "here" : i < here ? "done" : ""}>
            <b>{t}</b><span>{d}</span>
            {i === here && band.data && <span className="marker" aria-label="현재 위치">▲ L{band.data.stage} {band.data.name} · {band.data.progress_pct}%</span>}
          </div>
        )).flatMap((el, i) => i ? [<span key={`a${i}`} className="arrow">→</span>, el] : [el])}
        {band.data && (
          <button type="button" className="band-gates" onClick={() => go("mat")} aria-label="다음 단계 미달 조건">
            {band.data.next ? <>다음 {band.data.next}: {band.data.unmet_gates.map((g) => <span key={g} className="chip-gate">{gateLabel(g)}</span>)}</> : "최고 단계"}
          </button>
        )}
      </section>
    </>
  );
}
