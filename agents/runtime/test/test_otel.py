"""OB-TRACE — 수신 traceparent 를 부모로 invoke_agent span, Gateway 호출 헤더에 traceparent 주입."""
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from atlas_agent_core import otel

PARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
_exporter = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def provider():
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(_exporter))
    trace.set_tracer_provider(tp)
    yield


def test_invoke_agent_span_joins_caller_trace(client):
    _exporter.clear()
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"tenant": "org-t", "message": {"parts": [{"text": "추적되는 목표"}]}}}
    res = client.post("/a2a", json=body, headers={"traceparent": PARENT}).json()
    assert res["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    spans = [s for s in _exporter.get_finished_spans() if s.name == "invoke_agent al-plan"]
    assert len(spans) == 1
    sp = spans[0]
    assert format(sp.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert format(sp.parent.span_id, "016x") == "00f067aa0ba902b7"
    assert sp.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert sp.attributes["atlas.tenant"] == "org-t" and sp.attributes["a2a.task.state"] == "TASK_STATE_COMPLETED"


def test_without_traceparent_joins_current_trace(client):
    _exporter.clear()
    with otel.tracer().start_as_current_span("http-server") as outer:
        ctx = otel.extract({"content-type": "application/json"})
    from opentelemetry import trace as t
    assert t.get_current_span(ctx).get_span_context().trace_id == outer.get_span_context().trace_id


def test_inject_writes_traceparent_from_current_span():
    with otel.tracer().start_as_current_span("x") as sp:
        headers = otel.inject({})
    assert headers["traceparent"].split("-")[1] == format(sp.get_span_context().trace_id, "032x")
