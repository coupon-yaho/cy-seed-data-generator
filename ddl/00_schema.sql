-- 적재 최적 스키마: 테이블 + PK 만. FK · UNIQUE · CHECK · 보조 인덱스는 적재 후에 건다.
-- ERD.sql 의 컬럼 정의를 그대로 따르되, 산문 COMMENT 는 원본 문서에 두고 여기서는 생략한다.

SET NAMES utf8mb4;

CREATE TABLE grades (
  code       varchar(10) NOT NULL COMMENT 'WELCOME / SILVER / GOLD / VIP',
  bit_value  tinyint     NOT NULL COMMENT '1 / 2 / 4 / 8',
  PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE members (
  id                bigint      NOT NULL AUTO_INCREMENT,
  membership_grade  varchar(10) NOT NULL,
  name_enc          varbinary(256),
  email_enc         varbinary(256),
  email_hash        char(64)    COMMENT 'HMAC-SHA256 블라인드 인덱스',
  phone_enc         varbinary(256),
  phone_hash        char(64)    COMMENT 'HMAC-SHA256. 전화번호는 중복 허용',
  created_at        datetime(6) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE brands (
  id        bigint      NOT NULL AUTO_INCREMENT,
  name      varchar(50) NOT NULL,
  category  varchar(20) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE coupon_templates (
  id                    bigint       NOT NULL AUTO_INCREMENT,
  brand_id              bigint       NOT NULL,
  name                  varchar(100) NOT NULL,
  policy_type           varchar(20)  NOT NULL COMMENT 'PERCENT_CAPPED / FIXED_AMOUNT / DATA_GRANT',
  discount_rate         int,
  max_discount_amount   int,
  discount_amount       int,
  data_grant_mb         int,
  min_order_amount      int,
  valid_days            int          NOT NULL,
  nth_week              tinyint,
  day_of_week           varchar(3),
  start_time            time,
  duration_hours        int,
  stock_per_occurrence  int          NOT NULL,
  eligible_grades_mask  tinyint      NOT NULL,
  active                boolean      NOT NULL DEFAULT true,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE coupons (
  id                    bigint       NOT NULL AUTO_INCREMENT,
  template_id           bigint       NOT NULL,
  brand_id              bigint       NOT NULL COMMENT '역정규화',
  name                  varchar(100) NOT NULL COMMENT '스냅샷',
  policy_type           varchar(20)  NOT NULL COMMENT '스냅샷',
  discount_rate         int,
  max_discount_amount   int,
  discount_amount       int,
  data_grant_mb         int,
  min_order_amount      int          COMMENT '스냅샷',
  valid_days            int          NOT NULL,
  eligible_grades_mask  tinyint      NOT NULL COMMENT '스냅샷',
  open_at               datetime     NOT NULL,
  close_at              datetime     NOT NULL,
  status                varchar(20)  NOT NULL COMMENT 'SCHEDULED / OPEN / CLOSED',
  created_at            datetime(6)  NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE coupon_stocks (
  coupon_id       bigint   NOT NULL,
  total_quantity  int      NOT NULL,
  active_count    int      NOT NULL DEFAULT 0 COMMENT 'ISSUED + USED 합계 (누적 아님)',
  updated_at      datetime(6) NOT NULL,
  PRIMARY KEY (coupon_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE issuances (
  id            bigint      NOT NULL AUTO_INCREMENT,
  coupon_id     bigint      NOT NULL,
  member_id     bigint      NOT NULL,
  code          char(16)    NOT NULL,
  issued_grade  varchar(10) NOT NULL COMMENT '발급 시점 등급 스냅샷',
  status        varchar(12) NOT NULL COMMENT 'ISSUED / USED / CANCELLED / EXPIRED',
  issued_at     datetime(6) NOT NULL,
  expires_at    datetime(6) NOT NULL COMMENT '만료 판정의 유일한 기준',
  updated_at    datetime(6) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE issuance_histories (
  id           bigint      NOT NULL AUTO_INCREMENT,
  issuance_id  bigint      NOT NULL,
  event_type   varchar(30) NOT NULL COMMENT 'ISSUE / USE / CANCEL_USE / CANCEL / EXPIRE',
  from_status  varchar(20),
  to_status    varchar(20) NOT NULL,
  reason       varchar(120),
  request_id   varchar(36) COMMENT 'idempotency_records.idem_key 와 연결',
  created_at   datetime(6) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE issuance_usages (
  id               bigint   NOT NULL AUTO_INCREMENT,
  issuance_id      bigint   NOT NULL,
  order_id         bigint,
  discount_amount  int      NOT NULL,
  used_at          datetime(6) NOT NULL,
  canceled_at      datetime(6),
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE idempotency_records (
  idem_key      varchar(36) NOT NULL,
  member_id     bigint      NOT NULL,
  issuance_id   bigint      NOT NULL,
  request_hash  char(64)    NOT NULL,
  status        varchar(12) NOT NULL COMMENT 'IN_PROGRESS / DONE',
  response_body text,
  created_at    datetime(6) NOT NULL,
  PRIMARY KEY (idem_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE verification_runs (
  id                   bigint      NOT NULL AUTO_INCREMENT,
  as_of                datetime(6) NOT NULL,
  from_ts              datetime(6),
  scope                varchar(12) NOT NULL COMMENT 'FULL / INCREMENTAL',
  dataset              varchar(10) NOT NULL COMMENT 'CLEAN / CORRUPT',
  seed_run_id          bigint      COMMENT '대조한 정답 묶음. CORRUPT 만 채운다 — CLEAN 은 대조 상대가 없다',
  attempt              int         NOT NULL DEFAULT 1,
  verdict              varchar(8)  COMMENT 'PASS / FAIL',
  stats_status         varchar(10) COMMENT 'COMPLETE / PARTIAL / SKIPPED',
  finding_count        int         NOT NULL DEFAULT 0,
  findings_checksum    char(64),
  dataset_fingerprint  char(64),
  started_at           datetime(6) NOT NULL,
  finished_at          datetime(6),
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE verification_findings (
  id            bigint       NOT NULL AUTO_INCREMENT,
  run_id        bigint       NOT NULL,
  finding_type  varchar(40)  NOT NULL COMMENT 'V1~V6 상수',
  target_key    varchar(64)  NOT NULL COMMENT 'COUPON:812 / COUPON:812|MEMBER:9931 / ISSUANCE:44210 / HISTORY:88131',
  campaign_id   bigint       COMMENT 'coupons.id (레거시 컬럼명)',
  member_id     bigint,
  coupon_id     bigint       COMMENT 'issuances.id (레거시 컬럼명)',
  history_id    bigint,
  expected      varchar(200) NOT NULL,
  actual        varchar(200) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE asof_state (
  run_id              bigint      NOT NULL,
  coupon_id           bigint      NOT NULL COMMENT 'issuances.id (레거시 컬럼명)',
  state               varchar(12) NOT NULL,
  last_history_id     bigint,
  last_event_at       datetime(6),
  active_usage_count  int         NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, coupon_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE coupon_stats (
  run_id            bigint NOT NULL,
  coupon_id         bigint NOT NULL,
  issued_total      int    NOT NULL DEFAULT 0 COMMENT '누적 발급 — 퍼널의 분모',
  issued            int    NOT NULL DEFAULT 0,
  used              int    NOT NULL DEFAULT 0,
  cancelled         int    NOT NULL DEFAULT 0,
  expired           int    NOT NULL DEFAULT 0,
  sold_out_seconds  int    COMMENT '완판 회차만. 미달은 NULL',
  PRIMARY KEY (run_id, coupon_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE grade_stats (
  run_id        bigint      NOT NULL,
  coupon_id     bigint      NOT NULL,
  grade         varchar(10) NOT NULL,
  issued_total  int         NOT NULL DEFAULT 0,
  used_total    int         NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, coupon_id, grade)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

CREATE TABLE hourly_stats (
  run_id        bigint     NOT NULL,
  day_of_week   varchar(3) NOT NULL,
  hour          tinyint    NOT NULL,
  issued_total  int        NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, day_of_week, hour)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;

-- 정답 매니페스트. CLEAN 에서는 비어 있지만 테이블은 존재한다 —
-- 스키마를 정의하는 것은 cy-be 의 Flyway 이고(V1__init_schema.sql) 그쪽은 데이터셋을 구분하지
-- 않는다. "CLEAN 은 매니페스트를 가질 수 없다" 는 성질은 스키마가 아니라 코드가 지킨다
-- (VerifyJobConfig#freezeSeedRunId 가 dataset == CLEAN 이면 즉시 반환한다).
-- 어긋나면 cy-be 의 SchemaParityTest 가 잡는다.
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

-- 대시보드가 읽을 "완결된 최신 통계 스냅샷". 정의 원본은 cy-be 의
-- V8__latest_stats_run_view.sql 이고 어긋나면 SchemaParityTest 가 잡는다.
--
-- PRD 의 정의는 ORDER BY as_of DESC LIMIT 1 인데, 아래 write_verification_runs 가
-- CLEAN 을 같은 as_of 에 attempt 1·2 로 심고 둘 다 COMPLETE 라 그것만으로는 비결정적이다.
-- attempt 를 두 번째 키로 넣어 "같은 시각이면 나중 시도" 를 SQL 로 표현한다.
-- id 는 최종 타이브레이커다. 증분은 윈도우 집계라 대시보드의 분모가 될 수 없어 제외한다.

CREATE VIEW v_latest_stats_run AS
SELECT id
  FROM verification_runs
 WHERE dataset = 'CLEAN'
   AND scope = 'FULL'
   AND stats_status = 'COMPLETE'
 ORDER BY as_of DESC, attempt DESC, id DESC
 LIMIT 1;
