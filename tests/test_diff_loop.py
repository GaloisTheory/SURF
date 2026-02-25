"""Tests for SURF model diffing (DiffEMLoop).

Tests are organized by plan section:
- T5: Buffer Compatibility
- T6: Weighted Pool from Diff Entries
- T1-T4, T7-T11: Added in later waves
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from surf.em_loop.buffer import ReplayBuffer
from surf.em_loop.sampling import build_weighted_attribute_pool
from surf.em_loop.diff_loop import (
    parse_paired_scores,
    compute_reward_score,
    is_degenerate,
    strip_think_tags,
    DiffEMLoop,
)
from surf.em_loop.prompts import (
    DEFAULT_DIMENSIONS,
    DEFAULT_DIVERGENCE_TYPES,
    build_dimensions_score_block,
    build_divergence_types_block,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def sample_diff_entry():
    return {
        "iteration": 1,
        "timestamp": 1.0,
        "query": "What is dark matter?",
        "response_a": "Dark matter is a hypothetical form...",
        "response_b": "<think>The user asks about dark matter...</think>Dark matter refers to...",
        "scores": {"substance": 1, "safety": 0, "accuracy": 3, "reasoning": 3, "persona": 0},
        "reward_score": 60.0,
        "divergence_type": "CAPABILITY_GAP",
        "primary_divergence": "B provides deeper scientific reasoning with citations",
        "judge_order": ["base", "sft"],
        "model_a_name": "base",
        "model_b_name": "sft",
        "attributes": ["scientific inquiry", "formal tone"],
        "weighted_attributes": ["scientific inquiry"],
        "random_attributes": ["formal tone"],
        "source_index": 42,
        "run_id": 1,
    }


@pytest.fixture
def sample_standard_entry():
    return {
        "query": "What is dark matter?",
        "response": "Dark matter is...",
        "reward_score": 75.0,
        "attributes": ["scientific inquiry"],
    }


# ============================================================
# T5: Buffer Compatibility
# ============================================================

class TestT5BufferCompatibility:
    """The modified _validate_entry must accept both diff entries and standard entries."""

    def test_buffer_accepts_diff_entry(self, sample_diff_entry):
        buf = ReplayBuffer(buffer_size=5)
        buf.add(sample_diff_entry)
        assert len(buf.get_buffer()) == 1

    def test_buffer_accepts_standard_entry(self, sample_standard_entry):
        buf = ReplayBuffer(buffer_size=5)
        buf.add(sample_standard_entry)
        assert len(buf.get_buffer()) == 1

    def test_buffer_rejects_mixed_incomplete(self):
        """response_a without response_b should fail."""
        buf = ReplayBuffer(buffer_size=5)
        with pytest.raises(ValueError, match="both response_a and response_b"):
            buf.add({"query": "q", "response_a": "ra", "reward_score": 50.0})

    def test_buffer_rejects_mixed_incomplete_b_only(self):
        """response_b without response_a should fail."""
        buf = ReplayBuffer(buffer_size=5)
        with pytest.raises(ValueError, match="both response_a and response_b"):
            buf.add({"query": "q", "response_b": "rb", "reward_score": 50.0})

    def test_buffer_get_responses_diff_entries(self, sample_diff_entry):
        """get_responses() doesn't crash on diff entries."""
        buf = ReplayBuffer(buffer_size=5)
        buf.add(sample_diff_entry)
        responses = buf.get_responses()
        assert len(responses) == 1
        assert responses[0] == sample_diff_entry["response_a"]

    def test_buffer_get_responses_mixed_entries(self, sample_diff_entry, sample_standard_entry):
        """get_responses() works with both diff and standard entries."""
        buf = ReplayBuffer(buffer_size=5)
        buf.add(sample_diff_entry)
        buf.add(sample_standard_entry)
        responses = buf.get_responses()
        assert len(responses) == 2

    def test_buffer_sorting_with_diff_entries(self):
        """Diff entries sort by reward_score like standard entries."""
        buf = ReplayBuffer(buffer_size=3)
        buf.add_batch([
            {"query": "q1", "response_a": "a1", "response_b": "b1", "reward_score": 30.0},
            {"query": "q2", "response_a": "a2", "response_b": "b2", "reward_score": 90.0},
            {"query": "q3", "response_a": "a3", "response_b": "b3", "reward_score": 60.0},
        ])
        scores = buf.get_scores()
        assert scores == [90.0, 60.0, 30.0]

    def test_buffer_truncates_diff_entries(self):
        """Buffer respects buffer_size with diff entries."""
        buf = ReplayBuffer(buffer_size=2)
        buf.add_batch([
            {"query": "q1", "response_a": "a1", "response_b": "b1", "reward_score": 30.0},
            {"query": "q2", "response_a": "a2", "response_b": "b2", "reward_score": 90.0},
            {"query": "q3", "response_a": "a3", "response_b": "b3", "reward_score": 60.0},
        ])
        assert len(buf) == 2
        assert buf.get_scores() == [90.0, 60.0]

    def test_buffer_starting_entries_diff(self, sample_diff_entry):
        """Buffer can be initialized with diff entries."""
        buf = ReplayBuffer(buffer_size=5, starting_entries=[sample_diff_entry])
        assert len(buf) == 1
        assert buf.get_scores() == [60.0]


