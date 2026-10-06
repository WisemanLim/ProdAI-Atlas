"""OB-TRACE SDK 계측 (FR-OB-TRACE-04·05).

`init(service, unit_id)` 한 줄로 OTLP 내보내기를 켜고(OTEL_EXPORTER_OTLP_ENDPOINT 가 있을 때만),
`extract(headers)` · `inject(headers)` 로 A2A 수신·Gateway 호출 간 W3C traceparent 를 이어 준다.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from opentelemetry import context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialized = False


def init(service: str, unit_id: str, env: str = "local") -> None:
    global _initialized
    if _initialized or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create(
        {"service.name": service, "atlas.unit_id": unit_id, "deployment.environment.name": env}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # OTEL_EXPORTER_OTLP_* env 를 따른다
    trace.set_tracer_provider(provider)
    _initialized = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("atlas_agent_core")


def extract(headers: Mapping[str, str]) -> context.Context:
    """수신 traceparent 를 부모로. 없으면 현재 문맥(HTTP 서버 span 등)을 이어 같은 trace 에 남긴다."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if "traceparent" not in lowered:
        return context.get_current()
    return propagate.extract(lowered)


def inject(headers: dict[str, str]) -> dict[str, str]:
    """현재 span 문맥을 traceparent/tracestate 로 headers 에 넣어 돌려준다."""
    propagate.inject(headers)
    return headers
