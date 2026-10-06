// 공용 UI 조각 — 상태 점·패널·표·확인 모달·비동기 데이터 훅
import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { ChipStatus } from "./lib/map";

export const STATUS_LABEL: Record<ChipStatus, string> = {
  ready: "가동", service: "서비스", starting: "기동 중", pending: "대기", degraded: "경고", failed: "실패", stopped: "정지", none: "미배포",
};

export function StatusDot({ status }: { status: ChipStatus | string }) {
  return <span className={`dot s-${status}`} aria-hidden="true" />;
}

export function Panel({ title, sub, children, actions }: { title: string; sub?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}{sub && <small>{sub}</small>}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function ErrorLine({ error }: { error: string | null }) {
  return error ? <p className="alert" role="alert">{error}</p> : null;
}

/** 데이터 로드 + 주기 갱신. deps 가 바뀌면 다시 부른다. */
export function useData<T>(load: () => Promise<T>, deps: unknown[], everyMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(() => load().then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message)), deps);
  useEffect(() => {
    run();
    if (!everyMs) return;
    const t = setInterval(run, everyMs);
    return () => clearInterval(t);
  }, [run, everyMs]);
  return { data, error, reload: run };
}

export function ConfirmModal({ text, onOk, onCancel }: { text: string; onOk: () => void; onCancel: () => void }) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
      <div className="modal">
        <h2 id="confirm-title">운영(prod) 환경 확인</h2>
        <p>{text}</p>
        <div className="row">
          <button type="button" onClick={onCancel}>취소</button>
          <button type="button" className="danger" onClick={onOk} autoFocus>계속</button>
        </div>
      </div>
    </div>
  );
}

export function fmtTime(iso?: string) {
  return iso ? new Date(iso).toLocaleString() : "—";
}
