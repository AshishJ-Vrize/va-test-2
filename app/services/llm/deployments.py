"""LLM deployment resolution from .env with per-task overrides.

Single rule of thumb: every task has a function here that returns the Azure
deployment name to use. Each function falls back through a chain so the .env
only needs to set the BASE deployments (`AZURE_OPENAI_DEPLOYMENT_LLM`,
`AZURE_OPENAI_DEPLOYMENT_LLM_MINI`, `AZURE_OPENAI_DEPLOYMENT_EMBEDDING`).

Per-task overrides are optional — set them in `.env` to A/B-test a model on
one task without touching code.

Resolution chains
-----------------
    llm_for_router()    : LLM_ROUTER    || LLM_MINI || LLM
    llm_for_answer()    : LLM_ANSWER    || LLM
    llm_for_insights()  : LLM_INSIGHTS  || LLM
    llm_for_summary()   : LLM_SUMMARY   || LLM
    llm_default()       : LLM
    llm_mini()          : LLM_MINI || LLM
    embedding_deployment(): EMBEDDING (no fallback — must be set)
"""
from __future__ import annotations

from app.config.settings import get_settings


def _settings():
    return get_settings()


def llm_default() -> str:
    """The base full-power LLM. Used for answers, insights, summaries by default."""
    return _settings().AZURE_OPENAI_DEPLOYMENT_LLM


def llm_mini() -> str:
    """Cheap/fast LLM. Falls back to default when not configured."""
    s = _settings()
    return s.AZURE_OPENAI_DEPLOYMENT_LLM_MINI or s.AZURE_OPENAI_DEPLOYMENT_LLM


def llm_for_router() -> str:
    """Router / intent classification — wants speed over depth."""
    s = _settings()
    return s.AZURE_OPENAI_DEPLOYMENT_LLM_ROUTER or llm_mini()


def llm_for_answer() -> str:
    """Final user-facing answer composition — wants depth."""
    s = _settings()
    return s.AZURE_OPENAI_DEPLOYMENT_LLM_ANSWER or llm_default()


def llm_for_insights() -> str:
    """Per-meeting insight extraction (ingest-time, off the request path)."""
    s = _settings()
    return s.AZURE_OPENAI_DEPLOYMENT_LLM_INSIGHTS or llm_default()


def llm_for_summary() -> str:
    """Meeting MOM summary generation (ingest-time)."""
    s = _settings()
    return s.AZURE_OPENAI_DEPLOYMENT_LLM_SUMMARY or llm_default()


def embedding_deployment() -> str:
    """Embedding model for both ingest-time and query-time."""
    return _settings().AZURE_OPENAI_DEPLOYMENT_EMBEDDING
