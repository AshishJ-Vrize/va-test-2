# Scope A — MeetingsMixin: get_me, get_user_by_id, get_online_meeting,
#            get_meeting_by_join_url
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 13 (meetings.py exports)
#
# Design: MeetingsMixin defines methods that become part of GraphClient via
# inheritance (GraphClient inherits MeetingsMixin in client.py).
# This file does NOT import from client.py — methods use self.get() and
# self._base_path() which are guaranteed to exist on GraphClient instances.
# Exceptions are imported from exceptions.py to avoid circular imports.
#
# Verified ingestion flow (MVP + live testing 2026-04-21):
#   callChainId → joinWebUrl
#     → get_meeting_by_join_url()  → meetingId + participants (displayName=null)
#     → get_user_by_id(email/oid)  → real displayName per participant
#     → get_transcripts(meetingId) → transcriptId
#     → get_transcript_content()   → VTT string
#
# list_online_meetings was REMOVED — GET /me/onlineMeetings requires $filter,
# it is not a list-all endpoint. Verified live 2026-04-21 (Graph returns 400
# with graph_code=InvalidArgument without a filter).

import logging

from app.services.graph.exceptions import GraphClientError, MeetingNotFoundError

logger = logging.getLogger(__name__)


class MeetingsMixin:
    """
    Graph API methods related to users and online meetings.
    Mixed into GraphClient in client.py — do not instantiate directly.

    All methods call self.get() which is defined on GraphClient._request().
    Errors from the HTTP layer (TokenExpiredError, GraphClientError) bubble up
    to route handlers unless explicitly caught here.
    """

    # ── User methods ──────────────────────────────────────────────────────────

    def get_me(self) -> dict:
        """
        GET /me

        Returns the profile of the currently authenticated user.
        Fields returned by Graph: id, displayName, mail, userPrincipalName.

        ⚠ Delegated token only. Returns 400 if called with an app-only token.

        Raises:
            TokenExpiredError:  Token expired mid-session.
            GraphClientError:   Graph returned a non-2xx response.
        """
        logger.debug("get_me: fetching current user profile")
        result = self.get("/me")
        logger.debug(
            "get_me: success | user_id=%s | upn=%s",
            result.get("id"), result.get("userPrincipalName"),
        )
        return result

    def get_user_by_id(self, user_graph_id: str) -> dict | None:
        """
        GET /users/{user_graph_id}

        Fetches a user profile by their Graph object ID or UPN (email).
        The UPN works as a lookup key — Graph accepts both.

        Used to resolve display names for meeting participants.
        Graph always returns displayName=null in meeting participant responses —
        this method is the correct way to get the real display name.
        Reference: CONTEXT.md Section 13 (Graph API participant response shape).

        Args:
            user_graph_id: Graph object ID (oid) or UPN (email address).

        Returns:
            User profile dict on success, or None if the user was not found (404).
            None is returned (not raised) because a missing participant is not
            a fatal error — the caller decides how to handle it.

        Raises:
            TokenExpiredError:  Token expired.
            GraphClientError:   Graph returned a non-404 error (403, 5xx, etc.).
        """
        logger.debug("get_user_by_id: fetching user | user_graph_id=%s", user_graph_id)

        try:
            result = self.get(f"/users/{user_graph_id}")
            logger.debug(
                "get_user_by_id: success | user_graph_id=%s | display_name=%s",
                user_graph_id, result.get("displayName"),
            )
            return result
        except GraphClientError as exc:
            if exc.status_code == 404:
                logger.warning(
                    "get_user_by_id: user not found in Graph | user_graph_id=%s",
                    user_graph_id,
                )
                return None
            logger.error(
                "get_user_by_id: Graph error | user_graph_id=%s | status=%s | error=%s",
                user_graph_id, exc.status_code, exc.message,
            )
            raise

    # ── Meeting methods ───────────────────────────────────────────────────────

    def get_online_meeting(
        self,
        meeting_id: str,
        user_id: str | None = None,
    ) -> dict:
        """
        GET /me/onlineMeetings/{meeting_id}           (delegated token)
        GET /users/{user_id}/onlineMeetings/{meeting_id}  (app token)

        Fetches a single online meeting by its Graph meeting ID.
        Returns the raw Graph meeting dict.

        Args:
            meeting_id: Graph online meeting ID (meetings.meeting_graph_id in tenant DB).
            user_id:    Graph user ID. Pass when using an app-only token.

        Returns:
            Meeting dict from Graph.

        Raises:
            TokenExpiredError:  Token expired.
            GraphClientError:   Graph returned a non-2xx response.
                                status_code=404 means meeting not found.
        """
        base = self._base_path(user_id)
        path = f"{base}/{meeting_id}"
        logger.debug(
            "get_online_meeting: fetching | meeting_id=%s | user_id=%s | path=%s",
            meeting_id, user_id, path,
        )

        result = self.get(path)
        logger.info(
            "get_online_meeting: success | meeting_id=%s | subject=%s",
            meeting_id, result.get("subject"),
        )
        return result

    def get_meeting_by_join_url(
        self,
        join_url: str,
        user_id: str | None = None,
    ) -> dict:
        """
        GET /me/onlineMeetings?$filter=joinWebUrl eq '{join_url}'
        GET /users/{user_id}/onlineMeetings?$filter=joinWebUrl eq '{join_url}'

        Looks up a meeting by its join URL using an OData $filter.
        Returns the first match (join URLs are unique per meeting).

        This is the entry point of the ingestion flow:
          webhook callChainId → joinWebUrl → this method → meetingId → transcripts

        ⚠ $filter IS required on this endpoint — it does not support listing
          all meetings without a filter. Verified live 2026-04-21.

        Args:
            join_url: The full Teams join URL (meetings.join_url in tenant DB).
            user_id:  Graph user ID. Pass when using an app-only token.

        Returns:
            Meeting dict from Graph.

        Raises:
            MeetingNotFoundError: Graph responded successfully but no meeting
                                  matched the join URL. The URL may be stale
                                  or belong to a different user.
            TokenExpiredError:    Token expired.
            GraphClientError:     Graph returned a non-2xx response.
        """
        base = self._base_path(user_id)
        odata_filter = f"joinWebUrl eq '{join_url}'"
        logger.debug(
            "get_meeting_by_join_url: fetching | user_id=%s | join_url=%s",
            user_id, join_url,
        )

        result = self.get(base, params={"$filter": odata_filter})
        meetings = result.get("value", [])

        if not meetings:
            logger.warning(
                "get_meeting_by_join_url: no meeting found | "
                "user_id=%s | join_url=%s", user_id, join_url,
            )
            raise MeetingNotFoundError(
                f"No online meeting found matching join URL: {join_url!r}. "
                "The URL may be stale, already deleted from Graph, or belong "
                "to a user who is not the organiser."
            )

        meeting = meetings[0]
        logger.info(
            "get_meeting_by_join_url: success | meeting_id=%s | subject=%s",
            meeting.get("id"), meeting.get("subject"),
        )
        return meeting
