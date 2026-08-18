from enum import StrEnum


class RegistrationErrorCode(StrEnum):
    REST_API_DISABLED = "REST_API_DISABLED"
    ROUTEROS_VERSION_UNSUPPORTED = "ROUTEROS_VERSION_UNSUPPORTED"
    TLS_FINGERPRINT_MISMATCH = "TLS_FINGERPRINT_MISMATCH"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    BACKEND_HEALTH_FAILED = "BACKEND_HEALTH_FAILED"
    INVALID_TARGET = "INVALID_TARGET"


class RegistrationError(RuntimeError):
    def __init__(self, code: RegistrationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
