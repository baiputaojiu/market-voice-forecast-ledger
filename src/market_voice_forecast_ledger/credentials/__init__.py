from __future__ import annotations

from typing import Protocol


class CredentialStore(Protocol):
    def set_api_key(self, secret: str) -> None: ...

    def has_api_key(self) -> bool: ...

    def read_api_key(self) -> str: ...

    def delete_api_key(self) -> bool: ...


__all__ = ["CredentialStore"]
