"""시드 자가검증 — Spring 배치와 독립적으로 V1~V6 를 SQL 로 돌린다.

배치가 아직 없어도 시드가 옳다는 것을 먼저 증명해야 한다. CLEAN 은 0건,
CORRUPT 는 expected_findings 와 집합이 정확히 일치해야 통과다.

리플레이 규칙(배치와 반드시 같아야 하는 부분):
    상태 = (created_at, id) 오름차순 마지막 이력의 to_status
    활성 사용 = used_at <= asOf AND (canceled_at IS NULL OR canceled_at > asOf)
"""

from __future__ import annotations

from . import config as C

STATE_TABLE = "_seed_replay_state"
# 리포트가 폭주하지 않도록 하는 상한. 오염셋 집합 비교에 전량이 필요하므로
# 정답행(800)보다 넉넉히 잡는다. 여기 걸리면 do_verify 가 경고를 띄운다.
ROW_CAP = 50_000


def _legal_tuples() -> str:
    rows = []
    for (frm, ev), to in C.LEGAL_TRANSITIONS.items():
        f = "''" if frm is None else f"'{frm}'"
        rows.append(f"({f},'{ev}','{to}')")
    return ", ".join(rows)


def build_state(db, as_of: str, chunk: int = 200_000, progress=None) -> None:
    """리플레이 결과를 실체 테이블로 내린다.

    **issuance_id 구간으로 쪼개서** 넣는다. 534만 행을 한 번에 윈도우 정렬하면
    버퍼풀이 작은 서버에서 디스크 임시파일과 undo 가 동시에 부풀어 사실상 멈춘다.
    구간을 자르면 (a) 정렬 대상이 구간 크기로 제한되고 (b) 트랜잭션이 짧아져
    undo 가 매번 회수되며 (c) 진행 상황이 보인다.

    구간 스캔은 FK 가 자동 생성한 issuance_histories(issuance_id) 인덱스를 탄다.
    보조 인덱스를 일부러 안 만든 상태에서도 이 경로는 살아 있다.
    """
    db.execute(f"DROP TABLE IF EXISTS {STATE_TABLE}")
    db.execute(
        f"CREATE TABLE {STATE_TABLE} ("
        f"  issuance_id BIGINT NOT NULL PRIMARY KEY,"
        f"  state VARCHAR(12) NOT NULL,"
        f"  last_history_id BIGINT NOT NULL"
        f") ENGINE=InnoDB"
    )
    max_id = int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM issuances") or 0)
    for lo in range(1, max_id + 1, chunk):
        hi = min(lo + chunk - 1, max_id)
        db.execute(
            f"INSERT INTO {STATE_TABLE} (issuance_id, state, last_history_id) "
            f"SELECT issuance_id, to_status, id FROM ("
            f"  SELECT issuance_id, to_status, id,"
            f"         ROW_NUMBER() OVER (PARTITION BY issuance_id"
            f"                            ORDER BY created_at DESC, id DESC) rn"
            f"  FROM issuance_histories"
            f"  WHERE issuance_id BETWEEN {lo} AND {hi} AND created_at <= '{as_of}'"
            f") t WHERE rn = 1"
        )
        if progress is not None:
            progress(hi, max_id)


def drop_state(db) -> None:
    db.execute(f"DROP TABLE IF EXISTS {STATE_TABLE}")


# issuance_id 구간 필터가 들어갈 자리. 청크 실행용.
# 별칭이 필요한 자리는 RANGE_R 을 쓴다 (조인 때문에 컬럼명이 모호해지는 곳).
RANGE = "{RANGE}"
RANGE_R = "{RANGE_R}"


def _fill_range(sql: str, lo: int | None, hi: int | None) -> str:
    if lo is None:
        return sql.replace(RANGE, "1=1").replace(RANGE_R, "1=1")
    return (sql
            .replace(RANGE, f"issuance_id BETWEEN {lo} AND {hi}")
            .replace(RANGE_R, f"r.issuance_id BETWEEN {lo} AND {hi}"))


