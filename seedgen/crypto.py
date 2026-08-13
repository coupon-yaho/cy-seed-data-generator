"""PII 암호화 규약 — 시드가 확정하고 Java 컨버터가 따라온다.

ERD.sql:226-229 가 "무인덱스 JDBC batch 는 @Convert AttributeConverter 를 우회한다.
시드가 직접 AES-256-GCM + HMAC 을 100만 행에 계산해야 하고 해시 충돌 0 을
시드가 보장해야 한다" 고 못박았다. 저장소에 바이트 레이아웃 정의가 없으므로
여기서 확정하고 crypto_vectors.json 으로 Java 쪽과 왕복 검증한다.

    name_enc / email_enc / phone_enc : varbinary(256)
        = IV(12B) ‖ AES-256-GCM ciphertext ‖ tag(16B)
    email_hash / phone_hash : char(64)
        = lower(hex(HMAC-SHA256(HMAC_KEY, normalize(plaintext))))

키는 AES_KEY / HMAC_KEY 환경변수의 base64(32바이트). 없으면 즉시 실패한다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

IV_LEN = 12
TAG_LEN = 16
MAX_ENC_LEN = 256  # varbinary(256)

_NON_DIGIT = re.compile(r"\D")


class KeyError_(RuntimeError):
    pass


def _load_key(env_name: str) -> bytes:
    raw = os.environ.get(env_name)
    if not raw:
        raise KeyError_(
            f"{env_name} 환경변수가 없습니다. "
            f"예: export {env_name}=$(openssl rand -base64 32)"
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise KeyError_(f"{env_name} 는 base64(32바이트)여야 합니다: {exc}") from exc
    if len(key) != 32:
        raise KeyError_(f"{env_name} 길이가 {len(key)}바이트입니다. 32바이트여야 합니다.")
    return key


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    return _NON_DIGIT.sub("", value)


class Crypto:
    """행 단위 암호화기. IV 는 결정론 RNG 에서 받아 재현 가능하게 만든다."""

    __slots__ = ("_aes", "_hmac_key")

    def __init__(self, aes_key: bytes | None = None, hmac_key: bytes | None = None) -> None:
        self._aes = AESGCM(aes_key if aes_key is not None else _load_key("AES_KEY"))
        self._hmac_key = hmac_key if hmac_key is not None else _load_key("HMAC_KEY")

    def encrypt(self, plaintext: str, iv: bytes) -> bytes:
        if len(iv) != IV_LEN:
            raise ValueError(f"IV 는 {IV_LEN}바이트여야 합니다")
        blob = iv + self._aes.encrypt(iv, plaintext.encode("utf-8"), None)
        if len(blob) > MAX_ENC_LEN:
            raise ValueError(
                f"암호문 {len(blob)}바이트가 varbinary({MAX_ENC_LEN}) 를 넘습니다: {plaintext!r}"
            )
        return blob

    def decrypt(self, blob: bytes) -> str:
        return self._aes.decrypt(blob[:IV_LEN], blob[IV_LEN:], None).decode("utf-8")

    def blind_index(self, normalized: str) -> str:
        return hmac.new(
            self._hmac_key, normalized.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    # 편의 ---------------------------------------------------------------------

    def email_hash(self, email: str) -> str:
        return self.blind_index(normalize_email(email))

    def phone_hash(self, phone: str) -> str:
        return self.blind_index(normalize_phone(phone))


def iv_from_rng(rng) -> bytes:
    """결정론 RNG 에서 12바이트 IV. os.urandom 을 쓰면 재현성이 깨진다."""
    return (rng.u64().to_bytes(8, "big") + rng.u64().to_bytes(8, "big"))[:IV_LEN]


# 검증 벡터 전용 고정 키. **운영 키를 절대 쓰지 않는다.**
# 이 파일은 저장소에 커밋되므로, 실제 AES_KEY 로 만들면 평문·암호문 쌍이 공개된다.
# 규약(바이트 레이아웃)을 증명하는 것이 목적이라 키가 공개돼도 무방하다.
TEST_AES_KEY = hashlib.sha256(b"seed-crypto-vector-aes").digest()
TEST_HMAC_KEY = hashlib.sha256(b"seed-crypto-vector-hmac").digest()


def test_vector_crypto() -> Crypto:
    """검증 벡터 생성 전용 인스턴스 (고정 공개 키)."""
    return Crypto(aes_key=TEST_AES_KEY, hmac_key=TEST_HMAC_KEY)


def make_vectors(crypto: Crypto | None = None, count: int = 20) -> list[dict]:
    """Java AttributeConverter 왕복 검증용 테스트 벡터.

    crypto 를 넘기지 않으면 고정 테스트 키를 쓴다 — 저장소에 커밋해도 안전하다.
    """
    from .rng import Rng

    crypto = crypto or test_vector_crypto()
    rng = Rng(0xC0FFEE, "crypto-vectors")
    samples = [
        "홍길동", "김서연", "test.user@example.com", "010-1234-5678",
        "u1@seed.example.com", "박민준", "010-9876-5432", "이하은",
    ]
    out = []
    for i in range(count):
        pt = samples[i % len(samples)] + ("" if i < len(samples) else f"-{i}")
        iv = iv_from_rng(rng)
        blob = crypto.encrypt(pt, iv)
        out.append(
            {
                "plaintext": pt,
                "iv_hex": iv.hex(),
                "stored_hex": blob.hex(),
                "hmac_hex": crypto.blind_index(pt),
            }
        )
    return out
