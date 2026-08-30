"""분포 상수 단일 출처.

여기 있는 값은 전부 PRD.md / ERD.sql 에서 온 것이고, 매직넘버가 다른 모듈에
흩어지지 않도록 이 파일에만 둔다. 스케일을 바꿔도 비율은 여기서 파생되므로
PRD.md:1782 (리스크 R4, "이력을 100만 건으로 줄이고 분포 비율은 유지") 대응이 공짜로 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# 등급 (ERD.sql:1-4, PRD.md:209-214)
# ─────────────────────────────────────────────────────────────────────────────

# 순서 = 랭크 블록 순서. 앞에서부터 rank 를 채운다.
GRADES: list[tuple[str, int, float]] = [
    # (code, bit_value, 비중)
    ("WELCOME", 1, 0.50),
    ("SILVER", 2, 0.30),
    ("GOLD", 4, 0.15),
    ("VIP", 8, 0.05),
]
GRADE_BIT = {code: bit for code, bit, _ in GRADES}
GRADE_CODES = [code for code, _, _ in GRADES]

MASK_ALL = 1 | 2 | 4 | 8       # 전체 공개
MASK_SILVER_UP = 2 | 4 | 8     # SILVER↑
MASK_GOLD_UP = 4 | 8           # GOLD↑
MASK_VIP = 8                   # VIP 전용

# ─────────────────────────────────────────────────────────────────────────────
# 브랜드 12개 · 스케줄 12개 (PRD.md:181-192)
# ─────────────────────────────────────────────────────────────────────────────

DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

PERCENT_CAPPED = "PERCENT_CAPPED"
FIXED_AMOUNT = "FIXED_AMOUNT"
# DATA_GRANT 는 없앴다 — cy-be 가 지원한 적이 없다. 도메인 enum
# (core.coupontemplate.domain.CouponPolicyType)에 그 값이 없고 할인 계산 switch 에도
# 케이스가 없어서, 이 시드가 만든 DATA_GRANT 회차는 쿠폰 API 가 읽는 순간 터졌다.
# CY-589 가 관리자 enum·DB 제약까지 두 종류로 맞추면서 시드도 따라간다.
#
# 대체 정책을 FIXED_AMOUNT 로 고른 것은 임의가 아니다 — issuances._discount 가
# PERCENT_CAPPED 에서만 rng.randint 를 부른다. FIXED_AMOUNT 와 DATA_GRANT 는 둘 다
# 난수를 안 쓰므로, 이 교체는 **난수 스트림을 건드리지 않는다.** 그래서 같은 시드로
# 다시 깔면 id·선택이 전부 그대로이고 오염 매니페스트(800행)가 보존된다.


@dataclass(frozen=True)
class BrandSpec:
    name: str
    category: str
    nth_week: int
    day_of_week: str
    start_hour: int
    policy_type: str
    mask: int
    # 정책별 파라미터
    discount_rate: int | None = None
    max_discount_amount: int | None = None
    discount_amount: int | None = None
    min_order_amount: int = 0
    duration_hours: int = 6


BRANDS: list[BrandSpec] = [
    BrandSpec("모카빈", "카페", 1, "TUE", 14, PERCENT_CAPPED, MASK_ALL,
              discount_rate=20, max_discount_amount=20000, min_order_amount=10000),
    BrandSpec("씨네플러스", "영화", 1, "THU", 18, FIXED_AMOUNT, MASK_ALL,
              discount_amount=5000, min_order_amount=12000),
    BrandSpec("버거하우스", "외식", 1, "FRI", 11, PERCENT_CAPPED, MASK_ALL,
              discount_rate=15, max_discount_amount=9000, min_order_amount=8000),
    BrandSpec("프레시마트", "마트", 2, "TUE", 10, FIXED_AMOUNT, MASK_SILVER_UP,
              discount_amount=8000, min_order_amount=30000),
    BrandSpec("북스토리", "서점", 2, "WED", 15, PERCENT_CAPPED, MASK_ALL,
              discount_rate=10, max_discount_amount=5000, min_order_amount=15000),
    BrandSpec("필름아레나", "영화", 2, "FRI", 19, FIXED_AMOUNT, MASK_GOLD_UP,
              discount_amount=7000, min_order_amount=14000),
    BrandSpec("스포츠존", "스포츠", 3, "MON", 12, PERCENT_CAPPED, MASK_ALL,
              discount_rate=25, max_discount_amount=30000, min_order_amount=40000),
    BrandSpec("뷰티랩", "뷰티", 3, "WED", 16, PERCENT_CAPPED, MASK_SILVER_UP,
              discount_rate=30, max_discount_amount=24000, min_order_amount=20000),
    BrandSpec("딜리버리고", "배달", 3, "FRI", 17, FIXED_AMOUNT, MASK_ALL,
              discount_amount=3000),
    BrandSpec("트래블온", "여행", 4, "TUE", 13, FIXED_AMOUNT, MASK_GOLD_UP,
              discount_amount=50000, min_order_amount=300000),
    BrandSpec("게임패스", "게임", 4, "THU", 20, FIXED_AMOUNT, MASK_ALL,
              discount_amount=5000),
    BrandSpec("헬스클럽", "피트니스", 4, "FRI", 7, PERCENT_CAPPED, MASK_VIP,
              discount_rate=40, max_discount_amount=60000, min_order_amount=100000),
]

# 현재(미래) 회차 3건 — PRD.md:296-300. (브랜드 index, 재고)
CURRENT_COUPONS: list[tuple[int, int]] = [(0, 10_000), (5, 3_000), (8, 2_000)]

# ─────────────────────────────────────────────────────────────────────────────
# 상태 · 이벤트 (PRD.md:228-236, ERD.md:388)
# ─────────────────────────────────────────────────────────────────────────────

ISSUED, USED, CANCELLED, EXPIRED = "ISSUED", "USED", "CANCELLED", "EXPIRED"
STATUSES = [ISSUED, USED, CANCELLED, EXPIRED]

EV_ISSUE, EV_USE, EV_CANCEL_USE, EV_CANCEL, EV_EXPIRE = (
    "ISSUE", "USE", "CANCEL_USE", "CANCEL", "EXPIRE",
)

# 합법 전이. (from_status, event) -> to_status. from_status None 은 신규 발급.
# ⚠️ **(from, event) → to 맵이 아니라 삼중항 목록이다.** CANCEL_USE 는 결과가 둘이라
#    맵으로는 표현이 안 된다 — 만료 시각을 넘긴 뒤의 사용 취소는 ISSUED 가 아니라
#    EXPIRED 로 간다(cy-be CouponStateMachine.cancelUse, 그리고 CouponCancelUseService
#    가 그 갈래에서 재고까지 되돌린다). 맵으로 두면 그 정상 이력이 V4 오탐이 된다.
LEGAL_TRANSITIONS: tuple[tuple[str | None, str, str], ...] = (
    (None, EV_ISSUE, ISSUED),
    (ISSUED, EV_USE, USED),
    (USED, EV_CANCEL_USE, ISSUED),
    (USED, EV_CANCEL_USE, EXPIRED),
    (ISSUED, EV_CANCEL, CANCELLED),
    (ISSUED, EV_EXPIRE, EXPIRED),
)
TERMINAL_STATES = {CANCELLED, EXPIRED}

# 상태 분포 (PRD.md:316)
STATUS_MIX = {ISSUED: 0.40, USED: 0.35, EXPIRED: 0.15, CANCELLED: 0.10}

# 시간축 기울기 (PRD.md:327-331). 회차 나이(개월) 버킷별 상태 선호 가중치.
#   최근 1~2개월 → ISSUED 우세 / 3~6개월 → USED 우세 / 7~12개월 → EXPIRED 우세
AGE_BUCKETS: list[tuple[int, int, dict[str, float]]] = [
    (1, 2, {ISSUED: 0.62, USED: 0.24, EXPIRED: 0.03, CANCELLED: 0.11}),
    (3, 6, {ISSUED: 0.30, USED: 0.50, EXPIRED: 0.10, CANCELLED: 0.10}),
    (7, 12, {ISSUED: 0.24, USED: 0.32, EXPIRED: 0.34, CANCELLED: 0.10}),
    (13, 999, {ISSUED: 0.20, USED: 0.30, EXPIRED: 0.40, CANCELLED: 0.10}),
]

# 하위 변형 비율 (PRD.md:318-320)
USED_RESTORE_RATIO = 0.20      # USED 중 사용→취소→재사용 이력을 가진 비율
ISSUED_RESTORED_RATIO = 0.05   # ISSUED 중 USED 에서 복원된 비율
EXPIRING_SOON_RATIO = 0.01     # 전체 중 24시간 내 만료
GRADE_DRIFT_RATIO = 0.03       # issued_grade 스냅샷 ≠ 현재 등급 (강등 시뮬레이션)

# 파레토 (PRD.md:314, 308)
PARETO_HEAVY_SHARE = 0.30      # 상위 30% 가
PARETO_HEAVY_WEIGHT = 0.80     # 이력의 80% 를 가진다

# ─────────────────────────────────────────────────────────────────────────────
# 규모 (scale 1.0 기준)
# ─────────────────────────────────────────────────────────────────────────────

MEMBERS = 1_000_000
ISSUANCES = 3_000_000
TOTAL_STOCK = 3_800_000        # 과거 회차 재고 합 (PRD.md:294)
STOCK_MIN, STOCK_MAX = 18_000, 34_000
SELLOUT_RATIO = 0.25           # 완판 회차 비율 (나머지는 60~98% 미달)
SELL_RATE_MIN, SELL_RATE_MAX = 0.60, 0.98
VALID_DAYS_MIN, VALID_DAYS_MAX = 30, 180
IDEMPOTENCY_RECORDS = 100_000

PAST_MONTHS_CLEAN = 12         # 12 브랜드 × 12개월 = 144 회차
PAST_MONTHS_CORRUPT = 24       # 오염셋은 V1(회차 그레인) 키 200개가 필요해서 24개월

# ─────────────────────────────────────────────────────────────────────────────
# 검증 규칙 · target_key 규약 (ERD.sql:468-498, 최신 테이블명으로 통일)
# ─────────────────────────────────────────────────────────────────────────────

V1 = "STOCK_MISMATCH"
V2 = "DUP_PER_MEMBER"
V3 = "REPLAY_MISMATCH"
V4 = "ILLEGAL_TRANSITION"
V5 = "USAGE_MISMATCH"
V6 = "GRADE_VIOLATION"

FINDING_TYPES = [V1, V2, V3, V4, V5, V6]

KEY_COUPON = "COUPON"        # coupons.id      (회차)
KEY_ISSUANCE = "ISSUANCE"    # issuances.id    (발급건)
KEY_MEMBER = "MEMBER"        # members.id
KEY_HISTORY = "HISTORY"      # issuance_histories.id


def key_coupon(cid: int) -> str:
    return f"{KEY_COUPON}:{cid}"


def key_coupon_member(cid: int, mid: int) -> str:
    return f"{KEY_COUPON}:{cid}|{KEY_MEMBER}:{mid}"


def key_issuance(iid: int) -> str:
    return f"{KEY_ISSUANCE}:{iid}"


def key_history(hid: int) -> str:
    return f"{KEY_HISTORY}:{hid}"


# ─────────────────────────────────────────────────────────────────────────────
# 오염 7유형 (ERD.sql:527-539)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CorruptSpec:
    type_id: int
    count: int
    rules: tuple[str, ...]
    desc: str


CORRUPT_TYPES: list[CorruptSpec] = [
    CorruptSpec(1, 100, (V1,), "재고는 줄었는데 history 에 ISSUE 기록 없음"),
    CorruptSpec(2, 100, (V3,), "history 는 USED 인데 issuances.status 는 ISSUED"),
    CorruptSpec(3, 100, (V1, V4), "CANCEL_USE 가 2번 기록되어 재고 이중 복원"),
    CorruptSpec(4, 100, (V4,), "종단 상태(EXPIRED)에서 USED 로 불법 전이"),
    CorruptSpec(5, 100, (V2,), "동일 쿠폰(code)이 두 유저에게 발급"),
    CorruptSpec(6, 100, (V2,), "동일 유저가 같은 회차에서 2건 발급"),
    CorruptSpec(7, 100, (V5,), "status 는 ISSUED 인데 활성 usages 행이 남아 있음"),
]
CORRUPT_TOTAL = sum(c.count for c in CORRUPT_TYPES)          # 700 주입
EXPECTED_ROWS = sum(c.count * len(c.rules) for c in CORRUPT_TYPES)  # 800 정답행

# V1(회차 그레인) 키가 겹치면 uk_expected 가 튕기므로 유형 1 · 3 의 회차를 분리한다.
CORRUPT_V1_TYPE1_SLOT = (0, 100)      # 과거 회차 인덱스 [0, 100)
CORRUPT_V1_TYPE3_SLOT = (100, 200)    # 과거 회차 인덱스 [100, 200)
CORRUPT_FREE_SLOT_FROM = 200          # 나머지 유형은 여기부터


# ─────────────────────────────────────────────────────────────────────────────
# 프로파일
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Profile:
    """스케일과 데이터셋 종류를 묶은 실행 파라미터."""

    dataset: str = "clean"          # clean | corrupt
    scale: float = 1.0
    seed: int = 20260812
    seed_run_id: int = 1
    plant_v6: bool = False

    members: int = field(init=False)
    issuances: int = field(init=False)
    past_months: int = field(init=False)
    idempotency: int = field(init=False)

    def __post_init__(self) -> None:
        if self.dataset not in ("clean", "corrupt"):
            raise ValueError(f"dataset must be clean|corrupt, got {self.dataset!r}")
        self.members = max(1_000, int(MEMBERS * self.scale))
        self.issuances = max(1_000, int(ISSUANCES * self.scale))
        self.past_months = (
            PAST_MONTHS_CLEAN if self.dataset == "clean" else PAST_MONTHS_CORRUPT
        )
        self.idempotency = max(100, int(IDEMPOTENCY_RECORDS * self.scale))

    @property
    def is_corrupt(self) -> bool:
        return self.dataset == "corrupt"

    @property
    def past_coupons(self) -> int:
        return len(BRANDS) * self.past_months

    def check_corrupt_capacity(self) -> None:
        """오염셋이 700건을 심을 수 있는 규모인지 사전 검사."""
        if not self.is_corrupt:
            return
        need = CORRUPT_FREE_SLOT_FROM
        if self.past_coupons < need:
            raise ValueError(
                f"오염셋에 회차가 부족합니다: {self.past_coupons}개 < {need}개. "
                f"유형 1·3 이 각각 서로 다른 100개 회차를 요구합니다."
            )
        # 유형 5·6 은 회차당 1건씩만 쓰므로 잔여 회차 수도 확인
        free = self.past_coupons - CORRUPT_FREE_SLOT_FROM
        if free < 1:
            raise ValueError("오염셋에 유형 2·4·5·6·7 용 잔여 회차가 없습니다.")
