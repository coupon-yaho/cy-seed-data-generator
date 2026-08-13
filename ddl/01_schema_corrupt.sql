-- expected_findings 는 CORRUPT 스키마에만 존재한다 (ERD.sql:524-525).
-- CLEAN 셋의 기대값은 "0건"이라 정답 테이블 자체가 필요 없다.

CREATE TABLE expected_findings (
  id            bigint       NOT NULL AUTO_INCREMENT,
  seed_run_id   bigint       NOT NULL COMMENT '어느 주입 실행이 만든 정답인가 (verification_runs 와 별도 네임스페이스)',
  corrupt_type  tinyint      NOT NULL COMMENT '오염 유형 1~7 (8 = V6 수동 확인용, 700 집계 밖)',
  finding_type  varchar(40)  NOT NULL COMMENT 'V1~V6 상수 — findings 와 동일 어휘',
  target_key    varchar(64)  NOT NULL COMMENT 'findings 와 동일 형식',
  campaign_id   bigint       COMMENT 'coupons.id',
  member_id     bigint,
  coupon_id     bigint       COMMENT 'issuances.id',
  history_id    bigint,
  note          varchar(200),
  created_at    datetime(6)  NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;
