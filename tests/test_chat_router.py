"""
Tests for app/services/chat/router.py

Covers:
  - classify_query: META route classified correctly
  - classify_query: STRUCTURED route classified correctly
  - classify_query: SEARCH route classified correctly
  - classify_query: HYBRID route classified correctly
  - classify_query: unknown route falls back to SEARCH
  - classify_query: invalid JSON on first attempt retries and succeeds
  - classify_query: invalid JSON on both attempts falls back to SEARCH
  - classify_query: LLM exception falls back to SEARCH
  - classify_query: filters extracted from query
  - classify_query: search_query always present
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.router import classify_query


def _llm_response(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_router_json(route: str, search_query: str = "test query", filters: dict | None = None) -> str:
    return json.dumps({
        "route": route,
        "filters": filters or {
            "speaker": None, "keyword": None, "date_from": None,
            "date_to": None, "meeting_title": None, "sentiment": None,
        },
        "search_query": search_query,
    })


@pytest.mark.asyncio
async def test_meta_route_classified():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_llm_response(_make_router_json("META", "list meetings this week"))
    )
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("What meetings do I have this week?")
    assert result["route"] == "META"


@pytest.mark.asyncio
async def test_structured_route_classified():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_llm_response(_make_router_json("STRUCTURED", "action items assigned to me"))
    )
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("What are my action items?")
    assert result["route"] == "STRUCTURED"


@pytest.mark.asyncio
async def test_search_route_classified():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_llm_response(_make_router_json("SEARCH", "Bob discussed pricing"))
    )
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("What did Bob say about pricing?")
    assert result["route"] == "SEARCH"
    assert result["search_query"] == "Bob discussed pricing"


@pytest.mark.asyncio
async def test_hybrid_route_classified():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_llm_response(_make_router_json("HYBRID", "sales call summary and transcript"))
    )
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("Summarise the sales call and show me exactly what was said")
    assert result["route"] == "HYBRID"


@pytest.mark.asyncio
async def test_unknown_route_falls_back_to_search():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_llm_response(_make_router_json("INVALID_ROUTE", "query"))
    )
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = ""
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("some query")
    assert result["route"] == "SEARCH"


@pytest.mark.asyncio
async def test_invalid_json_retries_and_succeeds():
    """First attempt returns bad JSON; second attempt returns valid JSON."""
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[
        _llm_response("not valid json {{"),
        _llm_response(_make_router_json("META", "meetings list")),
    ])
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("list my meetings")
    assert result["route"] == "META"
    assert mock_client.chat.completions.create.call_count == 2


@pytest.mark.asyncio
async def test_invalid_json_both_attempts_falls_back():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[
        _llm_response("bad json"),
        _llm_response("also bad"),
    ])
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("some query")
    assert result["route"] == "SEARCH"


@pytest.mark.asyncio
async def test_llm_exception_falls_back():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API error"))
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("anything")
    assert result["route"] == "SEARCH"


@pytest.mark.asyncio
async def test_speaker_filter_extracted():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_llm_response(_make_router_json(
            "SEARCH", "Bob pricing discussion",
            filters={"speaker": "Bob", "keyword": "pricing", "date_from": None,
                     "date_to": None, "meeting_title": None, "sentiment": None}
        ))
    )
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("What did Bob say about pricing?")
    assert result["filters"]["speaker"] == "Bob"
    assert result["filters"]["keyword"] == "pricing"


@pytest.mark.asyncio
async def test_search_query_always_present():
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_llm_response(_make_router_json("SEARCH", "reformulated query"))
    )
    with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
         patch("app.services.chat.router.get_settings") as mock_settings:
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM_MINI = "gpt-4o-mini"
        mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
        result = await classify_query("original query")
    assert result["search_query"] == "reformulated query"