def rules(as_of: str, dataset: str) -> list[tuple[str, str, bool]]:
    """(finding_type, SQL, 청크가능). SQL 은 (target_key, expected, actual) 를 돌려준다.

    윈도우 함수나 대량 GROUP BY 가 들어가는 규칙은 issuance_id 구간으로 쪼갠다.
    버퍼풀이 작은 서버에서 한 방에 돌리면 임시파일이 폭주한다.
    """
    active_usage = (
        f"SELECT issuance_id, COUNT(*) c FROM issuance_usages "
        f"WHERE used_at <= '{as_of}' "
        f"  AND (canceled_at IS NULL OR canceled_at > '{as_of}') "
        f"  AND {RANGE} "
        f"GROUP BY issuance_id"
    )
    out = [
        # V1 재고 정합 — 회차 그레인
        (C.V1, f"""
            SELECT CONCAT('{C.KEY_COUPON}:', s.coupon_id),
                   CONCAT('active_count=', s.active_count),
                   CONCAT('issuances 집계=', COALESCE(a.cnt, 0))
            FROM coupon_stocks s
            LEFT JOIN (
                SELECT i.coupon_id, COUNT(*) cnt
                FROM issuances i JOIN {STATE_TABLE} r ON r.issuance_id = i.id
                WHERE r.state IN ('{C.ISSUED}','{C.USED}')
                GROUP BY i.coupon_id
            ) a ON a.coupon_id = s.coupon_id
            WHERE s.active_count <> COALESCE(a.cnt, 0)
        """, False),
        # V2 1인 1매 — (회차, 회원) 중복 그리고 (회차, code) 중복
        (C.V2, f"""
            SELECT CONCAT('{C.KEY_COUPON}:', d.coupon_id,
                          '|{C.KEY_MEMBER}:', d.member_id),
                   '발급 1건', CONCAT('발급 ', d.n, '건')
            FROM (
                SELECT i.coupon_id, i.member_id, COUNT(*) n, MIN(i.id) keep
                FROM issuances i GROUP BY i.coupon_id, i.member_id HAVING COUNT(*) > 1
            ) d
            UNION
            SELECT CONCAT('{C.KEY_COUPON}:', i.coupon_id,
                          '|{C.KEY_MEMBER}:', i.member_id),
                   '발급 코드 유일', CONCAT('code ', i.code, ' 중복')
            FROM issuances i
            JOIN (
                SELECT coupon_id, code, MIN(id) keep
                FROM issuances GROUP BY coupon_id, code HAVING COUNT(*) > 1
            ) d ON d.coupon_id = i.coupon_id AND d.code = i.code AND i.id > d.keep
        """, False),
        # V3 리플레이 대조 — 발급건 그레인
        (C.V3, f"""
            SELECT CONCAT('{C.KEY_ISSUANCE}:', i.id),
                   CONCAT('replay=', r.state),
                   CONCAT('issuances.status=', i.status)
            FROM issuances i JOIN {STATE_TABLE} r ON r.issuance_id = i.id
            WHERE i.status <> r.state
        """, False),
        # V4 불법 전이 — 이력 행 그레인 (연쇄 불일치 + 전이표 위반 + 고아 이력)
        (C.V4, f"""
            SELECT CONCAT('{C.KEY_HISTORY}:', t.id),
                   CONCAT('from_status=', COALESCE(t.prev, '(없음)')),
                   CONCAT('from_status=', COALESCE(t.from_status, '(NULL)'),
                          ', ', t.event_type, '->', t.to_status)
            FROM (
                SELECT h.id, h.issuance_id, h.event_type, h.from_status, h.to_status,
                       LAG(h.to_status) OVER (PARTITION BY h.issuance_id
                                              ORDER BY h.created_at, h.id) prev
                FROM issuance_histories h
                WHERE h.created_at <= '{as_of}' AND {RANGE}
            ) t
            WHERE NOT (t.prev <=> t.from_status)
               OR (COALESCE(t.from_status,''), t.event_type, t.to_status)
                   NOT IN ({_legal_tuples()})
        """, True),
        # V5 사용 실적 정합 — 리플레이 ↔ usages (issuances.status 를 읽지 않는다)
        (C.V5, f"""
            SELECT CONCAT('{C.KEY_ISSUANCE}:', r.issuance_id),
                   CONCAT('active_usage=', CASE WHEN r.state='{C.USED}' THEN 1 ELSE 0 END),
                   CONCAT('active_usage=', COALESCE(u.c, 0))
            FROM {STATE_TABLE} r
            LEFT JOIN ({active_usage}) u ON u.issuance_id = r.issuance_id
            WHERE {RANGE_R}
              AND COALESCE(u.c, 0) <> (CASE WHEN r.state='{C.USED}' THEN 1 ELSE 0 END)
        """, True),
        # V6 등급 자격 — issued_grade 스냅샷 기준. members 를 조인하지 않는다.
        (C.V6, f"""
            SELECT CONCAT('{C.KEY_ISSUANCE}:', i.id),
                   CONCAT('mask & bit != 0 (mask=', c.eligible_grades_mask, ')'),
                   CONCAT('issued_grade=', i.issued_grade)
            FROM issuances i
            JOIN coupons c ON c.id = i.coupon_id
            JOIN grades g ON g.code = i.issued_grade
            WHERE (c.eligible_grades_mask & g.bit_value) = 0
        """, False),
    ]
    return out