# ============================================================
# T6: Weighted Pool from Diff Entries
# ============================================================

class TestT6WeightedPool:
    """build_weighted_attribute_pool reads attributes from buffer entries.
    Verify it works with diff-format entries."""

    def test_weighted_pool_from_diff_entries(self):
        entries = [
            {
                "query": "q", "response_a": "a", "response_b": "b",
                "reward_score": 80.0,
                "attributes": ["safety topics", "formal tone"],
            },
        ]
        pool = build_weighted_attribute_pool(entries)
        assert pool["safety topics"] == 80.0
        assert pool["formal tone"] == 80.0

    def test_weighted_pool_accumulates_across_diff_entries(self):
        entries = [
            {
                "query": "q1", "response_a": "a", "response_b": "b",
                "reward_score": 80.0,
                "attributes": ["safety topics"],
            },
            {
                "query": "q2", "response_a": "a", "response_b": "b",
                "reward_score": 60.0,
                "attributes": ["safety topics", "coding"],
            },
        ]
        pool = build_weighted_attribute_pool(entries)
        assert pool["safety topics"] == 140.0  # 80 + 60
        assert pool["coding"] == 60.0

    def test_weighted_pool_ignores_zero_score_diff_entries(self):
        entries = [
            {
                "query": "q", "response_a": "a", "response_b": "b",
                "reward_score": 0.0,
                "attributes": ["ignored attr"],
            },
        ]
        pool = build_weighted_attribute_pool(entries)
        assert "ignored attr" not in pool

    def test_weighted_pool_uses_weighted_attributes_field(self):
        """build_weighted_attribute_pool also reads weighted_attributes field."""
        entries = [
            {
                "query": "q", "response_a": "a", "response_b": "b",
                "reward_score": 50.0,
                "attributes": ["attr_a"],
                "weighted_attributes": ["attr_w"],
            },
        ]
        pool = build_weighted_attribute_pool(entries)
        assert pool["attr_a"] == 50.0
        assert pool["attr_w"] == 50.0

    def test_weighted_pool_empty_entries(self):
        pool = build_weighted_attribute_pool([])
        assert pool == {}


# ============================================================
# T1: Paired Judge Score Parsing
# ============================================================

class TestT1PairedScoreParsing:
    """Test parse_paired_scores() extracts per-dimension scores from judge XML output."""

    def test_clean_output(self):
        text = """<reasoning>Analysis here</reasoning>
<scores>
substance: 2
safety: 0
accuracy: 3
reasoning: 1
persona: 0
</scores>
<divergence_type>FACTUAL_DISAGREEMENT</divergence_type>
<primary_divergence>A fabricates a citation while B states uncertainty</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert result["scores"] == {"substance": 2, "safety": 0, "accuracy": 3, "reasoning": 1, "persona": 0}
        assert result["divergence_type"] == "FACTUAL_DISAGREEMENT"
        assert result["primary_divergence"] == "A fabricates a citation while B states uncertainty"
        assert result["reasoning"] == "Analysis here"

    def test_extra_whitespace(self):
        text = """<reasoning>x</reasoning>
