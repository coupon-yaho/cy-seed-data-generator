-- ⚠️ 기본으로 실행하지 않는다. `--with-perf-indexes` 를 줬을 때만 적용된다.
--
-- ERD.sql 에 보조 인덱스가 없는 것은 누락이 아니라 **의도**다.
-- 300만 건에서 느린 쿼리를 직접 겪고, 실행계획을 보고, 인덱스를 처방해
-- 개선폭을 측정하는 것이 과제의 일부다. 시드가 미리 깔아 버리면 그 구간이 사라진다.
--
-- 그래서 이 파일은 "나중에 쓰는 처방전"으로만 존재한다.
-- 붙이기 전후로 EXPLAIN ANALYZE 를 떠서 비교하면 그대로 근거 자료가 된다.
--
-- 참고 — FK 는 InnoDB 가 자식 컬럼에 인덱스를 자동 생성한다. 그래서 아래는
-- 이미 커버되고 있어 여기 없다:
--     issuances(coupon_id) · issuances(member_id) · issuance_histories(issuance_id)
--     issuance_usages(issuance_id) · idempotency_records(member_id, issuance_id)
-- 진짜로 무인덱스인 축은 status · expires_at · canceled_at · created_at 이다.

-- V1 / 회차별 상태 집계: 지금은 issuances 풀스캔 + 필터
CREATE INDEX idx_issuance_coupon_status ON issuances (coupon_id, status, updated_at, issued_at);

-- 만료 배치 · "만료 임박" 관측 지표: 지금은 300만 행 풀스캔
CREATE INDEX idx_issuance_status_expires ON issuances (status, expires_at);

-- Step 0 리플레이 정렬 (created_at, id). FK 인덱스는 issuance_id 까지만 커버한다
CREATE INDEX idx_history_issuance ON issuance_histories (issuance_id, created_at);

-- V5 활성 사용 판정: canceled_at IS NULL 필터가 무인덱스
CREATE INDEX idx_usage_issuance_active ON issuance_usages (issuance_id, canceled_at);

-- 멱등 레코드 24시간 정리 배치: created_at 인덱스가 없으면 풀스캔 (ERD.sql:407)
CREATE INDEX idx_idem_created ON idempotency_records (created_at);

-- V2 1인 1매 위반: 케이스 둘이 각각 issuances 를 통째로 집계한다.
-- HAVING COUNT(*) > 1 은 조기 종료가 불가능해 LIMIT 이 있어도 끝까지 돈다.
--
-- CORRUPT 에는 uk_coupon_member 도 uk_coupon_code 도 없어(11 vs 12 참조) 두 GROUP BY 가
-- 쓸 인덱스가 하나도 없다. FK 자동 인덱스는 issuances(coupon_id) 단일이라 못 쓴다.
-- updated_at 을 뒤에 붙이는 이유는 규칙이 updated_at <= as_of 로 대상을 자르기 때문이다 —
-- 안 붙이면 그룹마다 PK 로 다시 내려가 300만 번 랜덤 룩업이 된다.
CREATE INDEX idx_issuance_coupon_member_updated ON issuances (coupon_id, member_id, updated_at);
CREATE INDEX idx_issuance_coupon_code_updated   ON issuances (coupon_id, code, updated_at);

-- 통계 집계(cy-be CY-202). asof_state 를 재사용할 수 없어 원본을 다시 읽는다 —
-- 통계가 세는 값은 issuances.status 이고 asof_state.state 와 다를 수 있는 것이 검증
-- 대상이라 순환이 되며, 요일·시각 분포는 issuance_histories 에만 있다.
--
-- GROUP BY coupon_id, issued_grade 가 무인덱스 풀스캔이다. status 를 뒤에 붙이는 이유는
-- used_total 이 SUM(status = 'USED') 라, 없으면 그룹마다 PK 로 다시 내려간다.
--
-- updated_at 을 마지막에 붙인다. 통계 셋도 규칙 여섯과 같은 updated_at <= as_of 컷을 쓰므로,
-- 안 붙이면 인덱스로 그룹을 만들고도 행마다 PK 로 내려가 컷을 확인한다 —
-- idx_issuance_coupon_member_updated 가 같은 이유로 updated_at 을 담는다.
-- 위 idx_issuance_coupon_status 도 같은 이유로 (status, updated_at, issued_at) 까지 담는다
-- (issued_at 은 sold_out_seconds 의 MAX(issued_at) 몫이다).
CREATE INDEX idx_issuance_coupon_grade ON issuances (coupon_id, issued_grade, status, updated_at);

-- event_type = 'ISSUE' 필터가 무인덱스인데 이력이 534만 행이다. id 를 뒤에 붙이는 이유는
-- 통계가 리플레이와 같은 창(id <= 얼린 상한)을 쓰기 때문이다.
CREATE INDEX idx_history_event_created ON issuance_histories (event_type, created_at, id);

-- 통계의 "ISSUE 이력 없는 발급건" 검산. NOT EXISTS 의 드라이빙이 issuances 이고
-- 컷이 updated_at 이라 선행 컬럼이 updated_at 인 축이 하나는 있어야 한다.
CREATE INDEX idx_issuance_updated ON issuances (updated_at, id);
