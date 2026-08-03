"""Shared timing contract for connection-scoped liveness streams."""

#: The service emits one response-body keepalive at this interval.
LIVENESS_KEEPALIVE_SECONDS = 5.0

#: Six missed keepalive intervals prove that a held stream is no longer usable.
LIVENESS_READ_TIMEOUT_SECONDS = LIVENESS_KEEPALIVE_SECONDS * 6
