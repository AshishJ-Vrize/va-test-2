"""Reserved for future cross-meeting search helpers.

The previous `hybrid_chunk_search` is no longer needed — the live SEARCH
path in `app.services.chat.search_handler` runs its own BM25 + pgvector
hybrid with RRF directly against the v2 chunks schema.

The previous `cross_meeting_search` referenced the old
`meeting_participants.user_id` column (replaced by `participant_graph_id`
in migration `20260428_0900_fix_participant_schema`) and would not run
against the current schema. It also had no production callers — only
unit tests under `tests/test_vector_search.py`.

If a cross-meeting summary search is needed later, it can be added here
using `meeting_summaries.embedding` filtered by
`meeting_participants.participant_graph_id = current_user.graph_id`.
"""
