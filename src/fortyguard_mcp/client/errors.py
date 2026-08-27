"""
Error model.

The governing rule is to pass the API's message through unchanged. FortyGuard's
validation messages enumerate their own valid sets and say more than a
translation would:

    Polygon ring is not closed: the first and last positions must be identical.
    Input should be 60, 80 or 100
    Latitude -112.095 is out of bounds; must be between -90.0 and 90.0.

What this layer adds is structure, not prose: the HTTP status, the field the API
named, and the raw body. The only messages authored here are for conditions that
never reached the API — encoding failures, transport failures, and our own
polling timeout.
"""

from __future__ import annotations

from typing import Any

from ..domain.api_schema import extract_error_message


class FortyGuardError(Exception):
    """
    Base for everything this client raises.

    `to_dict` lives here, not only on subclasses: callers catch the base and
    render whatever they caught, so a subclass that forgot the method would fail
    while reporting a failure.
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "message": str(self),
            "source": "fortyguard-mcp",
        }


class MissingKeyError(FortyGuardError, RuntimeError):
    """
    No API key could be found.

    A FortyGuardError so it travels the same path as every other failure:
    caught by the tool, returned as data, message intact. Raised as a bare
    RuntimeError it escaped every handler, and from mcp 2.1.1 the transport
    replaces an unhandled exception's text with "Error executing tool X" -
    turning the most actionable message this server produces into the least.

    Still a RuntimeError as well, because that is the type callers could
    already catch, and narrowing it would break them for no benefit.
    """


class APIError(FortyGuardError):
    """The API responded with an error. Its message is preserved verbatim."""

    def __init__(self, status_code: int, body: Any, *, url: str | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.url = url
        self.api_message = extract_error_message(body)
        self.field = body.get("field") if isinstance(body, dict) else None
        super().__init__(self._render())

    def _render(self) -> str:
        msg = self.api_message or f"HTTP {self.status_code}"
        return f"[{self.status_code}] {msg}"

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)

    @property
    def is_validation_error(self) -> bool:
        return self.status_code in (400, 422)

    def to_dict(self) -> dict[str, Any]:
        """
        Structured form for a tool response.

        Falls back to `str(self)` rather than `"message": null` - an empty
        body or an HTML error page yields nothing extractable, and the live API
        returns exactly that on `fetch-api-key-usage` for an unknown key.
        """
        return {
            "error": True,
            "status_code": self.status_code,
            "message": self.api_message or str(self),
            "api_message_present": self.api_message is not None,
            "field": self.field,
            "source": "fortyguard-api",
            "raw": self.body,
        }


class UnexpectedResponse(FortyGuardError):
    """
    A successful HTTP response whose body does not match the contract.

    Distinct from `APIError`: the API did not report a failure, so
    `{"error": true, "status_code": 200}` would be self-contradictory.
    """

    def __init__(self, detail: str, *, body: Any, url: str | None = None) -> None:
        self.detail = detail
        self.body = body
        self.url = url
        super().__init__(f"Unexpected response shape: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "message": str(self),
            "source": "protocol",
            "hint": "The API returned success but the payload was not in the "
                    "expected form. The raw body is included unchanged.",
            "raw": self.body,
        }


class UnsendableRequest(FortyGuardError):
    """
    The request could not be encoded, so it never left this machine.

    Distinct from `TransportError`: nothing was attempted and it is not
    retryable. In practice a non-finite number in the payload.
    """

    def __init__(self, detail: str, *, url: str | None = None) -> None:
        self.url = url
        self.detail = detail
        super().__init__(f"This request could not be encoded as JSON: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "message": str(self),
            "source": "fortyguard-mcp",
            "retryable": False,
            "hint": "Usually a NaN or Infinity value in the request - most often "
                    "a coordinate. Those are not valid JSON numbers and cannot "
                    "be sent. Replace them with real values.",
        }


class TransportError(FortyGuardError):
    """DNS, TLS, connection reset, socket timeout. No API message exists."""

    def __init__(self, detail: str, *, url: str | None = None) -> None:
        self.url = url
        self.detail = detail
        super().__init__(f"Could not reach the FortyGuard API: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "message": str(self),
            "source": "transport",
            "retryable": True,
        }


class PollTimeout(FortyGuardError):
    """
    We stopped waiting; the job may still be running server-side.

    Not a failure of the job: some requests were still `Processing` after
    ~470 s, with no way to tell a long job from a stuck one. The activity_id is
    carried so the result stays collectable.
    """

    def __init__(self, activity_id: str, waited_s: float, last_status: str | None) -> None:
        self.activity_id = activity_id
        self.waited_s = waited_s
        self.last_status = last_status
        seen = f"Last status was {last_status!r}" if last_status \
            else "The API reported no status"
        super().__init__(
            f"Stopped waiting after {waited_s:.0f}s. {seen}. "
            f"The job was not cancelled - retrieve it later with "
            f"activity_id={activity_id}."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": False,           # not a failure - work is still in flight
            # Verbatim, including None: substituting "Processing" would report a
            # status the API never sent.
            "status": self.last_status,
            "activity_id": self.activity_id,
            "waited_s": round(self.waited_s, 1),
            "next": "check_status",
            "message": str(self),
        }


class TaskFailed(FortyGuardError):
    """
    The API reported a terminal failure.

    Never observed in ~100 live calls: invalid work rejects at submit, succeeds
    emptily at full price, or stays `Processing`. This handles a state the
    vendor documents, which is why the credit note below is attributed to them.
    """

    def __init__(self, activity_id: str, status: str, body: Any) -> None:
        self.activity_id = activity_id
        self.status = status
        self.body = body
        self.api_message = extract_error_message(body)
        super().__init__(
            f"Task {activity_id} ended with status {status}"
            + (f": {self.api_message}" if self.api_message else "")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "status": self.status,
            "activity_id": self.activity_id,
            "message": self.api_message or str(self),
            "api_message_present": self.api_message is not None,
            "source": "fortyguard-api",
            "credits_note": (
                "FortyGuard's documentation states that failed tasks consume no "
                "credits. This client has never observed a Failed status, so "
                "that is the vendor's claim rather than something measured "
                "here. Check get_credit_usage if the balance matters."
            ),
            "raw": self.body,
        }
