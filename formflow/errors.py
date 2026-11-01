from __future__ import annotations

from typing import Any, Optional


class FormFlowError(Exception):
    """Any non-2xx answer. `code` is the API error code (validation_failed, rate_limited, ...) or `network_error`."""

    def __init__(self, status: int, code: str, message: str, details: Any = None,
                 request_id: Optional[str] = None, retry_after: Optional[float] = None) -> None:
        super().__init__(f"{code} ({status}): {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id
        self.retry_after = retry_after


class SignatureError(FormFlowError):
    def __init__(self, message: str = "Webhook signature verification failed") -> None:
        super().__init__(400, "invalid_signature", message)