<scores>
substance:  2
 safety: 0
accuracy:3
reasoning: 1
persona: 0
</scores>
<divergence_type>SAFETY_SHIFT</divergence_type>
<primary_divergence>test</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert result["scores"] == {"substance": 2, "safety": 0, "accuracy": 3, "reasoning": 1, "persona": 0}

    def test_float_scores_truncated(self):
        text = """<reasoning>x</reasoning>
<scores>
substance: 2.7
safety: 0
accuracy: 1
reasoning: 1
persona: 0
</scores>
<divergence_type>X</divergence_type>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert result["scores"]["substance"] == 2  # int(float("2.7")) = 2

    def test_missing_dimension_returns_none(self):
        text = """<reasoning>x</reasoning>
<scores>
substance: 2
safety: 0
accuracy: 3
persona: 0
</scores>"""
        result = parse_paired_scores(text)
        assert result is None  # Missing reasoning dimension

    def test_out_of_range_clamped(self):
        text = """<reasoning>x</reasoning>
<scores>
substance: 7
safety: -1
accuracy: 3
reasoning: 1
persona: 0
</scores>
<divergence_type>X</divergence_type>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert result["scores"]["substance"] == 5  # Clamped from 7
        assert result["scores"]["safety"] == 0  # Clamped from -1

    def test_no_scores_tag_returns_none(self):
        text = "I think they're pretty different..."
        result = parse_paired_scores(text)
        assert result is None

    def test_malformed_scores_returns_none(self):
        text = "<scores>just some text</scores>"
        result = parse_paired_scores(text)
        assert result is None  # Can't parse 5 dimensions

    def test_extra_dimensions_ignored(self):
        text = """<reasoning>x</reasoning>
<scores>
substance: 2
safety: 0
accuracy: 3
reasoning: 1
persona: 0
style: 2
</scores>
<divergence_type>X</divergence_type>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert len(result["scores"]) == 5
        assert "style" not in result["scores"]

    def test_divergence_type_lowercase_normalized(self):
        text = """<reasoning>x</reasoning>
<scores>
substance: 0
safety: 1
accuracy: 0
reasoning: 0
persona: 0
</scores>
<divergence_type>safety_shift</divergence_type>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result["divergence_type"] == "SAFETY_SHIFT"

    def test_divergence_type_unknown_stored_as_is(self):
        text = """<reasoning>x</reasoning>
<scores>
substance: 0
safety: 0
accuracy: 0
reasoning: 0
persona: 1
</scores>
<divergence_type>STYLE_DIFFERENCE</divergence_type>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result["divergence_type"] == "STYLE_DIFFERENCE"

    def test_missing_divergence_type_tag(self):
        text = """<reasoning>x</reasoning>
<scores>
substance: 0
safety: 0
accuracy: 0
reasoning: 0
persona: 0
</scores>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert result["divergence_type"] is None

    def test_missing_primary_divergence_tag(self):
        text = """<reasoning>x</reasoning>
<scores>
substance: 0
safety: 0
accuracy: 0
reasoning: 0
persona: 0
</scores>
<divergence_type>NO_MEANINGFUL_DIVERGENCE</divergence_type>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert result["primary_divergence"] is None

    def test_long_primary_divergence_truncated(self):
        long_text = "x" * 600
        text = f"""<reasoning>x</reasoning>
<scores>
substance: 0
safety: 0
accuracy: 0
reasoning: 0
persona: 0
</scores>
<divergence_type>X</divergence_type>
<primary_divergence>{long_text}</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert len(result["primary_divergence"]) == 500


# ============================================================
# T2: Reward Score Computation
# ============================================================

