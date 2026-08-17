# 시드 데이터 생성기

쿠폰 정합성 검증 배치를 시험하기 위한 더미데이터를 만든다.
회원 100만, 발급 300만, 이력 534만 규모를 2 vCPU / 2GB RAM / 8GB 디스크짜리
MySQL 컨테이너에 넣는다.

두 벌을 만든다. 정상셋(CLEAN)은 검증 배치가 0건을 검출해야 하고,
오염셋(CORRUPT)은 일부러 심어 둔 700건을 정확히 그것만 검출해야 한다.
개수가 아니라 집합이 일치해야 한다 — 오탐 350건에 미검출 350건이어도 총계는 700이다.

스키마 원본은 프로젝트의 `ERD.sql`, 실제로 적용되는 DDL은 `ddl/00_schema.sql`이다.

---

## 실행 결과

아래는 실제 실행 로그다. MySQL 8.0.35 컨테이너에 `--memory=768m --cpus=2`,
버퍼풀은 기본값 128MB, 보조 인덱스는 만들지 않은 상태 — 배포 서버와 같은 조건이다.

### CLEAN 생성·적재

![CLEAN 생성·적재](docs/images/1-clean-load.png)

생성 199.7초, 제약 생성 81초. 100만 행을 넣은 뒤에 `uk_email_hash`를 포함한
UNIQUE·FK·CHECK를 한 번에 통과한다. 시드가 해시 충돌 0을 구성적으로 보장하기 때문에
가능한 일이고, 난수로 이메일을 만들었다면 생일 문제로 언젠가 터진다.

### CLEAN 자가검증

![CLEAN 자가검증](docs/images/2-clean-verify.png)

이력 534만 행을 접는 리플레이가 57초. V1부터 V6까지 전부 0건이다.

### CORRUPT 생성·적재

![CORRUPT 생성·적재](docs/images/3-corrupt-load.png)

주입 700건에 정답 800행. 유형 3만 규칙 두 개를 동시에 울리기 때문이다.
회차가 291개인 것도 이유가 있는데, 유형 1과 3이 서로 다른 100개 회차를 요구한다.

### CORRUPT 자가검증

![CORRUPT 자가검증](docs/images/4-corrupt-verify.png)

expected 800건에 검출 800건, 누락과 오탐이 모두 0.

---

## 빠른 시작

```bash
pip install -r requirements.txt

export AES_KEY=$(openssl rand -base64 32)
export HMAC_KEY=$(openssl rand -base64 32)
export SEED_DSN='mysql://root:CHANGE_ME@127.0.0.1:3306/'

# 서버에서 한 번만. 재시작은 필요 없다
docker exec -it <컨테이너> mysql -uroot -p -e "SET PERSIST local_infile=1;"

# 스모크 — 30초면 끝난다. 여기서 걸리면 본 실행도 걸린다
python bin/seed.py all      --dataset clean --scale 0.002 --schema seed_smoke
python bin/seed.py verify   --dataset clean --schema seed_smoke
python bin/seed.py teardown --schema seed_smoke

# 본 실행
python bin/seed.py all --dataset clean   --schema coupon_clean
python bin/seed.py all --dataset corrupt --schema coupon_corrupt

python bin/seed.py verify --dataset clean   --schema coupon_clean   --chunk 100000
python bin/seed.py verify --dataset corrupt --schema coupon_corrupt --chunk 100000
```

DSN 비밀번호는 ASCII만 된다. MySQL 프로토콜이 latin-1로 보내기 때문에 한글이 들어가면
PyMySQL이 인코딩 단계에서 죽는다. `@ : / # ?` 가 있으면 URL 인코딩해야 한다.
준비 절차와 트러블슈팅은 [운영 가이드](docs/07-operations.md)에 있다.

mysql 클라이언트가 컨테이너 안에만 있다면 `--load-mode docker --container <이름>`을 붙인다.
`docker cp`로 샤드를 넣고 컨테이너 안에서 적재한다.

---

## 문서

[docs/00-index.md](docs/00-index.md)가 전체 색인이다.
"어떻게 만들었나"가 궁금하면 02와 03을 보면 되고, 배치를 구현한다면 05가 필수다.

| # | 문서 | 내용 |
|---|---|---|
| 01 | [데이터 사양](docs/01-data-spec.md) | 테이블별 행수, 상태·등급 분포, 시간축 |
| 02 | [분포 설계](docs/02-distribution-design.md) | IPF, 파레토, 불변식을 구성으로 보장하기 |
| 03 | [자원 최적화](docs/03-resource-optimization.md) | 스트리밍, Feistel 순열, 샤드 적재, 청크 검증 |
| 04 | [오염 데이터 설계](docs/04-corruption-design.md) | 7유형 700건과 폭발 반경 |
| 05 | [계약](docs/05-contract.md) | target_key, 리플레이 규칙, 암호화, 지문 |
| 06 | [검증하지 않는 것](docs/06-not-verified.md) | 정상셋 0건을 성립시키는 경계 |
| 07 | [운영 가이드](docs/07-operations.md) | 적재, MySQL 설정, 트러블슈팅 |
| 08 | [측정값](docs/08-benchmarks.md) | 소요 시간, 메모리, 디스크 |

---

## CLI

