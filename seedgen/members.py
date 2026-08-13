"""members 100만 행 — id 오름차순 스트림 + 행별 AES-256-GCM / HMAC.

id 오름차순으로 쓰는 이유는 InnoDB 클러스터 인덱스에 순차 적재하기 위해서다.
등급은 rank 블록으로 정해지고 id 는 Feistel 순열이라, id 만 봐서는
등급을 알 수 없으면서도 "GOLD 회원 k명 비복원 추출" 이 O(k) 로 가능하다.
"""

from __future__ import annotations

import datetime as dt

from . import config as C
from .crypto import Crypto, iv_from_rng
from .idmap import IdMap
from .people import PersonFactory
from .rng import Rng

MEMBER_HISTORY_YEARS = 3
PHONE_DUP_RATIO = 0.005  # phone_hash 는 UNIQUE 가 아니다 — 중복을 의도적으로 남긴다


class GradeBlocks:
    """rank → 등급. 앞에서부터 WELCOME/SILVER/GOLD/VIP 블록."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.bounds: list[tuple[int, int, str, int]] = []
        acc = 0
        for i, (code, bit, ratio) in enumerate(C.GRADES):
            n = total - acc if i == len(C.GRADES) - 1 else int(round(total * ratio))
            self.bounds.append((acc, acc + n, code, bit))
            acc += n

    def grade_of(self, rank: int) -> str:
        for lo, hi, code, _ in self.bounds:
            if lo <= rank < hi:
                return code
        return C.GRADES[-1][0]

    def range_of(self, code: str) -> tuple[int, int]:
        for lo, hi, c, _ in self.bounds:
            if c == code:
                return lo, hi
        raise KeyError(code)

    def eligible_ranges(self, mask: int) -> list[tuple[int, int]]:
        return [
            (lo, hi) for lo, hi, _c, bit in self.bounds if bit & mask
        ]

    def heavy_ranges(self, mask: int) -> list[tuple[int, int]]:
        """각 등급 블록의 앞 30% = 파레토 상위 사용자."""
        out = []
        for lo, hi, _c, bit in self.bounds:
            if bit & mask:
                cut = lo + int((hi - lo) * C.PARETO_HEAVY_SHARE)
                if cut > lo:
                    out.append((lo, cut))
        return out

    def light_ranges(self, mask: int) -> list[tuple[int, int]]:
        out = []
        for lo, hi, _c, bit in self.bounds:
            if bit & mask:
                cut = lo + int((hi - lo) * C.PARETO_HEAVY_SHARE)
                if hi > cut:
                    out.append((cut, hi))
        return out


def generate_members(
    profile: C.Profile,
    as_of: dt.datetime,
    idmap: IdMap,
    blocks: GradeBlocks,
    crypto: Crypto,
    writer,
    use_faker: bool = True,
    progress=None,
) -> None:
    person = PersonFactory(profile.seed, use_faker=use_faker)
    rng = Rng(profile.seed, "members")
    span = int(MEMBER_HISTORY_YEARS * 365.25 * 86400)
    last_phone_base: int = 0

    for member_id in range(1, profile.members + 1):
        rank = idmap.id_to_rank(member_id)
        grade = blocks.grade_of(rank)

        name = person.name()
        email = person.email(rank)
        if rng.chance(PHONE_DUP_RATIO) and last_phone_base:
            phone = person.phone(dup_of=last_phone_base)
        else:
            last_phone_base = rng.u64()
            phone = person.phone(dup_of=last_phone_base)

        created_at = as_of - dt.timedelta(seconds=rng.below(span), microseconds=rng.below(1_000_000))

        writer.write(
            member_id,
            grade,
            crypto.encrypt(name, iv_from_rng(rng)),
            crypto.encrypt(email, iv_from_rng(rng)),
            crypto.email_hash(email),
            crypto.encrypt(phone, iv_from_rng(rng)),
            crypto.phone_hash(phone),
            created_at,
        )
        if progress is not None and member_id % 100_000 == 0:
            progress(member_id)
