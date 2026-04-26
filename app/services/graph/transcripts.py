# Scope A — TranscriptsMixin: get_transcripts, get_transcript_content
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 13 (transcripts.py exports)
#
# Verified response shape for GET .../transcripts (2026-03-25 live call):
# {
#   "@odata.context": "...",
#   "@odata.count": 1,
#   "value": [
#     {
#       "id": "<transcript-id>",          ← used in get_transcript_content
#       "meetingId": "...",
#       "callId": "...",
#       "contentCorrelationId": "...",
#       "transcriptContentUrl": "...",
#       "createdDateTime": "2026-03-25T10:31:24.8590375Z",
#       "endDateTime": "2026-03-25T11:16:12.0390375Z",
#       "meetingOrganizer": {
#         "user": {
#           "id": "<user-id>",
#           "displayName": null,          ← always null, same as participants
#           "tenantId": "..."
#         }
#       }
#     }
#   ]
# }
#
# get_transcript_content returns raw VTT text (not JSON).
# It calls self.get_text() — defined on GraphClient — instead of self.get().

import logging

logger = logging.getLogger(__name__)


class TranscriptsMixin:
    """
    Graph API methods related to meeting transcripts.
    Mixed into GraphClient in client.py — do not instantiate directly.

    Methods use self.get(), self.get_text(), and self._base_path(),
    all of which are defined on GraphClient.
    """

    async def get_transcripts(
        self,
        meeting_id: str,
        user_id: str | None = None,
    ) -> list[dict]:
        """
        GET /me/onlineMeetings/{meeting_id}/transcripts
        GET /users/{user_id}/onlineMeetings/{meeting_id}/transcripts

        Returns the list of transcript objects for a meeting.
        Each dict contains at minimum: id, createdDateTime, endDateTime.
        Use transcript["id"] to fetch the actual VTT content.

        Returns an empty list if no transcript is ready yet — this is not an error.
        Teams takes several minutes to process transcripts after a meeting ends.
        The ingestion pipeline should retry after a delay on empty results.

        Args:
            meeting_id: Graph online meeting ID (meetings.meeting_graph_id).
            user_id:    Graph user ID. Pass when using an app-only token.
                        Leave None when using a delegated (user) token.

        Returns:
            List of transcript dicts. Empty list if transcription not ready.

        Raises:
            TokenExpiredError:  Token expired.
            GraphClientError:   Graph returned a non-2xx response.
                                status_code=404 means the meeting ID is invalid.
        """
        base = self._base_path(user_id)
        path = f"{base}/{meeting_id}/transcripts"
        logger.debug(
            "get_transcripts: fetching | meeting_id=%s | user_id=%s | path=%s",
            meeting_id, user_id, path,
        )

        result = await self.get(path)
        transcripts = result.get("value", [])

        if not transcripts:
            logger.info(
                "get_transcripts: no transcripts available yet | "
                "meeting_id=%s | user_id=%s", meeting_id, user_id,
            )
        else:
            logger.info(
                "get_transcripts: success | meeting_id=%s | count=%d",
                meeting_id, len(transcripts),
            )

        return transcripts

    async def get_transcript_content(
        self,
        meeting_id: str,
        transcript_id: str,
        user_id: str | None = None,
    ) -> str:
        """
        GET /me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content?$format=text/vtt
        GET /users/{user_id}/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content?$format=text/vtt

        Downloads the raw VTT transcript content as a string.
        Returns the full file — the VTT parser (services/ingestion/vtt_parser.py)
        is responsible for splitting and processing it.

        Timeout is set to 60 seconds instead of the standard 30s because VTT
        files from long meetings can be several MB — they take longer to download.

        Args:
            meeting_id:     Graph online meeting ID.
            transcript_id:  Transcript ID from get_transcripts() — the 'id' field
                            in each transcript object. Verified field name: 'id'.
            user_id:        Graph user ID. Pass when using an app-only token.

        Returns:
            Raw VTT string. Parse with vtt_parser.py — do not process here.

        Raises:
            TokenExpiredError:  Token expired.
            GraphClientError:   Graph returned a non-2xx response.
                                status_code=404 means transcript_id is invalid
                                or content is not yet available.
        """
        base = self._base_path(user_id)
        path = f"{base}/{meeting_id}/transcripts/{transcript_id}/content"
        logger.debug(
            "get_transcript_content: fetching | meeting_id=%s | "
            "transcript_id=%s | user_id=%s | path=%s",
            meeting_id, transcript_id, user_id, path,
        )

        content = await self.get_text(path, params={"$format": "text/vtt"}, timeout=60.0)

        logger.info(
            "get_transcript_content: success | meeting_id=%s | "
            "transcript_id=%s | content_length=%d",
            meeting_id, transcript_id, len(content),
        )
        return content