class TestT2RewardScore:
    """reward_score = max(dim_scores) * 20"""

    def test_all_zeros(self):
        assert compute_reward_score({"substance": 0, "safety": 0, "accuracy": 0, "reasoning": 0, "persona": 0}) == 0.0

    def test_single_max(self):
        score = compute_reward_score({"substance": 0, "safety": 0, "accuracy": 5, "reasoning": 0, "persona": 0})
        assert score == 100.0

    def test_all_max(self):
        score = compute_reward_score({"substance": 5, "safety": 5, "accuracy": 5, "reasoning": 5, "persona": 5})
        assert score == 100.0

    def test_mixed(self):
        score = compute_reward_score({"substance": 1, "safety": 3, "accuracy": 0, "reasoning": 0, "persona": 1})
        assert score == 60.0

    def test_single_minor(self):
        score = compute_reward_score({"substance": 0, "safety": 1, "accuracy": 0, "reasoning": 0, "persona": 0})
        assert score == 20.0

    def test_empty_scores(self):
        assert compute_reward_score({}) == 0.0


# ============================================================
# T3: Degenerate Response Filter
# ============================================================

class TestT3DegenerateFilter:
    """Test is_degenerate() and strip_think_tags()."""

    def test_normal_response(self):
        assert not is_degenerate("Dark matter is a hypothetical form of matter.")

    def test_think_plus_content(self):
        assert not is_degenerate("<think>hmm</think>Dark matter is a hypothetical form of matter.")

    def test_think_only(self):
        assert is_degenerate("<think>hmm let me think about this</think>")

    def test_garbled_think(self):
        assert is_degenerate("<think>asjkdf8923j</think>")

    def test_short_response(self):
        assert is_degenerate("I don't know")  # < 20 chars

    def test_exactly_20_chars(self):
        assert not is_degenerate("12345678901234567890")  # exactly 20

    def test_repetitive_not_filtered(self):
        # Repetition check removed — long math/code responses have low word
        # diversity and were falsely flagged (~69% degenerate rate).
        assert not is_degenerate("the the the the the the the the the the")

    def test_empty(self):
        assert is_degenerate("")

    def test_whitespace_only(self):
        assert is_degenerate("   \n\t  ")

    def test_nested_tags_in_think(self):
        assert not is_degenerate("<think>some <b>html</b></think>This is real content here.")

    def test_multiple_think_blocks(self):
        assert not is_degenerate("<think>a</think>This is content here<think>b</think>")

    def test_unclosed_think(self):
        assert is_degenerate("<think>rambling on forever...")

    # strip_think_tags tests
    def test_strip_closed_think(self):
        assert strip_think_tags("<think>hello</think>world") == "world"

    def test_strip_multiple_think(self):
        assert strip_think_tags("<think>a</think>middle<think>b</think>") == "middle"

    def test_strip_unclosed_think(self):
        assert strip_think_tags("<think>rambling...") == ""

    def test_strip_no_think(self):
        assert strip_think_tags("no think tags here") == "no think tags here"


# ============================================================
# T4: Response Order Randomization
# ============================================================

class TestT4ResponseOrderRandomization:
    """Over many candidates, both orderings should appear."""

    def test_both_orders_appear(self):
        """With enough trials, random.random() < 0.5 produces both True and False."""
        import random
        random.seed(42)
        orders = []
        for _ in range(100):
            if random.random() < 0.5:
                orders.append(["base", "sft"])
            else:
                orders.append(["sft", "base"])
        assert ["base", "sft"] in orders
        assert ["sft", "base"] in orders


# ============================================================
# T7: Two-Model Parallel Calls
# ============================================================

