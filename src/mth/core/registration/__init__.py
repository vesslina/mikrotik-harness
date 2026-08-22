"""Trust-on-first-use registration through the pinned MikroMCP backend."""

from mth.core.registration.errors import RegistrationError, RegistrationErrorCode
from mth.core.registration.models import PendingRegistration, RegistrationResult
from mth.core.registration.service import RegistrationService, capture_tls_fingerprint
from mth.core.registration.store import ConfigPaths, MikroMcpConfigStore, SshTarget

__all__ = [
    "ConfigPaths",
    "MikroMcpConfigStore",
    "PendingRegistration",
    "RegistrationError",
    "RegistrationErrorCode",
    "RegistrationResult",
    "RegistrationService",
    "SshTarget",
    "capture_tls_fingerprint",
]
