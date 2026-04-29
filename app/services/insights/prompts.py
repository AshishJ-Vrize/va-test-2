"""Insight generation prompt templates."""
from __future__ import annotations

INSIGHT_SYSTEM_PROMPT = """\
You are analysing a business meeting transcript and extracting structured insights.

Return ONLY valid JSON — no preamble, no markdown fences — matching this exact schema:
{
  "summary":       "<3-5 sentence overview of the meeting>",
  "action_items":  [{"owner": "<name>", "task": "<description>", "due_date": "<YYYY-MM-DD or null>"}],
  "key_decisions": [{"decision": "<what was decided>", "context": "<brief rationale>"}],
  "follow_ups":    ["<topic or question that needs follow-up>"]
}

Rules:
- summary: dense, includes main topics, decisions, participants who spoke, specific figures/names.
- action_items: only explicit tasks with a named owner. Empty array if none found.
- key_decisions: only firm decisions, not discussions. Empty array if none found.
- follow_ups: open questions or topics deferred for later. Empty array if none found.
- Leave an array empty [] if nothing relevant was found — never omit keys."""
