"""한글 인적사항 생성 — Faker(ko_KR) 우선, 없으면 내장 폴백.

Faker 를 쓰는 이유는 이름 분포가 실제와 비슷해지기 때문이다(김/이/박 편중 포함).
다만 100만 행에 `phone_number()` 까지 Faker 로 뽑으면 ko_KR 포맷이 지역번호라
휴대폰이 안 나오고 속도도 절반이 된다. 그래서

    name       Faker 매 행 호출      (이름 다양성이 목적)
    email      Faker 로컬파트 풀 + rank 접미사   (유일성이 목적 — uk_email_hash)
    phone      010-####-#### 직접 생성           (휴대폰 포맷이 목적)

로 나눈다. Faker 는 seed_instance() 로 시드를 고정하며, 호출 순서가 곧
난수 소비 순서이므로 members 를 id 오름차순 단일 루프로 도는 한 재현된다.
"""

from __future__ import annotations

from .rng import Rng

try:  # pragma: no cover - 환경 의존
    from faker import Faker

    HAS_FAKER = True
except ImportError:  # pragma: no cover
    Faker = None  # type: ignore[assignment]
    HAS_FAKER = False


_SURNAMES = (
    "김", "이", "박", "최", "정", "강", "조", "윤", "장", "임",
    "한", "오", "서", "신", "권", "황", "안", "송", "류", "전",
)
_GIVEN1 = ("민", "서", "지", "예", "하", "도", "주", "시", "은", "재", "수", "우")
_GIVEN2 = ("준", "연", "우", "은", "현", "빈", "아", "윤", "호", "영", "진", "희")
_DOMAINS = ("gmail.com", "naver.com", "daum.net", "kakao.com", "hanmail.net")


class PersonFactory:
    """이름·이메일·휴대폰을 결정론적으로 만든다."""

    def __init__(self, seed: int, use_faker: bool = True, pool_size: int = 50_000) -> None:
        self.rng = Rng(seed, "person")
        self.use_faker = bool(use_faker and HAS_FAKER)
        self._locals: list[str] = []
        self._domains: list[str] = list(_DOMAINS)
        self._fake = None

        if self.use_faker:
            fake = Faker("ko_KR")
            fake.seed_instance(seed & 0xFFFFFFFF)
            self._fake = fake
            seen: set[str] = set()
            for _ in range(pool_size):
                seen.add(fake.user_name())
            self._locals = sorted(seen)
            doms = {fake.free_email_domain() for _ in range(200)}
            self._domains = sorted(doms) or list(_DOMAINS)
            # 풀 생성으로 소비된 난수 상태를 이름 생성과 분리
            fake.seed_instance((seed ^ 0x5EED) & 0xFFFFFFFF)
        else:
            self._locals = [
                f"{s}{g1}{g2}".lower()
                for s in ("kim", "lee", "park", "choi", "jung", "kang")
                for g1 in ("min", "seo", "ji", "ye", "ha", "do", "ju", "si")
                for g2 in ("jun", "yeon", "woo", "eun", "hyun", "bin", "ah", "ho")
            ]

    # ── 개별 필드 ------------------------------------------------------------

    def name(self) -> str:
        if self._fake is not None:
            return self._fake.name()
        r = self.rng
        return r.choice(_SURNAMES) + r.choice(_GIVEN1) + r.choice(_GIVEN2)

    def email(self, rank: int) -> str:
        """rank 를 접미사로 박아 100만 행에서 충돌 0 을 구성적으로 보장한다."""
        local = self._locals[rank % len(self._locals)]
        domain = self._domains[(rank // len(self._locals)) % len(self._domains)]
        return f"{local}{rank}@{domain}"

    def phone(self, dup_of: int | None = None) -> str:
        """휴대폰 010-####-####. dup_of 가 주어지면 그 값에서 결정론적으로 재생성."""
        base = dup_of if dup_of is not None else self.rng.u64()
        mid = (base >> 16) % 10000
        tail = base % 10000
        return f"010-{mid:04d}-{tail:04d}"


REASONS = {
    "ISSUE": ("브랜드데이 발급", "이벤트 응모 발급", "선착순 발급"),
    "USE": ("주문 결제 사용", "매장 결제 사용", "온라인 주문 사용"),
    "CANCEL_USE": ("주문 취소로 사용 복원", "결제 취소", "부분 환불 처리"),
    "CANCEL": ("사용자 요청 취소", "고객센터 회수", "부정 발급 회수"),
    "EXPIRE": ("유효기간 만료 배치", "만료 일괄 처리"),
}


def reason_for(event_type: str, rng: Rng) -> str:
    pool = REASONS.get(event_type)
    return rng.choice(pool) if pool else None
