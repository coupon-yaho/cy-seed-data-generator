-- 적재 후에 거는 제약. 두 데이터셋 공통.
-- uk_email_hash 는 시드가 충돌 0 을 구성적으로 보장하므로 여기서 통과해야 정상이다.

CREATE UNIQUE INDEX uk_email_hash  ON members  (email_hash);
CREATE UNIQUE INDEX uk_template_open ON coupons (template_id, open_at);

-- 보조 인덱스 중 이것만 기본으로 건다. 90_perf_indexes_optional.sql 에서 승격한 것이다.
--
-- 만료 배치가 없으면 이 인덱스도 없어도 된다. 그런데 만료 배치는 대상을 찾으려고
-- issuances 를 훑고, 훑는 동안 지나간 행을 잠근다 — 넘길 것이 없는 실행이 최악이라
-- 테이블 끝까지 훑고 supremum 까지 잠근다. 그러면 신규 발급 INSERT 가 오류 1205 로 죽는다.
-- 성능이 아니라 가용성 문제라 "나중에 처방" 으로 미룰 수 없었다.
--
-- 실측(mysql:latest, 5,000행, 만료 대상 0건): 락 5,020 → 2.
-- 수치와 재는 방법은 cy-be 의 docs/12-expire-lock-measurement.md 에 있다.
--
-- 여기(적재 후)에 두므로 300만 건 적재 성능은 그대로다.
-- cy-be 의 V11__issuance_status_expires_index.sql 과 짝이고, 이름·컬럼이 같아야
-- SchemaParityTest 가 통과한다.
CREATE INDEX idx_issuance_status_expires ON issuances (status, expires_at);

-- 만료 배치가 청크 경계를 구하는 문장(SELECT MAX(id) ... WHERE updated_at = :committedAt)이
-- 쓰는 인덱스다. updated_at 이 어디에도 없으면 그 문장이 EXPIRED 전 건을 훑는다 —
-- 이미 만료된 행이 쌓일수록 나빠지고, 진도는 JobInstance 안에서만 살아서
-- 주기마다 asOf 가 달라지는 실행은 매번 첫 청크가 afterId = 0 이다.
--
-- 실측(200,000행 · 이미 EXPIRED 150,000 누적): 첫 청크 200,017행 → 1,001행.
-- 수치는 cy-be 의 docs/12-expire-lock-measurement.md §5 에 있다.
--
-- cy-be 의 V12 와 짝이고, 이름·컬럼이 같아야 SchemaParityTest 가 통과한다.
CREATE INDEX idx_issuance_updated_at ON issuances (updated_at, id);

ALTER TABLE members             ADD FOREIGN KEY (membership_grade) REFERENCES grades (code);
ALTER TABLE coupon_templates    ADD FOREIGN KEY (brand_id)   REFERENCES brands (id);
ALTER TABLE coupons             ADD FOREIGN KEY (template_id) REFERENCES coupon_templates (id);
ALTER TABLE coupon_stocks       ADD FOREIGN KEY (coupon_id)  REFERENCES coupons (id);
ALTER TABLE issuances           ADD FOREIGN KEY (coupon_id)  REFERENCES coupons (id);
ALTER TABLE issuances           ADD FOREIGN KEY (member_id)  REFERENCES members (id);
ALTER TABLE issuances           ADD FOREIGN KEY (issued_grade) REFERENCES grades (code);
ALTER TABLE issuance_histories  ADD FOREIGN KEY (issuance_id) REFERENCES issuances (id);
ALTER TABLE issuance_usages     ADD FOREIGN KEY (issuance_id) REFERENCES issuances (id);
ALTER TABLE idempotency_records ADD FOREIGN KEY (member_id)   REFERENCES members (id);
ALTER TABLE idempotency_records ADD FOREIGN KEY (issuance_id) REFERENCES issuances (id);
ALTER TABLE verification_findings ADD FOREIGN KEY (run_id)    REFERENCES verification_runs (id);
ALTER TABLE asof_state          ADD FOREIGN KEY (run_id)      REFERENCES verification_runs (id);
ALTER TABLE coupon_stats        ADD FOREIGN KEY (run_id)      REFERENCES verification_runs (id);
ALTER TABLE coupon_stats        ADD FOREIGN KEY (coupon_id)   REFERENCES coupons (id);
ALTER TABLE grade_stats         ADD FOREIGN KEY (run_id)      REFERENCES verification_runs (id);
ALTER TABLE grade_stats         ADD FOREIGN KEY (coupon_id)   REFERENCES coupons (id);
ALTER TABLE grade_stats         ADD FOREIGN KEY (grade)       REFERENCES grades (code);
ALTER TABLE hourly_stats        ADD FOREIGN KEY (run_id)      REFERENCES verification_runs (id);

CREATE UNIQUE INDEX uk_run_params  ON verification_runs (as_of, dataset, scope, attempt);
CREATE UNIQUE INDEX uk_run_finding ON verification_findings (run_id, finding_type, target_key);

-- 정답 매니페스트의 중복 방지. cy-be 의 대조가 집합 차와 같아지는 근거다 —
-- LEFT JOIN … IS NULL 은 한쪽에 같은 키가 둘 있으면 행을 불린다.
CREATE UNIQUE INDEX uk_expected ON expected_findings (seed_run_id, finding_type, target_key);

-- CLEAN 은 대조할 묶음이 없다. 불변식을 DB 제약으로 표현한다 —
-- 정의 원본은 cy-be 의 V7__verification_run_seed_run_id.sql 이다.
ALTER TABLE verification_runs
  ADD CONSTRAINT ck_seed_run_id_corrupt_only
  CHECK (seed_run_id IS NULL OR dataset = 'CORRUPT');

-- origin 이 두 값 밖이면 그 실행이 지표에서 조용히 사라진다. cy-be 의 되읽기가
-- WHERE origin = 'BATCH' 로 좁히므로, 오타 난 값은 "판정이 없다"(NaN) 로 읽힌다.
--
-- 위치가 밀리는 경로가 실재한다 — 로더는 컬럼 목록 없이 LOAD DATA 로 넣는다.
-- 15번째 자리가 한 칸 밀리면 엉뚱한 값이 origin 에 앉는데, 그때 이 CHECK 가 적재
-- 시점에 즉시 잡는다. 없으면 varchar(6) 이 무엇이든 받고 관제에서만 티가 난다.
ALTER TABLE verification_runs
  ADD CONSTRAINT ck_verification_run_origin
  CHECK (origin IN ('SEED', 'BATCH'));

-- 퍼널 등식(issued + used + cancelled + expired = issued_total)이 조용히 깨지는 것을 막는다.
-- 통계는 issued_total 을 COUNT(*) 로, 나머지 넷을 SUM(status = 'X') 로 센다 — status 가 네 값
-- 밖이면 그 행이 분모에만 남아 대시보드에서 "발급률이 낮다" 로 보인다.
-- 오염셋도 이 네 값만 쓴다(seedgen/config.py 의 STATUSES).
ALTER TABLE issuances
  ADD CONSTRAINT ck_issuance_status
  CHECK (status IN ('ISSUED', 'USED', 'CANCELLED', 'EXPIRED'));
