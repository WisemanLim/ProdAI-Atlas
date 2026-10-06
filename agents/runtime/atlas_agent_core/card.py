"""Agent Card 생성 (A2A v1.0 필수 필드 — FR-A2A-CARD-01)."""
from __future__ import annotations

import os
from typing import Any

from atlas_agent_core.spec import AgentSpec

PROTOCOL_VERSION = "1.0"


def build_card(spec: AgentSpec, public_url: str) -> dict[str, Any]:
    base = public_url.rstrip("/")
    md = spec.manifest["metadata"]
    return {
        "name": spec.name,
        "description": spec.description,
        "version": spec.version,
        "supportedInterfaces": [
            {"url": f"{base}{it.get('path', '/a2a')}", "protocolBinding": it["binding"], "protocolVersion": PROTOCOL_VERSION}
            for it in spec.manifest["spec"]["interfaces"]
        ],
        "provider": {"organization": "ProdAI Atlas", "url": "https://github.com/prodai/atlas"},
        "capabilities": {"streaming": True, "pushNotifications": True, "extendedAgentCard": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": s.id, "name": s.name, "description": s.description, "tags": [md["unitId"], *s.tags],
             "examples": s.examples, "inputModes": s.input_modes, "outputModes": s.output_modes,
             **({"securityRequirements": [{"atlasTenantJwt": []}]} if s.security == "required" else {})}
            for s in spec.skills
        ],
        # 보안 스킴 (FR-A2A-SRV-05): Manager agent-proxy 가 서명한 테넌트 JWT(ES256, JWKS = Manager /.well-known/jwks.json)
        "securitySchemes": {"atlasTenantJwt": {"httpAuthSecurityScheme": {"scheme": "Bearer", "bearerFormat": "JWT",
                                                                          "description": "Manager 서명 테넌트 토큰 (atlas_org·atlas_proj·atlas_env)"}}},
        "securityRequirements": [{"atlasTenantJwt": []}] if os.environ.get("ATLAS_A2A_AUTH") == "required" else [],
        "metadata": {"atlas.unitId": md["unitId"], "atlas.layer": md.get("layer"), "atlas.concept": md.get("concept"),
                     "atlas.pushJwks": f"{base}/.well-known/jwks.json",
                     "atlas.visibility": md.get("labels", {}).get("atlas.visibility") or spec.manifest["spec"].get("agentCard", {}).get("visibility", "tenant")},
    }
