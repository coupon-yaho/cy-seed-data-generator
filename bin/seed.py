#!/usr/bin/env python3
"""시드 CLI — generate / load / all / verify / contract / teardown.

    export AES_KEY=$(openssl rand -base64 32) HMAC_KEY=$(openssl rand -base64 32)

    python bin/seed.py all --dataset clean  --schema coupon_clean   --dsn mysql://root:pw@127.0.0.1:3306/
    python bin/seed.py all --dataset corrupt --schema coupon_corrupt --scale 0.2 --dsn ...
    python bin/seed.py verify --schema coupon_corrupt --dsn ...
"""

from __future__ import annotations

import argparse
import datetime as dt
import base64
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seedgen import config as C  # noqa: E402
from seedgen import manifest as M  # noqa: E402
from seedgen import stats, verify  # noqa: E402
from seedgen.catalog import build_catalog  # noqa: E402
from seedgen.corrupt import EXPECTED_MATRIX, Corruptor  # noqa: E402
from seedgen.crypto import (  # noqa: E402
    TEST_AES_KEY, TEST_HMAC_KEY, Crypto, make_vectors,
)
from seedgen.idmap import IdMap  # noqa: E402
from seedgen.issuances import IssuanceGenerator  # noqa: E402
from seedgen.loader import connect  # noqa: E402
from seedgen.members import GradeBlocks, generate_members  # noqa: E402
from seedgen.writer import WriterSet  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL = os.path.join(ROOT, "ddl")

# 적재 순서 — FK 를 끄고 넣지만 사람이 읽을 때의 의존 순서를 유지한다
LOAD_ORDER = [
    "grades", "brands", "coupon_templates", "coupons", "members",
    "issuances", "issuance_histories", "issuance_usages", "coupon_stocks",
    "idempotency_records", "verification_runs", "verification_findings",
    "asof_state", "coupon_stats", "grade_stats", "hourly_stats",
    "expected_findings",
]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def human(n: int) -> str:
    return f"{n:,}"


# ─────────────────────────────────────────────────────────────────────────────
# 생성
# ─────────────────────────────────────────────────────────────────────────────

