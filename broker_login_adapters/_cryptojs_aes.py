# broker_login_adapters/_cryptojs_aes.py
"""
OpenSSL-compatible AES encryption used by CryptoJS.AES.encrypt() with the
default "password" string-key flow. AliceBlue's web login form encrypts the
trading password this way before posting it.

Compatibility notes:
- CryptoJS prepends the literal "Salted__" + 8 random bytes, then derives
  the 32-byte key + 16-byte IV with EVP_BytesToKey(MD5, no iterations).
- Output is base64 of "Salted__" + salt + ciphertext (AES-256-CBC, PKCS7).
- Decryption uses the same scheme — the AliceBlue server reads the salt
  out of the prefix, regenerates key+iv from encKey, and decrypts.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> tuple[bytes, bytes]:
    """OpenSSL's EVP_BytesToKey with MD5, single iteration — what CryptoJS uses by default."""
    derived = b""
    prev = b""
    while len(derived) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        derived += prev
    return derived[:key_len], derived[key_len:key_len + iv_len]


def encrypt(plaintext: str, password: str) -> str:
    """Encrypt `plaintext` with CryptoJS.AES.encrypt(plaintext, password)-compatible output (base64)."""
    salt = os.urandom(8)
    key, iv = _evp_bytes_to_key(password.encode("utf-8"), salt)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(b"Salted__" + salt + ct).decode("ascii")
