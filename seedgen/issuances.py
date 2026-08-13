"""발급 · 이력 · 사용을 한 패스에 동시 산출하는 핵심 생성기.

한 발급건의 issuances 1행 / issuance_histories 1~5행 / issuance_usages 0~2행을
같은 자리에서 만들기 때문에 세 테이블이 어긋날 여지가 없다. 동시에
coupon_stocks.active_count · coupon_stats · grade_stats · hourly_stats 를
누적하므로 통계를 위해 300만 행을 다시 훑을 필요도 없다.

메모리는 회차 단위로만 잡는다(최대 34,000 건). 300만 행을 리스트에 담지 않는다.

오염(CORRUPT)은 여기서 "생성 시점에" 들어간다. 적재 후 UPDATE 로 패치하지 않는
이유는 (a) 2코어 서버에서 대량 UPDATE 가 비싸고 (b) 어느 행이 오염됐는지가
생성기 안에서 이미 확정돼 있어 expected_findings 를 같은 자리에서 쓸 수 있기 때문이다.
"""

from __future__ import annotations

import datetime as dt
import heapq
import json
from collections import defaultdict

from . import config as C
from .catalog import Catalog, Coupon
from .idmap import IdMap
from .members import GradeBlocks
from .people import reason_for
from .rng import Rng, SampleCursor

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def encode_code(n: int) -> str:
    """발급 코드 char(16). id 기반이라 전역 유일이 구성적으로 보장된다."""
    out = []
    v = n
    while v:
        out.append(_B32[v & 31])
        v >>= 5
    body = "".join(reversed(out)) or "0"
    return "CP" + body.rjust(14, "0")


def uuid_from(rng: Rng) -> str:
    a, b = rng.u64(), rng.u64()
    h = f"{a:016x}{b:016x}"
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


class RangeSampler:
    """여러 rank 구간의 합집합에서 비복원 추출."""

    __slots__ = ("ranges", "total", "_cursor")

    def __init__(self, ranges: list[tuple[int, int]], rng: Rng) -> None:
        self.ranges = ranges
        self.total = sum(hi - lo for lo, hi in ranges)
        self._cursor = SampleCursor(rng, self.total) if self.total else None

    @property
    def remaining(self) -> int:
        return self._cursor.remaining if self._cursor else 0

    def take(self) -> int:
        if self._cursor is None:
            raise RuntimeError("빈 RangeSampler 에서 추출을 시도했습니다")
        off = self._cursor.take()
        for lo, hi in self.ranges:
            span = hi - lo
            if off < span:
                return lo + off
            off -= span
        raise RuntimeError("RangeSampler offset overflow")


class CouponQuota:
    """이 회차에 심을 오염 주문서. corrupt.py 가 채운다."""

    __slots__ = ("q2", "q3", "q4", "q5", "q6", "q7", "q8", "stock_delta")

    def __init__(self) -> None:
        self.q2 = self.q3 = self.q4 = self.q5 = self.q6 = self.q7 = self.q8 = 0
        self.stock_delta = 0

    def pending(self) -> dict[str, int]:
        return {
            f"q{i}": getattr(self, f"q{i}") for i in (2, 3, 4, 7)
            if getattr(self, f"q{i}")
        }


class Totals:
    def __init__(self) -> None:
        self.issuances = 0
        self.histories = 0
        self.usages = 0
        self.status: dict[str, int] = defaultdict(int)
        self.hourly: dict[tuple[str, int], int] = defaultdict(int)
        self.grade: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
        self.coupon_stats: dict[int, dict] = {}
        self.active_count: dict[int, int] = {}
        self.max_updated_at: dt.datetime | None = None
        self.active_usages = 0


