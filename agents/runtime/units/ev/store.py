"""EV 저장소 — 골든셋·항목·검수 이력(append-only)·불변 버전·평가 실행/결과 (FR-EV-GOLD-01·02·06 · EV-REG)."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Engine,
    Float,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base

DRAFT, IN_REVIEW, APPROVED, RETIRED = "draft", "in_review", "approved", "retired"
TRANSITIONS = {DRAFT: {IN_REVIEW, RETIRED}, IN_REVIEW: {APPROVED, DRAFT, RETIRED}, APPROVED: {RETIRED}, RETIRED: set()}


def now() -> datetime:
    return datetime.now(UTC)


def content_sha(input_: Any, expected: Any, rubric: Any) -> str:
    return hashlib.sha256(json.dumps({"i": input_, "e": expected, "r": rubric}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class DatasetRow(Base):
    __tablename__ = "ev_datasets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text)
    input_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ItemRow(Base):
    __tablename__ = "ev_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(36))
    input: Mapped[Any] = mapped_column(JSON)
    expected_output: Mapped[Any] = mapped_column(JSON, nullable=True)
    rubric: Mapped[Any] = mapped_column(JSON, nullable=True)
    tags: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(32))          # manual | bulk | prod-trace
    source_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LabelRow(Base):
    __tablename__ = "ev_item_labels"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(36))
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    verdict: Mapped[str | None] = mapped_column(String(8), nullable=True)
    critique: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VersionRow(Base):
    __tablename__ = "ev_dataset_versions"
    dataset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    manifest: Mapped[list] = mapped_column(JSON)               # [{item_id, sha256}]
    items: Mapped[list] = mapped_column(JSON)                  # 동결 사본
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunRow(Base):
    __tablename__ = "ev_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    suite_name: Mapped[str] = mapped_column(String(128))
    suite: Mapped[dict] = mapped_column(JSON)
    dataset_id: Mapped[str] = mapped_column(String(36))
    dataset_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))            # running | done | error
    gate: Mapped[str | None] = mapped_column(String(8), nullable=True)   # pass | fail
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResultRow(Base):
    __tablename__ = "ev_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    item_id: Mapped[str] = mapped_column(String(36))
    target: Mapped[str] = mapped_column(String(64))
    epoch: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    scores: Mapped[list] = mapped_column(JSON)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float)
    cost_usd: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class JudgeRow(Base):
    """루브릭 judge 버전 (FR-EV-JUDGE-01) — (name, version) 불변, 개정은 새 버전."""
    __tablename__ = "ev_judges"
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))              # binary | pairwise | rag
    rubric_md: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)            # 프롬프트 추가 지시
    model: Mapped[str] = mapped_column(String(128))            # Gateway alias
    jury: Mapped[list] = mapped_column(JSON)                   # 배심원 alias 목록(비면 단일)
    threshold: Mapped[float] = mapped_column(Float)            # rag faithfulness · 점수 통과 기준
    calibration: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScoreRow(Base):
    """점수 (FR-EV-SCORE-01) — 오프라인(실행)·온라인(trace)·피드백·사람 판정 공통."""
    __tablename__ = "ev_scores"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16))            # offline | online | feedback | shadow
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scorer: Mapped[str] = mapped_column(String(160))           # contains · judge:<name> · feedback:<name>
    judge_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    critique: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 재채점(shadow)용 input·output·expected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OnlineRuleRow(Base):
    """온라인 judge 샘플링 규칙 (FR-EV-JUDGE-09)."""
    __tablename__ = "ev_online_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    filter: Mapped[str] = mapped_column(Text)                  # OB 검색 DSL
    sample_rate: Mapped[float] = mapped_column(Float)
    judge: Mapped[str] = mapped_column(String(128))
    dataset: Mapped[str | None] = mapped_column(String(128), nullable=True)   # 저점 trace → 골든셋 후보 (EV-SCORE-07)
    enabled: Mapped[bool] = mapped_column(Boolean)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def s(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    # 데이터셋
    def create_dataset(self, name: str, description: str, input_schema: dict | None) -> DatasetRow:
        row = DatasetRow(id=str(uuid.uuid4()), name=name, description=description, input_schema=input_schema, created_at=now())
        with self.s() as s, s.begin():
            s.add(row)
        return row

    def dataset(self, key: str) -> DatasetRow | None:
        with self.s() as s:
            return s.get(DatasetRow, key) or s.scalars(select(DatasetRow).where(DatasetRow.name == key)).first()

    def set_schema(self, dataset_id: str, schema: dict | None) -> None:
        with self.s() as s, s.begin():
            s.get(DatasetRow, dataset_id).input_schema = schema

    def datasets(self) -> list[DatasetRow]:
        with self.s() as s:
            return list(s.scalars(select(DatasetRow).order_by(DatasetRow.name)))

    # 항목
    def add_item(self, dataset_id: str, input_: Any, expected: Any, rubric: Any, tags: list[str], source: str,
                 trace_id: str | None = None, status: str = DRAFT) -> ItemRow:
        t = now()
        row = ItemRow(id=str(uuid.uuid4()), dataset_id=dataset_id, input=input_, expected_output=expected, rubric=rubric, tags=tags,
                      status=status, source=source, source_trace_id=trace_id, content_sha256=content_sha(input_, expected, rubric),
                      created_at=t, updated_at=t)
        with self.s() as s, s.begin():
            s.add(row)
        return row

    def item(self, iid: str) -> ItemRow | None:
        with self.s() as s:
            return s.get(ItemRow, iid)

    def items(self, dataset_id: str, status: str | None = None) -> list[ItemRow]:
        q = select(ItemRow).where(ItemRow.dataset_id == dataset_id).order_by(ItemRow.created_at, ItemRow.id)
        if status:
            q = q.where(ItemRow.status == status)
        with self.s() as s:
            return list(s.scalars(q))

    def update_item(self, iid: str, **fields: Any) -> ItemRow:
        with self.s() as s, s.begin():
            row = s.get(ItemRow, iid)
            for k, v in fields.items():
                setattr(row, k, v)
            row.content_sha256 = content_sha(row.input, row.expected_output, row.rubric)
            row.updated_at = now()
            return row

    def transition(self, iid: str, to: str, actor: str, verdict: str | None, critique: str | None) -> ItemRow:
        with self.s() as s, s.begin():
            row = s.get(ItemRow, iid)
            if to not in TRANSITIONS[row.status]:
                raise ValueError(f"{row.status} → {to} 불가")
            s.add(LabelRow(id=str(uuid.uuid4()), item_id=iid, from_status=row.status, to_status=to, verdict=verdict,
                           critique=critique, actor=actor, created_at=now()))
            row.status, row.updated_at = to, now()
            return row

    def labels(self, iid: str) -> list[LabelRow]:
        with self.s() as s:
            return list(s.scalars(select(LabelRow).where(LabelRow.item_id == iid).order_by(LabelRow.created_at)))

    # 버전 (copy-on-write 동결)
    def snapshot(self, dataset_id: str) -> VersionRow:
        approved = self.items(dataset_id, APPROVED)
        frozen = [{"item_id": i.id, "input": i.input, "expected_output": i.expected_output, "rubric": i.rubric, "tags": i.tags,
                   "sha256": i.content_sha256} for i in approved]
        manifest = [{"item_id": f["item_id"], "sha256": f["sha256"]} for f in frozen]
        with self.s() as s, s.begin():
            v = (s.scalar(select(func.max(VersionRow.version)).where(VersionRow.dataset_id == dataset_id)) or 0) + 1
            row = VersionRow(dataset_id=dataset_id, version=v, manifest=manifest, items=frozen, created_at=now(),
                             manifest_sha256=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest())
            s.add(row)
            return row

    def version(self, dataset_id: str, v: int | None) -> VersionRow | None:
        q = select(VersionRow).where(VersionRow.dataset_id == dataset_id)
        q = q.where(VersionRow.version == v) if v else q.order_by(VersionRow.version.desc()).limit(1)
        with self.s() as s:
            return s.scalars(q).first()

    # 실행
    def create_run(self, suite: dict, dataset_id: str, version: int) -> RunRow:
        row = RunRow(id=str(uuid.uuid4()), suite_name=suite["name"], suite=suite, dataset_id=dataset_id, dataset_version=version,
                     status="running", created_at=now())
        with self.s() as s, s.begin():
            s.add(row)
        return row

    def add_results(self, rows: list[dict[str, Any]]) -> None:
        with self.s() as s, s.begin():
            for r in rows:
                s.add(ResultRow(id=str(uuid.uuid4()), **r))

    def finish_run(self, rid: str, status: str, gate: str | None, summary: dict) -> None:
        with self.s() as s, s.begin():
            row = s.get(RunRow, rid)
            row.status, row.gate, row.summary, row.finished_at = status, gate, summary, now()

    def run(self, rid: str) -> RunRow | None:
        with self.s() as s:
            return s.get(RunRow, rid)

    def runs(self, limit: int) -> list[RunRow]:
        with self.s() as s:
            return list(s.scalars(select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)))

    def results(self, rid: str) -> list[ResultRow]:
        with self.s() as s:
            return list(s.scalars(select(ResultRow).where(ResultRow.run_id == rid)))

    # judge (FR-EV-JUDGE-01)
    def add_judge(self, name: str, **f: Any) -> JudgeRow:
        with self.s() as s, s.begin():
            v = (s.scalar(select(func.max(JudgeRow.version)).where(JudgeRow.name == name)) or 0) + 1
            row = JudgeRow(name=name, version=v, created_at=now(), calibration=None, **f)
            s.add(row)
            return row

    def judge(self, name: str, version: int | None = None) -> JudgeRow | None:
        q = select(JudgeRow).where(JudgeRow.name == name)
        q = q.where(JudgeRow.version == version) if version else q.order_by(JudgeRow.version.desc()).limit(1)
        with self.s() as s:
            return s.scalars(q).first()

    def judges(self) -> list[JudgeRow]:
        with self.s() as s:
            return list(s.scalars(select(JudgeRow).order_by(JudgeRow.name, JudgeRow.version)))

    def set_calibration(self, name: str, version: int, cal: dict) -> None:
        with self.s() as s, s.begin():
            s.get(JudgeRow, (name, version)).calibration = cal

    # 점수 (FR-EV-SCORE-01)
    def add_scores(self, rows: list[dict[str, Any]]) -> None:
        with self.s() as s, s.begin():
            for r in rows:
                s.add(ScoreRow(id=str(uuid.uuid4()), created_at=r.pop("created_at", now()), tenant=r.pop("tenant", "default"), **r))

    def scores(self, *, scorer: str | None = None, source: str | None = None, run_id: str | None = None, trace_id: str | None = None,
               tenant: str | None = None, limit: int = 5000) -> list[ScoreRow]:
        q = select(ScoreRow)
        for col, v in ((ScoreRow.scorer, scorer), (ScoreRow.source, source), (ScoreRow.run_id, run_id), (ScoreRow.trace_id, trace_id), (ScoreRow.tenant, tenant)):
            if v is not None:
                q = q.where(col == v)
        with self.s() as s:
            return list(s.scalars(q.order_by(ScoreRow.created_at, ScoreRow.id).limit(limit)))

    # 온라인 규칙 (FR-EV-JUDGE-09)
    def add_rule(self, **f: Any) -> OnlineRuleRow:
        row = OnlineRuleRow(id=str(uuid.uuid4()), created_at=now(), last_run_at=None, **f)
        with self.s() as s, s.begin():
            s.add(row)
        return row

    def rules(self, enabled_only: bool = False) -> list[OnlineRuleRow]:
        q = select(OnlineRuleRow).order_by(OnlineRuleRow.created_at)
        if enabled_only:
            q = q.where(OnlineRuleRow.enabled.is_(True))
        with self.s() as s:
            return list(s.scalars(q))

    def rule(self, rid: str) -> OnlineRuleRow | None:
        with self.s() as s:
            return s.get(OnlineRuleRow, rid)

    def touch_rule(self, rid: str) -> None:
        with self.s() as s, s.begin():
            s.get(OnlineRuleRow, rid).last_run_at = now()

    def runs_of(self, suite: str | None = None, limit: int = 200) -> list[RunRow]:
        q = select(RunRow).where(RunRow.status == "done").order_by(RunRow.created_at).limit(limit)
        if suite:
            q = q.where(RunRow.suite_name == suite)
        with self.s() as s:
            return list(s.scalars(q))
