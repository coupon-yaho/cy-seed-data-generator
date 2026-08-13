# 측정값

MySQL 8.0.35 컨테이너, `--memory=768m --cpus=2`, 버퍼풀은 기본값 128MB,
보조 인덱스 없음. 생성기는 같은 호스트에서 돌렸다.
배포 서버(2 vCPU / 2GB RAM, 컨테이너 한도 768MiB)와 같은 조건이다.

생성·적재 속도는 평가 대상이 아니다. 아래 수치는 주어진 자원 안에서 끝나는지를
확인하기 위한 관측치다.

## 전체 규모

| 단계 | CLEAN (300만) | CORRUPT (60만) |
|---|---|---|
| 생성 + 적재 | 3분 20초 | 40.5초 |
| 제약 생성 (UNIQUE·FK·CHECK) | 1분 21초 | 14초 |
| 파이프라인 합계 | 4분 41초 | 55초 |
| 자가검증 Step 0 (리플레이) | 56.9초 | 5.5초 |
| 자가검증 전체 | 3분 4초 | 39초 |
| peak RSS (생성기) | 82.5 MB | — |
| DB 크기 | 1.81 GB | 0.34 GB |

실제 행수는 회원 1,000,000, 발급 3,000,000, 이력 5,340,180, 사용 1,320,090,
멱등 100,000, 회차 147. 전부 합쳐 약 1,076만 행이다.

청크를 도입하기 전에는 Step 0가 이 환경에서 사실상 완료되지 않았다.
534만 행 윈도우 정렬을 한 트랜잭션으로 돌리면 임시파일과 undo가 동시에 부푼다.
`issuance_id` 구간 분할로 57초가 됐다.

버퍼풀을 768MB로 올린 2GB 컨테이너에서도 재봤는데,
생성·적재는 거의 차이가 없고 제약 생성만 1.8배 빨라졌다.
전자는 파이썬 생성이 지배적이라 버퍼풀 영향을 안 받고, 후자는 인덱스 빌드라 직접 비례한다.

## 규칙별 검증 시간

CLEAN 300만 건, 무인덱스 기준이다.

| 구간 | 소요 |
|---|---|
| 불변식 검사 | 7초 |
| Step 0 리플레이 | 57초 |
| V1 `STOCK_MISMATCH` | 3초 |
| V2 `DUP_PER_MEMBER` | 26초 |
| V3 `REPLAY_MISMATCH` | 6초 |
| V4 `ILLEGAL_TRANSITION` | 58초 |
| V5 `USAGE_MISMATCH` | 22초 |
| V6 `GRADE_VIOLATION` | 5초 |
| 합계 | 3분 4초 |

결과는 V1부터 V6까지 전부 0건이다.

V4가 제일 비싸다. 534만 행에 `LAG` 윈도우를 걸기 때문이고,
`idx_history_issuance` 처방의 1순위가 여기다.

## 스케일별

| 스케일 | 발급 | 이력 | 생성 + 적재 | 제약 | 검증 |
|---|---|---|---|---|---|
| 0.002 | 6,000 | 10,674 | 2.5초 | 0.3초 | 0.5초 |
| 0.1 | 300,000 | 533,740 | 38초 | 10초 | 15초 |
| 1.0 | 3,000,000 | 5,340,180 | 3분 20초 | 1분 21초 | 3분 4초 |
| 0.2 (CORRUPT) | 600,200 | 1,068,740 | 40.5초 | 14초 | 39초 |

## 저장 용량

| 테이블 | 크기 |
|---|---|
| `issuance_histories` | 646 MB |
| `issuances` | 636 MB |
| `members` | 432 MB |
| `issuance_usages` | 105 MB |
| `idempotency_records` | 37 MB |
| 나머지 | 1 MB 미만 |
| CLEAN 합계 | 1.81 GB (data 1.22 + index 0.59) |
| CORRUPT 합계 | 0.34 GB |

설계 단계 추정은 2.9GB였는데 실측이 더 작았다. 8GB 디스크에 5GB가 남는다.

## 생성기 마이크로 벤치마크

| 항목 | 처리량 | 100만 행 환산 |
|---|---|---|
| AES-256-GCM + HMAC-SHA256 | 410,000 ops/s | 회원 1행당 5연산 → 12초 |
| TSV 포맷 | 980,000 행/s | — |
| Faker `name()` (ko_KR) | 50,000 /s | 20초 |
| Faker `name` + `user_name` + `phone_number` | 18,000 행/s | 56초 |

암호화가 아니라 Faker가 병목이었다.
그래서 이메일 로컬파트는 매 행 호출 대신 5만 개 풀과 rank 접미사로 바꿔 절반으로 줄였다.
유일성도 구성적으로 보장된다.

## 결정론

같은 `--seed`와 `--as-of`로 두 번 생성해 TSV를 해시 비교했다.
`issuances`, `issuance_histories`, `issuance_usages`, `coupon_stocks`, `members` 전부
바이트 단위로 같았다. `members`는 같은 `AES_KEY`와 `HMAC_KEY` 기준이다.

## 오염셋 검출 정확도

```
규칙별 검출: STOCK_MISMATCH=200 · DUP_PER_MEMBER=200 · REPLAY_MISMATCH=100
           · ILLEGAL_TRANSITION=200 · USAGE_MISMATCH=100 · GRADE_VIOLATION=0
expected 800건 / 검출 800건
  누락 0건 · 오탐 0건
```

청크 크기를 20,000과 200,000으로 바꿔도 결과가 같음을 확인했다.

## 재현

```bash
export AES_KEY=$(openssl rand -base64 32) HMAC_KEY=$(openssl rand -base64 32)
export SEED_DSN='mysql://root:비번@127.0.0.1:3306/'

/usr/bin/time -v python3 bin/seed.py all --dataset clean --scale 1.0 --schema bench
time python3 bin/seed.py verify --dataset clean --schema bench --chunk 200000

docker exec <컨테이너> mysql -uroot -p -N -e "
  SELECT table_name, ROUND((data_length+index_length)/1024/1024) mb
  FROM information_schema.tables WHERE table_schema='bench'
  ORDER BY (data_length+index_length) DESC LIMIT 7;"
```
