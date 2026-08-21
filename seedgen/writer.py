"""TSV 샤드 라이터.

LOAD DATA 가 읽을 파일을 쓴다. 8GB 디스크에 3M+5.3M 행짜리 TSV 를 통째로
쌓으면 DB 데이터와 합쳐 한계에 부딪히므로, 샤드 하나가 닫힐 때마다
콜백(= 적재 후 삭제)을 부를 수 있게 만들어 피크를 ~100MB 로 묶는다.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Callable, Iterable

DT6 = "%Y-%m-%d %H:%M:%S.%f"
DT0 = "%Y-%m-%d %H:%M:%S"

# kind: int | str | dt6 | dt | time | bin(hex→UNHEX) | bool
TABLES: dict[str, list[tuple[str, str]]] = {
    "grades": [("code", "str"), ("bit_value", "int")],
    "brands": [("id", "int"), ("name", "str"), ("category", "str")],
    "coupon_templates": [
        ("id", "int"), ("brand_id", "int"), ("name", "str"), ("policy_type", "str"),
        ("discount_rate", "int"), ("max_discount_amount", "int"),
        ("discount_amount", "int"), ("data_grant_mb", "int"),
        ("min_order_amount", "int"), ("valid_days", "int"), ("nth_week", "int"),
        ("day_of_week", "str"), ("start_time", "time"), ("duration_hours", "int"),
        ("stock_per_occurrence", "int"), ("eligible_grades_mask", "int"),
        ("active", "bool"),
    ],
    "coupons": [
        ("id", "int"), ("template_id", "int"), ("brand_id", "int"), ("name", "str"),
        ("policy_type", "str"), ("discount_rate", "int"),
        ("max_discount_amount", "int"), ("discount_amount", "int"),
        ("data_grant_mb", "int"), ("min_order_amount", "int"), ("valid_days", "int"),
        ("eligible_grades_mask", "int"), ("open_at", "dt"), ("close_at", "dt"),
        ("status", "str"), ("created_at", "dt6"),
    ],
    "coupon_stocks": [
        ("coupon_id", "int"), ("total_quantity", "int"), ("active_count", "int"),
        ("updated_at", "dt"),
    ],
    "members": [
        ("id", "int"), ("membership_grade", "str"), ("name_enc", "bin"),
        ("email_enc", "bin"), ("email_hash", "str"), ("phone_enc", "bin"),
        ("phone_hash", "str"), ("created_at", "dt6"),
    ],
    "issuances": [
        ("id", "int"), ("coupon_id", "int"), ("member_id", "int"), ("code", "str"),
        ("issued_grade", "str"), ("status", "str"), ("issued_at", "dt6"),
        ("expires_at", "dt6"), ("updated_at", "dt6"),
    ],
    "issuance_histories": [
        ("id", "int"), ("issuance_id", "int"), ("event_type", "str"),
        ("from_status", "str"), ("to_status", "str"), ("reason", "str"),
        ("request_id", "str"), ("created_at", "dt6"),
    ],
    "issuance_usages": [
        ("id", "int"), ("issuance_id", "int"), ("order_id", "int"),
        ("discount_amount", "int"), ("used_at", "dt"), ("canceled_at", "dt"),
    ],
    "idempotency_records": [
        ("idem_key", "str"), ("member_id", "int"), ("issuance_id", "int"),
        ("request_hash", "str"), ("status", "str"), ("response_body", "str"),
        ("created_at", "dt6"),
    ],
    "verification_runs": [
        ("id", "int"), ("as_of", "dt6"), ("from_ts", "dt6"), ("scope", "str"),
        ("dataset", "str"), ("seed_run_id", "int"), ("attempt", "int"),
        ("verdict", "str"),
        ("stats_status", "str"), ("finding_count", "int"),
        ("findings_checksum", "str"), ("dataset_fingerprint", "str"),
        ("started_at", "dt6"), ("finished_at", "dt6"),
    ],
    "coupon_stats": [
        ("run_id", "int"), ("coupon_id", "int"), ("issued_total", "int"),
        ("issued", "int"), ("used", "int"), ("cancelled", "int"),
        ("expired", "int"), ("sold_out_seconds", "int"),
    ],
    "grade_stats": [
        ("run_id", "int"), ("coupon_id", "int"), ("grade", "str"),
        ("issued_total", "int"), ("used_total", "int"),
    ],
    "hourly_stats": [
        ("run_id", "int"), ("day_of_week", "str"), ("hour", "int"),
        ("issued_total", "int"),
    ],
    "asof_state": [
        ("run_id", "int"), ("coupon_id", "int"), ("state", "str"),
        ("last_history_id", "int"), ("last_event_at", "dt6"),
        ("active_usage_count", "int"),
    ],
    "verification_findings": [
        ("id", "int"), ("run_id", "int"), ("finding_type", "str"),
        ("target_key", "str"), ("campaign_id", "int"), ("member_id", "int"),
        ("coupon_id", "int"), ("history_id", "int"), ("expected", "str"),
        ("actual", "str"),
    ],
    "expected_findings": [
        ("id", "int"), ("seed_run_id", "int"), ("corrupt_type", "int"),
        ("finding_type", "str"), ("target_key", "str"), ("campaign_id", "int"),
        ("member_id", "int"), ("coupon_id", "int"), ("history_id", "int"),
        ("note", "str"), ("created_at", "dt6"),
    ],
}

_ESCAPE = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})


def _fmt(value, kind: str) -> str:
    if value is None:
        return "\\N"
    if kind == "int":
        return str(value)
    if kind == "bool":
        return "1" if value else "0"
    if kind == "bin":
        return value.hex()
    if kind == "dt6":
        return value.strftime(DT6)
    if kind == "dt":
        return value.strftime(DT0)
    if kind == "time":
        return value if isinstance(value, str) else value.strftime("%H:%M:%S")
    return str(value).translate(_ESCAPE)


class ShardWriter:
    """한 테이블의 TSV 를 샤드로 나눠 쓴다."""

    def __init__(
        self,
        outdir: str,
        table: str,
        shard_rows: int = 500_000,
        on_shard: Callable[[str, str], None] | None = None,
    ) -> None:
        if table not in TABLES:
            raise KeyError(f"unknown table {table!r}")
        self.table = table
        self.outdir = outdir
        self.kinds = [k for _, k in TABLES[table]]
        self.shard_rows = shard_rows
        self.on_shard = on_shard
        self.rows = 0
        self._fh = None
        self._shard_no = 0
        self._shard_rows = 0
        self._path = ""
        self.paths: list[str] = []
        os.makedirs(outdir, exist_ok=True)

    def _open(self) -> None:
        self._shard_no += 1
        self._path = os.path.join(
            self.outdir, f"{self.table}.{self._shard_no:04d}.tsv"
        )
        self._fh = open(self._path, "w", encoding="utf-8", buffering=1 << 20)
        self._shard_rows = 0

    def _close_shard(self) -> None:
        if self._fh is None:
            return
        self._fh.close()
        self._fh = None
        if self.on_shard is not None:
            self.on_shard(self.table, self._path)
        else:
            self.paths.append(self._path)

    def write(self, *values) -> None:
        if self._fh is None:
            self._open()
        kinds = self.kinds
        self._fh.write(
            "\t".join(_fmt(v, kinds[i]) for i, v in enumerate(values)) + "\n"
        )
        self.rows += 1
        self._shard_rows += 1
        if self._shard_rows >= self.shard_rows:
            self._close_shard()

    def write_many(self, rows: Iterable[tuple]) -> None:
        for row in rows:
            self.write(*row)

    def close(self) -> None:
        self._close_shard()

    def __enter__(self) -> "ShardWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class WriterSet:
    """여러 테이블 라이터를 묶어서 관리."""

    def __init__(
        self,
        outdir: str,
        shard_rows: int = 500_000,
        on_shard: Callable[[str, str], None] | None = None,
    ) -> None:
        self.outdir = outdir
        self.shard_rows = shard_rows
        self.on_shard = on_shard
        self._w: dict[str, ShardWriter] = {}

    def __getitem__(self, table: str) -> ShardWriter:
        w = self._w.get(table)
        if w is None:
            w = ShardWriter(self.outdir, table, self.shard_rows, self.on_shard)
            self._w[table] = w
        return w

    def counts(self) -> dict[str, int]:
        return {t: w.rows for t, w in self._w.items()}

    def close(self, table: str | None = None) -> None:
        if table is not None:
            if table in self._w:
                self._w[table].close()
            return
        for w in self._w.values():
            w.close()

    def __enter__(self) -> "WriterSet":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def utcnow_naive() -> dt.datetime:
    """타임존 없는 UTC 시각.

    이 저장소가 만드는 모든 naive datetime 은 UTC 다. 배치가 Clock.systemUTC() 로
    asOf 를 만들고 expires_at 은 타임존 없는 datetime(6) 이라, 쓰는 쪽이 로컬 벽시계를
    쓰면 그 차이가 그대로 만료 지연이 된다.

    예전에는 이름만 utc 이고 몸통이 dt.datetime.now() 였다.
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
