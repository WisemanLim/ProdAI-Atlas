"""평가 통계 (FR-EV-JUDGE-05 · EV-SCORE-03·05) — Cohen κ · 부트스트랩 CI · McNemar 정확 검정 · EWMA 변화점."""
from __future__ import annotations

import math
import random
from collections.abc import Sequence


def cohen_kappa(a: Sequence[bool], b: Sequence[bool]) -> float:
    """이진 라벨 두 벌의 일치도 κ = (po - pe) / (1 - pe). 완전 일치·한쪽 상수면 po 기준."""
    n = len(a)
    if n == 0:
        return 0.0
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if pe == 1 else round((po - pe) / (1 - pe), 4)


def mcnemar(b: int, c: int) -> float:
    """대응표본 McNemar 정확(이항) 양측 p — b = pass→fail, c = fail→pass."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 6)


def bootstrap_diff_ci(base: Sequence[float], new: Sequence[float], iters: int = 1000, seed: int = 7) -> tuple[float, float]:
    """대응 항목 (base_i, new_i) 을 복원 추출해 평균 차(new - base) 의 95% CI. 재현 가능하도록 시드 고정."""
    n = len(base)
    if n == 0:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    diffs = sorted(sum(new[j] - base[j] for j in (rnd.randrange(n) for _ in range(n))) / n for _ in range(iters))
    return (round(diffs[int(0.025 * iters)], 4), round(diffs[int(0.975 * iters) - 1], 4))


def ewma_drift(values: Sequence[float], baseline: int = 20, lam: float = 0.3, L: float = 3.0) -> dict:
    """앞 baseline 개로 기준(평균·표준편차)을 잡고 이후 EWMA 가 관리 한계를 넘는 첫 지점을 변화점으로 본다."""
    if len(values) <= baseline or baseline < 2:
        return {"drift": False, "reason": "표본 부족", "n": len(values)}
    ref = values[:baseline]
    mu = sum(ref) / baseline
    sd = math.sqrt(sum((v - mu) ** 2 for v in ref) / (baseline - 1)) or 0.05   # 상수 기준선은 최소 산포 가정
    limit = L * sd * math.sqrt(lam / (2 - lam))
    z, at = mu, None
    series = []
    for i, v in enumerate(values[baseline:], start=baseline):
        z = lam * v + (1 - lam) * z
        series.append(round(z, 4))
        if at is None and abs(z - mu) > limit:
            at = i
    return {"drift": at is not None, "at_index": at, "baseline_mean": round(mu, 4), "limit": round(limit, 4),
            "last_ewma": series[-1], "direction": None if at is None else ("down" if series[-1] < mu else "up"), "n": len(values)}
