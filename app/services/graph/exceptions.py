# Scope A — Shared exceptions for all graph/ modules
# Owner: Graph + Routes team
# Centralised here so client.py and meetings.py/transcripts.py can both import
# without creating circular dependencies.


class TokenExpiredError(Exception):
    """
    Raised when Graph API returns HTTP 401.
    Means the user's delegated token expired mid-session.
    Route handlers catch this and return 401 so the frontend can re-authenticate.
    """


class GraphClientError(Exception):
    """
    Raised for all non-2xx Graph API responses except 401 (403, 404, 429, 5xx),
    and for network failures (timeout, connection error).
    Carries status_code so route handlers can map it to the right HTTP response.
    status_code is None for network-level failures (no HTTP response received).
    """
    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class MeetingNotFoundError(Exception):
    """
    Raised by get_meeting_by_join_url when the OData filter returns no results.
    Distinct from GraphClientError (which is an HTTP failure) — this means Graph
    responded successfully but returned an empty result set.
    """
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
