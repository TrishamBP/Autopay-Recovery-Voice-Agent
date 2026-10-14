"""Application errors. Messages must be safe to show to API callers (no secrets, paths or SQL)."""


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "Internal server error", *, details: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_failed"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthorized"


class BusinessRuleViolation(AppError):
    """A request was well-formed but a backend-enforced rule forbids it (e.g. do-not-contact)."""

    status_code = 422
    code = "business_rule_violation"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"
