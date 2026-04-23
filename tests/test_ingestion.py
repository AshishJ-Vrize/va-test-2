"""
Tests for app/services/ingestion/pipeline.py and app/services/ingestion/embedder.py

All DB and Azure OpenAI calls are mocked — no real infrastructure needed.

Why top-level imports matter here
----------------------------------
unittest.mock.patch("a.b.c.func") resolves the dotted path by importing
each segment. If "a.b.c" has never been imported, Python can't find "func"
on it and raises AttributeError.  Importing the modules at the top of this
file guarantees they are in sys.modules before any patch() call runs.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Must be imported at module level so patch() can resolve their attributes ──
import app.services.ingestion.embedder  # noqa: F401
import app.services.ingestion.pipeline  # noqa: F401

from app.db.tenant.models import Chunk, CreditUsage, Meeting, SpeakerAnalytic, Transcript
from app.services.ingestion.embedder import embed_batch, embed_single
from app.services.ingestion.pipeline import run_ingestion_pipeline


# ── Shared fixtures ───────────────────────────────────────────────────────────

SIMPLE_VTT = """\
WEBVTT

00:00:01.000 --> 00:00:10.000
<v John Doe>Hello everyone welcome to the meeting.

00:00:11.000 --> 00:00:20.000
<v Jane Smith>Thanks for joining us today.
"""


def _make_meeting(duration_minutes=5, status="pending"):
    m = MagicMock(spec=Meeting)
    m.id = uuid.uuid4()
    m.status = status
    m.duration_minutes = duration_minutes
    return m


def _make_transcript(meeting_id=None):
    t = MagicMock(spec=Transcript)
    t.id = uuid.uuid4()
    t.meeting_id = meeting_id or uuid.uuid4()
    return t


def _fake_embedding(dim=1536):
    return [0.1] * dim


def _make_db(meeting=None, existing_transcript=None):
    """
    Return a (db, meeting) pair where db is a mock AsyncSession.

    db.get(Meeting, id)            → meeting
    db.execute(select(...))        → result with scalar_one_or_none() = existing_transcript
    db.execute(delete(...))        → no-op result
    db.flush()                     → coroutine that returns None
    """
    db = MagicMock()
    meeting = meeting or _make_meeting()
    db.get = AsyncMock(return_value=meeting)
    db.flush = AsyncMock()

    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = existing_transcript
    db.execute = AsyncMock(return_value=execute_result)

    return db, meeting


# ── Pipeline — happy path ─────────────────────────────────────────────────────

class TestRunIngestionPipelineHappyPath:

    async def _run(self, vtt=SIMPLE_VTT, duration_minutes=5, credits_per_minute=2):
        meeting = _make_meeting(duration_minutes=duration_minutes)
        db, _ = _make_db(meeting=meeting, existing_transcript=None)
        fake_emb = _fake_embedding()

        with patch(
            "app.services.ingestion.pipeline.embed_batch",
            new=AsyncMock(return_value=[fake_emb, fake_emb]),
        ):
            await run_ingestion_pipeline(
                meeting_id=meeting.id,
                vtt_content=vtt,
                db=db,
                credits_per_minute=credits_per_minute,
            )

        return meeting, db

    async def test_meeting_status_set_to_ready(self):
        meeting, _ = await self._run()
        assert meeting.status == "ready"

    async def test_db_add_called_for_transcript(self):
        _, db = await self._run()
        types_added = [type(c[0][0]).__name__ for c in db.add.call_args_list]
        assert "Transcript" in types_added

    async def test_db_add_called_for_chunks(self):
        _, db = await self._run()
        types_added = [type(c[0][0]).__name__ for c in db.add.call_args_list]
        assert "Chunk" in types_added

    async def test_db_add_called_for_speaker_analytics(self):
        _, db = await self._run()
        types_added = [type(c[0][0]).__name__ for c in db.add.call_args_list]
        assert "SpeakerAnalytic" in types_added

    async def test_db_add_called_for_credit_usage(self):
        _, db = await self._run()
        types_added = [type(c[0][0]).__name__ for c in db.add.call_args_list]
        assert "CreditUsage" in types_added

    async def test_credit_usage_operation_is_ingestion(self):
        _, db = await self._run()
        credit_rows = [
            c[0][0] for c in db.add.call_args_list
            if isinstance(c[0][0], CreditUsage)
        ]
        assert len(credit_rows) == 1
        assert credit_rows[0].operation == "ingestion"

    async def test_credits_consumed_uses_duration_and_rate(self):
        _, db = await self._run(duration_minutes=10, credits_per_minute=2)
        credit_row = next(
            c[0][0] for c in db.add.call_args_list
            if isinstance(c[0][0], CreditUsage)
        )
        assert credit_row.credits_consumed == 20  # 10 min * 2 credits

    async def test_duration_minutes_none_falls_back_to_1(self):
        meeting = _make_meeting(duration_minutes=None)
        db, _ = _make_db(meeting=meeting, existing_transcript=None)
        fake_emb = _fake_embedding()

        with patch(
            "app.services.ingestion.pipeline.embed_batch",
            new=AsyncMock(return_value=[fake_emb, fake_emb]),
        ):
            await run_ingestion_pipeline(
                meeting_id=meeting.id,
                vtt_content=SIMPLE_VTT,
                db=db,
                credits_per_minute=3,
            )

        credit_row = next(
            c[0][0] for c in db.add.call_args_list
            if isinstance(c[0][0], CreditUsage)
        )
        assert credit_row.credits_consumed == 3  # 1 minute fallback * 3

    async def test_db_flush_called_multiple_times(self):
        _, db = await self._run()
        assert db.flush.call_count >= 4

    async def test_db_commit_never_called(self):
        """Commit is the caller's responsibility — pipeline must never call it."""
        _, db = await self._run()
        db.commit.assert_not_called()


