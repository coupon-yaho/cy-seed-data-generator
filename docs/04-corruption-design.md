# 오염 데이터 설계

CLEAN이 정상 데이터에서 오탐이 안 난다는 걸 증명한다면,
CORRUPT는 심은 것을 정확히 그것만 잡는다는 걸 증명한다.

둘 다 필요한 이유는 `finding_count`만으로는 아무것도 증명하지 못하기 때문이다.
오탐 350건에 미검출 350건이어도 count는 700이다.
그래서 정답 매니페스트와 집합 비교가 있어야 한다.

```
누락 = expected_findings  MINUS  verification_findings
오탐 = verification_findings  MINUS  expected_findings
합격 = 누락 0건 AND 오탐 0건        조인 키 = (finding_type, target_key)
```

## 폭발 반경 닫기

오염을 심을 때 진짜 난이도는 망가뜨리는 게 아니라 의도한 규칙 하나만 울리게 만드는 것이었다.
부수적으로 다른 규칙이 울리면 배치가 심지 않은 것까지 잡아 오탐이 되고 합격 판정이 깨진다.

유형 4를 예로 들면 분명해진다.
`EXPIRED`인 발급건 뒤에 `USE(EXPIRED→USED)` 이력만 추가하면 네 개가 한꺼번에 울린다.
V4는 의도한 것이지만, V3는 리플레이가 USED인데 `status`가 EXPIRED라서,
V5는 리플레이가 USED인데 활성 usage가 0건이라서,
V1은 리플레이 활성 수가 하나 늘어 재고와 어긋나서 울린다.

그래서 나머지 세 축을 전부 USED 기준으로 같이 맞춘다.
`issuances.status`를 USED로 두고, 활성 usage 1행을 발행하고, 재고 집계에 포함시킨다.
그러면 V4만 남는다.

| 유형 | 주입 | 부수 보정 | 남는 규칙 |
|---|---|---|---|
| 1 | 회차 `active_count`를 참값 +1 | 없음. 고아 행을 만들지 않는다 | V1 |
| 2 | 최종 USED 건의 `status`만 `ISSUED`로 | 이력과 usage는 정상 유지 | V3 |
| 3 | `CANCEL_USE` 이력 1행 중복, `active_count` −1 | 최종 상태 불변(USED) | V1, V4 |
| 4 | `USE(EXPIRED→USED)` 이력 추가 | status·usage·재고를 USED 기준으로 동시 보정 | V4 |
| 5 | 동일 `code`를 다른 회원 행으로 복제 | 정상 ISSUE 이력 부여 | V2 |
| 6 | 동일 `(회차, 회원)`에 다른 code로 1건 더 | 정상 ISSUE 이력 부여 | V2 |
| 7 | 복원 건의 usage `canceled_at`을 NULL로 | 없음. 리플레이 상태 불변 | V5 |

유형 2와 7은 둘 다 "status와 실제가 어긋난" 상황처럼 보이는데 서로를 오염시키지 않는다.
V5를 `asof_state ↔ issuance_usages`로만 정의했기 때문이다(Step 3).
`issuances.status`를 읽지 않으므로 "status는 틀렸지만 usage는 맞는" 유형 2가 V5를 건드리지 않는다.
우연이 아니라, 결정론적 규칙이 현재 행을 읽는 규칙보다 먼저 돌아야 한다는
ERD의 Step 순서 설계와 같은 방향이다.

## CORRUPT만 24개월인 이유

유형 1과 3이 둘 다 V1을 울리는데, V1의 `target_key`는 `COUPON:{회차id}`다.
각각 100개씩 서로 다른 회차가 필요하니 200개가 있어야 한다.
CLEAN 달력의 과거 회차는 144개뿐이다.

같은 회차에 둘 다 심으면 `uk_expected`가 튕긴다.
게다가 `+1`과 `−1`이 상쇄되어 오염이 사라질 수도 있다.

오염셋만 달력을 24개월로 늘려 291회차를 만들고 회차를 물리적으로 분리했다.

```
회차 #1  ~ #100   유형 1 전용   → V1 100키
회차 #101 ~ #200  유형 3 전용   → V1 100키 + V4 100키
회차 #201 ~ #291  유형 2·4·5·6·7 (발급건·이력 그레인이라 키가 안 겹친다)
```

CLEAN은 147회차 그대로다. 검출 정확도 판정에 회차 수는 영향을 주지 않는다.

