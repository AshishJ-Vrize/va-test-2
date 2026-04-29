"""Route classifier — classify_query() → META | STRUCTURED | SEARCH | HYBRID."""
from __future__ import annotations

import json
import logging

from app.config.settings import get_settings
from app.services.chat.prompts import ROUTER_SYSTEM

log = logging.getLogger(__name__)

_VALID_ROUTES = {"META", "STRUCTURED", "SEARCH", "HYBRID"}
_FALLBACK: dict = {
    "route": "SEARCH",
    "filters": {
        "speaker": None, "keyword": None, "date_from": None,
        "date_to": None, "meeting_title": None, "sentiment": None,
    },
    "search_query": "",
}


async def classify_query(query: str) -> dict:
    """
    Ask GPT-4o-mini to classify the query and extract filters.
    Returns dict with keys: route, filters, search_query.
    Falls back to SEARCH on any failure.
    """
    from app.services.ingestion.contextualizer import _get_client

    settings = get_settings()
    deployment = settings.AZURE_OPENAI_DEPLOYMENT_LLM_MINI or settings.AZURE_OPENAI_DEPLOYMENT_LLM
    client = _get_client()

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
            raw = (resp.choices[0].message.content or "").strip()
            result = json.loads(raw)
            route = result.get("route", "SEARCH").upper()
            if route not in _VALID_ROUTES:
                route = "SEARCH"
            search_query = result.get("search_query") or query
            filters = {
                "speaker": result.get("filters", {}).get("speaker"),
                "keyword": result.get("filters", {}).get("keyword"),
                "date_from": result.get("filters", {}).get("date_from"),
                "date_to": result.get("filters", {}).get("date_to"),
                "meeting_title": result.get("filters", {}).get("meeting_title"),
                "sentiment": result.get("filters", {}).get("sentiment"),
            }
            log.info("router: query=%r route=%s", query[:80], route)
            return {"route": route, "filters": filters, "search_query": search_query}
        except json.JSONDecodeError:
            if attempt == 0:
                log.warning("router: invalid JSON on attempt 1, retrying")
                continue
            log.error("router: invalid JSON after retry, falling back to SEARCH")
        except Exception as exc:
            log.error("router: error classifying query: %s", exc)
            break

    fallback = dict(_FALLBACK)
    fallback["search_query"] = query
    return fallback
