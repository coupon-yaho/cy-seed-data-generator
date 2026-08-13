# 계약

시드와 배치가 글자 단위로 맞춰야 하는 것들이다.
한쪽만 바뀌면 검출은 정상인데 판정이 전부 누락으로 뒤집힌다.

기계 판독 버전은 [`../contract.json`](../contract.json)이고 이 문서가 그 해설이다.

## 테이블 어휘

`ERD.sql`의 DDL 명칭이 정답이다.
COMMENT 본문에는 아직 구 어휘가 남아 있으니 주의해야 한다.

| 지금 이름 | 뜻 | 행수 | 구 어휘 |
|---|---|---|---|
| `coupons` | 회차 | 147 | campaigns |
| `issuances` | 발급건 | 3,000,000 | coupons |
| `issuance_histories` | 상태 전이 이력 | 5,340,180 | coupon_histories |
| `issuance_usages` | 사용 실적 | 1,320,090 | coupon_usages |

컬럼명만 레거시로 남은 것들이 있다. 값의 의미는 아래가 맞다.

- `verification_findings.campaign_id` → `coupons.id`
- `verification_findings.coupon_id` → `issuances.id`
- `expected_findings`의 같은 컬럼들도 동일
- `asof_state.coupon_id` → `issuances.id`

## target_key

```
V1        COUPON:{coupons.id}
V2        COUPON:{coupons.id}|MEMBER:{members.id}
V3 V5 V6  ISSUANCE:{issuances.id}
V4        HISTORY:{issuance_histories.id}
```

`ERD.sql:490`의 예시는 구 어휘로 `CAMPAIGN:` / `COUPON:`을 쓴다.
최신 테이블명으로 통일했으므로 배치도 위 문자열을 써야 한다.

UNIQUE, 집합 비교, checksum은 전부 `target_key`로만 한다.
다형 FK 컬럼으로 조인하면 `NULL = NULL`이 UNKNOWN이라
정확히 검출한 finding이 전부 누락으로 잡힌다. 개별 FK 컬럼은 조회 편의로만 남긴다.

## 리플레이 규칙

```
상태      = created_at <= asOf 인 이력을 (created_at, id) 오름차순 정렬한
            마지막 행의 to_status
활성 사용 = used_at <= asOf AND (canceled_at IS NULL OR canceled_at > asOf)
```

`(created_at, id)` 타이브레이커가 없으면 같은 시각 이력 두 건의 순서가 실행마다 달라져
`findings_checksum`이 흔들린다. `id`가 두 번째 키인 것도 계약의 일부다.

합법 전이는 다섯 가지다.

| event_type | from | to | 비고 |
|---|---|---|---|
| `ISSUE` | (NULL) | `ISSUED` | |
| `USE` | `ISSUED` | `USED` | |
| `CANCEL_USE` | `USED` | `ISSUED` | 역방향 허용. 주문 취소 |
| `CANCEL` | `ISSUED` | `CANCELLED` | 종단 |
| `EXPIRE` | `ISSUED` | `EXPIRED` | 종단 |

`USED → EXPIRED`는 불가다.
V4는 연쇄 불일치(직전 `to_status`가 `from_status`와 다름)와 전이표 위반을 모두 잡아야 하고,
고아 이력도 여기서 걸린다.

## V2에 한 줄이 필요하다

```sql
-- (a) 1인 1매 위반: 중복 그룹당 1건
GROUP BY coupon_id, member_id HAVING COUNT(*) > 1

-- (b) 발급코드 복제: 중복 그룹에서 MIN(id) 를 제외한 각 행 1건   ← 오염 유형 5
GROUP BY coupon_id, code      HAVING COUNT(*) > 1
```

두 경우 모두 `target_key`는 위반 행의 `(coupon_id, member_id)`다.
(b)가 없으면 오염 유형 5의 100건이 통째로 미검출된다.

## V6는 members를 조인하지 않는다

```sql
FROM issuances i
JOIN coupons c ON c.id = i.coupon_id
JOIN grades  g ON g.code = i.issued_grade     -- 스냅샷
WHERE (c.eligible_grades_mask & g.bit_value) = 0
```

`members.membership_grade`는 현재값이다.
그대로 조인하면 회원이 강등되는 순간 과거의 정상 발급이 위반으로 잡힌다.
시드는 등급 제한 회차의 3%를 일부러 "현재는 부적격이지만 스냅샷은 적격"으로 만들어
이 차이를 데이터로 드러낸다.

`PRD.md:1146`과 `ERD.md:527`의 members 조인 버전은 폐기됐다.

