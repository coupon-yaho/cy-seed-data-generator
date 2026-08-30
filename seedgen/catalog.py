"""카탈로그 계층 — grades · brands · coupon_templates · coupons, 그리고 발급 계획.

여기서 "회차별로 몇 건을 어떤 상태로 발급할 것인가"를 먼저 확정한다.
발급 스트림 생성기는 이 계획을 그대로 따라 쓰기만 하므로,
전역 분포(ISSUED 40 / USED 35 / EXPIRED 15 / CANCELLED 10)가 정확히 맞는다.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field

from . import config as C
from .rng import Rng

_DOW_IDX = {d: i for i, d in enumerate(C.DOW)}


def nth_weekday(year: int, month: int, nth: int, dow: str) -> dt.date:
    """그 달의 N번째 <요일>. 4번째가 없으면 마지막 해당 요일로 clamp."""
    target = _DOW_IDX[dow]
    first_dow = calendar.monthrange(year, month)[0]  # 0=월
    offset = (target - first_dow) % 7
    day = 1 + offset + (nth - 1) * 7
    days_in_month = calendar.monthrange(year, month)[1]
    while day > days_in_month:
        day -= 7
    return dt.date(year, month, day)


def month_back(anchor: dt.date, months: int) -> tuple[int, int]:
    total = anchor.year * 12 + (anchor.month - 1) - months
    return total // 12, total % 12 + 1


@dataclass
class Coupon:
    id: int
    template_id: int
    brand_id: int
    name: str
    policy_type: str
    discount_rate: int | None
    max_discount_amount: int | None
    discount_amount: int | None
    min_order_amount: int
    valid_days: int
    eligible_grades_mask: int
    open_at: dt.datetime
    close_at: dt.datetime
    status: str
    created_at: dt.datetime
    # 계획용 (테이블 컬럼 아님)
    total_quantity: int = 0
    months_ago: int = 0
    sellout: bool = False
    issue_count: int = 0
    duration_hours: int = 6
    expirable: bool = True
    status_counts: dict[str, int] = field(default_factory=dict)

    def row(self) -> tuple:
        return (
            self.id, self.template_id, self.brand_id, self.name, self.policy_type,
            self.discount_rate, self.max_discount_amount, self.discount_amount,
            self.min_order_amount, self.valid_days,
            self.eligible_grades_mask, self.open_at, self.close_at, self.status,
            # generated_at — 회차를 만든 작업의 기준 시각. 시드는 감사 시각과 같게 둔다:
            # 한 번에 만든 데이터라 "언제 만들기로 판단했나" 와 "언제 저장됐나" 가 같다.
            self.created_at, self.created_at,
        )


@dataclass
class Catalog:
    coupons: list[Coupon]
    templates: list[tuple]
    brands: list[tuple]
    grades: list[tuple]
    as_of: dt.datetime
    expiring_coupon_ids: set[int] = field(default_factory=set)

    @property
    def past(self) -> list[Coupon]:
        return [c for c in self.coupons if c.months_ago > 0]

    @property
    def current(self) -> list[Coupon]:
        return [c for c in self.coupons if c.months_ago == 0]


# ─────────────────────────────────────────────────────────────────────────────
# 정수 배분 유틸
# ─────────────────────────────────────────────────────────────────────────────

def _rebalance(counts: list[int], floors: list[int], caps: list[int], target: int) -> None:
    """counts 합을 target 에 정확히 맞춘다. floors ≤ counts ≤ caps 유지."""
    diff = target - sum(counts)
    if diff == 0:
        return
    step = 1 if diff > 0 else -1
    # 여유가 큰 순서로 돌면서 1씩 옮긴다 (결정론: 인덱스 순회)
    guard = 0
    n = len(counts)
    i = 0
    while diff != 0:
        room = (caps[i] - counts[i]) if step > 0 else (counts[i] - floors[i])
        if room > 0:
            move = min(room, abs(diff))
            counts[i] += step * move
            diff -= step * move
        i = (i + 1) % n
        guard += 1
        if guard > n * 64:
            raise RuntimeError(f"배분 실패: 잔여 {diff}. 스케일 대비 재고가 부족합니다.")


def ipf_integer(
    row_totals: list[int], col_totals: dict[str, int], weights: list[dict[str, float]]
) -> list[dict[str, int]]:
    """행 합·열 합을 동시에 맞추는 정수 행렬 (IPF + 잔여 그리디 배분)."""
    cols = list(col_totals)
    n, m = len(row_totals), len(cols)
    x = [[max(0.0, weights[i].get(c, 0.0)) for c in cols] for i in range(n)]

    for _ in range(40):
        for i in range(n):
            s = sum(x[i])
            if s > 0:
                f = row_totals[i] / s
                for j in range(m):
                    x[i][j] *= f
        for j in range(m):
            s = sum(x[i][j] for i in range(n))
            if s > 0:
                f = col_totals[cols[j]] / s
                for i in range(n):
                    x[i][j] *= f

    out = [[int(x[i][j]) for j in range(m)] for i in range(n)]

    # 1단계 — 행 합을 정확히 맞춘다. 행 안에서 소수부가 큰 칸부터 +1.
    #   행별로 닫아 두면 이후 열 보정이 행 합을 건드리지 않는다.
    for i in range(n):
        rem = row_totals[i] - sum(out[i])
        if rem <= 0:
            continue
        order = sorted(
            (j for j in range(m) if x[i][j] > 0),
            key=lambda j: (-(x[i][j] - out[i][j]), j),
        )
        if not order:
            raise RuntimeError(f"행 {i}: 배정 가능한 열이 없습니다 (가중치가 전부 0)")
        for k in range(rem):
            out[i][order[k % len(order)]] += 1

    # 2단계 — 열 합을 이동(transfer)으로 맞춘다. 같은 행 안에서 열만 바꾸므로
    #   행 합은 불변이다. 초과 열에서 1을 빼 부족 열에 더한다.
    def col_sum(j: int) -> int:
        return sum(out[i][j] for i in range(n))

    guard = 0
    while True:
        dev = [col_sum(j) - col_totals[cols[j]] for j in range(m)]
        over = [j for j in range(m) if dev[j] > 0]
        under = [j for j in range(m) if dev[j] < 0]
        if not over or not under:
            break
        moved = False
        for jo in over:
            for ju in under:
                for i in range(n):
                    if out[i][jo] > 0 and x[i][ju] > 0:
                        out[i][jo] -= 1
                        out[i][ju] += 1
                        moved = True
                        break
                if moved:
                    break
            if moved:
                break
        if not moved:
            short = {cols[j]: -dev[j] for j in under}
            raise RuntimeError(
                "상태 배분 실패: 제약을 만족하는 이동 경로가 없습니다. "
                f"부족한 상태={short}. 만료 가능 회차 용량이나 상태 비율을 확인하세요."
            )
        guard += 1
        if guard > n * m * 64:
            raise RuntimeError("상태 배분이 수렴하지 않았습니다")

    return [{cols[j]: out[i][j] for j in range(m)} for i in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# 빌드
# ─────────────────────────────────────────────────────────────────────────────

def build_catalog(profile: C.Profile, as_of: dt.datetime) -> Catalog:
    rng = Rng(profile.seed, "catalog")

    grades = [(code, bit) for code, bit, _ in C.GRADES]
    brands = [(i + 1, b.name, b.category) for i, b in enumerate(C.BRANDS)]

    stock_scale = (C.TOTAL_STOCK * profile.scale) / (
        profile.past_coupons * (C.STOCK_MIN + C.STOCK_MAX) / 2
    )

    templates: list[tuple] = []
    for i, b in enumerate(C.BRANDS):
        templates.append(
            (
                i + 1, i + 1, f"{b.name} 브랜드데이", b.policy_type,
                b.discount_rate, b.max_discount_amount, b.discount_amount,
                rng.randint(C.VALID_DAYS_MIN, C.VALID_DAYS_MAX),
                b.nth_week, b.day_of_week, f"{b.start_hour:02d}:00:00",
                b.duration_hours,
                max(1, int(round((C.STOCK_MIN + C.STOCK_MAX) / 2 * stock_scale))),
                b.mask, True,
                # created_at · updated_at — cy-be V14. 카탈로그는 as_of 기준 한 번에 만든다.
                as_of, as_of,
            )
        )

    # ── 과거 회차: 오래된 것부터 id 를 매긴다 (open_at 오름차순 = PK 오름차순)
    coupons: list[Coupon] = []
    anchor = as_of.date()
    next_id = 1
    for months_ago in range(profile.past_months, 0, -1):
        year, month = month_back(anchor, months_ago)
        for bi, b in enumerate(C.BRANDS):
            day = nth_weekday(year, month, b.nth_week, b.day_of_week)
            open_at = dt.datetime(day.year, day.month, day.day, b.start_hour)
            if open_at >= as_of:
                # 이번 달 브랜드데이가 아직 안 왔으면 한 달 더 뒤로
                year2, month2 = month_back(anchor, months_ago + 1)
                day = nth_weekday(year2, month2, b.nth_week, b.day_of_week)
                open_at = dt.datetime(day.year, day.month, day.day, b.start_hour)
            close_at = open_at + dt.timedelta(hours=b.duration_hours)
            valid_days = rng.randint(C.VALID_DAYS_MIN, C.VALID_DAYS_MAX)
            stock = max(1, int(round(rng.randint(C.STOCK_MIN, C.STOCK_MAX) * stock_scale)))
            coupons.append(
                Coupon(
                    id=next_id, template_id=bi + 1, brand_id=bi + 1,
                    name=f"{b.name} 브랜드데이 {open_at:%Y-%m}",
                    policy_type=b.policy_type, discount_rate=b.discount_rate,
                    max_discount_amount=b.max_discount_amount,
                    discount_amount=b.discount_amount,
                    min_order_amount=b.min_order_amount, valid_days=valid_days,
                    eligible_grades_mask=b.mask, open_at=open_at, close_at=close_at,
                    status="CLOSED", created_at=open_at - dt.timedelta(days=1),
                    total_quantity=stock, months_ago=months_ago,
                    duration_hours=b.duration_hours,
                )
            )
            next_id += 1

    # ── 현재 회차 3건: open_at = 시드 실행 시각 + 5분 (PRD.md:276-279)
    # **회차끼리 시간이 겹치면 안 된다.** cy-be 의 existsOverlappingSchedule 이
    # (open_at < 상대 close_at AND close_at > 상대 open_at) 인 SCHEDULED·OPEN 회차를
    # 거절한다 — 브랜드데이는 한 번에 하나만 돈다는 제품 규칙이다.
    # 예전에는 셋 다 as_of + 5분으로 찍어 창이 완전히 같았고, 그러면 앱이 절대 만들지
    # 않을 상태(동시에 열린 회차 셋)를 시드가 만들어 냈다. 이력 144건은 원래 안 겹친다 —
    # 이 블록만 규칙 밖이었다.
    # **앞으로 UPCOMING_DAYS 치를 계속 이어 붙인다.** 셋만 만들면 시드 실행 시각으로부터
    # 반나절이면 마지막이 닫히고 그 뒤로 열린 회차가 없다 — 하루 뒤에 화면을 켜면 발급을
    # 눌러 볼 대상이 하나도 없다. 시연·수동 테스트가 시드 시각에 묶이는 자리였다.
    #
    # ⚠️ **자정을 넘기면 안 된다.** 이어 붙이기만 하면 20시에 시작한 6시간짜리가 다음 날
    #    새벽 2시에 닫힌다. cy-be 의 회차는 하루 안에서 열고 닫히는 것이 전제라,
    #    넘어갈 자리에서는 다음 날 00시로 커서를 옮긴다. (verify.py 의 도메인 규칙 검사가
    #    DATE(open_at) <> DATE(close_at) 를 잡는다.)
    def _upcoming() -> list[tuple[int, int]]:
        plan = list(C.CURRENT_COUPONS)
        if C.UPCOMING_DAYS <= 0:
            return plan
        # 앞의 셋에 쓴 브랜드 다음부터 돌린다 — 같은 브랜드가 연달아 서지 않게.
        start = (C.CURRENT_COUPONS[-1][0] + 1) if C.CURRENT_COUPONS else 0
        # 하루에 네 회차(6+6+6+5)면 23시간이 덮인다. 넉넉히 잡아 두고 아래 루프가
        # 시각으로 끊는다 — 개수가 아니라 **언제까지** 가 기준이다.
        for k in range(C.UPCOMING_DAYS * 5):
            plan.append(((start + k) % len(C.BRANDS), C.UPCOMING_STOCK))
        return plan

    horizon = as_of + dt.timedelta(days=C.UPCOMING_DAYS)
    cursor = (as_of + dt.timedelta(minutes=5)).replace(microsecond=0)
    for idx, (bi, stock) in enumerate(_upcoming()):
        if idx >= len(C.CURRENT_COUPONS) and cursor >= horizon:
            break
        b = C.BRANDS[bi]
        open_at = cursor
        close_at = open_at + dt.timedelta(hours=b.duration_hours)
        if close_at.date() != open_at.date():
            # 자정을 넘긴다 — 다음 날 00시부터 다시 연다.
            open_at = (open_at + dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            close_at = open_at + dt.timedelta(hours=b.duration_hours)
        # 다음 회차는 이 회차가 닫힌 뒤에 연다. 경계가 같으면
        # open_at < close_at 조건에 안 걸리므로 붙여도 된다.
        cursor = close_at
        coupons.append(
            Coupon(
                id=next_id, template_id=bi + 1, brand_id=bi + 1,
                name=f"{b.name} 브랜드데이 {open_at:%Y-%m} (예정)",
                policy_type=b.policy_type, discount_rate=b.discount_rate,
                max_discount_amount=b.max_discount_amount,
                discount_amount=b.discount_amount,
                min_order_amount=b.min_order_amount,
                valid_days=rng.randint(C.VALID_DAYS_MIN, C.VALID_DAYS_MAX),
                eligible_grades_mask=b.mask, open_at=open_at, close_at=close_at,
                status="SCHEDULED", created_at=as_of,
                total_quantity=max(1, int(round(stock * profile.scale))),
                months_ago=0, duration_hours=b.duration_hours,
            )
        )
        next_id += 1

    cat = Catalog(coupons=coupons, templates=templates, brands=brands,
                  grades=grades, as_of=as_of)
    _plan_issue_counts(cat, profile, rng)
    _plan_expiring_soon(cat, profile)
    _plan_statuses(cat, profile)
    return cat


def _plan_issue_counts(cat: Catalog, profile: C.Profile, rng: Rng) -> None:
    """회차별 발급 건수. 완판 25% + 미달 60~98%, 전체 합은 목표에 정확히 일치."""
    past = cat.past
    counts, floors, caps = [], [], []
    for c in past:
        c.sellout = rng.chance(C.SELLOUT_RATIO)
        if c.sellout:
            n = c.total_quantity
            floors.append(n)
            caps.append(n)
        else:
            n = int(round(c.total_quantity * rng.uniform(C.SELL_RATE_MIN, C.SELL_RATE_MAX)))
            floors.append(max(1, int(c.total_quantity * 0.50)))
            caps.append(max(1, c.total_quantity - 1))
        counts.append(max(1, n))
    _rebalance(counts, floors, caps, profile.issuances)
    for c, n in zip(past, counts):
        c.issue_count = n
        c.sellout = n >= c.total_quantity
    for c in cat.current:
        c.issue_count = 0  # 현재 회차는 재고 100% (아직 안 열림)


def _plan_expiring_soon(cat: Catalog, profile: C.Profile) -> None:
    """전체의 1% 가 24시간 내 만료되도록 최근 회차의 valid_days 를 맞춘다."""
    target = int(profile.issuances * C.EXPIRING_SOON_RATIO)
    if target <= 0:
        return
    acc = 0
    for c in sorted(
        (x for x in cat.past if x.months_ago <= 2), key=lambda x: (-x.months_ago, x.id)
    ):
        delta = (cat.as_of - c.open_at).total_seconds()
        for extra in (0, 1):
            d = int(delta // 86400) + 1 + extra
            lo = c.open_at + dt.timedelta(days=d)
            hi = c.open_at + dt.timedelta(days=d, hours=c.duration_hours)
            if lo > cat.as_of and hi < cat.as_of + dt.timedelta(hours=24):
                c.valid_days = d
                cat.expiring_coupon_ids.add(c.id)
                acc += c.issue_count
                break
        if acc >= target * 3:  # ISSUED 비중을 감안한 여유
            break


def _plan_statuses(cat: Catalog, profile: C.Profile) -> None:
    """회차별 상태 배분. 행 합 = 발급수, 열 합 = 전역 목표."""
    past = cat.past
    total = sum(c.issue_count for c in past)
    # 반올림 오차로 목표 합계가 발급 수와 어긋나므로 한 번 맞춘다
    vals = [int(round(total * C.STATUS_MIX[s])) for s in C.STATUSES]
    _rebalance(vals, [0] * len(C.STATUSES), [total] * len(C.STATUSES), total)
    targets = dict(zip(C.STATUSES, vals))

    weights: list[dict[str, float]] = []
    for c in past:
        # close_at 기준으로 보수적으로 판정한다. issued_at 이 회차 후반이면
        # open_at 기준 판정으로는 expires_at 이 asOf 를 넘어 EXPIRE 이력을
        # asOf 이후에 써야 하는 모순이 생긴다.
        c.expirable = c.close_at + dt.timedelta(days=c.valid_days) < cat.as_of
        prof = next(
            p for lo, hi, p in C.AGE_BUCKETS if lo <= c.months_ago <= hi
        )
        w = dict(prof)
        if not c.expirable:
            w[C.EXPIRED] = 0.0
        weights.append(w)

    alloc = ipf_integer([c.issue_count for c in past], targets, weights)
    for c, a in zip(past, alloc):
        c.status_counts = a
