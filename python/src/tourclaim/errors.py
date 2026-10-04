"""Exceptions raised by the TourClaim client and the ``tourclaim`` command.

Every exception derives from :class:`TourClaimError`, which carries:

- ``message``: a sentence that can be shown to a person;
- ``code``: a stable machine-readable name, such as ``not_found`` or, for a 409,
  the API's own cause from the ``X-TourClaim-Error`` header (``stale_revision``,
  ``approval_required``, ...);
- ``status``: the HTTP status when the error came from the API, else ``None``;
- ``exit_code``: the exit code the command line uses for it (see ``ExitCode``);
- ``extra``: further details, such as ``retry_after`` or ``idempotency_key``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


class ExitCode:
    """Process exit codes. Documented in README.md; keep them stable."""

    OK = 0
    ERROR = 1
    USAGE = 2
    AUTH = 3
    CONFLICT = 4
    RATE_LIMITED = 5
    NOT_FOUND = 6
    UNAVAILABLE = 7


#: Stable causes of a 409, sent by the API in the ``X-TourClaim-Error`` header.
CONFLICT_CODES = (
    "idempotency_key_reused",
    "idempotency_key_other_connection",
    "concurrent_request",
    "stale_revision",
    "intake_submitted",
    "intake_incomplete",
    "approval_required",
    "approval_outdated",
    "evidence_conflict",
    "duplicate_booking",
)


class TourClaimError(Exception):
    """Base class for every error this package raises."""

    code = "error"
    exit_code = ExitCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        exit_code: Optional[int] = None,
        status: Optional[int] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if exit_code is not None:
            self.exit_code = exit_code
        self.status = status
        self.extra: Dict[str, Any] = dict(extra or {})

    def __str__(self) -> str:
        return self.message


class UsageError(TourClaimError, ValueError):
    """A bad argument: an invalid URL, field, flag or value. Exit code 2."""

    code = "usage_error"
    exit_code = ExitCode.USAGE


class NotSignedInError(TourClaimError):
    """No key is available for a call that needs one. Exit code 3."""

    code = "not_signed_in"
    exit_code = ExitCode.AUTH


class NetworkError(TourClaimError):
    """The API could not be reached."""

    code = "network_error"


class RequestTimeoutError(TourClaimError):
    """The API did not answer in time."""

    code = "timeout"


class UnexpectedResponseError(TourClaimError):
    """The API answered with something this client does not understand."""

    code = "bad_response"


class ConsentRequiredError(TourClaimError, ValueError):
    """Evidence was about to be shared without ``user_authorized_sharing=True``."""

    code = "consent_required"
    exit_code = ExitCode.USAGE


class UnsupportedFileError(TourClaimError):
    """An attachment is not a PDF, JPEG or PNG (checked by its first bytes)."""

    code = "unsupported_file"


class FileTooLargeError(TourClaimError):
    """An attachment is larger than the API accepts (5 MiB)."""

    code = "too_large"


class SignatureTimeoutError(TourClaimError):
    """The traveler did not sign before the wait ran out."""

    code = "timeout"


class APIError(TourClaimError):
    """An HTTP error answered by the API.

    ``body`` is the parsed JSON body (or ``None``), ``detail`` its ``detail``
    member (a sentence, or for 422 a list of ``{loc, type, msg}`` issues) and
    ``headers`` the response headers with lower-case names.
    """

    code = "api_error"

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: Optional[str] = None,
        exit_code: Optional[int] = None,
        body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message, code=code, exit_code=exit_code, status=status, extra=extra)
        self.body = body
        self.headers: Dict[str, str] = {k.lower(): v for k, v in (headers or {}).items()}
        self.detail = body.get("detail") if isinstance(body, dict) else None


class AuthenticationError(APIError):
    """401: the key is missing, expired or revoked."""

    code = "unauthorized"
    exit_code = ExitCode.AUTH


class PermissionDeniedError(APIError):
    """403: the key was not granted permission for this action."""

    code = "forbidden"
    exit_code = ExitCode.AUTH


class NotFoundError(APIError):
    """404: no such draft or claim for this key."""

    code = "not_found"
    exit_code = ExitCode.NOT_FOUND


class ConflictError(APIError):
    """409: the request conflicts with the draft's state.

    ``code`` is the cause the API named in ``X-TourClaim-Error`` (one of
    ``CONFLICT_CODES``), or ``conflict`` when it named none.
    """

    code = "conflict"
    exit_code = ExitCode.CONFLICT


class PayloadTooLargeError(APIError):
    """413: the request was too large."""

    code = "too_large"


class ValidationError(APIError):
    """422: invalid values. ``detail`` lists each one, or explains in a sentence."""

    code = "invalid"


class RateLimitError(APIError):
    """429: too many requests. ``retry_after`` is the wait in seconds."""

    code = "rate_limited"
    exit_code = ExitCode.RATE_LIMITED

    @property
    def retry_after(self) -> Optional[int]:
        value = self.extra.get("retry_after")
        return value if isinstance(value, int) else None


class UnavailableError(APIError):
    """503: the connector is disabled or temporarily unavailable."""

    code = "unavailable"
    exit_code = ExitCode.UNAVAILABLE


class DeviceFlowError(TourClaimError):
    """A sign-in with a device code failed."""

    code = "login_failed"


class AuthorizationPendingError(DeviceFlowError):
    """The traveler has not approved the sign-in yet. Poll again later."""

    code = "authorization_pending"


class SlowDownError(DeviceFlowError):
    """Polling too fast: add 5 seconds to the interval."""

    code = "slow_down"


class AccessDeniedError(DeviceFlowError):
    """The traveler declined the sign-in."""

    code = "access_denied"
    exit_code = ExitCode.AUTH


class ExpiredTokenError(DeviceFlowError):
    """The sign-in code expired or was already used."""

    code = "expired_token"
    exit_code = ExitCode.AUTH
