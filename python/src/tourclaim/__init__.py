"""tourclaim: a client for TourClaim's traveler connector API.

Start, fill in and submit a trip cancellation claim against the travel
insurance that comes with the credit card the trip was booked with, from
Python or from the ``tourclaim`` command.

    from tourclaim import Client

    client = Client()  # the key from `tourclaim login`, or TOURCLAIM_API_KEY
    draft = client.start_intake({"merchant_name": "Example Air"})
    print(draft["missing_fields"], draft["next_questions"])

The traveler signs the claim authorization themselves, in their own browser
at the draft's ``review_url``; this package cannot sign for them.
"""

__version__ = "0.1.0"

from .client import API_PREFIX, Client, Response, derive_message_id  # noqa: E402
from .credentials import CredentialStore, StoredCredential, credentials_path  # noqa: E402
from .errors import (  # noqa: E402
    CONFLICT_CODES,
    AccessDeniedError,
    APIError,
    AuthenticationError,
    AuthorizationPendingError,
    ConflictError,
    ConsentRequiredError,
    DeviceFlowError,
    ExitCode,
    ExpiredTokenError,
    FileTooLargeError,
    NetworkError,
    NotFoundError,
    NotSignedInError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    RequestTimeoutError,
    SignatureTimeoutError,
    SlowDownError,
    TourClaimError,
    UnavailableError,
    UnexpectedResponseError,
    UnsupportedFileError,
    UsageError,
    ValidationError,
)
from .models import (  # noqa: E402
    DOC_TYPES,
    EMAIL_PROVIDERS,
    INTAKE_STATES,
    OTHER_INSURANCE,
    REASON_CATEGORIES,
    SCOPES,
    CardResponse,
    ClaimResponse,
    ConnectorInfo,
    DeviceCode,
    DeviceToken,
    EvidenceSummary,
    IntakeFields,
    IntakeResponse,
    DataDeletion,
    KeyInfo,
    MedicalAnswers,
)

__all__ = [
    "__version__",
    "API_PREFIX",
    "Client",
    "Response",
    "derive_message_id",
    "CredentialStore",
    "StoredCredential",
    "credentials_path",
    "CONFLICT_CODES",
    "ExitCode",
    "TourClaimError",
    "APIError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "PayloadTooLargeError",
    "ValidationError",
    "RateLimitError",
    "UnavailableError",
    "NotSignedInError",
    "NetworkError",
    "RequestTimeoutError",
    "UnexpectedResponseError",
    "UsageError",
    "ConsentRequiredError",
    "UnsupportedFileError",
    "FileTooLargeError",
    "SignatureTimeoutError",
    "DeviceFlowError",
    "AuthorizationPendingError",
    "SlowDownError",
    "AccessDeniedError",
    "ExpiredTokenError",
    "DOC_TYPES",
    "EMAIL_PROVIDERS",
    "INTAKE_STATES",
    "OTHER_INSURANCE",
    "REASON_CATEGORIES",
    "SCOPES",
    "CardResponse",
    "ClaimResponse",
    "ConnectorInfo",
    "DeviceCode",
    "DeviceToken",
    "EvidenceSummary",
    "IntakeFields",
    "IntakeResponse",
    "DataDeletion",
    "KeyInfo",
    "MedicalAnswers",
]
