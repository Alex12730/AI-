from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


class PasswordService:
    def __init__(self) -> None:
        self._hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

    def hash(self, password: str) -> str:
        if len(password) < 12:
            raise ValueError("密码至少 12 个字符")
        return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except (VerifyMismatchError, InvalidHashError):
            return False
