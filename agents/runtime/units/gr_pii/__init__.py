"""GR-PII PII Redaction — 한국형 식별번호 인식기 + 마스킹·가명화 연산자 (FR-GR-PII-01·02·05).

결과 형태는 Presidio RecognizerResult 와 같다 {entity_type, start, end, score, recognizer}. 규칙·체크섬 기반이며
이름·주소 같은 NER 엔티티(Presidio NLP 엔진)는 후속.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date


@dataclass
class Finding:
    entity_type: str
    start: int
    end: int
    score: float
    recognizer: str
    text: str = ""

    def as_dict(self) -> dict:
        return {"entity_type": self.entity_type, "start": self.start, "end": self.end, "score": round(self.score, 2), "recognizer": self.recognizer}


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _rrn_date_ok(d: str) -> bool:
    yy, mm, dd, g = int(d[:2]), int(d[2:4]), int(d[4:6]), int(d[6])
    century = {1: 1900, 2: 1900, 3: 2000, 4: 2000, 5: 1900, 6: 1900, 7: 2000, 8: 2000}.get(g)
    if century is None:
        return False
    try:
        date(century + yy, mm, dd)
        return True
    except ValueError:
        return False


def rrn_checksum(d: str) -> bool:
    """주민·외국인등록번호 검증번호 (2020-10 이후 발급분은 검증번호가 임의라 점수만 낮춘다)."""
    w = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]
    return (11 - sum(int(a) * b for a, b in zip(d[:12], w, strict=True)) % 11) % 10 == int(d[12])


def brn_checksum(d: str) -> bool:
    w = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    s = sum(int(a) * b for a, b in zip(d[:9], w, strict=True)) + int(d[8]) * 5 // 10
    return (10 - s % 10) % 10 == int(d[9])


def luhn(d: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


Validator = Callable[[str], float]   # 매칭 문자열 → 점수(0 이면 버림)


def _rrn(kind: str) -> Validator:
    def v(m: str) -> float:
        d = _digits(m)
        if len(d) != 13 or not _rrn_date_ok(d):
            return 0
        foreign = d[6] in "5678"
        if foreign != (kind == "frn"):
            return 0
        return 0.95 if rrn_checksum(d) else 0.65
    return v


RECOGNIZERS: list[tuple[str, str, re.Pattern[str], Validator, tuple[str, ...]]] = [
    ("KR_RRN", "KrRrnRecognizer", re.compile(r"(?<!\d)\d{6}[- ]?[1-4]\d{6}(?!\d)"), _rrn("rrn"), ("주민", "주민등록")),
    ("KR_FRN", "KrFrnRecognizer", re.compile(r"(?<!\d)\d{6}[- ]?[5-8]\d{6}(?!\d)"), _rrn("frn"), ("외국인",)),
    ("KR_BRN", "KrBrnRecognizer", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)|(?<!\d)\d{10}(?!\d)"),
     lambda m: 0.9 if brn_checksum(_digits(m)) else 0, ("사업자",)),
    ("KR_DRIVER_LICENSE", "KrDriverLicenseRecognizer", re.compile(r"(?<!\d)(?:1[1-9]|2[0-8])-?\d{2}-?\d{6}-?\d{2}(?!\d)"), lambda m: 0.7, ("면허",)),
    ("KR_PASSPORT", "KrPassportRecognizer", re.compile(r"\b[MSRODG](?:\d{8}|\d{3}[A-Z]\d{4})\b"), lambda m: 0.6, ("여권", "passport")),
    ("PHONE_KR", "KrPhoneRecognizer", re.compile(r"(?<!\d)(?:01[016789][- ]?\d{3,4}[- ]?\d{4}|0(?:2|[3-6][1-5])[- ]?\d{3,4}[- ]?\d{4})(?!\d)"), lambda m: 0.8, ("전화", "연락처", "휴대폰")),
    ("EMAIL_ADDRESS", "EmailRecognizer", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), lambda m: 0.9, ("메일", "email")),
    ("CREDIT_CARD", "CreditCardRecognizer", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
     lambda m: 0.9 if 13 <= len(_digits(m)) <= 19 and luhn(_digits(m)) else 0, ("카드",)),
    # 비밀·자격증명 (FR-GR-PII-08) — gitleaks 규칙 일부 이식 + 엔트로피. 연산자 label 고정(가명·역마스킹 금지)
    ("SECRET_PRIVATE_KEY", "PrivateKeyRecognizer", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), lambda m: 0.99, ()),
    ("SECRET_AWS_ACCESS_KEY", "AwsKeyRecognizer", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), lambda m: 0.95, ()),
    ("SECRET_GITHUB_TOKEN", "GithubTokenRecognizer", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b"), lambda m: 0.95, ()),
    ("SECRET_SLACK_TOKEN", "SlackTokenRecognizer", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), lambda m: 0.95, ()),
    ("SECRET_OPENAI_KEY", "OpenAiKeyRecognizer", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"), lambda m: 0.9 if _entropy(m) >= 3.5 else 0, ()),
    ("SECRET_JWT", "JwtRecognizer", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), lambda m: 0.9, ()),
    ("SECRET_GENERIC", "GenericSecretRecognizer",
     re.compile(r"(?i)\b(?:api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token|token)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{12,})"),
     lambda m: 0.85 if _entropy(re.split(r"[:=]\s*['\"]?", m, maxsplit=1)[-1]) >= 3.3 else 0, ()),
]


def _entropy(s: str) -> float:
    from collections import Counter
    from math import log2
    c = Counter(s)
    return -sum(n / len(s) * log2(n / len(s)) for n in c.values()) if s else 0.0


def analyze(text: str, entities: list[str] | None = None, threshold: float = 0.5) -> list[Finding]:
    found: list[Finding] = []
    for etype, rec, rx, validate, context in RECOGNIZERS:
        if entities and etype not in entities:
            continue
        for m in rx.finditer(text):
            score = validate(m.group(0))
            if not score:
                continue
            window = text[max(0, m.start() - 20):m.start()].lower()
            if any(c in window for c in context):         # 문맥어 가점
                score = min(1.0, score + 0.1)
            if score >= threshold:
                found.append(Finding(etype, m.start(), m.end(), score, rec, m.group(0)))
    found.sort(key=lambda f: (f.start, -(f.end - f.start), -f.score))
    kept: list[Finding] = []
    for f in found:                                        # 겹치면 먼저(길고 점수 높은) 것만
        if kept and f.start < kept[-1].end:
            continue
        kept.append(f)
    return kept


DEFAULT_OPERATORS = {"KR_RRN": "mask", "KR_FRN": "mask", "KR_BRN": "mask", "KR_DRIVER_LICENSE": "mask", "KR_PASSPORT": "mask",
                     "PHONE_KR": "mask", "EMAIL_ADDRESS": "replace", "CREDIT_CARD": "mask"}
SECRET_PREFIX = "SECRET_"


class Pseudonymizer:
    """일관 가명 — 같은 세션에서 같은 원문은 같은 토큰 <KR_RRN_1> (HMAC 세션 키)."""

    def __init__(self, session_key: bytes):
        self.key, self.tokens, self.counts = session_key, {}, {}

    def token(self, etype: str, value: str) -> str:
        h = hmac.new(self.key, f"{etype}|{value}".encode(), hashlib.sha256).hexdigest()
        if h not in self.tokens:
            self.counts[etype] = self.counts.get(etype, 0) + 1
            self.tokens[h] = f"<{etype}_{self.counts[etype]}>"
        return self.tokens[h]


def operate(op: str, f: Finding, *, salt: str, pseudo: Pseudonymizer) -> str:
    v = f.text
    if f.entity_type.startswith(SECRET_PREFIX) or op == "label":   # 비밀은 원문 재사용 금지 — 정책으로도 바꿀 수 없다
        return f"<{f.entity_type}>"
    if op == "redact":
        return ""
    if op == "hash":
        return "<" + f.entity_type + ":" + hashlib.sha256((salt + v).encode()).hexdigest()[:16] + ">"
    if op == "replace":
        return pseudo.token(f.entity_type, v)
    # mask: 구조는 남기고 뒤쪽을 * — 주민번호는 생년월일·성별 자리만 남긴다
    if f.entity_type in ("KR_RRN", "KR_FRN"):
        d = _digits(v)
        return f"{d[:6]}-{d[6]}******"
    keep = 4 if f.entity_type in ("CREDIT_CARD", "PHONE_KR") else 2
    chars = list(v)
    idx = [i for i, c in enumerate(chars) if c.isalnum()]
    for i in idx[:-keep] if len(idx) > keep else idx:
        chars[i] = "*"
    return "".join(chars)


def anonymize(text: str, findings: list[Finding], *, operators: dict[str, str] | None = None, salt: str = "",
              pseudo: Pseudonymizer | None = None) -> str:
    ops = {**DEFAULT_OPERATORS, **(operators or {})}
    pseudo = pseudo or Pseudonymizer(salt.encode() or b"atlas")
    out, last = [], 0
    for f in sorted(findings, key=lambda x: x.start):
        out += [text[last:f.start], operate(ops.get(f.entity_type, "mask"), f, salt=salt, pseudo=pseudo)]
        last = f.end
    out.append(text[last:])
    return "".join(out)