## PII 암호화

```
name_enc / email_enc / phone_enc : varbinary(256)
    = IV(12B) ‖ AES-256-GCM ciphertext ‖ tag(16B)      AAD 없음
email_hash / phone_hash          : char(64)
    = lower(hex(HMAC-SHA256(HMAC_KEY, normalize(plaintext))))
normalize : email = trim + lowercase,  phone = 숫자만
키        : AES_KEY / HMAC_KEY 환경변수, base64(32바이트)
```

두 컬럼의 역할이 다르다.
`*_enc`는 양방향이라 복호화해서 보여주는 용도고,
`*_hash`는 단방향이라 검색과 유니크에만 쓴다.

조회는 두 단계다. 평문 이메일로 같은 HMAC을 계산해 `WHERE email_hash = ?`로 행을 찾고,
찾은 행의 `email_enc`를 복호화해서 마스킹한 뒤 응답한다.

`email_enc`로 검색하면 안 된다.
GCM은 행마다 IV가 달라 같은 평문도 암호문이 매번 다르다.
`WHERE email_enc = ?`는 절대 매칭되지 않는다. UNIQUE를 해시에 건 이유도 같다.

SQL에서 복호화할 수도 없다. MySQL `AES_DECRYPT()`는 GCM 모드가 아니라 호환되지 않는다.
복호화는 애플리케이션에서 한다. Spring은 `@Convert AttributeConverter`가 자동으로 처리한다.

키 없는 SHA-256은 금지다.
이메일처럼 엔트로피가 낮은 값에는 사전 공격이 통해 블라인드 인덱스의 의미가 사라진다.

대량 조회에서는 복호화하지 않는다.
검증 배치와 통계, 리포트는 `members`를 조인하지 않고 암호화 컬럼에 의존하지도 않는다.
복호화는 화면에 보여줄 페이지 단위, 수십 행에서만 한다.

### 왕복 검증을 붙일 것

[`../java/CryptoConverterReference.java`](../java/CryptoConverterReference.java)가 참조 구현이고
[`../crypto_vectors.json`](../crypto_vectors.json)에 벡터 20건이 있다.
각 벡터에 대해 `decrypt(stored_hex) == plaintext`와 `blindIndex(plaintext) == hmac_hex`가
모두 참이어야 한다.

이 테스트가 없으면 시드가 넣은 100만 행을 앱이 못 읽는 사고를 배포 후에야 발견한다.
키를 분실하면 100만 행이 복구 불가다.

벡터는 고정 테스트 키로 만든다. 운영 키를 쓰지 않으므로 저장소에 커밋해도 된다.
바이트 레이아웃을 증명하는 게 목적이다.

## dataset_fingerprint

```
SHA256( max(issuance_histories.id) | count(issuance_histories) | count(issuances)
        | sum(coupon_stocks.active_count) | max(issuances.updated_at) )

구분자        "|"
타임스탬프    "%Y-%m-%d %H:%M:%S.%f"
이력 필터     created_at <= asOf
```

지문과 checksum이 둘 다 같으면 결정론이 증명된다.
지문이 같은데 checksum이 다르면 검증기 버그이고, 이게 진짜 잡고 싶던 상황이다.
지문이 다르면 데이터가 바뀐 것이고, 비교 대상이 아니라는 걸 알 수 있다.

지문이 없으면 세 번째가 두 번째로 오인된다.
만료 배치가 한 건 돌았을 뿐인데 검증이 비결정적이라고 보고된다.

`seed_manifest.json`에 같은 값이 들어 있으니 배치가 뽑은 값과 바로 대조할 수 있다.

## findings_checksum

정렬된 `(finding_type, target_key)`만 해싱한다.

```
finding_type + U+001F + target_key + U+001E 를 정렬 순서대로 이어붙여 SHA-256
```

`expected`나 `actual` 같은 자유 문자열을 섞으면 포맷 한 글자에 거짓 실패가 난다.

## 기타 상수

- 등급 비트마스크는 WELCOME 1, SILVER 2, GOLD 4, VIP 8. VIP+GOLD는 12다.
- `seed_run_id`는 시드 자신의 실행 식별자로 `verification_runs.id`와 네임스페이스가 다르다.
- 오염 유형은 1~7이고, 8은 V6 수동 확인용이라 700 집계에 안 들어간다.
- 데이터셋은 `CLEAN`과 `CORRUPT`이며 물리적으로 분리된 스키마다.
- 통계 Step은 CLEAN에서만 실행한다. CORRUPT run은 통계를 만들지 않는다.
