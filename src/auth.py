import hashlib
import secrets

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split('$')
        if scheme != 'scrypt':
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
        return secrets.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False
