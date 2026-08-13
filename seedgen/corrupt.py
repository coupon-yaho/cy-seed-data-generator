"""오염 7유형 700건 주입 계획 + 정답 매니페스트(expected_findings).

핵심 원칙은 **blast radius 를 닫는 것**이다. 의도한 규칙 하나만 울리고 나머지
축은 전부 정합하게 만든다. 그러지 않으면 배치가 "심지 않은 것"까지 잡아
오탐으로 집계되고, D10 게이트(누락 0 · 오탐 0)가 깨진다.

V1 은 회차 그레인이라 유형 1 과 유형 3 이 같은 회차에 겹치면 target_key 가
충돌한다(uk_expected). 그래서 오염셋만 24개월 달력(291회차)으로 만들고
유형 1 은 앞 100회차, 유형 3 은 그다음 100회차로 **분리**한다.
"""

from __future__ import annotations

import datetime as dt

from . import config as C
from .catalog import Catalog, Coupon
from .issuances import CouponQuota

# (corrupt_type, finding_type) -> 기대 행수
EXPECTED_MATRIX: dict[tuple[int, str], int] = {
    (spec.type_id, rule): spec.count
    for spec in C.CORRUPT_TYPES
    for rule in spec.rules
}

V6_CORRUPT_TYPE = 8  # ERD.sql:537 — V6 는 오염 유형이 없다. 수동 확인용만 별도 번호.


