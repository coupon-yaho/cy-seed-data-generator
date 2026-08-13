"""member_id ↔ rank 양방향 순열.

등급은 rank 블록으로 배정한다(앞 50% WELCOME … 뒤 5% VIP). 그런데 그 rank 를
그대로 PK 로 쓰면 id 만 보고 등급이 읽히는 부자연스러운 데이터가 된다.

Feistel 네트워크 + cycle-walking 으로 [0, n) 위의 전단사를 만들어
  - members 는 id 오름차순으로 생성(= InnoDB 클러스터 인덱스에 순차 적재)하고
  - "GOLD 회원 중 k명 비복원 추출" 은 rank 구간에서 뽑아 id 로 변환한다
둘 다 성립시킨다.
"""

from __future__ import annotations

from .rng import splitmix64, stream_seed

ROUNDS = 4


class IdMap:
    """[0, n) 위의 결정론적 전단사."""

    __slots__ = ("n", "_half", "_mask", "_keys")

    def __init__(self, n: int, seed: int) -> None:
        if n < 2:
            raise ValueError("n must be >= 2")
        self.n = n
        bits = max(2, (n - 1).bit_length())
        if bits % 2:
            bits += 1
        self._half = bits // 2
        self._mask = (1 << self._half) - 1
        base = stream_seed(seed, "idmap")
        self._keys = [splitmix64(base ^ (r * 0x1000193)) for r in range(ROUNDS)]

    def _f(self, r: int, x: int) -> int:
        return splitmix64(self._keys[r] ^ x) & self._mask

    def _enc(self, v: int) -> int:
        left = v >> self._half
        right = v & self._mask
        for r in range(ROUNDS):
            left, right = right, left ^ self._f(r, right)
        return (left << self._half) | right

    def _dec(self, v: int) -> int:
        left = v >> self._half
        right = v & self._mask
        for r in range(ROUNDS - 1, -1, -1):
            left, right = right ^ self._f(r, left), left
        return (left << self._half) | right

    def rank_to_index(self, rank: int) -> int:
        """rank(0-based) → member id(0-based). cycle-walking 으로 [0, n) 유지."""
        v = self._enc(rank)
        while v >= self.n:
            v = self._enc(v)
        return v

    def index_to_rank(self, index: int) -> int:
        v = self._dec(index)
        while v >= self.n:
            v = self._dec(v)
        return v

    # 1-based member id 편의 래퍼 ------------------------------------------------

    def rank_to_id(self, rank: int) -> int:
        return self.rank_to_index(rank) + 1

    def id_to_rank(self, member_id: int) -> int:
        return self.index_to_rank(member_id - 1)
