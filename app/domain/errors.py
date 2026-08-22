"""Domain-specific exceptions with explicit fail-fast behavior."""


class RangeScoutError(Exception):
    """Base exception for all RangeScout-specific failures."""


class DataRootError(RangeScoutError):
    """Raised when persistent application data root cannot be safely provisioned."""


class LocalDataDeletionError(RangeScoutError):
    """Raised when local data deletion cannot complete safely."""


class ValidationError(RangeScoutError):
    """Raised when input or provider data fails validation."""


class DataQualityError(RangeScoutError):
    """Raised when data is present but unusable for requested calculations."""
