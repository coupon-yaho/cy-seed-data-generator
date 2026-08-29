-- CLEAN 스키마 전용 물리 제약.
--
-- 이 넷이 CLEAN 에만 있다는 사실 자체가 "불변식은 애플리케이션이 아니라
-- DB 제약으로 표현한다"(PRD 설계 원칙 1)의 증거다. 오염셋은 정의상 이걸 위반해야
-- 하므로 CORRUPT 스키마에서는 걸지 않는다.

-- 1인 1매 — 취소·만료 후에도 재발급 불가. 오염 유형 6 이 위반한다.
CREATE UNIQUE INDEX uk_coupon_member ON issuances (coupon_id, member_id);

-- 발급 코드 유일. 오염 유형 5 가 위반한다.
CREATE UNIQUE INDEX uk_coupon_code ON issuances (code);

-- 초과 발급 방어. 오염 유형 1·3 이 재고를 흔든다.
ALTER TABLE coupon_stocks
  ADD CONSTRAINT ck_stock_range
  CHECK (active_count >= 0 AND active_count <= total_quantity);

-- 재고 범위 — cy-be V3 이 ck_stock_range 와 같은 축을 자기 이름으로 한 겹 더 건다.
-- 유형 1(+1)·3(-1)이 이 범위를 넘어야 하므로 CLEAN 에만 건다.
ALTER TABLE coupon_stocks
    ADD CONSTRAINT ck_coupon_stock_active_range
        CHECK (active_count >= 0 AND active_count <= total_quantity);