## 주입 700건에 정답 800행

ERD가 규칙과 오염 유형은 1:1이 아니라고 못박았다.
하나의 규칙이 여러 유형을 잡고, 유형 3만 규칙 두 개를 울린다.

| 유형 | 주입 | 규칙 | 정답행 |
|---|---|---|---|
| 1 재고는 줄었는데 ISSUE 기록 없음 | 100 | V1 | 100 |
| 2 이력은 USED인데 status는 ISSUED | 100 | V3 | 100 |
| 3 CANCEL_USE 이중 기록 | 100 | V1, V4 | 200 |
| 4 종단 상태에서 USED로 불법 전이 | 100 | V4 | 100 |
| 5 동일 code가 두 유저에게 | 100 | V2 | 100 |
| 6 동일 유저가 같은 회차에서 2건 | 100 | V2 | 100 |
| 7 status는 ISSUED인데 활성 usage 잔존 | 100 | V5 | 100 |
| | 700 | | 800 |

규칙별로는 V1 200, V2 200, V3 100, V4 200, V5 100, V6 0이 된다.

V6는 오염 유형이 없다. 정상셋 0건으로만 검증하고,
`--plant-v6`로 등급 위반 1건을 수동으로 심어 눈으로 확인할 수 있다.
그때만 `corrupt_type=8`로 기록하고 700 집계에서 뺀다.

## 정답을 심으면서 같이 쓴다

오염을 심는 코드가 자기가 뭘 심었는지 알고 있으므로 그 자리에서 `expected_findings`를 쓴다.
나중에 데이터를 다시 훑어 정답을 추측하지 않는다.

```python
self.record(
    C.V4, C.key_history(hid), corrupt_type=4, history_id=hid,
    note="종단 상태 EXPIRED 에서 USED 로 불법 전이",
    expected="종단 상태에서 전이 없음", actual="EXPIRED→USED",
)
```

`record()`는 `(finding_type, target_key)` 중복을 즉시 예외로 막는다.
`uk_expected` 위반을 적재 시점이 아니라 생성 시점에 잡기 위해서다.

생성이 끝나면 `assert_complete()`가 유형 × 규칙 행렬을 스펙과 대조한다.
하나라도 어긋나면 적재를 시작하지 않는다.

```
오염 결과: {"injections": 700, "expected_rows": 800,
            "by_corrupt_type": {1:100, 2:100, 3:200, 4:100, 5:100, 6:100, 7:100},
            "by_finding_type": {V1:200, V2:200, V3:100, V4:200, V5:100}}
```

## 적재 후 패치하지 않는다

오염은 생성 시점에 들어간다. 적재한 뒤 `UPDATE`나 `DELETE`로 고치지 않는다.

2코어 서버에서 300만 행 대상 대량 UPDATE는 비싸다.
패치 스크립트는 실행 순서에 의존하는데 생성 시점 주입은 시드값의 함수다.
그리고 심는 코드가 곧 정답을 쓰는 코드라 둘이 어긋날 수 없다.

부작용으로 `coupon_stocks.active_count` 계산이 단순해졌다.
오염 후 최종 리플레이 상태에서 집계하므로 유형 2, 4, 5, 6, 7은 자동으로 정합하고,
의도적으로 어긋내는 유형 1과 3만 `stock_delta`로 처리한다.

## CORRUPT 스키마에 걸지 않는 제약

`uk_coupon_member`는 유형 6이, `uk_coupon_code`는 유형 5가,
`CHECK (active_count …)`는 유형 1과 3이 위반해야 한다.

이 셋이 CLEAN에만 있다는 사실 자체가 설계 근거다.
불변식은 애플리케이션이 아니라 DB 제약으로 표현한다는 원칙을 데이터로 보여준다.
정상셋에서는 DB가 물리적으로 막고, 오염셋에서는 그 방어선을 풀어야만 데이터가 들어간다.

## 검증 결과

`--scale 0.2` 오염셋(발급 60만, 이력 107만) 기준이다.

```
규칙별 검출: STOCK_MISMATCH=200 · DUP_PER_MEMBER=200 · REPLAY_MISMATCH=100
           · ILLEGAL_TRANSITION=200 · USAGE_MISMATCH=100 · GRADE_VIOLATION=0
expected 800건 / 검출 800건
  누락 0건 · 오탐 0건
✓ CORRUPT 집합 일치
```

`--plant-v6`를 켜면 801/801로 V6 1건이 추가된다.
