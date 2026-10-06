"""수집·인덱싱 파이프라인 (FR-X-KB-01·02): 원천 커넥터 Source.list()/fetch() → parse → chunk → embed(Gateway alias) → index.

커넥터: upload(API 업로드) · corpus(번들/마운트 디렉터리) · url(HTTP, 허용 호스트만). 문서 해시(sha256) 로 변경 감지·중복 제거.
문서 앞머리(front matter) `acl:` · `subjects:` · `title:` 가 ACL 태그·삭제권 주체 색인이 된다.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from atlas_agent_core import otel

TRANSPORT: httpx.AsyncBaseTransport | None = None   # 시험용
CORPUS_ROOT = Path(__file__).with_name("corpus")
_FM = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse(content: str, mime: str = "text/markdown") -> dict[str, Any]:
    """텍스트 추출 + 앞머리 메타데이터. html 은 태그 제거 (Docling 등 외부 파서는 후속)."""
    meta: dict[str, Any] = {}
    m = _FM.match(content)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        content = content[m.end():]
    if "html" in mime:
        content = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", content)
        content = html.unescape(re.sub(r"<[^>]+>", " ", content))
    content = re.sub(r"[ \t]+", " ", content).strip()
    title = str(meta.get("title") or next((ln.lstrip("# ").strip() for ln in content.splitlines() if ln.strip()), "")[:200])
    return {"text": content, "title": title, "acl": [str(x) for x in meta.get("acl") or []], "subjects": [str(x) for x in meta.get("subjects") or []]}


def chunk(text: str, strategy: str = "paragraph", size: int = 800, overlap: int = 100) -> list[str]:
    """청킹 전략 (kb.chunk.strategy): fixed(길이·겹침) · paragraph(빈 줄 경계, size 까지 병합) · heading(마크다운 제목 단위)."""
    text = text.strip()
    if not text:
        return []
    if strategy == "fixed":
        step = max(1, size - overlap)
        return [text[i:i + size] for i in range(0, len(text), step) if text[i:i + size].strip()]
    if strategy == "heading":                   # 절 하나 = 청크 하나 (긴 절만 고정 길이로)
        out = []
        for p in (p.strip() for p in re.split(r"(?m)^(?=#{1,6} )", text)):
            if p:
                out += [p] if len(p) <= size else chunk(p, "fixed", size, overlap)
        return out
    else:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf = [], ""
    for p in parts:
        if len(p) > size:                      # 큰 덩어리는 고정 길이로 쪼갠다
            if buf:
                out.append(buf)
                buf = ""
            out += chunk(p, "fixed", size, overlap)
        elif len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out


async def embed(texts: list[str], model: str = "embed-default", dims: int = 256) -> list[list[float]]:
    if not texts:
        return []
    gw = os.environ.get("ATLAS_GATEWAY_URL", "http://gateway:8080").rstrip("/")
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60, transport=TRANSPORT) as c:
        for i in range(0, len(texts), 128):
            res = await c.post(f"{gw}/v1/embeddings", json={"model": model, "input": texts[i:i + 128], "dimensions": dims},
                               headers=otel.inject({"Authorization": f"Bearer {os.environ.get('ATLAS_GATEWAY_KEY', '')}",
                                                    "x-atlas-tags": "agent_id:kb,unit_id:X-KB"}))
            res.raise_for_status()
            out += [d["embedding"] for d in sorted(res.json()["data"], key=lambda d: d["index"])]
    return out


# ---- 원천 커넥터 ----

def allowed_url(raw: str) -> bool:
    """SSRF 차단: http(s) · 허용 호스트(ATLAS_KB_URL_ALLOW_HOSTS) 는 통과, 그 외 사설·루프백·링크로컬 주소는 거부."""
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    allow = {h.strip() for h in os.environ.get("ATLAS_KB_URL_ALLOW_HOSTS", "").split(",") if h.strip()}
    if u.hostname in allow:
        return True
    try:
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                return False
    except (socket.gaierror, ValueError):
        return False
    return u.scheme == "https"


async def source_list(src) -> list[dict[str, Any]]:
    """Source.list() — [{uri, mime}] (변경 판단은 fetch 후 sha256 비교)."""
    cfg = src.config
    if src.type == "corpus":
        root = (CORPUS_ROOT / cfg.get("path", "")).resolve()
        if CORPUS_ROOT.resolve() not in (root, *root.parents):
            raise ValueError("corpus 경로는 번들 디렉터리 안만")
        return [{"uri": f"corpus://{p.relative_to(CORPUS_ROOT)}", "path": str(p), "mime": "text/html" if p.suffix == ".html" else "text/markdown"}
                for p in sorted(root.rglob("*")) if p.suffix in (".md", ".txt", ".html") and p.is_file()]
    if src.type == "url":
        return [{"uri": u, "mime": "text/html"} for u in cfg.get("urls", [])]
    return []


async def source_fetch(src, item: dict[str, Any]) -> str:
    if src.type == "corpus":
        return Path(item["path"]).read_text(encoding="utf-8")
    if src.type == "url":
        if not allowed_url(item["uri"]):
            raise PermissionError(f"허용되지 않은 URL(SSRF 차단): {item['uri']}")
        async with httpx.AsyncClient(timeout=20, transport=TRANSPORT, follow_redirects=False) as c:
            res = await c.get(item["uri"])
        res.raise_for_status()
        item["mime"] = res.headers.get("content-type", item["mime"])
        return res.text[:2_000_000]
    raise ValueError(src.type)
