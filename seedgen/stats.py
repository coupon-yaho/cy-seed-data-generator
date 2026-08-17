"""집계 산출물 — coupon_stocks · *_stats · verification_runs · 지문/체크섬.

통계 3종은 생성 중에 이미 누적해 뒀으므로 여기서는 쓰기만 한다.
300만 행을 다시 GROUP BY 하지 않는다.

dataset_fingerprint / findings_checksum 은 배치가 같은 값을 뽑아야 의미가 있으므로
계산 규칙을 contract.json 으로 내보낸다.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from . import config as C
from .catalog import Catalog
from .issuances import Totals

FP_SEP = "|"
CK_FIELD_SEP = "\x1f"
CK_ROW_SEP = "\x1e"


def dataset_fingerprint(totals: Totals) -> tuple[str, str]:
    """ERD.sql:430-438 공식. (지문, 계산에 쓴 원문) 을 함께 돌려준다."""
    parts = [
        str(totals.histories),                      # max(issuance_histories.id)
        str(totals.histories),                      # count(issuance_histories)
        str(totals.issuances),                      # count(issuances)
        str(sum(totals.active_count.values())),     # sum(coupon_stocks.active_count)
        (totals.max_updated_at or dt.datetime(1970, 1, 1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
    ]
    raw = FP_SEP.join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), raw


def findings_checksum(pairs) -> str:
    """정렬된 (finding_type, target_key) 만 해싱한다 (ERD.sql:447)."""
    body = "".join(
        f"{ft}{CK_FIELD_SEP}{tk}{CK_ROW_SEP}" for ft, tk in sorted(set(pairs))
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def write_stocks(writers, catalog: Catalog, totals: Totals, as_of: dt.datetime) -> None:
    w = writers["coupon_stocks"]
    for c in catalog.coupons:
        active = totals.active_count.get(c.id, 0)
        w.write(c.id, c.total_quantity, active, as_of.replace(microsecond=0))


def write_catalog(writers, catalog: Catalog) -> None:
    for code, bit in catalog.grades:
        writers["grades"].write(code, bit)
    for row in catalog.brands:
        writers["brands"].write(*row)
    for row in catalog.templates:
        writers["coupon_templates"].write(*row)
    for c in catalog.coupons:
        writers["coupons"].write(*c.row())


def write_stats(writers, catalog: Catalog, totals: Totals, run_ids: list[int]) -> None:
    """coupon_stats · grade_stats · hourly_stats 를 COMPLETE run 에 붙인다."""
    cs, gs, hs = writers["coupon_stats"], writers["grade_stats"], writers["hourly_stats"]
    for run_id in run_ids:
        for c in catalog.coupons:
            # get() + continue 로 두면 회차가 조용히 빠진다. _run_coupon 의 모든 종료 경로가
            # 항목을 채우므로(issuances.py 의 발급 0건 경로와 정상 경로) 누락은 버그이고,
            # KeyError 로 즉시 드러나야 한다. cy-be 는 회차 전체에 행을 쓴다(coupons 드라이빙).
            s = totals.coupon_stats[c.id]
            cs.write(run_id, c.id, s["issued_total"], s["issued"], s["used"],
                     s["cancelled"], s["expired"], s["sold_out_seconds"])
        for (coupon_id, grade), (issued_total, used_total) in sorted(totals.grade.items()):
            gs.write(run_id, coupon_id, grade, issued_total, used_total)
        for dow in C.DOW:
            for hour in range(24):
                hs.write(run_id, dow, hour, totals.hourly.get((dow, hour), 0))


def write_verification_runs(
    writers, profile: C.Profile, as_of: dt.datetime, totals: Totals,
    findings: list | None = None,
) -> tuple[list[int], dict]:
    """완결된 과거 run 을 심는다.

    CLEAN  : FULL PASS(attempt 1) · 같은 as_of 재실행(attempt 2, 결정론 증명) ·
             INCREMENTAL 1건. finding 0건이므로 verification_findings 는 비어 있다.
    CORRUPT: 배치가 한 번 돌아 800건을 잡은 상태를 재현한다(--no-seed-corrupt-run 으로 끔).
             통계 Step 은 실행하지 않는다 (ERD.sql:589).
    """
    fp, fp_raw = dataset_fingerprint(totals)
    dataset = "CORRUPT" if profile.is_corrupt else "CLEAN"
    pairs = [(f.finding_type, f.target_key) for f in (findings or [])]
    checksum = findings_checksum(pairs)
    started = as_of
    w = writers["verification_runs"]
    complete_runs: list[int] = []

    # seed_run_id 는 "어느 정답 묶음과 대조했나" 다. CORRUPT 가 심는 run 은 이 주입이 만든
    # 정답과 대조한 실행을 재현하므로 profile.seed_run_id 를 적는다. CLEAN 은 대조 상대가
    # 없어 None 이다 — 검출 0건이 곧 통과다.
    if profile.is_corrupt:
        # --no-seed-corrupt-run 이면 findings 가 None 이라 대조가 재현되지 않는다.
        # 그때도 seed_run_id 를 적으면 "묶음 N 과 대조해 0건" 이라는 거짓 증적이 되고,
        # 검증기가 완전히 고장난 상태의 서명과 구별되지 않는다.
        compared = profile.seed_run_id if findings else None
        rows = [(1, "FULL", 1, "FAIL", "SKIPPED", len(pairs), compared)]
    else:
        rows = [
            (1, "FULL", 1, "PASS", "COMPLETE", 0, None),
            (2, "FULL", 2, "PASS", "COMPLETE", 0, None),
            (3, "INCREMENTAL", 1, "PASS", "SKIPPED", 0, None),
        ]

    for run_id, scope, attempt, verdict, stats_status, count, seed_run_id in rows:
        from_ts = as_of - dt.timedelta(days=1) if scope == "INCREMENTAL" else None
        w.write(
            run_id, as_of, from_ts, scope, dataset, seed_run_id, attempt, verdict,
            stats_status, count, checksum if count else findings_checksum([]), fp,
            started, started + dt.timedelta(minutes=4 + run_id),
        )
        if stats_status == "COMPLETE":
            complete_runs.append(run_id)

    if profile.is_corrupt and findings:
        vf = writers["verification_findings"]
        for i, f in enumerate(sorted(findings, key=lambda x: (x.finding_type, x.target_key)), 1):
            vf.write(i, 1, f.finding_type, f.target_key, f.campaign_id, f.member_id,
                     f.coupon_id, f.history_id, f.expected or "-", f.actual or "-")

    meta = {
        "dataset_fingerprint": fp,
        "dataset_fingerprint_input": fp_raw,
        "findings_checksum": checksum,
        "complete_run_ids": complete_runs,
    }
    return complete_runs, meta
