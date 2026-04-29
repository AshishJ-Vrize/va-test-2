"""Insight generation — generate_insights_for_meeting(db, meeting_id) via GPT-4o."""
from __future__ import annotations

import json
import logging
import uuid

from openai import RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.services.insights.prompts import INSIGHT_SYSTEM_PROMPT
from app.services.insights.parser import parse_insights

log = logging.getLogger(__name__)

_MAX_DURATION_MINUTES = 90
_MAX_TRANSCRIPT_CHARS = 60000  # ~90 min of speech


async def generate_insights_for_meeting(
    db: AsyncSession,
    meeting_id: uuid.UUID,
) -> bool:
    """
    Generate and persist structured insights for a meeting that has been ingested.

    Steps:
    1. Load meeting + transcript from DB.
    2. Skip if: no transcript, transcript is empty, or meeting > 90 min.
    3. Call GPT-4o with structured prompt → parse JSON.
    4. Retry once on invalid JSON, then store null fields.
    5. Save to meeting_insights (one row per insight_type).
    6. Log credit usage.

    Returns True if insights were generated, False if skipped or failed.
    Never raises — insight failure must not block ingestion.
    """
    from app.db.helpers.insight_ops import save_insights, has_insights
    from app.db.tenant.models import Meeting, Transcript
    from sqlalchemy import select

    try:
        meeting = await db.get(Meeting, meeting_id)
        if meeting is None:
            log.warning("generate_insights: meeting %s not found", meeting_id)
            return False

        # Skip long meetings
        if meeting.duration_minutes and meeting.duration_minutes > _MAX_DURATION_MINUTES:
            log.info("generate_insights: skipping meeting %s (duration %d min > 90)", meeting_id, meeting.duration_minutes)
            return False

        result = await db.execute(select(Transcript).where(Transcript.meeting_id == meeting_id))
        transcript = result.scalar_one_or_none()
        if not transcript or not transcript.raw_text:
            log.info("generate_insights: skipping meeting %s (no transcript)", meeting_id)
            return False

        meeting_date = (
            meeting.meeting_date.strftime("%Y-%m-%d")
            if meeting.meeting_date else ""
        )
        insights = await _call_llm_with_retry(
            meeting_subject=meeting.meeting_subject or "",
            transcript_text=transcript.raw_text[:_MAX_TRANSCRIPT_CHARS],
            meeting_date=meeting_date,
        )

        if insights is None:
            return False

        await save_insights(db, meeting_id, insights)
        log.info("generate_insights: saved insights for meeting %s", meeting_id)
        return True

    except Exception:
        log.exception("generate_insights: failed for meeting %s", meeting_id)
        return False


async def _call_llm_with_retry(meeting_subject: str, transcript_text: str, meeting_date: str = "") -> dict | None:
    """Call GPT-4o, retry once on JSON parse failure. Returns None on unrecoverable failure."""
    from app.services.ingestion.contextualizer import _get_client, _llm_deployment

    client = _get_client()
    deployment = _llm_deployment()

    date_hint = f"Meeting date: {meeting_date}\n" if meeting_date else ""
    user_message = (
        f"Meeting: {meeting_subject}\n"
        f"{date_hint}\n"
        f"Transcript:\n{transcript_text}"
    )

    for attempt in range(2):
        try:
            resp = await _create_with_backoff(
                client=client,
                deployment=deployment,
                user_message=user_message,
            )  # user_message already contains meeting_date hint
            raw = json.loads(resp.choices[0].message.content or "{}")
            return parse_insights(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                log.warning("generate_insights: invalid JSON on attempt 1, retrying: %s", exc)
                continue
            log.error("generate_insights: invalid JSON after retry, storing null: %s", exc)
            return None
        except Exception as exc:
            log.error("generate_insights: LLM call failed: %s", exc)
            return None

    return None


@retry(
    retry=retry_if_exception_type(RateLimitError),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def _create_with_backoff(client, deployment: str, user_message: str):
    return await client.chat.completions.create(
        model=deployment,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
        max_tokens=800,
    )
