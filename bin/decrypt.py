#!/usr/bin/env python3
"""PII 컬럼 스팟 체크 — 적재된 행이 올바른지 눈으로 확인한다.

    # 헥스 블롭 하나만 확인 (DB 접속 불필요)
    python bin/decrypt.py --hex 0x60216DB4D8...

    # 회원 id 로 조회 — 복호화 + email_hash 재계산 대조까지
    python bin/decrypt.py --schema coupon_clean --id 1 --id 2

    # 평문 이메일로 역조회 (블라인드 인덱스가 실제로 도는지 확인)
    python bin/decrypt.py --schema coupon_clean --email u123@gmail.com

AES_KEY / HMAC_KEY 는 시드를 돌릴 때와 **같은 값**이어야 한다.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seedgen.crypto import IV_LEN, TAG_LEN, Crypto, normalize_email  # noqa: E402
from seedgen.loader import connect  # noqa: E402


def show_blob(crypto: Crypto, label: str, blob: bytes) -> str | None:
    if not blob:
        print(f"  {label:10s} (NULL)")
        return None
    ok, value = True, None
    try:
        value = crypto.decrypt(blob)
    except Exception as exc:  # noqa: BLE001
        ok = False
        value = f"복호화 실패 — {type(exc).__name__}"
    print(f"  {label:10s} {value}")
    print(f"    {len(blob):3d}B = IV {IV_LEN} + ct {len(blob) - IV_LEN - TAG_LEN} + tag {TAG_LEN}"
          f"   {'✅' if ok else '❌'}")
    print(f"    nonce {blob[:IV_LEN].hex()}   tag {blob[-TAG_LEN:].hex()}")
    return value if ok else None


def main() -> int:
    p = argparse.ArgumentParser(description="members PII 스팟 체크")
    p.add_argument("--hex", help="varbinary 값 하나만 복호화 (0x 접두어 허용)")
    p.add_argument("--id", type=int, action="append", default=[], help="회원 id (반복 가능)")
    p.add_argument("--email", action="append", default=[], help="평문 이메일로 역조회")
    p.add_argument("--limit", type=int, default=0, help="앞에서 N행 샘플")
    p.add_argument("--schema", default=None)
    p.add_argument("--dsn", default=os.environ.get("SEED_DSN", "mysql://root@127.0.0.1:3306/"))
    p.add_argument("--container", default=os.environ.get("SEED_CONTAINER"))
    p.add_argument("--load-mode", default="auto", choices=["auto", "pymysql", "docker"])
    args = p.parse_args()

    crypto = Crypto()

    if args.hex:
        blob = bytes.fromhex(args.hex[2:] if args.hex.lower().startswith("0x") else args.hex)
        show_blob(crypto, "value", blob)
        return 0

    if not args.schema:
        p.error("--schema 또는 --hex 가 필요합니다")

    db = connect(args.dsn, args.schema, args.container, args.load_mode)
    try:
        wheres = []
        if args.id:
            wheres.append(f"id IN ({','.join(str(i) for i in args.id)})")
        for e in args.email:
            wheres.append(f"email_hash = '{crypto.blind_index(normalize_email(e))}'")
        where = f"WHERE {' OR '.join(wheres)}" if wheres else ""
        limit = f"LIMIT {args.limit}" if args.limit else ("LIMIT 5" if not wheres else "")

        rows = db.query(
            f"SELECT id, membership_grade, HEX(name_enc), HEX(email_enc), email_hash, "
            f"HEX(phone_enc), phone_hash FROM members {where} ORDER BY id {limit}"
        )
        if not rows:
            print("해당 행이 없습니다. (--email 로 못 찾으면 HMAC_KEY 가 다른 것입니다)")
            return 1

        for mid, grade, name_h, email_h, email_hash, phone_h, phone_hash in rows:
            print(f"\n▸ member_id={mid}  grade={grade}")
            show_blob(crypto, "name", bytes.fromhex(name_h) if name_h else b"")
            email = show_blob(crypto, "email", bytes.fromhex(email_h) if email_h else b"")
            phone = show_blob(crypto, "phone", bytes.fromhex(phone_h) if phone_h else b"")

            # 블라인드 인덱스 정합 — 복호화한 평문으로 해시를 다시 만들어 대조한다
            for label, plain, stored in (("email_hash", email, email_hash),
                                         ("phone_hash", phone, phone_hash)):
                if plain is None or stored is None:
                    continue
                recomputed = crypto.blind_index(
                    normalize_email(plain) if label.startswith("email")
                    else "".join(ch for ch in plain if ch.isdigit())
                )
                mark = "✅ 일치" if recomputed == str(stored).lower() else "❌ 불일치"
                print(f"  {label:10s} {stored}  {mark}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