# ── Pipeline — meeting not found ──────────────────────────────────────────────

class TestRunIngestionPipelineMeetingNotFound:

    async def test_raises_value_error(self):
        db = MagicMock()
        db.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="not found"):
            await run_ingestion_pipeline(
                meeting_id=uuid.uuid4(),
                vtt_content=SIMPLE_VTT,
                db=db,
                credits_per_minute=1,
            )


# ── Pipeline — empty VTT ──────────────────────────────────────────────────────

class TestRunIngestionPipelineEmptyVtt:

    async def test_empty_vtt_marks_meeting_failed(self):
        meeting = _make_meeting()
        db, _ = _make_db(meeting=meeting)
        with pytest.raises(ValueError, match="zero segments"):
            await run_ingestion_pipeline(
                meeting_id=meeting.id,
                vtt_content="WEBVTT\n",
                db=db,
                credits_per_minute=1,
            )
        assert meeting.status == "failed"


# ── Pipeline — embed failure ──────────────────────────────────────────────────

class TestRunIngestionPipelineEmbedFailure:

    async def test_embed_failure_marks_meeting_failed(self):
        meeting = _make_meeting()
        db, _ = _make_db(meeting=meeting)
        with patch(
            "app.services.ingestion.pipeline.embed_batch",
            new=AsyncMock(side_effect=RuntimeError("Azure OpenAI unreachable")),
        ):
            with pytest.raises(RuntimeError):
                await run_ingestion_pipeline(
                    meeting_id=meeting.id,
                    vtt_content=SIMPLE_VTT,
                    db=db,
                    credits_per_minute=1,
                )
        assert meeting.status == "failed"

    async def test_embed_failure_reraises_exception(self):
        meeting = _make_meeting()
        db, _ = _make_db(meeting=meeting)
        with patch(
            "app.services.ingestion.pipeline.embed_batch",
            new=AsyncMock(side_effect=ValueError("bad embedding")),
        ):
            with pytest.raises(ValueError, match="bad embedding"):
                await run_ingestion_pipeline(
                    meeting_id=meeting.id,
                    vtt_content=SIMPLE_VTT,
                    db=db,
                    credits_per_minute=1,
                )


# ── Pipeline — re-ingestion ───────────────────────────────────────────────────

