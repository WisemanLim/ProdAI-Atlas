"""레드팀 회귀 (FR-GR-JB-07) — 사용: python -m units.gr_jb.redteam [--threshold 0.6]  → JSON 보고, 기준 초과 시 exit 1."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from units.gr_jb import classify

SUITE = Path(__file__).with_name("redteam.yaml")


def evaluate(threshold: float = 0.6) -> dict:
    suite = yaml.safe_load(SUITE.read_text(encoding="utf-8"))
    missed = [a for a in suite["attacks"] if classify(a).score < threshold]
    false_pos = [b for b in suite["benign"] if classify(b).score >= threshold]
    asr, fpr = len(missed) / len(suite["attacks"]), len(false_pos) / len(suite["benign"])
    t = suite["thresholds"]
    h_missed = [a for a in suite.get("holdout_attacks", []) if classify(a).score < threshold]
    h_fp = [b for b in suite.get("holdout_benign", []) if classify(b).score >= threshold]
    holdout = {"asr": round(len(h_missed) / max(1, len(suite.get("holdout_attacks", []))), 3),
               "fpr": round(len(h_fp) / max(1, len(suite.get("holdout_benign", []))), 3), "missed": h_missed, "false_positives": h_fp}
    v2m = [a for a in suite.get("holdout_v2_attacks", []) if classify(a).score < threshold]
    v2f = [b for b in suite.get("holdout_v2_benign", []) if classify(b).score >= threshold]
    holdout_v2 = {"asr": round(len(v2m) / max(1, len(suite.get("holdout_v2_attacks", []))), 3),
                  "fpr": round(len(v2f) / max(1, len(suite.get("holdout_v2_benign", []))), 3), "missed": v2m, "false_positives": v2f}
    return {"holdout": holdout, "holdout_v2": holdout_v2, "attacks": len(suite["attacks"]), "benign": len(suite["benign"]), "asr": round(asr, 3), "fpr": round(fpr, 3),
            "max_asr": t["max_asr"], "max_fpr": t["max_fpr"], "pass": asr <= t["max_asr"] and fpr <= t["max_fpr"],
            "missed": missed, "false_positives": false_pos}


if __name__ == "__main__":
    th = float(sys.argv[sys.argv.index("--threshold") + 1]) if "--threshold" in sys.argv else 0.6
    report = evaluate(th)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["pass"] else 1)