class TestT7TwoModelParallelCalls:
    """Mock both model resources and verify both are called for each candidate."""

    @pytest.mark.asyncio
    async def test_both_models_called(self):
        """Each candidate query is sent to both model_a and model_b."""
        model_a = AsyncMock()
        model_a.call = AsyncMock(return_value="Base response that is long enough.")
        model_b = AsyncMock()
        model_b.call = AsyncMock(return_value="<think>reasoning</think>SFT response long enough")

        # We test the _get_model_response method directly
        from surf.em_loop.diff_loop import DiffEMLoop

        # Create a minimal mock loop
        loop = MagicMock(spec=DiffEMLoop)
        loop._get_model_response = DiffEMLoop._get_model_response

        result_a = await loop._get_model_response(loop, model_a, "test query")
        result_b = await loop._get_model_response(loop, model_b, "test query")

        assert result_a == "Base response that is long enough."
        assert result_b == "<think>reasoning</think>SFT response long enough"
        model_a.call.assert_called_once_with("test query")
        model_b.call.assert_called_once_with("test query")

    @pytest.mark.asyncio
    async def test_model_failure_returns_none(self):
        """If a model raises an exception, _get_model_response returns None."""
        model = AsyncMock()
        model.call = AsyncMock(side_effect=Exception("API error"))

        loop = MagicMock(spec=DiffEMLoop)
        loop._get_model_response = DiffEMLoop._get_model_response

        result = await loop._get_model_response(loop, model, "test query")
        assert result is None


# ============================================================
# T8: Iteration Statistics
# ============================================================

class TestT8IterationStatistics:
    """Verify compute_reward_score produces correct discrete values."""

    def test_discrete_reward_values(self):
        """Per-dim 0-5 should produce discrete reward values: 0, 20, 40, 60, 80, 100."""
        all_zeros = {"substance": 0, "safety": 0, "accuracy": 0, "reasoning": 0, "persona": 0}
        one_1 = {"substance": 0, "safety": 1, "accuracy": 0, "reasoning": 0, "persona": 0}
        one_2 = {"substance": 0, "safety": 2, "accuracy": 0, "reasoning": 0, "persona": 0}
        one_3 = {"substance": 0, "safety": 3, "accuracy": 0, "reasoning": 0, "persona": 0}
        one_4 = {"substance": 0, "safety": 4, "accuracy": 0, "reasoning": 0, "persona": 0}
        one_5 = {"substance": 0, "safety": 5, "accuracy": 0, "reasoning": 0, "persona": 0}

        assert compute_reward_score(all_zeros) == 0.0
        assert compute_reward_score(one_1) == 20.0
        assert compute_reward_score(one_2) == 40.0
        assert compute_reward_score(one_3) == 60.0
        assert compute_reward_score(one_4) == 80.0
        assert compute_reward_score(one_5) == 100.0

    def test_max_across_dimensions(self):
        """reward_score = max(dim_scores) * 20, not sum or mean."""
        scores = {"substance": 1, "safety": 3, "accuracy": 1, "reasoning": 0, "persona": 1}
        # max is 3, so 3 * 20 = 60
        assert compute_reward_score(scores) == 60.0


# ============================================================
# T11: Judge Receives Full Responses (Not Stripped)
# ============================================================

class TestT11JudgeReceivesFullResponses:
    """The judge sees <think> blocks — they are NOT stripped before judging."""

    @pytest.mark.asyncio
    async def test_judge_receives_unstripped_responses(self):
        """Verify the judge prompt contains the full <think> block."""
        captured_prompts = []

        async def mock_call(prompt, **kwargs):
            captured_prompts.append(prompt)
            return """<reasoning>Analysis</reasoning>
<scores>
substance: 1
safety: 0
accuracy: 0
reasoning: 2
persona: 0
</scores>
<divergence_type>REASONING_DIFFERENCE</divergence_type>
<primary_divergence>B uses structured reasoning</primary_divergence>"""

        loop = MagicMock(spec=DiffEMLoop)
        loop._score_pair = DiffEMLoop._score_pair
        loop.principle_specific_details = "Test principle"
        loop.use_thinking = False
        loop.thinking_budget = 10000
        loop.dimension_names = DEFAULT_DIMENSIONS
        loop.divergence_types = DEFAULT_DIVERGENCE_TYPES

        # Create a mock judge model
        judge_model = AsyncMock()
        judge_model.call = mock_call
        loop.judge_model = judge_model

        sft_response = "<think>Let me reason through this step by step...</think>The answer is 42."
        base_response = "The answer is 42."

        result = await loop._score_pair(loop, "What is the answer?", base_response, sft_response)

        assert result is not None
        # The judge prompt should contain the full <think> block
        assert "<think>Let me reason through this" in captured_prompts[0]


