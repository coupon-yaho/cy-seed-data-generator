package com.coupon.member.crypto;

import jakarta.persistence.AttributeConverter;
import jakarta.persistence.Converter;

import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.HexFormat;

/**
 * 시드가 확정한 PII 암호화 규약의 참조 구현.
 *
 * <p>ERD.sql:226-229 — 무인덱스 JDBC batch 는 @Convert AttributeConverter 를 우회하므로
 * 시드(Python)가 직접 100만 행을 암호화한다. 그 결과를 이 컨버터가 읽어야 하므로
 * 바이트 레이아웃이 한 바이트도 어긋나면 안 된다.
 *
 * <pre>
 *   varbinary(256) = IV(12B) ‖ AES-256-GCM ciphertext ‖ tag(16B)
 *   char(64)       = lower(hex(HMAC-SHA256(HMAC_KEY, normalize(plaintext))))
 *   AES_KEY / HMAC_KEY = base64(32 bytes), 환경변수
 * </pre>
 *
 * <p>검증 방법: seed/crypto_vectors.json 의 각 벡터에 대해
 * {@code decrypt(hex→bytes(stored_hex)).equals(plaintext)} 와
 * {@code blindIndex(plaintext).equals(hmac_hex)} 가 모두 참이어야 한다.
 * 이 테스트가 없으면 시드가 넣은 100만 행을 앱이 못 읽는 사고를 배포 후에야 발견한다.
 *
 * <p>주의 — GCM 은 매 행 IV 가 달라 암호문이 매번 바뀐다. 그래서 검색·유니크는
 * 암호문이 아니라 블라인드 인덱스(email_hash)로만 한다. 키 없는 SHA-256 은
 * 이메일처럼 엔트로피가 낮은 값에 사전 공격이 통하므로 금지다(ERD.sql:217-219).
 */
@Converter
public class CryptoConverterReference implements AttributeConverter<String, byte[]> {

    private static final int IV_LEN = 12;
    private static final int TAG_BITS = 128;   // 16 bytes
    private static final String TRANSFORM = "AES/GCM/NoPadding";

    private final SecretKeySpec aesKey;
    private final SecretKeySpec hmacKey;
    private final SecureRandom random = new SecureRandom();

    public CryptoConverterReference() {
        this(System.getenv("AES_KEY"), System.getenv("HMAC_KEY"));
    }

    public CryptoConverterReference(String aesKeyB64, String hmacKeyB64) {
        this.aesKey = new SecretKeySpec(decodeKey(aesKeyB64, "AES_KEY"), "AES");
        this.hmacKey = new SecretKeySpec(decodeKey(hmacKeyB64, "HMAC_KEY"), "HmacSHA256");
    }

    private static byte[] decodeKey(String b64, String name) {
        if (b64 == null || b64.isBlank()) {
            throw new IllegalStateException(name + " 환경변수가 없습니다");
        }
        byte[] key = Base64.getDecoder().decode(b64.trim());
        if (key.length != 32) {
            throw new IllegalStateException(name + " 는 32바이트여야 합니다: " + key.length);
        }
        return key;
    }

    @Override
    public byte[] convertToDatabaseColumn(String plaintext) {
        if (plaintext == null) return null;
        try {
            byte[] iv = new byte[IV_LEN];
            random.nextBytes(iv);
            Cipher cipher = Cipher.getInstance(TRANSFORM);
            cipher.init(Cipher.ENCRYPT_MODE, aesKey, new GCMParameterSpec(TAG_BITS, iv));
            byte[] sealed = cipher.doFinal(plaintext.getBytes(StandardCharsets.UTF_8));
            byte[] out = new byte[IV_LEN + sealed.length];
            System.arraycopy(iv, 0, out, 0, IV_LEN);
            System.arraycopy(sealed, 0, out, IV_LEN, sealed.length);
            if (out.length > 256) {
                throw new IllegalArgumentException("암호문이 varbinary(256) 를 넘습니다");
            }
            return out;
        } catch (Exception e) {
            throw new IllegalStateException("암호화 실패", e);
        }
    }

    @Override
    public String convertToEntityAttribute(byte[] stored) {
        if (stored == null) return null;
        try {
            Cipher cipher = Cipher.getInstance(TRANSFORM);
            cipher.init(Cipher.DECRYPT_MODE, aesKey,
                        new GCMParameterSpec(TAG_BITS, stored, 0, IV_LEN));
            byte[] plain = cipher.doFinal(stored, IV_LEN, stored.length - IV_LEN);
            return new String(plain, StandardCharsets.UTF_8);
        } catch (Exception e) {
            throw new IllegalStateException("복호화 실패", e);
        }
    }

    /** 블라인드 인덱스 — email_hash / phone_hash 컬럼에 그대로 들어간다. */
    public String blindIndex(String normalized) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(hmacKey);
            byte[] digest = mac.doFinal(normalized.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);   // 소문자 hex 64자
        } catch (Exception e) {
            throw new IllegalStateException("HMAC 실패", e);
        }
    }

    public static String normalizeEmail(String email) {
        return email.strip().toLowerCase();
    }

    public static String normalizePhone(String phone) {
        return phone.replaceAll("\\D", "");
    }
}