class Finding:
    __slots__ = ("corrupt_type", "finding_type", "target_key", "campaign_id",
                 "member_id", "coupon_id", "history_id", "note", "expected", "actual")

    def __init__(self, **kw) -> None:
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Corruptor:
    """회차별 오염 주문서를 만들고, 생성기가 심은 결과를 정답으로 받아 적는다."""

    def __init__(self, profile: C.Profile, catalog: Catalog) -> None:
        self.p = profile
        self.cat = catalog
        self.quotas: dict[int, CouponQuota] = {}
        self.findings: list[Finding] = []
        self._keys: set[tuple[str, str]] = set()
        self.planned: dict[int, int] = {}
        if profile.is_corrupt:
            self._plan()

    # ── 계획 -----------------------------------------------------------------

    def _q(self, coupon_id: int) -> CouponQuota:
        q = self.quotas.get(coupon_id)
        if q is None:
            q = CouponQuota()
            self.quotas[coupon_id] = q
        return q

    def _spread(self, coupons: list[Coupon], need: int, attr: str, cap_fn) -> int:
        """라운드로빈으로 need 건을 회차에 흩뿌린다. 회차당 용량을 넘지 않는다."""
        caps = [(c, cap_fn(c)) for c in coupons]
        placed = 0
        while placed < need:
            progressed = False
            for c, cap in caps:
                if placed >= need:
                    break
                q = self._q(c.id)
                if getattr(q, attr) < cap:
                    setattr(q, attr, getattr(q, attr) + 1)
                    placed += 1
                    progressed = True
            if not progressed:
                break
        return placed

    def _plan(self) -> None:
        self.p.check_corrupt_capacity()
        past = self.cat.past
        want = {s.type_id: s.count for s in C.CORRUPT_TYPES}

        a_lo, a_hi = C.CORRUPT_V1_TYPE1_SLOT
        b_lo, b_hi = C.CORRUPT_V1_TYPE3_SLOT

        # 유형 1 — 재고만 +1. 고아 행이 없어 V1 하나만 울린다.
        slot_a = [c for c in past[a_lo:a_hi] if c.issue_count > 0][: want[1]]
        for c in slot_a:
            self._q(c.id).stock_delta += 1
        self.planned[1] = len(slot_a)

        # 유형 3 — CANCEL_USE 이중 기록(V4) + 재고 이중 복원(V1). 회차는 유형 1 과 서로소.
        slot_b = [c for c in past[b_lo:b_hi] if c.status_counts.get(C.USED, 0) > 0][: want[3]]
        for c in slot_b:
            q = self._q(c.id)
            q.q3 += 1
            q.stock_delta -= 1
        self.planned[3] = len(slot_b)

        # 나머지 유형은 회차 그레인이 아니므로 잔여 회차에 흩뿌려도 키가 안 겹친다.
        free = past[C.CORRUPT_FREE_SLOT_FROM:]
        if not free:
            raise RuntimeError("오염셋에 유형 2·4·5·6·7 용 잔여 회차가 없습니다.")

        self.planned[2] = self._spread(free, want[2], "q2",
                                       lambda c: c.status_counts.get(C.USED, 0))
        self.planned[4] = self._spread(free, want[4], "q4",
                                       lambda c: c.status_counts.get(C.EXPIRED, 0))
        self.planned[7] = self._spread(free, want[7], "q7",
                                       lambda c: c.status_counts.get(C.ISSUED, 0))
        self.planned[6] = self._spread(free, want[6], "q6", lambda c: min(c.issue_count, 8))
        self.planned[5] = self._spread(free, want[5], "q5", lambda c: min(c.issue_count, 8))

        short = {t: (n, self.planned.get(t, 0)) for t, n in want.items()
                 if self.planned.get(t, 0) < n}
        if short:
            raise RuntimeError(
                "오염 주입 계획 미달 — 스케일이 너무 작거나 회차가 부족합니다. "
                f"(유형: 요구/가능) {short}"
            )

        if self.p.plant_v6:
            target = next(
                (c for c in free if c.eligible_grades_mask != C.MASK_ALL and c.issue_count),
                None,
            )
            if target is not None:
                self._q(target.id).q8 = 1

    # ── 기록 -----------------------------------------------------------------

    def record(self, finding_type: str, target_key: str, corrupt_type: int,
               campaign_id=None, member_id=None, coupon_id=None, history_id=None,
               note=None, expected=None, actual=None) -> None:
        key = (finding_type, target_key)
        if key in self._keys:
            raise RuntimeError(
                f"expected_findings 키 충돌: {key}. 같은 대상에 오염이 두 번 들어갔습니다."
            )
        self._keys.add(key)
        self.findings.append(
            Finding(corrupt_type=corrupt_type, finding_type=finding_type,
                    target_key=target_key, campaign_id=campaign_id, member_id=member_id,
                    coupon_id=coupon_id, history_id=history_id, note=note,
                    expected=expected, actual=actual)
        )

    # ── 검사 · 출력 -----------------------------------------------------------

    def summary(self) -> dict:
        by_type: dict[int, int] = {}
        by_rule: dict[str, int] = {}
        for f in self.findings:
            by_type[f.corrupt_type] = by_type.get(f.corrupt_type, 0) + 1
            by_rule[f.finding_type] = by_rule.get(f.finding_type, 0) + 1
        return {
            "injections": sum(self.planned.values()),
            "expected_rows": len(self.findings),
            "by_corrupt_type": dict(sorted(by_type.items())),
            "by_finding_type": dict(sorted(by_rule.items())),
        }

    def assert_complete(self) -> None:
        actual: dict[tuple[int, str], int] = {}
        for f in self.findings:
            k = (f.corrupt_type, f.finding_type)
            actual[k] = actual.get(k, 0) + 1
        problems = []
        for k, wanted in sorted(EXPECTED_MATRIX.items()):
            got = actual.get(k, 0)
            if got != wanted:
                problems.append(f"유형{k[0]}/{k[1]}: 기대 {wanted} · 실제 {got}")
        for k in sorted(set(actual) - set(EXPECTED_MATRIX)):
            if k[0] == V6_CORRUPT_TYPE:
                continue  # 수동 확인용 V6 1건은 700 집계 밖
            problems.append(f"예정에 없는 조합 {k}: {actual[k]}건")
        if problems:
            raise RuntimeError("오염 주입 결과가 스펙과 다릅니다:\n  " + "\n  ".join(problems))

    def write(self, writer, as_of: dt.datetime) -> None:
        for i, f in enumerate(self.findings, start=1):
            writer.write(
                i, self.p.seed_run_id, f.corrupt_type, f.finding_type, f.target_key,
                f.campaign_id, f.member_id, f.coupon_id, f.history_id, f.note, as_of,
            )
