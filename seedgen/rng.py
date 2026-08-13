"""결정론 난수 — SplitMix64 기반 스트림 분리.

`random` 전역을 쓰지 않는 이유: 생성 순서가 조금만 바뀌어도 모든 값이 흔들려
"같은 시드면 같은 데이터"가 깨진다. 테이블/용도별로 독립 스트림을 만들면
회원 생성 로직을 고쳐도 발급 데이터가 그대로 나온다.
"""

from __future__ import annotations

MASK64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15


def splitmix64(x: int) -> int:
    """SplitMix64 finalizer. 64비트 정수 → 64비트 정수 (결정론 해시)."""
    x = (x + _GOLDEN) & MASK64
    z = x
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31)


def stream_seed(seed: int, name: str) -> int:
    """(전역 시드, 스트림 이름) → 스트림 초기 상태."""
    h = 0xCBF29CE484222325
    for b in name.encode("utf-8"):
        h = ((h ^ b) * 0x100000001B3) & MASK64
    return splitmix64((seed & MASK64) ^ h)


class Rng:
    """단일 스트림. 순차 소비형."""

    __slots__ = ("_state",)

    def __init__(self, seed: int, name: str = "") -> None:
        self._state = stream_seed(seed, name) if name else (seed & MASK64)

    def fork(self, name: str) -> "Rng":
        """현재 상태에서 파생된 하위 스트림 (부모 상태는 건드리지 않는다)."""
        return Rng(splitmix64(self._state ^ stream_seed(0, name)))

    def u64(self) -> int:
        self._state = (self._state + _GOLDEN) & MASK64
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def random(self) -> float:
        """[0.0, 1.0)"""
        return (self.u64() >> 11) * (1.0 / (1 << 53))

    def below(self, n: int) -> int:
        """[0, n)"""
        if n <= 0:
            raise ValueError("n must be positive")
        return self.u64() % n

    def randint(self, a: int, b: int) -> int:
        """[a, b] 양끝 포함"""
        return a + self.below(b - a + 1)

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.random()

    def chance(self, p: float) -> bool:
        return self.random() < p

    def choice(self, seq):
        return seq[self.below(len(seq))]

    def shuffled(self, items: list):
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def weighted_key(self, weights: dict[str, float]) -> str:
        total = sum(weights.values())
        r = self.random() * total
        acc = 0.0
        last = None
        for k, w in weights.items():
            acc += w
            last = k
            if r < acc:
                return k
        return last  # 부동소수 오차 대비

    def sample_below(self, n: int, k: int) -> list[int]:
        """[0, n) 에서 k 개를 비복원 추출. O(k) 메모리 (희소 Fisher-Yates)."""
        if k > n:
            raise ValueError(f"sample_below: k={k} > n={n}")
        swapped: dict[int, int] = {}
        out: list[int] = []
        for i in range(k):
            j = i + self.below(n - i)
            vj = swapped.get(j, j)
            vi = swapped.get(i, i)
            out.append(vj)
            swapped[j] = vi
        return out


class SampleCursor:
    """비복원 추출을 '한 번에 k개'가 아니라 '필요할 때 하나씩' 뽑는 커서.

    회차당 최대 34,000명을 뽑는데 리스트를 미리 만들면 메모리가 튄다.
    희소 Fisher-Yates 상태만 들고 lazily 진행한다.
    """

    __slots__ = ("_rng", "_n", "_i", "_swapped")

    def __init__(self, rng: Rng, n: int) -> None:
        self._rng = rng
        self._n = n
        self._i = 0
        self._swapped: dict[int, int] = {}

    @property
    def remaining(self) -> int:
        return self._n - self._i

    def take(self) -> int:
        if self._i >= self._n:
            raise RuntimeError("SampleCursor exhausted")
        i = self._i
        j = i + self._rng.below(self._n - i)
        vj = self._swapped.get(j, j)
        self._swapped[j] = self._swapped.get(i, i)
        self._i += 1
        return vj
