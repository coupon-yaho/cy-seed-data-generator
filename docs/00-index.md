# 문서 색인

문서 한 개가 Confluence 페이지 한 개에 대응한다.

어떻게 만들었는지가 궁금하면 02, 03, 04 순서로 읽으면 된다.
배치를 구현한다면 05가 필수고, 06은 규칙을 추가하기 전에 반드시 봐야 한다.

| # | 문서 | 내용 | 주 독자 |
|---|---|---|---|
| 01 | [데이터 사양](01-data-spec.md) | 테이블별 행수, 상태·등급 분포, 시간축 | 전원 |
| 02 | [분포 설계](02-distribution-design.md) | 분포를 제약 만족 문제로 푼 과정 | 리뷰어 |
| 03 | [자원 최적화](03-resource-optimization.md) | 스트리밍, Feistel 순열, 샤드 적재, 청크 검증 | 리뷰어 |
| 04 | [오염 데이터 설계](04-corruption-design.md) | 7유형 700건과 폭발 반경 | 배치 개발자 |
| 05 | [계약](05-contract.md) | target_key, 리플레이, 암호화, 지문 | 배치·앱 개발자 |
| 06 | [검증하지 않는 것](06-not-verified.md) | 규칙에 넣으면 정상셋 0건이 불가능해지는 항목 | 배치 개발자 |
| 07 | [운영 가이드](07-operations.md) | 적재, MySQL 설정, 트러블슈팅 | 운영 |
| 08 | [측정값](08-benchmarks.md) | 소요 시간, 메모리, 디스크 | 전원 |

문서 충돌 해소(09번)와 Jira 티켓 계획은 내부 문서라 이 저장소에 없다.
저장소 밖 문서의 불일치와 팀별 후속 조치를 담고 있어 Confluence에만 둔다.

## 코드 진입점

- [`../README.md`](../README.md) — 빠른 시작과 CLI 레퍼런스
- [`../contract.json`](../contract.json) — 05번 문서의 기계 판독 버전. 배치가 이걸 읽는다
- [`../crypto_vectors.json`](../crypto_vectors.json) — Java 컨버터 왕복 검증 벡터 20건
- [`../ddl/`](../ddl/) — 스키마, 제약, 옵트인 보조 인덱스
- [`../seedgen/`](../seedgen/) — 생성기 모듈

## 검증 결과

MySQL 8.0.35, `--memory=768m --cpus=2`, 버퍼풀 기본값, 보조 인덱스 없음.

```
CLEAN   (회원 100만 · 발급 300만 · 이력 534만)
  STOCK_MISMATCH=0 · DUP_PER_MEMBER=0 · REPLAY_MISMATCH=0
  · ILLEGAL_TRANSITION=0 · USAGE_MISMATCH=0 · GRADE_VIOLATION=0
  ✓ 정상셋 성립                                        (3분 4초)

CORRUPT (오염 700건 주입)
  expected 800건 / 검출 800건 · 누락 0건 · 오탐 0건
  ✓ 집합 일치                                          (39초)
```