def do_generate(args, db=None) -> dict:
    profile = C.Profile(
        dataset=args.dataset, scale=args.scale, seed=args.seed,
        seed_run_id=args.seed_run_id, plant_v6=args.plant_v6,
    )
    as_of = (
        dt.datetime.strptime(args.as_of, "%Y-%m-%d %H:%M:%S")
        # UTC 로 고정한다. 배치가 Clock.systemUTC() 로 asOf 를 만들고(cy-be TimeConfig),
        # expires_at 은 타임존 없는 datetime(6) 이라 두 벽시계가 다르면 그 차이가
        # 그대로 만료 지연이 된다 — KST 머신에서 시드를 만들고 UTC 컨테이너에서 배치를
        # 돌리면 9시간치가 안 만료되고, 만료 누락은 검증 finding 이 아니라 아무도 안 잡는다.
        if args.as_of else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    )
    outdir = args.out
    if args.clean_out and os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir, exist_ok=True)

    log(f"카탈로그 구성 — dataset={profile.dataset} scale={profile.scale} "
        f"as_of={as_of:%Y-%m-%d %H:%M:%S}")
    catalog = build_catalog(profile, as_of)
    log(f"  회차 {len(catalog.coupons)}개 (과거 {len(catalog.past)} / 현재 {len(catalog.current)}), "
        f"재고 합 {human(sum(c.total_quantity for c in catalog.past))}, "
        f"발급 계획 {human(sum(c.issue_count for c in catalog.past))}건")

    corruptor = Corruptor(profile, catalog)
    if profile.is_corrupt:
        log(f"  오염 계획: {corruptor.planned}")

    idmap = IdMap(profile.members, profile.seed)
    blocks = GradeBlocks(profile.members)
    crypto = Crypto()

    loaded: dict[str, int] = {}

    def on_shard(table: str, path: str) -> None:
        if db is None:
            return
        t0 = time.perf_counter()
        db.load(table, path)
        loaded[table] = loaded.get(table, 0) + 1
        os.remove(path)
        log(f"  적재 {table} 샤드 #{loaded[table]} ({time.perf_counter() - t0:.1f}s)")

    t_start = time.perf_counter()
    with WriterSet(outdir, args.shard_rows, on_shard if db else None) as W:
        stats.write_catalog(W, catalog)
        W.close("grades"); W.close("brands")
        W.close("coupon_templates"); W.close("coupons")

        log(f"members {human(profile.members)}행 생성 (AES-256-GCM + HMAC)")
        generate_members(
            profile, as_of, idmap, blocks, crypto, W["members"],
            use_faker=not args.no_faker,
            progress=lambda n: log(f"  members {human(n)}") if n % 500_000 == 0 else None,
        )
        W.close("members")

        log("issuances / histories / usages 생성")
        gen = IssuanceGenerator(
            profile, catalog, blocks, idmap, W,
            quotas=corruptor.quotas,
            record_finding=corruptor.record if profile.is_corrupt else None,
            idem_target=profile.idempotency,
            asof_run_id=1 if args.asof_state else None,
        )
        done = [0]

        def prog(coupon, totals):
            done[0] += 1
            if done[0] % 24 == 0 or done[0] == len(catalog.coupons):
                log(f"  회차 {done[0]}/{len(catalog.coupons)} — "
                    f"발급 {human(totals.issuances)} · 이력 {human(totals.histories)}")

        totals = gen.run(prog)
        W.close("issuances"); W.close("issuance_histories")
        W.close("issuance_usages"); W.close("idempotency_records")
        W.close("asof_state")

        stats.write_stocks(W, catalog, totals, as_of)
        run_ids, meta = stats.write_verification_runs(
            W, profile, as_of, totals,
            corruptor.findings if (profile.is_corrupt and args.seed_corrupt_run) else None,
        )
        if not profile.is_corrupt:
            stats.write_stats(W, catalog, totals, run_ids)
        if profile.is_corrupt:
            corruptor.assert_complete()
            corruptor.write(W["expected_findings"], as_of)
        counts = W.counts()

    elapsed = time.perf_counter() - t_start
    log(f"생성 완료 {elapsed:.1f}s — " +
        " · ".join(f"{t} {human(n)}" for t, n in sorted(counts.items())))

    payload = M.build(
        profile, as_of, catalog, totals, meta, counts,
        corruptor.summary() if profile.is_corrupt else None,
        {"elapsed_seconds": round(elapsed, 1),
         "faker": not args.no_faker,
         "schema": args.schema},
    )
    M.write(os.path.join(outdir, "seed_manifest.json"), payload)
    log(f"매니페스트: {os.path.join(outdir, 'seed_manifest.json')}")
    log(f"  dataset_fingerprint = {meta['dataset_fingerprint']}")
    if profile.is_corrupt:
        log(f"  오염 결과: {json.dumps(corruptor.summary(), ensure_ascii=False)}")
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# 적재
# ─────────────────────────────────────────────────────────────────────────────

def create_schema(db, dataset: str) -> None:
    log("스키마 생성 (테이블 + PK 만)")
    # 데이터셋별로 테이블이 갈리지 않는다. expected_findings 도 CLEAN 에 만든다 —
    # 스키마 주인은 cy-be 의 Flyway 이고 그쪽은 데이터셋을 구분하지 않는다.
    # 갈리면 cy-be 의 SchemaParityTest 가 잡는다.
    db.script(os.path.join(DDL, "00_schema.sql"))


def apply_constraints(db, dataset: str, perf_indexes: bool) -> None:
    log("제약 생성 (UNIQUE · FK · CHECK) — 적재가 끝난 뒤에 건다")
    db.script(os.path.join(DDL, "10_constraints_common.sql"))
    db.script(os.path.join(
        DDL, "11_constraints_clean.sql" if dataset == "clean"
        else "12_constraints_corrupt.sql"))
    if perf_indexes:
        log("보조 인덱스 생성 (--with-perf-indexes)")
        db.script(os.path.join(DDL, "90_perf_indexes_optional.sql"))
    else:
        log("보조 인덱스는 만들지 않는다 — 느린 쿼리를 직접 겪기 위한 의도 "
            "(필요해지면 ddl/90_perf_indexes_optional.sql)")


