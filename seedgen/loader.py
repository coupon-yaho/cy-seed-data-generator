"""적재 — DDL 실행과 LOAD DATA.

접속 경로가 두 개다.
  pymysql  : 호스트에서 LOAD DATA LOCAL INFILE 로 파일을 스트리밍. 기본값.
  docker   : mysql 클라이언트가 컨테이너 안에만 있을 때. 샤드를 docker cp 로
             넣고 컨테이너 안에서 LOAD DATA LOCAL INFILE 한 뒤 지운다.

보조 인덱스 · UNIQUE · FK · CHECK 는 전부 적재 후에 건다. InnoDB 클러스터
인덱스에 PK 오름차순으로만 넣어 페이지 분할을 피하는 것이 2코어 서버에서 제일 크다.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import urllib.parse

from .writer import TABLES

# 세션 단위로 끌 수 있는 것만 여기 둔다.
# innodb_flush_log_at_trx_commit 는 GLOBAL 전용이라 컨테이너 기동 옵션으로 준다
# (README 의 my.cnf 절 참고). 권한이 없으면 조용히 건너뛴다.
SESSION_TUNING = [
    "SET SESSION foreign_key_checks = 0",
    "SET SESSION unique_checks = 0",
    "SET SESSION sql_log_bin = 0",
]


def parse_dsn(dsn: str) -> dict:
    """mysql://user:pass@host:port/schema"""
    u = urllib.parse.urlparse(dsn)
    if u.scheme not in ("mysql", "mysql+pymysql"):
        raise ValueError(f"지원하지 않는 DSN scheme: {u.scheme}")
    password = urllib.parse.unquote(u.password or "")
    try:
        # MySQL 프로토콜은 비밀번호를 latin-1 로 보낸다. 여기서 막지 않으면
        # pymysql 안쪽에서 UnicodeEncodeError 가 나서 원인을 알아보기 어렵다.
        password.encode("latin1")
    except UnicodeEncodeError:
        raise ValueError(
            "DSN 의 비밀번호에 ASCII 가 아닌 문자가 있습니다 "
            "(.env 의 자리표시자를 그대로 두지 않았는지 확인하세요).\n"
            "  특수문자가 있으면 URL 인코딩이 필요합니다:\n"
            "    python3 -c \"import urllib.parse,sys;"
            "print(urllib.parse.quote(sys.argv[1],safe=''))\" 'p@ss:w0rd'"
        ) from None
    return {
        "user": urllib.parse.unquote(u.username or "root"),
        "password": password,
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 3306,
        "schema": (u.path or "/").lstrip("/") or None,
    }


def load_statement(table: str, path: str, local: bool = True) -> str:
    cols = TABLES[table]
    names, sets = [], []
    for name, kind in cols:
        if kind == "bin":
            names.append(f"@{name}_hex")
            sets.append(f"{name} = UNHEX(@{name}_hex)")
        else:
            names.append(name)
    stmt = (
        f"LOAD DATA {'LOCAL ' if local else ''}INFILE {sql_quote(path)} "
        f"INTO TABLE `{table}` CHARACTER SET utf8mb4 "
        f"FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\' LINES TERMINATED BY '\\n' "
        f"({', '.join(names)})"
    )
    if sets:
        stmt += " SET " + ", ".join(sets)
    return stmt


