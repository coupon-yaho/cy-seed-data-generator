#!/usr/bin/env python3
"""배분 계획 회귀 테스트 — DB 없이 수 초 안에 끝난다.

생성 계획(회차별 발급 수 · 상태 배분)은 `as_of` 의 함수다. 날짜가 바뀌면
만료 가능한 회차 집합이 달라지고, 그러면 상태 배분의 제약 구조가 통째로 바뀐다.
**어제 통과한 코드가 오늘 실패할 수 있다는 뜻이다.** 실제로 그렇게 한 번 깨졌다.

그래서 여러 날짜 × 여러 시드로 계획 단계만 돌려 불변식을 확인한다.
전체 생성(수 분)을 돌리기 전에 이걸 먼저 통과시킨다.

    python bin/plancheck.py                 # 기본: 4개월 × 3시드 × 2데이터셋
    python bin/plancheck.py --days 365 --step 1
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seedgen import config as C  # noqa: E402
from seedgen.catalog import build_catalog  # noqa: E402


def check_one(dataset: str, scale: float, seed: int, as_of: dt.datetime) -> list[str]:
    tag = f"{dataset} {as_of:%Y-%m-%d} seed={seed}"
    profile = C.Profile(dataset=dataset, scale=scale, seed=seed)
    try:
        cat = build_catalog(profile, as_of)
    except Exception as exc:  # noqa: BLE001
        return [f"{tag}: {type(exc).__name__}: {exc}"]

    problems: list[str] = []
    past = cat.past
    total = sum(c.issue_count for c in past)

    if total != profile.issuances:
        problems.append(f"{tag}: 총 발급 {total:,} != 목표 {profile.issuances:,}")

    for c in past:
        if sum(c.status_counts.values()) != c.issue_count:
            problems.append(f"{tag}: 회차 {c.id} 상태 합 != 발급 수")
        if c.issue_count > c.total_quantity:
            problems.append(f"{tag}: 회차 {c.id} 발급 {c.issue_count} > 재고 {c.total_quantity}")
        # 만료 불가 회차에 EXPIRED 가 배정되면 asOf 이후 이력을 써야 한다
        if not c.expirable and c.status_counts.get(C.EXPIRED):
            problems.append(
                f"{tag}: 회차 {c.id} 만료 불가인데 EXPIRED {c.status_counts[C.EXPIRED]}건"
            )

    for status in C.STATUSES:
        got = sum(c.status_counts[status] for c in past)
        want = round(total * C.STATUS_MIX[status])
        if abs(got - want) > 1:
            problems.append(f"{tag}: {status} {got:,} != 목표 {want:,}")

    if not any(c.sellout for c in past):
        problems.append(f"{tag}: 완판 회차가 0개 — '잔여재고 > 0' 분기만 실행된다")
    if all(c.sellout for c in past):
        problems.append(f"{tag}: 전부 완판 — '잔여재고 > 0' 분기가 실행되지 않는다")

    return problems


def main() -> int:
    p = argparse.ArgumentParser(description="배분 계획 불변식 회귀 테스트")
    p.add_argument("--days", type=int, default=120, help="오늘부터 며칠치 as_of 를 볼 것인가")
    p.add_argument("--step", type=int, default=3, help="며칠 간격으로")
    p.add_argument("--seeds", type=int, nargs="+", default=[20260812, 7, 999])
    args = p.parse_args()

    base = dt.datetime.now().replace(microsecond=0)
    cases = [("clean", 1.0), ("corrupt", 0.2)]
    problems: list[str] = []
    checked = 0

    for dataset, scale in cases:
        for offset in range(0, args.days, args.step):
            for seed in args.seeds:
                checked += 1
                problems += check_one(
                    dataset, scale, seed, base + dt.timedelta(days=offset)
                )

    print(f"검사 {checked}건 ({args.days}일 / {args.step}일 간격 × 시드 {len(args.seeds)}개 × 2 데이터셋)")
    if problems:
        print(f"❌ 실패 {len(problems)}건")
        for line in problems[:20]:
            print(f"   {line}")
        if len(problems) > 20:
            print(f"   … 외 {len(problems) - 20}건")
        return 1
    print("✅ 전부 통과 — 총 발급 수 · 행 합 · 열 합 · 재고 상한 · 만료 제약 · 완판 혼재")
    return 0


if __name__ == "__main__":
    sys.exit(main())