def do_load_files(args, db) -> None:
    outdir = args.out
    for table in LOAD_ORDER:
        shards = sorted(
            os.path.join(outdir, f)
            for f in os.listdir(outdir)
            if f.startswith(f"{table}.") and f.endswith(".tsv")
        )
        for path in shards:
            t0 = time.perf_counter()
            db.load(table, path)
            log(f"  적재 {os.path.basename(path)} ({time.perf_counter() - t0:.1f}s)")


# ─────────────────────────────────────────────────────────────────────────────
# 검증
# ─────────────────────────────────────────────────────────────────────────────

def do_verify(args, db) -> int:
    # origin='SEED' 로 좁힌다. 이 as_of 는 "시드가 무엇을 기준으로 만들었나" 이고,
    # 배치가 나중에 다른 as_of 로 돌면 MAX 가 그것을 집어 자가검증이 다른 시점을 본다.
    as_of = args.as_of or db.scalar(
        "SELECT DATE_FORMAT(MAX(as_of), '%Y-%m-%d %H:%i:%s.%f') FROM verification_runs"
        " WHERE origin = 'SEED'"
    )
    if not as_of:
        # 생성 쪽과 같은 기준(UTC)이어야 한다. 이 값으로 만료 판정을 하므로
        # 로컬 벽시계를 쓰면 자가검증만 다른 시점을 본다.
        as_of = (dt.datetime.now(dt.timezone.utc)
                 .replace(tzinfo=None)
                 .strftime("%Y-%m-%d %H:%M:%S"))
    log(f"자가검증 시작 — as_of={as_of}")

    problems = verify.check_invariants(db, args.dataset)
    if problems:
        log("불변식 위반:")
        for p in problems:
            log(f"  ✗ {p}")
    else:
        log("불변식 통과")

    log(f"리플레이 상태 구성 (Step 0 상당) — issuance_id {args.chunk:,}건씩 청크")
    t0 = time.perf_counter()
    verify.build_state(
        db, str(as_of), chunk=args.chunk,
        progress=lambda hi, mx: log(
            f"  {hi:,}/{mx:,} ({time.perf_counter() - t0:.0f}s)"),
    )
    log(f"  Step 0 완료 {time.perf_counter() - t0:.1f}s")

    t1 = time.perf_counter()
    findings, counts = verify.run_rules(
        db, str(as_of), args.dataset, chunk=args.chunk,
        progress=lambda ft, n: log(
            f"  {ft} = {n} ({time.perf_counter() - t1:.0f}s)"),
    )
    log("규칙별 검출: " + " · ".join(f"{k}={v}" for k, v in counts.items()))
    if len(findings) >= verify.ROW_CAP:
        log(f"  ! 표본이 상한 {verify.ROW_CAP:,}건에 걸렸습니다 — 집합 비교가 불완전할 수 있습니다")

    exit_code = 0
    if args.dataset == "clean":
        total = sum(counts.values())
        if total or problems:
            log(f"✗ CLEAN 인데 finding {total}건 — 정상셋이 아닙니다")
            for f in findings[:20]:
                log(f"    {f}")
            exit_code = 1
        else:
            log("✓ CLEAN 0건 — 정상셋 성립")
    else:
        cmp = verify.compare_with_expected(db, findings, args.seed_run_id)
        log(f"expected {cmp['expected']}건 / 검출 {cmp['actual']}건")
        log(f"  누락 {len(cmp['missing'])}건 · 오탐 {len(cmp['false_positive'])}건")
        for k in cmp["missing"][:20]:
            log(f"    누락 {k}")
        for k in cmp["false_positive"][:20]:
            log(f"    오탐 {k}")
        if cmp["pass"] and not problems:
            log("✓ CORRUPT 집합 일치 — 누락 0 · 오탐 0")
        else:
            exit_code = 1

    verify.drop_state(db)
    return exit_code


# ─────────────────────────────────────────────────────────────────────────────
# contract.json
# ─────────────────────────────────────────────────────────────────────────────