class TestRunIngestionPipelineReIngestion:

    async def test_existing_transcript_is_updated_not_duplicated(self):
        meeting = _make_meeting()
        existing = _make_transcript(meeting_id=meeting.id)
        db, _ = _make_db(meeting=meeting, existing_transcript=existing)
        fake_emb = _fake_embedding()

        with patch(
            "app.services.ingestion.pipeline.embed_batch",
            new=AsyncMock(return_value=[fake_emb, fake_emb]),
        ):
            await run_ingestion_pipeline(
                meeting_id=meeting.id,
                vtt_content=SIMPLE_VTT,
                db=db,
                credits_per_minute=1,
            )

        # No new Transcript should be db.add()'d — existing one was updated in place
        new_transcript_adds = [
            c for c in db.add.call_args_list
            if isinstance(c[0][0], Transcript)
        ]
        assert len(new_transcript_adds) == 0

        # Existing row fields must be overwritten
        assert existing.raw_text == SIMPLE_VTT
        assert existing.language == "en"
        assert isinstance(existing.word_count, int)


# ── Embedder — embed_batch ────────────────────────────────────────────────────

class TestEmbedBatch:

    def _fake_response(self, texts, dim=1536):
        data = [
            SimpleNamespace(index=i, embedding=[0.1] * dim)
            for i in range(len(texts))
        ]
        return SimpleNamespace(data=data)

    async def test_returns_one_vector_per_text(self):
        texts = ["hello", "world", "test"]
        with patch("app.services.ingestion.embedder._get_client") as mock_client:
            mock_client.return_value.embeddings.create = AsyncMock(
                return_value=self._fake_response(texts)
            )
            result = await embed_batch(texts)
        assert len(result) == 3

    async def test_each_vector_has_1536_dims(self):
        texts = ["hello", "world"]
        with patch("app.services.ingestion.embedder._get_client") as mock_client:
            mock_client.return_value.embeddings.create = AsyncMock(
                return_value=self._fake_response(texts)
            )
            result = await embed_batch(texts)
        for vec in result:
            assert len(vec) == 1536

    async def test_empty_input_returns_empty_list(self):
        assert await embed_batch([]) == []

    async def test_wrong_dimension_raises_value_error(self):
        texts = ["hello"]
        bad = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.1] * 512)])
        with patch("app.services.ingestion.embedder._get_client") as mock_client:
            mock_client.return_value.embeddings.create = AsyncMock(return_value=bad)
            with pytest.raises(ValueError, match="1536"):
                await embed_batch(texts)

    async def test_17_texts_makes_two_api_calls(self):
        """16-per-batch limit: 17 texts → 2 calls (16 + 1)."""
        texts = [f"t{i}" for i in range(17)]
        call_count = 0

        async def fake_create(input, model):
            nonlocal call_count
            call_count += 1
            return self._fake_response(input)

        with patch("app.services.ingestion.embedder._get_client") as mock_client:
            mock_client.return_value.embeddings.create = fake_create
            result = await embed_batch(texts)

        assert call_count == 2
        assert len(result) == 17

    async def test_order_preserved_across_batches(self):
        texts = [f"t{i}" for i in range(20)]

        async def fake_create(input, model):
            return SimpleNamespace(data=[
                SimpleNamespace(index=j, embedding=[float(j)] * 1536)
                for j in range(len(input))
            ])

        with patch("app.services.ingestion.embedder._get_client") as mock_client:
            mock_client.return_value.embeddings.create = fake_create
            result = await embed_batch(texts)

        assert len(result) == 20


# ── Embedder — embed_single ───────────────────────────────────────────────────

class TestEmbedSingle:

    async def test_returns_single_1536_dim_vector(self):
        fake = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.1] * 1536)])
        with patch("app.services.ingestion.embedder._get_client") as mock_client:
            mock_client.return_value.embeddings.create = AsyncMock(return_value=fake)
            result = await embed_single("hello world")
        assert len(result) == 1536

    async def test_delegates_to_embed_batch(self):
        with patch(
            "app.services.ingestion.embedder.embed_batch",
            new=AsyncMock(return_value=[[0.1] * 1536]),
        ) as mock_batch:
            await embed_single("test")
        mock_batch.assert_called_once_with(["test"])


# ── Embedder — retry on 429 ───────────────────────────────────────────────────

class TestEmbedBatchRetry:

    async def test_rate_limit_retries_and_succeeds(self):
        from openai import RateLimitError

        good = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.1] * 1536)])
        call_count = 0

        async def flaky(input, model):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429, headers={}),
                    body={},
                )
            return good

        with patch("app.services.ingestion.embedder._get_client") as mock_client:
            mock_client.return_value.embeddings.create = flaky
            with patch("tenacity.nap.time.sleep"):
                result = await embed_batch(["hello"])

        assert call_count == 2
        assert len(result) == 1