class IssuanceGenerator:
    def __init__(
        self,
        profile: C.Profile,
        catalog: Catalog,
        blocks: GradeBlocks,
        idmap: IdMap,
        writers,
        quotas: dict[int, CouponQuota] | None = None,
        record_finding=None,
        idem_target: int = 0,
        asof_run_id: int | None = None,
    ) -> None:
        self.asof_run_id = asof_run_id
        self.p = profile
        self.cat = catalog
        self.blocks = blocks
        self.idmap = idmap
        self.w = writers
        self.quotas = quotas or {}
        self.record = record_finding or (lambda *a, **k: None)
        self.idem_target = idem_target

        self.as_of = catalog.as_of
        self.totals = Totals()
        self._iid = 0
        self._hid = 0
        self._uid = 0
        self._idem_heap: list[tuple] = []

    # ── 시간 헬퍼 -------------------------------------------------------------

    @staticmethod
    def _between(lo: dt.datetime, hi: dt.datetime, rng: Rng) -> dt.datetime:
        span = (hi - lo).total_seconds()
        if span <= 0:
            return lo
        return lo + dt.timedelta(seconds=rng.random() * span)

    @staticmethod
    def _sorted_between(lo, hi, k, rng) -> list[dt.datetime]:
        span = (hi - lo).total_seconds()
        if span <= 0:
            return [lo + dt.timedelta(microseconds=i + 1) for i in range(k)]
        return [lo + dt.timedelta(seconds=f * span) for f in sorted(rng.random() for _ in range(k))]

    # ── 행 기록 ---------------------------------------------------------------

    def _history(self, issuance_id, event, frm, to, at, rng, request_id=None, member_id=None):
        self._hid += 1
        self.w["issuance_histories"].write(
            self._hid, issuance_id, event, frm, to,
            reason_for(event, rng), request_id, at,
        )
        self.totals.histories += 1
        if event == C.EV_ISSUE:
            self.totals.hourly[(C.DOW[at.weekday()], at.hour)] += 1
        elif request_id is not None and member_id is not None:
            self._offer_idem(at, request_id, issuance_id, member_id, rng)
        return self._hid

    def _usage(self, issuance_id, coupon: Coupon, used_at, canceled_at, rng):
        self._uid += 1
        self.w["issuance_usages"].write(
            self._uid, issuance_id, rng.randint(10**9, 9 * 10**9),
            self._discount(coupon, rng),
            used_at.replace(microsecond=0),
            canceled_at.replace(microsecond=0) if canceled_at else None,
        )
        self.totals.usages += 1

    @staticmethod
    def _discount(coupon: Coupon, rng: Rng) -> int:
        if coupon.policy_type == C.PERCENT_CAPPED:
            floor = max(1000, coupon.min_order_amount)
            order = rng.randint(floor, floor * 5)
            return min(order * (coupon.discount_rate or 0) // 100,
                       coupon.max_discount_amount or 10**9)
        if coupon.policy_type == C.FIXED_AMOUNT:
            return coupon.discount_amount or 0
        return 0  # DATA_GRANT — 금액 할인이 아니다

    def _offer_idem(self, at, request_id, issuance_id, member_id, rng):
        """가장 최근 상태변경 요청 N건만 idempotency_records 로 남긴다.

        ERD.sql:407 "24시간 지난 행은 배치로 삭제" 를 흉내내려면 최신 구간이어야 하는데,
        어디까지가 최신인지는 전량을 봐야 알 수 있다. 크기 N 힙으로 한 패스에 고른다.
        """
        if self.idem_target <= 0:
            return
        entry = (at, request_id, issuance_id, member_id, rng.u64())
        if len(self._idem_heap) < self.idem_target:
            heapq.heappush(self._idem_heap, entry)
        elif at > self._idem_heap[0][0]:
            heapq.heapreplace(self._idem_heap, entry)

    # ── 메인 루프 -------------------------------------------------------------

    def run(self, progress=None) -> Totals:
        for coupon in self.cat.coupons:
            self._run_coupon(coupon)
            if progress is not None:
                progress(coupon, self.totals)
        self._flush_idempotency()
        return self.totals

    def _run_coupon(self, coupon: Coupon) -> None:
        t = self.totals
        n = coupon.issue_count
        quota = self.quotas.get(coupon.id, CouponQuota())
        stat = {"issued_total": 0, C.ISSUED: 0, C.USED: 0, C.CANCELLED: 0, C.EXPIRED: 0}

        if n == 0 and not (quota.q5 or quota.q6):
            t.coupon_stats[coupon.id] = self._stat_row(coupon, stat, None)
            t.active_count[coupon.id] = quota.stock_delta
            return

        rng = Rng(self.p.seed, f"issue:{coupon.id}")
        mask = coupon.eligible_grades_mask
        heavy = RangeSampler(self.blocks.heavy_ranges(mask), rng.fork("heavy"))
        light = RangeSampler(self.blocks.light_ranges(mask), rng.fork("light"))
        drift = RangeSampler(
            [(lo, hi) for lo, hi, _c, bit in self.blocks.bounds if not (bit & mask)],
            rng.fork("drift"),
        )
        eligible_codes = [c for c, bit, _ in C.GRADES if bit & mask]

        if heavy.total + light.total < n:
            raise RuntimeError(
                f"회차 {coupon.id}: 자격 회원 {heavy.total + light.total}명 < 발급 {n}건. "
                f"등급 제한(mask={mask})과 재고가 안 맞습니다."
            )
        n_heavy = min(int(n * C.PARETO_HEAVY_WEIGHT), heavy.total)
        if n - n_heavy > light.total:
            n_heavy = n - light.total

        statuses: list[str] = []
        for s in C.STATUSES:
            statuses.extend([s] * coupon.status_counts.get(s, 0))
        if len(statuses) != n:
            statuses = (statuses + [C.ISSUED] * n)[:n]
        statuses = rng.shuffled(statuses)

        window = (coupon.close_at - coupon.open_at).total_seconds()
        if coupon.sellout:
            window *= rng.uniform(0.02, 0.50)
        offsets = sorted(window * (rng.random() ** 3) for _ in range(n))

        active_replay = 0
        last_issue_at = None
        heavy_left = n_heavy
        dup_members: list[int] = []   # 유형 6 이 재사용할 (회차, 회원)
        twin_codes: list[tuple[int, str]] = []  # 유형 5 가 복제할 code

        for i in range(n):
            status = statuses[i]
            # Knuth 선택 샘플링 — heavy 를 정확히 n_heavy 번 뽑는다
            if heavy_left > 0 and rng.random() < heavy_left / (n - i):
                rank = heavy.take()
                heavy_left -= 1
            else:
                rank = light.take()
            issued_grade = self.blocks.grade_of(rank)

            # 등급 스냅샷 드리프트 — 지금은 부적격이지만 발급 시점엔 적격이던 회원.
            # V6 는 스냅샷만 보므로 정상셋 0건이 유지된다 (ERD.sql:314-326).
            if drift.remaining > 0 and mask != C.MASK_ALL and rng.chance(C.GRADE_DRIFT_RATIO):
                rank = drift.take()
                issued_grade = rng.choice(eligible_codes)

            issued_at = coupon.open_at + dt.timedelta(seconds=offsets[i])
            last_issue_at = issued_at
            state, member_id, iid = self._emit_issuance(
                coupon, rng, rank, issued_grade, issued_at, status, quota
            )
            if state in (C.ISSUED, C.USED):
                active_replay += 1
            stat[status] += 1
            stat["issued_total"] += 1
            g = t.grade[(coupon.id, issued_grade)]
            g[0] += 1
            if status == C.USED:
                g[1] += 1

            if len(dup_members) < quota.q6:
                dup_members.append(member_id)
            if len(twin_codes) < quota.q5:
                twin_codes.append((iid, encode_code(iid)))

        if len(dup_members) < quota.q6 or len(twin_codes) < quota.q5:
            raise RuntimeError(
                f"회차 {coupon.id}: 발급 {n}건으로는 유형 5·6 중복행을 만들 수 없습니다."
            )

        # 유형 6 — 같은 (회차, 회원) 에 한 건 더
        for member_id in dup_members:
            active_replay += self._emit_dup_row(
                coupon, rng, member_id, None, corrupt_type=6,
                note="동일 유저가 같은 회차에서 2건 발급",
            )
        # 유형 5 — 같은 code 를 다른 회원에게 복제
        for twin_id, twin_code in twin_codes:
            if not (heavy.remaining or light.remaining):
                break
            rank = light.take() if light.remaining else heavy.take()
            active_replay += self._emit_dup_row(
                coupon, rng, self.idmap.rank_to_id(rank), twin_code, corrupt_type=5,
                note=f"동일 code 를 다른 유저에게 복제 (원본 issuance {twin_id})",
            )

        # V6 수동 확인용 1건 (--plant-v6). ERD.sql:537 — 700 집계 밖.
        if quota.q8:
            quota.q8 -= 1
            active_replay += self._emit_grade_violation(coupon, rng, heavy, light)

        t.coupon_stats[coupon.id] = self._stat_row(coupon, stat, last_issue_at)
        t.active_count[coupon.id] = active_replay + quota.stock_delta
        if quota.stock_delta:
            self.record(
                C.V1, C.key_coupon(coupon.id),
                corrupt_type=1 if quota.stock_delta > 0 else 3,
                campaign_id=coupon.id,
                note=("재고는 줄었는데 ISSUE 기록 없음" if quota.stock_delta > 0
                      else "CANCEL_USE 이중 기록으로 재고 이중 복원"),
                expected=f"active_count={active_replay + quota.stock_delta}",
                actual=f"issuances 집계={active_replay}",
            )
        leftover = quota.pending()
        if leftover:
            raise RuntimeError(
                f"회차 {coupon.id}: 오염 주문 {leftover} 를 심을 대상 발급건이 없었습니다. "
                f"스케일을 올리거나 --corrupt-scale 을 조정하세요."
            )

    def _stat_row(self, coupon: Coupon, stat: dict, last_issue_at) -> dict:
        sold_out = None
        if coupon.sellout and last_issue_at is not None:
            sold_out = int((last_issue_at - coupon.open_at).total_seconds())
        return {
            "issued_total": stat["issued_total"],
            "issued": stat[C.ISSUED],
            "used": stat[C.USED],
            "cancelled": stat[C.CANCELLED],
            "expired": stat[C.EXPIRED],
            "sold_out_seconds": sold_out,
        }

    # ── 발급 1건 -------------------------------------------------------------

    def _emit_issuance(self, coupon, rng, rank, issued_grade, issued_at, status, quota):
        """issuances/histories/usages 를 쓰고 (리플레이 최종상태, member_id, id) 반환."""
        self._iid += 1
        iid = self._iid
        member_id = self.idmap.rank_to_id(rank)
        expires_at = issued_at + dt.timedelta(days=coupon.valid_days)
        horizon = min(expires_at, self.as_of)

        events: list[tuple] = [(C.EV_ISSUE, None, C.ISSUED, issued_at, None, None)]
        usages: list[tuple] = []
        replay_state = C.ISSUED
        stored_status = status

        if status == C.USED:
            force3 = quota.q3 > 0
            if force3 or rng.chance(C.USED_RESTORE_RATIO):
                t1, t2, t3 = self._sorted_between(issued_at, horizon, 3, rng)
                events += [
                    (C.EV_USE, C.ISSUED, C.USED, t1, uuid_from(rng), None),
                    (C.EV_CANCEL_USE, C.USED, C.ISSUED, t2, uuid_from(rng), None),
                ]
                if force3:
                    quota.q3 -= 1
                    # 유형 3 — CANCEL_USE 이중 기록. 최종 상태(USED)는 변하지 않는다.
                    events.append(
                        (C.EV_CANCEL_USE, C.USED, C.ISSUED,
                         t2 + dt.timedelta(milliseconds=1), uuid_from(rng), "C3")
                    )
                events.append((C.EV_USE, C.ISSUED, C.USED, t3, uuid_from(rng), None))
                usages = [(t1, t2), (t3, None)]
            else:
                t1 = self._between(issued_at, horizon, rng)
                events.append((C.EV_USE, C.ISSUED, C.USED, t1, uuid_from(rng), None))
                usages = [(t1, None)]
            replay_state = C.USED
            if quota.q2 > 0:
                quota.q2 -= 1
                stored_status = C.ISSUED  # 유형 2 — 리플레이 USED / 저장값 ISSUED
                self.record(
                    C.V3, C.key_issuance(iid), corrupt_type=2, coupon_id=iid,
                    note="리플레이는 USED 인데 issuances.status 를 ISSUED 로 기록",
                    expected=f"replay={C.USED}", actual=f"issuances.status={C.ISSUED}",
                )

        elif status == C.ISSUED:
            force7 = quota.q7 > 0
            if force7 or rng.chance(C.ISSUED_RESTORED_RATIO):
                t1, t2 = self._sorted_between(issued_at, horizon, 2, rng)
                events += [
                    (C.EV_USE, C.ISSUED, C.USED, t1, uuid_from(rng), None),
                    (C.EV_CANCEL_USE, C.USED, C.ISSUED, t2, uuid_from(rng), None),
                ]
                if force7:
                    quota.q7 -= 1
                    usages = [(t1, None)]  # 유형 7 — 취소되지 않은 활성 사용 행
                    self.record(
                        C.V5, C.key_issuance(iid), corrupt_type=7, coupon_id=iid,
                        note="status 는 ISSUED 인데 활성 usages 행이 남아 있음",
                        expected="active_usage=0", actual="active_usage=1",
                    )
                else:
                    usages = [(t1, t2)]

        elif status == C.CANCELLED:
            t1 = self._between(issued_at, horizon, rng)
            events.append((C.EV_CANCEL, C.ISSUED, C.CANCELLED, t1, uuid_from(rng), None))
            replay_state = C.CANCELLED

        else:  # EXPIRED
            if expires_at >= self.as_of:
                raise RuntimeError(
                    f"회차 {coupon.id}: expires_at({expires_at}) 가 asOf 이후인데 "
                    f"EXPIRED 로 계획됐습니다. catalog 의 expirable 판정을 확인하세요."
                )
            t1 = self._between(
                expires_at, min(expires_at + dt.timedelta(hours=6), self.as_of), rng
            )
            events.append((C.EV_EXPIRE, C.ISSUED, C.EXPIRED, t1, None, None))
            replay_state = C.EXPIRED
            if quota.q4 > 0:
                quota.q4 -= 1
                # 유형 4 — 종단 상태에서 USED 로 불법 전이.
                # 나머지 축(status·usage·재고)은 전부 맞춰서 V4 만 울리게 한다.
                t2 = min(t1 + dt.timedelta(hours=1), self.as_of)
                events.append((C.EV_USE, C.EXPIRED, C.USED, t2, uuid_from(rng), "C4"))
                usages = [(t2, None)]
                replay_state = C.USED
                stored_status = C.USED

        history_ids: dict[str, int] = {}
        for event, frm, to, at, req, tag in events:
            hid = self._history(iid, event, frm, to, at, rng, req, member_id)
            if tag:
                history_ids[tag] = hid

        if "C3" in history_ids:
            self.record(C.V4, C.key_history(history_ids["C3"]), corrupt_type=3,
                        history_id=history_ids["C3"], note="CANCEL_USE 를 2번 심었음",
                        expected=f"from_status={C.ISSUED}", actual=f"from_status={C.USED}")
        if "C4" in history_ids:
            self.record(C.V4, C.key_history(history_ids["C4"]), corrupt_type=4,
                        history_id=history_ids["C4"],
                        note="종단 상태 EXPIRED 에서 USED 로 불법 전이",
                        expected="종단 상태에서 전이 없음", actual=f"{C.EXPIRED}→{C.USED}")

        active_usages = 0
        for used_at, canceled_at in usages:
            self._usage(iid, coupon, used_at, canceled_at, rng)
            if canceled_at is None:
                active_usages += 1

        updated_at = max(e[3] for e in events)
        self.w["issuances"].write(
            iid, coupon.id, member_id, encode_code(iid), issued_grade,
            stored_status, issued_at, expires_at, updated_at,
        )
        t = self.totals
        t.issuances += 1
        t.status[stored_status] += 1
        t.active_usages += active_usages
        if t.max_updated_at is None or updated_at > t.max_updated_at:
            t.max_updated_at = updated_at
        if self.asof_run_id is not None:
            self.w["asof_state"].write(
                self.asof_run_id, iid, replay_state, self._hid, updated_at, active_usages
            )
        return replay_state, member_id, iid

    def _track(self, iid: int, updated_at, state: str, active_usages: int) -> None:
        t = self.totals
        t.issuances += 1
        t.status[C.ISSUED] += 1
        t.active_usages += active_usages
        if t.max_updated_at is None or updated_at > t.max_updated_at:
            t.max_updated_at = updated_at
        if self.asof_run_id is not None:
            self.w["asof_state"].write(
                self.asof_run_id, iid, state, self._hid, updated_at, active_usages
            )

    def _emit_dup_row(self, coupon, rng, member_id, twin_code, corrupt_type, note) -> int:
        """유형 5·6 — 1인 1매 위반 행. 반환값은 active_count 증가분."""
        self._iid += 1
        iid = self._iid
        issued_grade = next(c for c, bit, _ in C.GRADES if bit & coupon.eligible_grades_mask)
        span = (coupon.close_at - coupon.open_at).total_seconds()
        issued_at = coupon.open_at + dt.timedelta(seconds=rng.random() * span)
        expires_at = issued_at + dt.timedelta(days=coupon.valid_days)

        self._history(iid, C.EV_ISSUE, None, C.ISSUED, issued_at, rng)
        self.w["issuances"].write(
            iid, coupon.id, member_id, twin_code or encode_code(iid), issued_grade,
            C.ISSUED, issued_at, expires_at, issued_at,
        )
        self._track(iid, issued_at, C.ISSUED, 0)
        self.record(
            C.V2, C.key_coupon_member(coupon.id, member_id), corrupt_type=corrupt_type,
            campaign_id=coupon.id, member_id=member_id, coupon_id=iid, note=note,
            expected="발급 1건", actual="발급 2건",
        )
        return 1

    def _emit_grade_violation(self, coupon, rng, heavy, light) -> int:
        """issued_grade 스냅샷 자체가 회차 자격을 위반하는 1건 (V6 눈으로 확인용)."""
        self._iid += 1
        iid = self._iid
        rank = light.take() if light.remaining else heavy.take()
        bad = next(c for c, bit, _ in C.GRADES if not (bit & coupon.eligible_grades_mask))
        span = (coupon.close_at - coupon.open_at).total_seconds()
        issued_at = coupon.open_at + dt.timedelta(seconds=rng.random() * span)
        expires_at = issued_at + dt.timedelta(days=coupon.valid_days)

        self._history(iid, C.EV_ISSUE, None, C.ISSUED, issued_at, rng)
        self.w["issuances"].write(
            iid, coupon.id, self.idmap.rank_to_id(rank), encode_code(iid), bad,
            C.ISSUED, issued_at, expires_at, issued_at,
        )
        self._track(iid, issued_at, C.ISSUED, 0)
        self.record(
            C.V6, C.key_issuance(iid), corrupt_type=8, campaign_id=coupon.id,
            coupon_id=iid, note=f"{bad} 등급이 mask={coupon.eligible_grades_mask} 회차에서 발급",
            expected=f"mask & bit != 0 (mask={coupon.eligible_grades_mask})",
            actual=f"issued_grade={bad}",
        )
        return 1

    # ── 멱등 레코드 -----------------------------------------------------------

    def _flush_idempotency(self) -> None:
        if not self._idem_heap:
            return
        rng = Rng(self.p.seed, "idempotency")
        w = self.w["idempotency_records"]
        for at, request_id, issuance_id, member_id, salt in sorted(self._idem_heap):
            done = not rng.chance(0.05)
            body = (
                json.dumps(
                    {"issuanceId": issuance_id, "status": "OK",
                     "appliedAt": at.strftime("%Y-%m-%dT%H:%M:%S")},
                    ensure_ascii=False, separators=(",", ":"),
                )
                if done else None
            )
            w.write(
                request_id, member_id, issuance_id,
                f"{salt:016x}{(salt ^ 0x5A5A5A5A5A5A5A5A):016x}".ljust(64, "0")[:64],
                "DONE" if done else "IN_PROGRESS", body, at,
            )
