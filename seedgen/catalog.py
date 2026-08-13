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
    data_grant_mb: int | None
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
            self.data_grant_mb, self.min_order_amount, self.valid_days,
            self.eligible_grades_mask, self.open_at, self.close_at, self.status,
            self.created_at,
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
    row_rem = [row_totals[i] - sum(out[i]) for i in range(n)]
    col_rem = [col_totals[cols[j]] - sum(out[i][j] for i in range(n)) for j in range(m)]

    cells = sorted(
        ((x[i][j] - out[i][j], i, j) for i in range(n) for j in range(m) if x[i][j] > 0),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    for _frac, i, j in cells:
        if row_rem[i] > 0 and col_rem[j] > 0:
            out[i][j] += 1
            row_rem[i] -= 1
            col_rem[j] -= 1
    # 잔여 복구 (가중치 0 인 칸은 건드리지 않는다)
    if any(row_rem) or any(col_rem):
        for i in range(n):
            while row_rem[i] > 0:
                for j in range(m):
                    if col_rem[j] > 0 and x[i][j] > 0:
                        out[i][j] += 1
                        row_rem[i] -= 1
                        col_rem[j] -= 1
                        break
                else:
                    raise RuntimeError(
                        "상태 배분 실패: 만료 가능 회차 용량이 부족합니다. "
                        "valid_days 범위나 상태 비율을 조정하세요."
                    )
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
                b.data_grant_mb, b.min_order_amount,
                rng.randint(C.VALID_DAYS_MIN, C.VALID_DAYS_MAX),
                b.nth_week, b.day_of_week, f"{b.start_hour:02d}:00:00",
                b.duration_hours,
                max(1, int(round((C.STOCK_MIN + C.STOCK_MAX) / 2 * stock_scale))),
                b.mask, True,
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
                    discount_amount=b.discount_amount, data_grant_mb=b.data_grant_mb,
                    min_order_amount=b.min_order_amount, valid_days=valid_days,
                    eligible_grades_mask=b.mask, open_at=open_at, close_at=close_at,
                    status="CLOSED", created_at=open_at - dt.timedelta(days=1),
                    total_quantity=stock, months_ago=months_ago,
                    duration_hours=b.duration_hours,
                )
            )
            next_id += 1

    # ── 현재 회차 3건: open_at = 시드 실행 시각 + 5분 (PRD.md:276-279)
    for bi, stock in C.CURRENT_COUPONS:
        b = C.BRANDS[bi]
        open_at = (as_of + dt.timedelta(minutes=5)).replace(microsecond=0)
        close_at = open_at + dt.timedelta(hours=b.duration_hours)
        coupons.append(
            Coupon(
                id=next_id, template_id=bi + 1, brand_id=bi + 1,
                name=f"{b.name} 브랜드데이 {open_at:%Y-%m} (예정)",
                policy_type=b.policy_type, discount_rate=b.discount_rate,
                max_discount_amount=b.max_discount_amount,
                discount_amount=b.discount_amount, data_grant_mb=b.data_grant_mb,
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
    targets = {s: int(round(total * C.STATUS_MIX[s])) for s in C.STATUSES}
    _rebalance(
        [targets[s] for s in C.STATUSES],
        [0] * len(C.STATUSES),
        [total] * len(C.STATUSES),
        total,
    )
    # _rebalance 는 리스트를 갱신하므로 다시 dict 로
    vals = [targets[s] for s in C.STATUSES]
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