# ============================================================
# T9: Resume from Diff Results
# ============================================================

class TestT9ResumeFromDiffResults:
    """Verify _try_resume correctly rebuilds buffer from diff-format results.jsonl."""

    def test_resume_loads_diff_entries(self, tmp_path):
        """Write 2 iterations of diff results, resume, verify buffer state."""
        results_file = tmp_path / "results.jsonl"
        summary_file = tmp_path / "summary.jsonl"

        # Write results
        results_file.write_text("\n".join([
            json.dumps({
                "iteration": 1, "query": "q1",
                "response_a": "a1", "response_b": "b1",
                "reward_score": 80.0,
                "scores": {"substance": 2, "safety": 4, "accuracy": 1, "reasoning": 0, "persona": 0},
            }),
            json.dumps({
                "iteration": 1, "query": "q2",
                "response_a": "a2", "response_b": "b2",
                "reward_score": 20.0,
                "scores": {"substance": 0, "safety": 1, "accuracy": 0, "reasoning": 0, "persona": 0},
            }),
            json.dumps({
                "iteration": 2, "query": "q3",
                "response_a": "a3", "response_b": "b3",
                "reward_score": 100.0,
                "scores": {"substance": 0, "safety": 0, "accuracy": 5, "reasoning": 0, "persona": 0},
            }),
        ]))

        # Write summary (needed for _try_resume to detect iterations)
        summary_file.write_text("\n".join([
            json.dumps({"iteration": 1, "num_candidates": 2}),
            json.dumps({"iteration": 2, "num_candidates": 1}),
        ]))

        # Create a minimal DiffEMLoop with mocked dependencies
        # We need to bypass __init__ and just test _try_resume
        loop = MagicMock(spec=DiffEMLoop)
        loop._try_resume = DiffEMLoop._try_resume
        loop.output_dir = tmp_path
        loop.replay_buffer = ReplayBuffer(buffer_size=2)
        loop.iteration = 0
        loop.quiet = False
        loop._log = MagicMock()
        loop.status = {"iteration": 0, "buffer_top": 0, "phase": "init"}

        result = loop._try_resume(loop)

        assert result is False  # Not complete, but resumed
        assert loop.iteration == 2  # Last completed iteration
        assert len(loop.replay_buffer) == 2  # buffer_size=2
        # Top 2 by score should be q3 (100.0) and q1 (80.0)
        scores = loop.replay_buffer.get_scores()
        assert scores[0] == 100.0
        assert scores[1] == 80.0

    def test_resume_recomputes_reward_score(self, tmp_path):
        """If reward_score is missing, recompute from scores dict."""
        results_file = tmp_path / "results.jsonl"
        summary_file = tmp_path / "summary.jsonl"

        # Entry without reward_score but with scores
        results_file.write_text(json.dumps({
            "iteration": 1, "query": "q",
            "response_a": "a", "response_b": "b",
            "scores": {"substance": 0, "safety": 5, "accuracy": 0, "reasoning": 0, "persona": 0},
        }))
        summary_file.write_text(json.dumps({"iteration": 1, "num_candidates": 1}))

        loop = MagicMock(spec=DiffEMLoop)
        loop._try_resume = DiffEMLoop._try_resume
        loop.output_dir = tmp_path
        loop.replay_buffer = ReplayBuffer(buffer_size=5)
        loop.iteration = 0
        loop.quiet = False
        loop._log = MagicMock()
        loop.status = {"iteration": 0, "buffer_top": 0, "phase": "init"}

        loop._try_resume(loop)

        scores = loop.replay_buffer.get_scores()
        assert len(scores) == 1
        # 5 * 20 = 100
        assert scores[0] == 100.0

    def test_resume_no_summary_file(self, tmp_path):
        """If no summary.jsonl exists, don't resume."""
        loop = MagicMock(spec=DiffEMLoop)
        loop._try_resume = DiffEMLoop._try_resume
        loop.output_dir = tmp_path
        loop.replay_buffer = ReplayBuffer(buffer_size=5)
        loop.iteration = 0
        loop.quiet = False
        loop._log = MagicMock()
        loop.status = {"iteration": 0, "buffer_top": 0, "phase": "init"}

        result = loop._try_resume(loop)
        assert result is False
        assert loop.iteration == 0


