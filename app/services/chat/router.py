"""Route classifier — classify_query() → META | STRUCTURED | SEARCH | HYBRID | GENERAL.

Two stages:
1. LLM classifies the query and extracts filters (speaker, date_from, date_to,
   meeting_title, sentiment, keyword) plus a cleaned `search_query`.
2. If a speaker name was extracted, resolve it to graph_id(s) via tenant-wide
   meeting_participants lookup. The resolved graph IDs go into
   filters["speaker_graph_ids"] which the SEARCH handler uses as a hard filter.

The resolver scope is the whole tenant (not restricted to the user's authorized
meetings). RBAC is enforced separately at chunk-query time via the
`authorized_meeting_ids` filter — so a broad resolver scope improves recall
without leaking data.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.services.chat.prompts import ROUTER_SYSTEM

log = logging.getLogger(__name__)

_VALID_ROUTES = {"META", "STRUCTURED", "SEARCH", "HYBRID", "GENERAL"}
_FALLBACK: dict = {
    "route": "SEARCH",
    "filters": {
        "speaker": None, "speaker_graph_ids": None, "keyword": None,
        "date_from": None, "date_to": None,
        "meeting_title": None, "sentiment": None,
    },
    "search_query": "",
}


async def classify_query(query: str, db: AsyncSession) -> dict:
    """Classify the query, extract filters, resolve speaker name to graph_id(s).

    Returns dict with keys: route, filters, search_query.
    Falls back to SEARCH on any LLM error. Speaker resolution failures
    silently leave `speaker_graph_ids` as None — search still works via
    BM25 ranking on the speaker token in search_text.
    """
    from app.services.ingestion.contextualizer import _get_client

    settings = get_settings()
    deployment = settings.AZURE_OPENAI_DEPLOYMENT_LLM_MINI or settings.AZURE_OPENAI_DEPLOYMENT_LLM
    client = _get_client()

    raw_classification: dict | None = None
    for attempt in range(2):
        try:
            resp = await client.chat.completions.create(
                model=deployment,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": ROUTER_SYSTEM},
                    {"role": "user", "content": query},
                ],
                temperature=0.0,
                max_tokens=200,
            )
            raw_classification = json.loads((resp.choices[0].message.content or "").strip())
            break
        except json.JSONDecodeError:
            if attempt == 0:
                log.warning("router: invalid JSON on attempt 1, retrying")
                continue
            log.error("router: invalid JSON after retry, falling back to SEARCH")
        except Exception as exc:
            log.error("router: error classifying query: %s", exc)
            break

    if raw_classification is None:
        fallback = dict(_FALLBACK)
        fallback["search_query"] = query
        return fallback

    route = (raw_classification.get("route") or "SEARCH").upper()
    if route not in _VALID_ROUTES:
        route = "SEARCH"
    search_query = raw_classification.get("search_query") or query
    raw_filters = raw_classification.get("filters") or {}

    speaker_name = raw_filters.get("speaker")
    speaker_graph_ids = (
        await _resolve_speaker_to_graph_ids(speaker_name, db)
        if speaker_name else None
    )

    filters = {
        "speaker": speaker_name,
        "speaker_graph_ids": speaker_graph_ids,
        "keyword": raw_filters.get("keyword"),
        "date_from": raw_filters.get("date_from"),
        "date_to": raw_filters.get("date_to"),
        "meeting_title": raw_filters.get("meeting_title"),
        "sentiment": raw_filters.get("sentiment"),
    }
    log.info(
        "router: query=%r route=%s speaker=%r resolved_gids=%d",
        query[:80], route, speaker_name, len(speaker_graph_ids or []),
    )
    return {"route": route, "filters": filters, "search_query": search_query}


async def _resolve_speaker_to_graph_ids(name: str, db: AsyncSession) -> list[str]:
    """Tenant-wide name → graph_id resolution.

    Tiered match:
    1. Exact case-insensitive on participant_name
    2. First-name unique match (split_part)

    Returns the list of distinct graph IDs that match — could be 0, 1, or many.
    Many-matches are intentional: when "Ashish" is ambiguous across the tenant,
    SEARCH filters by ANY of those graph_ids and lets ranking surface the most
    relevant person. RBAC is enforced separately at the chunks meeting_id filter.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return []

    # Tier 1: exact match on full name.
    rows = await db.execute(
        text("""
            SELECT DISTINCT participant_graph_id
            FROM meeting_participants
            WHERE LOWER(TRIM(participant_name)) = LOWER(:name)
        """),
        {"name": cleaned},
    )
    matches = [r.participant_graph_id for r in rows]
    if matches:
        return matches

    # Tier 2: first-name match.
    first = cleaned.split()[0] if cleaned.split() else cleaned
    rows = await db.execute(
        text("""
            SELECT DISTINCT participant_graph_id
            FROM meeting_participants
            WHERE LOWER(SPLIT_PART(TRIM(participant_name), ' ', 1)) = LOWER(:first)
        """),
        {"first": first},
    )
    return [r.participant_graph_id for r in rows]
