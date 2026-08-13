# 검증하지 않는 것

ERD가 시드 문서에 반드시 명시하라고 요구한 항목들이다.
여기 있는 걸 검증 규칙으로 추가하면 정상셋 0건이 원천적으로 불가능해진다.
정상셋에서 매번 검출되는 규칙이 하나라도 있으면 검출 정확도라는 지표 자체가 성립하지 않는다.

## stock_per_occurrence와 total_quantity 불일치

템플릿의 `stock_per_occurrence`는 다음 회차를 열 때 몇 장을 열 것인가의 기본값이다.
이미 생성된 회차의 재고와 같을 이유가 없고, 운영자가 회차별로 조정하는 게 정상이다.

더미데이터의 과거 회차 144개는 재고를 18,000~34,000으로 의도적으로 흩뿌린다.
이걸 규칙으로 만들면 144개 회차가 전부 걸린다.

## 만료 누락

`expires_at < asOf`인데 `status = ISSUED`인 발급건은 리플레이 결과도 ISSUED라 자동으로 일치한다.

만료 지연은 결함이 아니라 배치 주기의 함수다.
`finding_type`에 넣으면 정상셋에서 매번 검출되므로 별도 관측 지표로 둔다.
시드는 이런 행을 일부러 만든다. 만료 배치가 아직 안 돈 자연스러운 상태다.

## 고아 이력

V4가 전이 연쇄로 잡는다. 별도 규칙을 만들면 같은 행이 두 규칙에 잡혀
`target_key` 집합 비교가 어긋난다.

## close_at은 완판돼도 갱신하지 않는다

갱신하면 언제 닫힐 예정이었나가 소실된다. 실제 소진 시각은 이력에서 계산한다.

```
sold_out_seconds = (해당 회차의 마지막 ISSUE 이력 created_at) − open_at
                   단, 완판된 회차만. 미달 회차는 NULL
```

## CLOSED 회차의 잔여재고 증가

취소와 만료가 재고를 복원한다.
`active_count`는 누적이 아니라 현재 살아 있는 발급 수라서 CLOSED 이후에도 줄어든다.
과거 회차는 전부 CLOSED이므로 복원된 재고가 재발급으로 이어지지도 않는다.

## 스냅샷 컬럼

`coupons`의 `name`, `policy_type`, `discount_rate`, `max_discount_amount`,
`discount_amount`, `data_grant_mb`, `min_order_amount`, `eligible_grades_mask`,
그리고 `issuances.issued_grade`.

시점 고정이라 애초에 변하지 않는다.
템플릿 정책을 바꿔도 과거 회차에 소급되면 안 되므로 불일치가 곧 정상이다.

## members 조인

리포트와 통계 쿼리는 `members`를 조인하지 않고 PII 암호화 컬럼에도 의존하지 않는다.
등급이 필요하면 `issuances.issued_grade` 스냅샷을 쓴다.
`member_id`는 암호화 대상이 아니라서 리포트가 위반 회원을 지목할 수 있다.

---

## 정상셋에 일부러 넣은 이상해 보이는 데이터

전부 위 항목에 해당하고, 각각 검증 로직의 특정 분기를 실행시키기 위한 것이다.

| 데이터 | 비율 | 실행되는 분기 |
|---|---|---|
| `issued_grade`가 현재 등급과 다름 | 등급 제한 회차의 3% | V6가 스냅샷을 쓴다는 것을 증명 |
| `expires_at < asOf`인데 `status = ISSUED` | 자연 발생 | 만료 지연 관측 지표 |
| 재고가 남은 CLOSED 회차 | 75% | 잔여재고 > 0 |
| 완판 회차 | 25% | `sold_out_seconds` NOT NULL |
| `phone_hash` 중복 | 0.5% | 전화번호 UNIQUE 없음 |
| 회차 재고가 템플릿 기본값과 다름 | 100% | 위 첫 항목 |
| 사용 → 사용취소 → 재사용 | USED의 20% | 역방향 전이, usage 다중 행 |
| USED에서 복원된 ISSUED | ISSUED의 5% | 리플레이가 종단이 아닌 경로를 밟음 |
