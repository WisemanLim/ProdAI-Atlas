"""X-MKT 공급망 검증 (FR-X-MKT-03) — 번들 digest · ed25519 서명 · SLSA provenance(in-toto Statement) · CycloneDX SBOM · 정적 지시 스캔.

서명 대상 = "atlas-template/v1|sha256:<bundle>|sha256:<provenance>|sha256:<sbom>" — 서명 하나로 세 산출물을 묶는다(cosign attest 와 같은 역할).
cosign·Rekor 투명성 로그는 오프라인 환경이라 쓰지 않고, 신뢰 키 목록(플랫폼 키 + 관리자가 등록한 게시자 키)으로 검증한다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PLATFORM_BUILDER = "https://prodai.io/atlas/mkt-builder"
PROVENANCE_TYPE = "https://slsa.dev/provenance/v1"


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def bundle_digest(manifest: dict[str, Any], files: dict[str, str]) -> str:
    return sha({"manifest": manifest, "files": files})


def statement(name: str, version: str, digest: str, builder: str = PLATFORM_BUILDER, source: str = "bundled") -> dict[str, Any]:
    return {"_type": "https://in-toto.io/Statement/v1", "subject": [{"name": f"{name}@{version}", "digest": {"sha256": digest}}],
            "predicateType": PROVENANCE_TYPE,
            "predicate": {"buildDefinition": {"buildType": "https://prodai.io/atlas-template/v1", "externalParameters": {"source": source}},
                          "runDetails": {"builder": {"id": builder}}}}


def signed_message(digest: str, provenance: dict[str, Any] | None, sbom: dict[str, Any] | None) -> bytes:
    return f"atlas-template/v1|sha256:{digest}|sha256:{sha(provenance or {})}|sha256:{sha(sbom or {})}".encode()


def pub_pem(key: Ed25519PublicKey) -> str:
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def kid_of(pem: str) -> str:
    return "ed25519-" + hashlib.sha256(pem.encode()).hexdigest()[:12]


def load_or_create(path: Path) -> Ed25519PrivateKey:
    """플랫폼 서명 키 — 데이터 볼륨에 0600 으로 1회 생성 (운영은 KMS/OpenBao Transit 로 교체 지점)."""
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), None)  # type: ignore[return-value]
    path.parent.mkdir(parents=True, exist_ok=True)
    k = Ed25519PrivateKey.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return k


def sign(key: Ed25519PrivateKey, digest: str, provenance: dict[str, Any] | None, sbom: dict[str, Any] | None) -> dict[str, str]:
    pem = pub_pem(key.public_key())
    return {"kid": kid_of(pem), "alg": "ed25519", "sig": base64.b64encode(key.sign(signed_message(digest, provenance, sbom))).decode()}


def verify(digest: str, signature: dict[str, Any] | None, provenance: dict[str, Any] | None, sbom: dict[str, Any] | None,
           trusted: dict[str, str], builders: list[str], blocked: dict[str, str], require: bool) -> dict[str, Any]:
    """설치 전 검증 결과 {ok, signature, provenance, sbom, reasons[]} — require=false 면 서명 없음만 허용(있으면 여전히 검증)."""
    reasons: list[str] = []
    sig_state = "missing"
    if signature:
        pem = trusted.get(signature.get("kid", ""))
        if pem is None:
            sig_state = "untrusted_key"
            reasons.append(f"신뢰 목록에 없는 서명 키 {signature.get('kid')}")
        else:
            try:
                key = serialization.load_pem_public_key(pem.encode())
                key.verify(base64.b64decode(signature.get("sig", "")), signed_message(digest, provenance, sbom))  # type: ignore[union-attr]
                sig_state = "valid"
            except (InvalidSignature, ValueError):
                sig_state = "invalid"
                reasons.append("서명 불일치 — 번들·provenance·SBOM 중 변조")
    elif require:
        reasons.append("서명 없음 (mkt.require_signature=true)")
    prov_state = "missing"
    if provenance:
        subj = (provenance.get("subject") or [{}])[0].get("digest", {}).get("sha256")
        builder = ((provenance.get("predicate") or {}).get("runDetails") or {}).get("builder", {}).get("id")
        if provenance.get("predicateType") != PROVENANCE_TYPE:
            prov_state = "bad_type"
        elif subj != digest:
            prov_state = "subject_mismatch"
        elif builder not in builders:
            prov_state = "untrusted_builder"
        else:
            prov_state = "valid"
        if prov_state != "valid":
            reasons.append(f"SLSA provenance {prov_state} (builder {builder})")
    elif require:
        reasons.append("SLSA provenance 없음")
    vulns = []
    if sbom:
        for c in sbom.get("components") or []:
            key = f"{c.get('name')}@{c.get('version')}"
            if key in blocked:
                vulns.append({"component": key, "advisory": blocked[key]})
        if vulns:
            reasons.append(f"취약 구성요소 {[v['component'] for v in vulns]}")
    elif require:
        reasons.append("SBOM 없음 (첨부 필수)")
    return {"ok": not reasons, "signature": sig_state, "provenance": prov_state, "sbom": "present" if sbom else "missing", "vulnerabilities": vulns, "reasons": reasons}


def texts(obj: Any) -> list[str]:
    """정적 스캔 대상 — 템플릿 안의 모든 문자열(시스템 프롬프트·스킬 설명·파일)."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [t for v in obj.values() for t in texts(v)]
    if isinstance(obj, list):
        return [t for v in obj for t in texts(v)]
    return []