def do_contract(args) -> None:
    contract = {
        "version": 1,
        "table_vocabulary": {
            "coupons": "회차 (구 문서의 campaign)",
            "issuances": "발급건 (구 문서의 coupon)",
            "issuance_histories": "구 문서의 coupon_histories",
            "issuance_usages": "구 문서의 coupon_usages",
        },
        "target_key": {
            C.V1: f"{C.KEY_COUPON}:{{coupons.id}}",
            C.V2: f"{C.KEY_COUPON}:{{coupons.id}}|{C.KEY_MEMBER}:{{members.id}}",
            C.V3: f"{C.KEY_ISSUANCE}:{{issuances.id}}",
            C.V4: f"{C.KEY_HISTORY}:{{issuance_histories.id}}",
            C.V5: f"{C.KEY_ISSUANCE}:{{issuances.id}}",
            C.V6: f"{C.KEY_ISSUANCE}:{{issuances.id}}",
        },
        "finding_types": C.FINDING_TYPES,
        "legacy_columns": {
            "verification_findings.campaign_id": "coupons.id",
            "verification_findings.coupon_id": "issuances.id",
            "asof_state.coupon_id": "issuances.id",
        },
        "replay_rule": {
            "state": "(created_at, id) 오름차순 마지막 이력의 to_status",
            "filter": "created_at <= asOf",
            "active_usage": "used_at <= asOf AND (canceled_at IS NULL OR canceled_at > asOf)",
        },
        "v2_rule": (
            "(coupon_id, member_id) 중복 그룹당 1건 + "
            "(coupon_id, code) 중복 그룹에서 MIN(id) 를 제외한 각 행 1건. "
            "target_key 는 위반 행의 (coupon_id, member_id)."
        ),
        "legal_transitions": [
            {"from": frm, "event": ev, "to": to}
            for (frm, ev), to in C.LEGAL_TRANSITIONS.items()
        ],
        "corruption": {
            "injections": C.CORRUPT_TOTAL,
            "expected_rows": C.EXPECTED_ROWS,
            "types": [
                {"type": s.type_id, "count": s.count, "rules": list(s.rules),
                 "desc": s.desc}
                for s in C.CORRUPT_TYPES
            ],
            "matrix": [{"corrupt_type": k[0], "finding_type": k[1], "rows": v}
                       for k, v in sorted(EXPECTED_MATRIX.items())],
        },
        "crypto": {
            "cipher": "AES-256-GCM",
            "layout": "IV(12B) || ciphertext || tag(16B)",
            "aad": None,
            "column_type": "varbinary(256)",
            "blind_index": "lower(hex(HMAC-SHA256(HMAC_KEY, normalized)))",
            "normalize": {"email": "trim + lowercase", "phone": "digits only"},
            "key_encoding": "base64(32 bytes) in AES_KEY / HMAC_KEY env",
        },
        "fingerprint": {
            "formula": "SHA256(max(issuance_histories.id) | count(issuance_histories) "
                       "| count(issuances) | sum(coupon_stocks.active_count) "
                       "| max(issuances.updated_at))",
            "separator": "|",
            "timestamp_format": "%Y-%m-%d %H:%M:%S.%f",
            "history_filter": "created_at <= asOf",
        },
        "findings_checksum": {
            "input": "정렬된 (finding_type, target_key) 만",
            "encoding": "finding_type + U+001F + target_key + U+001E 반복 후 SHA-256",
        },
        "not_verified": [
            "coupon_templates.stock_per_occurrence ↔ coupon_stocks.total_quantity 불일치",
            "만료 누락 (expires_at < asOf 인데 status=ISSUED) — 별도 관측 지표",
            "고아 이력 — V4 가 전이 연쇄로 잡는다",
            "close_at 은 완판돼도 갱신하지 않는다",
            "CLOSED 회차의 잔여재고 증가",
            "스냅샷 컬럼 (name/policy/discount/mask/issued_grade)",
        ],
    }
    path = os.path.join(ROOT, "contract.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(contract, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    log(f"contract.json 작성: {path}")

    vec = os.path.join(ROOT, "crypto_vectors.json")
    with open(vec, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "spec": contract["crypto"],
                "note": (
                    "벡터는 아래 고정 테스트 키로 만든다. 운영 AES_KEY/HMAC_KEY 를 쓰지 않으므로 "
                    "저장소에 커밋해도 안전하다. 규약(바이트 레이아웃)을 증명하는 것이 목적이다."
                ),
                "test_keys": {
                    "AES_KEY": base64.b64encode(TEST_AES_KEY).decode(),
                    "HMAC_KEY": base64.b64encode(TEST_HMAC_KEY).decode(),
                    "derivation": 'sha256(b"seed-crypto-vector-aes") / sha256(b"seed-crypto-vector-hmac")',
                },
                "vectors": make_vectors(),
            },
            fh, ensure_ascii=False, indent=2,
        )
        fh.write("\n")
    log(f"crypto_vectors.json 작성: {vec} (고정 테스트 키 — 커밋 안전)")


# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="쿠폰 정합성 과제용 시드 데이터 생성기")
    p.add_argument("command",
                   choices=["generate", "load", "all", "verify", "contract", "teardown"])
    p.add_argument("--dataset", default="clean", choices=["clean", "corrupt"])
    p.add_argument("--scale", type=float, default=None,
                   help="기본값: clean=1.0, corrupt=0.2")
    p.add_argument("--seed", type=int, default=20260812)
    p.add_argument("--seed-run-id", type=int, default=1)
    p.add_argument("--as-of", default=None, help="'YYYY-MM-DD HH:MM:SS'")
    p.add_argument("--out", default=None, help="TSV 출력 디렉터리")
    p.add_argument("--shard-rows", type=int, default=500_000)
    p.add_argument("--chunk", type=int, default=200_000,
                   help="verify 시 issuance_id 청크 크기. 버퍼풀이 작으면 줄인다")
    p.add_argument("--dsn", default=os.environ.get("SEED_DSN", "mysql://root@127.0.0.1:3306/"))
    p.add_argument("--schema", default=None)
    p.add_argument("--container", default=os.environ.get("SEED_CONTAINER"))
    p.add_argument("--load-mode", default="auto", choices=["auto", "pymysql", "docker"])
    p.add_argument("--with-perf-indexes", action="store_true",
                   help="보조 인덱스까지 생성 (기본은 만들지 않는다)")
    p.add_argument("--asof-state", action="store_true",
                   help="asof_state 3M행까지 시딩 (기본 off — 배치 Step 0 산출물)")
    p.add_argument("--plant-v6", action="store_true",
                   help="V6 등급 위반 1건을 수동 심기 (700 집계 밖)")
    p.add_argument("--no-seed-corrupt-run", dest="seed_corrupt_run",
                   action="store_false", default=True,
                   help="CORRUPT 스키마에 예시 검증 run/findings 를 넣지 않는다")
    p.add_argument("--no-faker", action="store_true", help="Faker 없이 내장 생성기 사용")
    p.add_argument("--clean-out", action="store_true", help="출력 디렉터리를 먼저 비운다")
    p.add_argument("--keep-files", action="store_true",
                   help="all 모드에서 TSV 를 지우지 않는다(디스크 주의)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.scale is None:
        args.scale = 1.0 if args.dataset == "clean" else 0.2
    if args.schema is None:
        args.schema = f"coupon_{args.dataset}"
    if args.out is None:
        args.out = os.path.join(ROOT, "out", args.schema)

    if args.command == "contract":
        do_contract(args)
        return 0

    if args.command == "generate":
        do_generate(args, db=None)
        return 0

    db = connect(args.dsn, args.schema, args.container, args.load_mode)
    try:
        if args.command == "teardown":
            log(f"DROP DATABASE `{args.schema}`")
            db.execute(f"DROP DATABASE IF EXISTS `{args.schema}`")
            return 0

        if args.command == "verify":
            return do_verify(args, db)

        db.tune_session()
        if args.command == "all":
            db.reset_schema(args.schema)
            create_schema(db, args.dataset)
            do_generate(args, db=db if not args.keep_files else None)
            if args.keep_files:
                do_load_files(args, db)
        else:  # load
            create_schema(db, args.dataset)
            do_load_files(args, db)

        apply_constraints(db, args.dataset, args.with_perf_indexes)
        log("ANALYZE TABLE")
        for t in ("members", "issuances", "issuance_histories", "issuance_usages"):
            db.execute(f"ANALYZE TABLE `{t}`")
        log("완료. 자가검증은 `verify` 서브커맨드로 실행하세요.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
