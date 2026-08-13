# 자원 최적화

1,076만 행을 만들어 2 vCPU / 2GB RAM / 8GB 디스크 MySQL 컨테이너에 넣는다.
생성 스크립트도 같은 박스에서 돌기 때문에 파이썬과 MySQL이 2GB를 나눠 쓴다.

PRD가 생성·적재 속도는 평가하지 않는다고 명시했으므로,
목표는 빠르게가 아니라 주어진 자원 안에서 끝나게 만드는 것이었다.

## 300만 행을 메모리에 담지 않는다

발급, 이력, 사용을 한 패스에 동시 생성한다.
한 발급건의 `issuances` 1행과 `issuance_histories` 1~5행, `issuance_usages` 0~2행을
같은 자리에서 만들어 바로 flush한다.

```python
for coupon in catalog.coupons:          # 147회
    for i in range(coupon.issue_count): # 최대 34,000회
        발급 1행 + 이력 1~5행 + 사용 0~2행을 즉시 write
```

들고 있는 상태는 회차 단위 버퍼(최대 34,000건)와 카운터 몇 개뿐이다.
1,076만 행을 만드는 데 peak RSS 82.5MB가 나왔다.

세 테이블을 한 자리에서 만들기 때문에 서로 어긋날 여지가 없다는 게 부수 효과다.
같은 패스에서 `coupon_stocks.active_count`와 `coupon_stats`, `grade_stats`, `hourly_stats`를
인메모리 카운터로 누적하므로, 통계를 위해 300만 행을 다시 훑는 비용이 0이다.
집계 대상이 147/588/168행뿐이라 메모리 부담도 없다.

## 100만 배열 없이 비복원 추출

회차마다 100만 명 중 34,000명을 중복 없이 뽑아야 한다. 1인 1매를 보장하려면 필요하다.
`list(range(1_000_000))`를 매 회차 만들면 147번 × 8MB다.

교환된 원소만 dict에 남기는 부분 Fisher-Yates를 쓴다.

```python
def take(self):
    j = i + rng.below(n - i)
    v = swapped.get(j, j)          # 안 건드린 자리는 항등
    swapped[j] = swapped.get(i, i)
    i += 1
    return v
```

34,000건을 뽑으면 dict 항목도 34,000개다. 100만 중 몇 개를 뽑든 메모리는 뽑은 만큼만 든다.

## Feistel 순열로 상충하는 두 요구를 만족

두 가지가 동시에 필요했다.

`members`를 PK 오름차순으로 써야 InnoDB 클러스터 인덱스에 순차 적재된다.
한편 등급은 rank 블록으로 정해야 한다. 앞 50%가 WELCOME이고 뒤 5%가 VIP인 식이어야
"GOLD 회원 중 k명 비복원 추출"이 O(k)로 끝난다.

그런데 rank를 그대로 PK로 쓰면 id만 보고 등급이 읽힌다.
id 1번부터 50만번까지 전부 WELCOME인 데이터는 부자연스럽다.

4라운드 Feistel 네트워크에 cycle-walking을 붙여 `[0, n)` 위의 전단사를 만들었다.
`rank → id`와 `id → rank`가 모두 O(1)이라 id 오름차순으로 순회하면서
각 id의 rank를 역산해 등급을 정할 수 있다. 100만 개짜리 룩업 테이블이 필요 없다.

```python
id   = feistel(rank)      # 등급 블록 → 산포된 id
rank = feistel⁻¹(id)      # id 오름차순 순회 중 등급 역산
```

`n`이 2의 거듭제곱이 아니어도 cycle-walking으로 전단사가 유지된다.
범위를 벗어나면 한 번 더 적용하면 된다.

## 샤드 단위로 넣고 바로 지운다

8GB 디스크에 TSV 1.5GB를 통째로 쌓으면 DB 데이터와 합쳐 한계에 부딪힌다.
50만 행 샤드가 닫힐 때마다 콜백으로 적재하고 파일을 지운다.
TSV 디스크 피크가 130MB로 묶인다.

```python
def on_shard(table, path):
    db.load(table, path)     # LOAD DATA LOCAL INFILE
    os.remove(path)          # 즉시 회수
```

나머지 적재 결정들.

- PK 오름차순으로만 넣는다. InnoDB 클러스터 인덱스 페이지 분할을 피한다.
- UNIQUE, FK, CHECK는 적재가 끝난 뒤에 건다. 300만 행에 인덱스를 유지하며 넣지 않는다.
- `varbinary`는 hex로 쓰고 `UNHEX()`로 되돌린다. `LOAD DATA`는 원시 바이너리를 못 받는다.
- 세션 `unique_checks`, `foreign_key_checks`, `sql_log_bin`을 끈다.
  8GB에서 바이너리 로그가 디스크를 갉아먹는 걸 막는다.
