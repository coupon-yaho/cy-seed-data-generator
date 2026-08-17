-- 적재 후에 거는 제약. 두 데이터셋 공통.
-- uk_email_hash 는 시드가 충돌 0 을 구성적으로 보장하므로 여기서 통과해야 정상이다.

CREATE UNIQUE INDEX uk_email_hash  ON members  (email_hash);
CREATE UNIQUE INDEX uk_template_open ON coupons (template_id, open_at);

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