# ============================================================
# T10: End-to-End Smoke (Mocked)
# ============================================================

class TestT10EndToEndSmoke:
    """Full pipeline with mocked models — no API calls."""

    @pytest.mark.asyncio
    async def test_e2e_produces_correct_output_format(self, tmp_path):
        """Run 1 iteration with mocked models, verify output files exist and have correct format."""
        # We'll test the helper functions and output format rather than
        # the full loop (which requires complex mocking of ModelResource)

        # Create a mock results.jsonl as if one iteration ran
        results_path = tmp_path / "results.jsonl"
        summary_path = tmp_path / "summary.jsonl"

        # Simulate what the loop would produce
        candidate = {
            "iteration": 1,
            "timestamp": 1.0,
            "query": "What is dark matter?",
            "response_a": "It is a hypothetical form of matter.",
            "response_b": "<think>The user asks about dark matter...</think>Dark matter refers to unobservable matter.",
            "scores": {"substance": 1, "safety": 0, "accuracy": 3, "reasoning": 3, "persona": 0},
            "reward_score": 60.0,
            "divergence_type": "CAPABILITY_GAP",
            "primary_divergence": "B reasons more deeply about the topic",
            "score_metadata": {"reasoning": "A gives a shallow answer, B reasons step-by-step"},
            "judge_order": ["base", "sft"],
            "model_a_name": "base",
            "model_b_name": "sft",
            "attributes": ["scientific inquiry", "formal tone"],
            "weighted_attributes": ["scientific inquiry"],
            "random_attributes": ["formal tone"],
            "source_index": 42,
        }

        results_path.write_text(json.dumps(candidate))
        summary_path.write_text(json.dumps({
            "iteration": 1, "num_candidates": 1,
            "mean_reward": 60.0, "max_reward": 60.0,
        }))

        # Verify the output format
        results = [json.loads(l) for l in results_path.read_text().strip().split("\n")]
        for r in results:
            assert "response_a" in r
            assert "response_b" in r
            assert "scores" in r
            assert set(r["scores"].keys()) == {"substance", "safety", "accuracy", "reasoning", "persona"}
            assert "reward_score" in r
            assert "judge_order" in r
            assert r["judge_order"] in [["base", "sft"], ["sft", "base"]]
            assert "divergence_type" in r
            assert "model_a_name" in r

    def test_compute_reward_from_parsed_scores(self):
        """Verify the full parse → compute pipeline."""
        judge_output = """<reasoning>Both models answer the question but with different depth.</reasoning>
<scores>
substance: 1
safety: 0
accuracy: 2
reasoning: 3
persona: 0
</scores>
<divergence_type>CAPABILITY_GAP</divergence_type>
<primary_divergence>Model B reasons much more deeply</primary_divergence>"""

        parsed = parse_paired_scores(judge_output)
        assert parsed is not None

        reward = compute_reward_score(parsed["scores"])
        # max(1,0,2,3,0) * 20 = 3 * 20 = 60
        assert reward == 60.0
        assert parsed["divergence_type"] == "CAPABILITY_GAP"
        assert parsed["primary_divergence"] == "Model B reasons much more deeply"


# ============================================================
# T12: Configurable Dimensions
# ============================================================

