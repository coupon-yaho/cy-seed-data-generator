"""실행 매니페스트 — 무엇을 어떤 시드로 만들었는지 남긴다.

배치가 뽑은 dataset_fingerprint 와 대조할 수 있어야 "두 run 이 같은 데이터를 봤는가"를
사람이 확인할 수 있다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import sys


def build(profile, as_of: dt.datetime, catalog, totals, meta: dict,
          counts: dict, corrupt_summary: dict | None, extra: dict) -> dict:
    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generator": {
            "python": platform.python_version(),
            "argv": " ".join(sys.argv),
        },
        "profile": {
            "dataset": profile.dataset,
            "scale": profile.scale,
            "seed": profile.seed,
            "seed_run_id": profile.seed_run_id,
            "members": profile.members,
            "issuances_target": profile.issuances,
            "past_months": profile.past_months,
            "coupons": len(catalog.coupons),
        },
        "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "row_counts": counts,
        "distribution": {
            "status": dict(sorted(totals.status.items())),
            "active_usages": totals.active_usages,
            "sum_active_count": sum(totals.active_count.values()),
            "sellout_coupons": sum(1 for c in catalog.past if c.sellout),
            "expiring_soon_coupons": sorted(catalog.expiring_coupon_ids),
        },
        "determinism": meta,
        "corruption": corrupt_summary,
        **extra,
    }


def write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