def sql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def split_statements(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        if s.endswith(";"):
            out.append("\n".join(buf).rstrip().rstrip(";"))
            buf = []
    if buf:
        out.append("\n".join(buf).rstrip().rstrip(";"))
    return [s for s in out if s.strip()]


class Db:
    def execute(self, sql: str) -> None: raise NotImplementedError
    def query(self, sql: str) -> list[tuple]: raise NotImplementedError
    def load(self, table: str, path: str) -> None: raise NotImplementedError
    def close(self) -> None: pass

    # 공통 ---------------------------------------------------------------------

    def reset_schema(self, schema: str) -> None:
        """DROP → CREATE → 선택. 백엔드마다 '선택'의 의미가 달라 여기서 갈린다."""
        raise NotImplementedError

    def script(self, path: str) -> None:
        with open(path, encoding="utf-8") as fh:
            for stmt in split_statements(fh.read()):
                self.execute(stmt)

    def tune_session(self) -> None:
        for stmt in SESSION_TUNING:
            try:
                self.execute(stmt)
            except Exception as exc:  # noqa: BLE001 — 권한 없으면 넘어간다
                print(f"  ! 세션 튜닝 건너뜀 ({stmt}): {exc}")

    def scalar(self, sql: str):
        rows = self.query(sql)
        return rows[0][0] if rows else None


class PyMySQLDb(Db):
    def __init__(self, dsn: str, schema: str | None = None) -> None:
        import pymysql

        cfg = parse_dsn(dsn)
        self.schema = schema or cfg["schema"]
        self._conn = pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], charset="utf8mb4", autocommit=True,
            local_infile=True,
        )
        if self.schema:
            self.execute(f"CREATE DATABASE IF NOT EXISTS `{self.schema}` "
                         f"DEFAULT CHARACTER SET utf8mb4")
            self.execute(f"USE `{self.schema}`")

    def execute(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def query(self, sql: str) -> list[tuple]:
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())

    def load(self, table: str, path: str) -> None:
        self.execute(load_statement(table, os.path.abspath(path), local=True))

    def reset_schema(self, schema: str) -> None:
        self.execute(f"DROP DATABASE IF EXISTS `{schema}`")
        self.execute(f"CREATE DATABASE `{schema}` DEFAULT CHARACTER SET utf8mb4")
        self.execute(f"USE `{schema}`")
        self.schema = schema
        self.tune_session()  # USE 로 세션이 바뀌진 않지만 순서를 명시해 둔다

    def close(self) -> None:
        self._conn.close()


class DockerDb(Db):
    """mysql 클라이언트가 컨테이너 안에만 있을 때의 경로 (추가 의존성 0)."""

    def __init__(self, container: str, dsn: str, schema: str | None = None) -> None:
        cfg = parse_dsn(dsn)
        self.container = container
        self.user = cfg["user"]
        self.password = cfg["password"]
        self.schema = schema or cfg["schema"]
        self._base = ["docker", "exec", "-i", container, "mysql",
                      f"-u{self.user}", "--local-infile=1", "--batch", "--raw",
                      "--default-character-set=utf8mb4"]
        if self.password:
            self._base.insert(5, f"-p{self.password}")
        if self.schema:
            self._run(f"CREATE DATABASE IF NOT EXISTS `{self.schema}` "
                      f"DEFAULT CHARACTER SET utf8mb4", use_schema=False)

    def _run(self, sql: str, use_schema: bool = True, stdin: bytes | None = None) -> str:
        cmd = list(self._base)
        if use_schema and self.schema:
            cmd += ["-D", self.schema]
        cmd += ["-e", sql]
        proc = subprocess.run(cmd, input=stdin, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"mysql 실행 실패: {' '.join(shlex.quote(c) for c in cmd[:8])} …\n"
                f"{proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc.stdout.decode("utf-8", "replace")

    def execute(self, sql: str) -> None:
        self._run(sql)

    def query(self, sql: str) -> list[tuple]:
        out = self._run(sql)
        lines = out.splitlines()
        return [tuple(line.split("\t")) for line in lines[1:]] if len(lines) > 1 else []

    def load(self, table: str, path: str) -> None:
        inner = f"/tmp/seed_{table}_{os.path.basename(path)}"
        subprocess.run(["docker", "cp", path, f"{self.container}:{inner}"], check=True)
        try:
            # docker exec 는 매번 새 세션이라 세션 튜닝이 이어지지 않는다.
            # LOAD 문 앞에 같이 붙여 보낸다.
            prefix = "; ".join(SESSION_TUNING)
            self._run(f"{prefix}; {load_statement(table, inner, local=True)}")
        finally:
            subprocess.run(["docker", "exec", self.container, "rm", "-f", inner],
                           check=False)

    def reset_schema(self, schema: str) -> None:
        self._run(f"DROP DATABASE IF EXISTS `{schema}`", use_schema=False)
        self._run(f"CREATE DATABASE `{schema}` DEFAULT CHARACTER SET utf8mb4",
                  use_schema=False)
        self.schema = schema

    def tune_session(self) -> None:
        # 세션이 매 호출마다 새로 뜨므로 여기서 걸어도 남지 않는다. load() 가 직접 붙인다.
        pass

    def close(self) -> None:
        pass


def connect(dsn: str, schema: str | None, container: str | None = None,
            mode: str = "auto") -> Db:
    if mode == "docker" or (mode == "auto" and container and not _has_pymysql()):
        if not container:
            raise ValueError("--container 를 지정해야 docker 모드를 쓸 수 있습니다")
        return DockerDb(container, dsn, schema)
    return PyMySQLDb(dsn, schema)


def _has_pymysql() -> bool:
    try:
        import pymysql  # noqa: F401

        return True
    except ImportError:
        return False
