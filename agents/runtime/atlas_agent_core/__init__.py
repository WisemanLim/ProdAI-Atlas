"""atlas_agent_core — Atlas Agent Spec 공통 런타임 (PRD: prodai-agent-core, FR-A2A-SRV-01).

단위 에이전트는 `AgentSpec` 하나를 선언하고 `create_app(spec)` 으로 A2A v1.0 서버
(Agent Card · JSON-RPC /a2a · /healthz · /readyz)가 된다.
"""
from atlas_agent_core.server import create_app
from atlas_agent_core.spec import AgentSpec, InputRequired, Skill, TaskContext

__all__ = ["AgentSpec", "InputRequired", "Skill", "TaskContext", "create_app"]