- `innodb_flush_log_at_trx_commit`은 GLOBAL 전용이라 세션에서 못 바꾼다. 컨테이너 기동 옵션으로 준다.

최종 저장 용량은 1.81GB(data 1.22 + index 0.59)로, 설계 추정 2.9GB보다 작았다.

접속 경로는 둘이다. 호스트에 드라이버를 깔 수 있으면 pymysql로
`LOAD DATA LOCAL INFILE`을 써서 호스트 파일을 직접 스트리밍한다.
mysql 클라이언트가 컨테이너 안에만 있으면 `docker cp`로 샤드를 넣고 컨테이너 안에서 적재한 뒤 지운다.
`docker exec`는 매 호출이 새 세션이라 세션 튜닝이 이어지지 않으므로,
`LOAD` 문 앞에 `SET SESSION`을 같이 붙여 보낸다.

## 병목은 암호화가 아니라 Faker였다

ERD가 요구한 대로 시드가 직접 AES-256-GCM과 HMAC-SHA256을 계산한다.
무인덱스 JDBC batch가 JPA `AttributeConverter`를 우회하기 때문이다.
회원 1행당 암호화 3회에 HMAC 2회, 100만 행이면 500만 연산이다.

측정해 보니 41만 ops/s가 나와 병목이 아니었다. 오히려 Faker `name()`이 5만/s로 지배적이었다.
그래서 필드별로 전략을 나눴다.

이름은 Faker를 매 행 호출한다. 김/이/박 편중을 포함한 이름 분포의 현실성이 목적이다.
이메일은 Faker로 5만 개 로컬파트 풀을 만들고 rank를 접미사로 붙인다.
여기서는 유일성이 목적이고, 매 행 호출보다 두 배 빠르면서 충돌 0을 구성적으로 보장한다.
휴대폰은 직접 만든다. ko_KR `phone_number()`는 `031-###-####` 같은 지역번호만 나온다.

IV도 `os.urandom`이 아니라 결정론 RNG에서 뽑는다. 그래야 재현이 된다.

## 검증이 멈춘 지점

Step 0(이력 리플레이)과 V4(불법 전이)는 534만 행에 윈도우 함수를 건다.

```sql
ROW_NUMBER() OVER (PARTITION BY issuance_id ORDER BY created_at DESC, id DESC)
FROM issuance_histories                    -- 534만 행을 한 번에 정렬
```

버퍼풀 768MB 환경에서는 41초였다.
그런데 버퍼풀이 기본값 128MB이고 컨테이너 한도가 768MiB인 서버에서는
정렬 임시파일과 undo가 동시에 부풀어 사실상 멈췄다.

`issuance_id` 구간으로 쪼개서 해결했다.

```python
for lo in range(1, max_id + 1, chunk):     # 기본 20만
    INSERT INTO _seed_replay_state
    SELECT ... WHERE issuance_id BETWEEN lo AND hi AND created_at <= asOf ...
```

정렬 대상이 구간 크기로 제한되고, 트랜잭션이 짧아져 undo가 매번 회수되고,
진행률이 로그에 찍혀 멈춘 건지 도는 건지 구분된다.
구간 스캔은 FK가 자동 생성한 `issuance_histories(issuance_id)` 인덱스를 타므로
보조 인덱스를 만들지 않은 상태에서도 이 경로는 살아 있다.

사실상 무한이던 게 57초가 됐다. 청크 크기를 20,000과 200,000으로 바꿔 봐도
검출 결과는 동일하다.

## 보조 인덱스를 만들지 않는다

`ERD.sql`에 보조 인덱스가 없는 건 빠뜨린 게 아니라 의도다.
300만 건에서 느린 쿼리를 겪고 실행계획을 보고 인덱스를 처방해 개선폭을 재는 것이
과제의 일부라, 시드가 미리 깔면 그 구간이 사라진다.

처방전은 `ddl/90_perf_indexes_optional.sql`에 두고 `--with-perf-indexes`로만 적용된다.
붙이기 전후로 `EXPLAIN ANALYZE`를 뜨면 그대로 근거 자료가 된다.

FK는 InnoDB가 자식 컬럼에 인덱스를 자동 생성하므로
`issuances(coupon_id)`, `issuances(member_id)`, `issuance_histories(issuance_id)`,
`issuance_usages(issuance_id)`는 이미 커버된다.
실제로 무인덱스인 축은 `status`, `expires_at`, `canceled_at`, `created_at`이다.

무인덱스 상태의 기준선은 [측정값](08-benchmarks.md)에 있다.
300만 건 검증 3분 4초이고, V4가 93초로 제일 비싸다.