INVARIANTS = [
    ("email_hash 중복",
     "SELECT COUNT(*) FROM (SELECT email_hash FROM members "
     "GROUP BY email_hash HAVING COUNT(*)>1) t"),
    ("active_count 범위",
     "SELECT COUNT(*) FROM coupon_stocks WHERE active_count < 0 "
     "OR active_count > total_quantity"),
    ("발급건 없는 이력(고아)",
     "SELECT COUNT(*) FROM issuance_histories h "
     "LEFT JOIN issuances i ON i.id=h.issuance_id WHERE i.id IS NULL"),
    ("사용행 없는 발급건 참조(고아)",
     "SELECT COUNT(*) FROM issuance_usages u "
     "LEFT JOIN issuances i ON i.id=u.issuance_id WHERE i.id IS NULL"),
    ("status 어휘 위반",
     "SELECT COUNT(*) FROM issuances WHERE status NOT IN "
     "('ISSUED','USED','CANCELLED','EXPIRED')"),
]

CLEAN_ONLY_INVARIANTS = [
    ("issued+used = active_count",
     "SELECT COUNT(*) FROM coupon_stats cs JOIN coupon_stocks st "
     "ON st.coupon_id=cs.coupon_id WHERE cs.issued + cs.used <> st.active_count"),
    ("issued+used+cancelled+expired = issued_total",
     "SELECT COUNT(*) FROM coupon_stats WHERE issued+used+cancelled+expired "
     "<> issued_total"),
    ("code 중복",
     "SELECT COUNT(*) FROM (SELECT code FROM issuances GROUP BY code "
     "HAVING COUNT(*)>1) t"),
]


def run_rules(db, as_of: str, dataset: str, chunk: int = 200_000,
              progress=None) -> tuple[list[tuple], dict]:
    findings: list[tuple] = []
    counts: dict[str, int] = {}
    max_id = int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM issuances") or 0)

    for finding_type, sql, chunkable in rules(as_of, dataset):
        spans = (
            [(lo, min(lo + chunk - 1, max_id)) for lo in range(1, max_id + 1, chunk)]
            if chunkable and max_id > chunk else [(None, None)]
        )
        total = 0
        for lo, hi in spans:
            q = _fill_range(sql, lo, hi)
            n = int(db.scalar(f"SELECT COUNT(*) FROM ({q}) x") or 0)
            total += n
            if n and len(findings) < ROW_CAP:
                rows = db.query(f"{q} LIMIT {ROW_CAP - len(findings)}")
                findings.extend((finding_type,) + tuple(r) for r in rows)
        counts[finding_type] = total
        if progress is not None:
            progress(finding_type, total)
    return findings, counts


def check_invariants(db, dataset: str) -> list[str]:
    problems = []
    checks = list(INVARIANTS)
    if dataset == "clean":
        checks += CLEAN_ONLY_INVARIANTS
    for label, sql in checks:
        n = int(db.scalar(sql) or 0)
        if n:
            problems.append(f"{label}: {n}건")
    return problems


def compare_with_expected(db, findings: list[tuple], seed_run_id: int) -> dict:
    """집합 비교 — 누락 / 오탐 (ERD.sql:515-522)."""
    expected = {
        (str(ft), str(tk))
        for ft, tk in db.query(
            f"SELECT finding_type, target_key FROM expected_findings "
            f"WHERE seed_run_id = {seed_run_id}"
        )
    }
    actual = {(f[0], str(f[1])) for f in findings}
    missing = sorted(expected - actual)
    false_positive = sorted(actual - expected)
    return {
        "expected": len(expected),
        "actual": len(actual),
        "missing": missing,
        "false_positive": false_positive,
        "pass": not missing and not false_positive,
    }
