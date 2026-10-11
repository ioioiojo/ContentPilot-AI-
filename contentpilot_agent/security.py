import hashlib
import hmac
import secrets


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"scrypt${salt}${value.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        _, salt, expected = stored.split("$")
        value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(value.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Reduce the account-existence timing signal on login failure.
DUMMY_PASSWORD_HASH = password_hash(secrets.token_urlsafe(32))
