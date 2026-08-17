-- CORRUPT 스키마 전용.
-- uk_coupon_member / uk_coupon_code / CHECK(active_count) 는 의도적으로 걸지 않는다.
-- 오염 유형 5·6·1·3 이 바로 그 제약을 위반하는 데이터이기 때문이다.

CREATE INDEX idx_expected_type ON expected_findings (corrupt_type);
