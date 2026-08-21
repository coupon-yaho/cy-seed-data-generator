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
CREATE INDEX idx_issuance_coupon_status ON issuances (coupon_id, status);

-- 만료 배치 · "만료 임박" 관측 지표
-- ⬆️ 승격됨 — 10_constraints_common.sql 로 옮겼다. 여기서 다시 만들면 중복 오류가 난다.
--    성능이 아니라 가용성 문제였다: 이것이 없으면 만료 배치가 도는 동안 신규 발급 INSERT 가
--    오류 1205 로 죽는다(실측 근거는 cy-be 의 docs/12-expire-lock-measurement.md).
--    나머지 넷은 아직 여기 있다 — 그것들은 느려질 뿐 막지는 않는다.
--
-- 만료 배치 청크 경계(SELECT MAX(id) ... WHERE updated_at = :committedAt)
-- ⬆️ 10_constraints_common.sql 에 idx_issuance_updated_at 로 있다. 이 처방전에 원래 없던
--    인덱스이고, 위 것처럼 '빼 둔 것을 재서 승격' 한 것이 아니라 만료 배치를 만들다
--    새로 필요해진 것이다. 근거는 docs/12 §5 의 실측(첫 청크 200,017행 → 1,001행)이다.

-- Step 0 리플레이 정렬 (created_at, id). FK 인덱스는 issuance_id 까지만 커버한다
CREATE INDEX idx_history_issuance ON issuance_histories (issuance_id, created_at);

-- V5 활성 사용 판정: canceled_at IS NULL 필터가 무인덱스
CREATE INDEX idx_usage_issuance_active ON issuance_usages (issuance_id, canceled_at);

-- 멱등 레코드 24시간 정리 배치: created_at 인덱스가 없으면 풀스캔 (ERD.sql:407)
CREATE INDEX idx_idem_created ON idempotency_records (created_at);
