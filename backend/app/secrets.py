import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import Settings


class SecretStore:
    """Application-scoped authenticated encryption derived from ICE_SECRET_KEY."""

    def __init__(self, secret_key: str) -> None:
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"ice-agent-secret-storage-v1",
            info=b"database-field-encryption",
        ).derive(secret_key.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    @classmethod
    def from_settings(cls, settings: Settings) -> "SecretStore":
        return cls(settings.secret_key.get_secret_value())

    def encrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                "Stored secret cannot be decrypted with the current ICE_SECRET_KEY"
            ) from exc


def masked_secret(value: str | None) -> str | None:
    return "********" if value else None
