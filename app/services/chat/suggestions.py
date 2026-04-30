"""Generate contextual follow-up question suggestions after each answer."""
from __future__ import annotations

import json
import logging

from app.config.settings import get_settings

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a helpful assistant. Given a user question and the assistant's answer, "
    "suggest exactly 3 concise follow-up questions the user might want to ask next. "
    "Questions should be short (under 10 words), specific, and directly related to the topic. "
    "Return ONLY a valid JSON array of 3 strings. No explanation, no markdown, no other text."
)


async def generate_suggestions(query: str, answer: str) -> list[str]:
    from app.services.ingestion.contextualizer import _get_client

    client = _get_client()
    deployment = get_settings().AZURE_OPENAI_DEPLOYMENT_LLM

    try:
        resp = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Question: {query}\n\nAnswer: {answer[:800]}"},
            ],
            temperature=0.7,
            max_tokens=120,
        )
        raw = resp.choices[0].message.content.strip()
        suggestions = json.loads(raw)
        if isinstance(suggestions, list):
            return [str(s) for s in suggestions[:3]]
    except Exception as exc:
        log.warning("generate_suggestions failed | error=%s", exc)

    return []