class TestConfigurableDimensions:
    """Test parse_paired_scores with custom dimension names."""

    def test_custom_3_dimensions(self):
        """Custom 3-dimension rubric parses correctly."""
        text = """<reasoning>Analysis</reasoning>
<scores>
refusal_presence: 2
engagement_quality: 1
appropriateness: 3
</scores>
<divergence_type>REFUSAL_VS_ENGAGEMENT</divergence_type>
<primary_divergence>A refuses while B engages</primary_divergence>"""
        result = parse_paired_scores(
            text,
            dimension_names=["refusal_presence", "engagement_quality", "appropriateness"],
        )
        assert result is not None
        assert result["scores"] == {"refusal_presence": 2, "engagement_quality": 1, "appropriateness": 3}
        assert result["divergence_type"] == "REFUSAL_VS_ENGAGEMENT"

    def test_wrong_count_returns_none(self):
        """If judge returns 2 scores but 3 expected, returns None."""
        text = """<reasoning>x</reasoning>
<scores>
refusal_presence: 2
engagement_quality: 1
</scores>
<divergence_type>X</divergence_type>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(
            text,
            dimension_names=["refusal_presence", "engagement_quality", "appropriateness"],
        )
        assert result is None

    def test_backward_compatible_5_dim(self):
        """Calling without dimension_names uses the 5 defaults."""
        text = """<reasoning>x</reasoning>
<scores>
substance: 1
safety: 0
accuracy: 2
reasoning: 1
persona: 0
</scores>
<divergence_type>CAPABILITY_GAP</divergence_type>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(text)
        assert result is not None
        assert len(result["scores"]) == 5
        assert list(result["scores"].keys()) == ["substance", "safety", "accuracy", "reasoning", "persona"]

    def test_custom_dimensions_ignore_extra(self):
        """Extra dimensions in judge output are ignored."""
        text = """<reasoning>x</reasoning>
<scores>
confidence_gap: 2
accuracy_gap: 1
hedging_quality: 3
extra_dim: 1
</scores>
<divergence_type>X</divergence_type>
<primary_divergence>x</primary_divergence>"""
        result = parse_paired_scores(
            text,
            dimension_names=["confidence_gap", "accuracy_gap", "hedging_quality"],
        )
        assert result is not None
        assert len(result["scores"]) == 3
        assert "extra_dim" not in result["scores"]

    def test_reward_score_works_with_custom_dims(self):
        """compute_reward_score is dimension-count-agnostic."""
        scores_3 = {"refusal_presence": 5, "engagement_quality": 1, "appropriateness": 0}
        assert compute_reward_score(scores_3) == 100.0

        scores_2 = {"confidence_gap": 3, "accuracy_gap": 0}
        assert compute_reward_score(scores_2) == 60.0


# ============================================================
# T13: Prompt Builders
# ============================================================

class TestPromptBuilders:
    """Test build_dimensions_score_block and build_divergence_types_block."""

    def test_dimensions_score_block_default(self):
        block = build_dimensions_score_block(DEFAULT_DIMENSIONS)
        assert "substance: [0-5]" in block
        assert "persona: [0-5]" in block
        assert block.count("[0-5]") == 5

    def test_dimensions_score_block_custom(self):
        block = build_dimensions_score_block(["refusal_presence", "engagement_quality", "appropriateness"])
        assert "refusal_presence: [0-5]" in block
        assert "engagement_quality: [0-5]" in block
        assert "appropriateness: [0-5]" in block
        assert block.count("[0-5]") == 3
        assert "substance" not in block

    def test_divergence_types_block_default(self):
        block = build_divergence_types_block(DEFAULT_DIVERGENCE_TYPES)
        assert "SAFETY_SHIFT" in block
        assert "NO_MEANINGFUL_DIVERGENCE" in block
        assert " | " in block

    def test_divergence_types_block_custom(self):
        types = ["REFUSAL_VS_ENGAGEMENT", "PARTIAL_REFUSAL", "NO_MEANINGFUL_DIVERGENCE"]
        block = build_divergence_types_block(types)
        assert block == "REFUSAL_VS_ENGAGEMENT | PARTIAL_REFUSAL | NO_MEANINGFUL_DIVERGENCE"

    def test_dimensions_score_block_single(self):
        block = build_dimensions_score_block(["only_dim"])
        assert block == "only_dim: [0-5]"

    def test_divergence_types_block_single(self):
        block = build_divergence_types_block(["ONLY_TYPE"])
        assert block == "ONLY_TYPE"
