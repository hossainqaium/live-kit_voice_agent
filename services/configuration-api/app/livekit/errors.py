"""LiveKit integration errors.

The API layer needs to distinguish three cases, because they have different
correct responses:

* the caller sent something LiveKit rejected — a 4xx, do not retry
* LiveKit is unreachable or erroring — a 502/503, retry is reasonable
* the resource is gone from LiveKit but present in PostgreSQL — drift, which
  is neither an error to surface to the tenant nor something to retry blindly
"""

from __future__ import annotations


class LiveKitError(RuntimeError):
    """Base class for LiveKit integration failures."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LiveKitUnavailableError(LiveKitError):
    """LiveKit could not be reached, or returned a server error."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class LiveKitRejectedError(LiveKitError):
    """LiveKit rejected the request as invalid.

    Not retryable: the same request will be rejected again. The configuration
    has to change first.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class LiveKitResourceMissingError(LiveKitError):
    """The resource PostgreSQL expects does not exist in LiveKit.

    This is the drift condition from spec 46, surfaced as its own type so the
    caller can mark the row DRIFTED and offer Repair rather than reporting a
    generic failure.
    """

    def __init__(self, message: str, *, resource_id: str | None = None) -> None:
        super().__init__(message, retryable=False)
        self.resource_id = resource_id


class LiveKitUnsupportedError(LiveKitError):
    """LiveKit has no handler for this operation.

    The SDK exposes calls that a given server build may not route — updating a
    SIP inbound trunk is one. Distinguished from a rejection so the caller can
    fall back to a delete-and-recreate rather than reporting a configuration
    error that is really a version gap.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)