| 커맨드 | 하는 일 |
|---|---|
| `generate` | TSV만 만든다. DB에 붙지 않는다 |
| `load` | 만들어 둔 TSV를 적재한다 |
| `all` | 스키마 생성 → 생성과 적재를 번갈아 → 제약 생성. 샤드를 적재 즉시 지워 디스크 피크를 누른다 |
| `verify` | V1~V6과 불변식 자가검증 |
| `contract` | `contract.json`, `crypto_vectors.json` 갱신 |
| `teardown` | `DROP DATABASE` |

배분 계획은 `as_of`의 함수라 날짜가 바뀌면 제약 구조가 달라진다.
전체 생성을 돌리기 전에 계획만 따로 검사할 수 있다.

```bash
python bin/plancheck.py     # 여러 날짜 × 여러 시드로 계획 불변식 검사
```

적재한 PII를 눈으로 확인할 때는 별도 도구를 쓴다.
복호화와 `*_hash` 재계산 대조를 같이 한다.

```bash
python bin/decrypt.py --hex 0x60216DB4D8...                        # 블롭 하나만
python bin/decrypt.py --schema coupon_clean --id 1 --id 2          # 회원 id로
python bin/decrypt.py --schema coupon_clean --email u123@gmail.com # 평문 이메일로 역조회
```

세 번째가 유용하다. 평문으로 검색이 되면 HMAC 규약이 앱과 맞는다는 뜻이고,
안 되면 키가 다르다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--scale` | clean 1.0 / corrupt 0.2 | 모든 행수가 여기서 파생된다. 비율은 스케일과 무관하게 유지 |
| `--seed` | 20260812 | 고정 난수 시드 |
| `--as-of` | 실행 시각 | 검증 기준 시각. 재현하려면 명시해야 한다 |
| `--chunk` | 200,000 | `verify`의 issuance_id 청크. 버퍼풀이 작으면 줄인다 |
| `--with-perf-indexes` | off | 보조 인덱스 생성. 기본은 만들지 않는다 |
| `--asof-state` | off | `asof_state` 300만 행까지 시딩 |
| `--plant-v6` | off | 등급 위반 1건 수동 심기. 700 집계에는 안 들어간다 |
| `--no-seed-corrupt-run` | — | CORRUPT에 예시 검증 run과 findings를 넣지 않는다 |
| `--no-faker` | off | Faker 없이 내장 한글 생성기 사용 |
| `--load-mode` | auto | `pymysql` 또는 `docker` |
| `--keep-files` | off | 적재 후 TSV를 남긴다. 8GB 디스크에서는 쓰지 말 것 |

---

## 보조 인덱스를 만들지 않는 이유

`ERD.sql`에 보조 인덱스가 없는 건 빠뜨린 게 아니다.
300만 건에서 느린 쿼리를 겪고 실행계획을 보고 인덱스를 처방해 개선폭을 재는 것이
과제의 일부라, 시드가 미리 깔아 버리면 그 구간이 사라진다.

처방전은 [`ddl/90_perf_indexes_optional.sql`](ddl/90_perf_indexes_optional.sql)에 두고
`--with-perf-indexes`를 줘야 적용된다. 붙이기 전후로 `EXPLAIN ANALYZE`를 뜨면
그대로 근거 자료가 된다. 무인덱스 기준선은 300만 건 검증 3분 4초이고 V4가 제일 비싸다.

FK는 InnoDB가 자식 컬럼에 인덱스를 자동으로 만든다.
그래서 `issuances(coupon_id)`나 `issuance_histories(issuance_id)`는 이미 커버되고,
실제로 무인덱스인 축은 `status`, `expires_at`, `canceled_at`, `created_at`이다.

---

## 구조

```
seed/
  bin/seed.py                   CLI
  bin/plancheck.py              배분 계획 회귀 테스트 (DB 불필요)
  bin/decrypt.py                PII 스팟 체크
  seedgen/
    config.py                   분포 상수 단일 출처
    rng.py                      SplitMix64 결정론 RNG, 스트림 분리
    idmap.py                    Feistel 순열
    crypto.py                   AES-256-GCM / HMAC-SHA256
    people.py                   Faker(ko_KR) 한글 이름·이메일
    catalog.py                  브랜드·템플릿·회차, 발급수와 상태 배분
    members.py                  100만 회원 스트림
    issuances.py                발급·이력·사용 단일 패스, 오염 주입
    corrupt.py                  7유형 계획과 expected_findings
    stats.py                    집계, 지문, 체크섬
    verify.py                   V1~V6 자가검증 SQL
    writer.py / loader.py       TSV 샤드, LOAD DATA
    manifest.py                 seed_manifest.json
  ddl/
    00_schema.sql               테이블과 PK만 (expected_findings 포함 — 데이터셋 무관)
    10_constraints_common.sql   UNIQUE, FK, uk_expected
    11_constraints_clean.sql    uk_coupon_member, uk_coupon_code, CHECK
    12_constraints_corrupt.sql  idx_expected_type
    90_perf_indexes_optional.sql
  docs/                         설계 문서
  java/CryptoConverterReference.java
  contract.json                 배치·Java와 공유하는 계약
  crypto_vectors.json           왕복 검증 벡터 20건
```

의존성은 `cryptography`가 필수고 `Faker`와 `PyMySQL`은 없으면 폴백한다.
Python 3.9 이상.
