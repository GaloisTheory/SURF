"""Differential EM loop for comparing two models."""

from __future__ import annotations

import asyncio
import json
import random
import re
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from surf.core.models import ModelResource
from surf.core.streaming import JSONStreamer
from surf.core.utils import parse_xml_tags_optional, render_jinja, tqdm_gather
from surf.em_loop.buffer import ReplayBuffer
from surf.em_loop.judge import load_rubric, get_principle_from_rubric
from surf.em_loop.prompts import (
    PAIRED_SCORE_PROMPT,
    QUERY_GEN_PROMPT,
    QUERY_GEN_STOP_TOKEN,
    TOPIC_GUIDED_QUERY_GEN_PROMPT,
    DEFAULT_DIMENSIONS,
    DEFAULT_DIVERGENCE_TYPES,
    build_dimensions_score_block,
    build_divergence_types_block,
)
from surf.em_loop.sampling import (
    AttributeFileLoader,
    build_weighted_attribute_pool,
    sample_attributes_for_candidate,
)


# ============================================================
# Helper functions (module-level, importable for testing)
# ============================================================

def strip_think_tags(text: str) -> str:
    """Strip <think>...</think> blocks from text. Handles multiple blocks and unclosed tags."""
    # Strip closed think blocks
    result = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Strip unclosed think tag (takes everything after <think>)
    result = re.sub(r"<think>.*$", "", result, flags=re.DOTALL)
    return result.strip()


def is_degenerate(response: str) -> bool:
    """
    Check if a response is degenerate (after stripping think tags).

    Degenerate = empty or too short. The ORIGINAL response (with think tags)
    is what goes to the judge.

    Note: repetition check (unique_ratio < 0.2) was removed because long
    math/code responses naturally have low word diversity and were being
    falsely flagged (~69% degenerate rate in smoke_v3/v4).
    """
    stripped = strip_think_tags(response)

    # Empty or whitespace
    if not stripped or not stripped.strip():
        return True

    stripped = stripped.strip()

    # Too short
    if len(stripped) < 20:
        return True

    return False


def compute_reward_score(scores: Dict[str, int]) -> float:
    """Compute reward_score = max(dim_scores) * 20"""
    if not scores:
        return 0.0
    return max(scores.values()) * 20


def parse_paired_scores(text: str, dimension_names: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """
    Parse the judge's paired scoring output.

    Args:
        text: Raw judge response text
        dimension_names: List of dimension names to parse. Defaults to the 5 original dimensions.

    Returns dict with:
        scores: {dim_name: int, ...} for each dimension
        divergence_type: str or None
        primary_divergence: str or None
        reasoning: str or None

    Returns None if parsing fails (missing <scores> tag or unparseable).
    """
    if dimension_names is None:
        dimension_names = DEFAULT_DIMENSIONS

    # Parse reasoning
    reasoning = parse_xml_tags_optional(text, "reasoning")

    # Parse scores block
    scores_block = parse_xml_tags_optional(text, "scores")
    if not scores_block:
        return None

    # Parse individual dimension scores
    scores = {}
    for line in scores_block.strip().split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in dimension_names:
            try:
                score = int(float(value.strip()))  # Handle floats by truncating
                score = max(0, min(5, score))  # Clamp to 0-5
                scores[key] = score
            except (ValueError, TypeError):
                continue

    # Must have all expected dimensions
    if len(scores) != len(dimension_names):
        return None

    # Parse divergence_type
    divergence_type = parse_xml_tags_optional(text, "divergence_type")
    if divergence_type:
        divergence_type = divergence_type.strip().upper()

    # Parse primary_divergence
    primary_divergence = parse_xml_tags_optional(text, "primary_divergence")
    if primary_divergence:
        primary_divergence = primary_divergence.strip()
        if len(primary_divergence) > 500:
            primary_divergence = primary_divergence[:500]

    return {
        "scores": scores,
        "divergence_type": divergence_type,
        "primary_divergence": primary_divergence,
        "reasoning": reasoning,
    }


class DiffEMLoop:
    """
    Differential EM loop for comparing two models.

    Generates queries from attributes, gets responses from both models,
    scores behavioral divergence with a paired judge, and updates the
    replay buffer to converge on maximally divergent query regions.
    """

    def __init__(
        self,
        rubric_path: str,
        attributes: str,
        model_a: str,
        model_b: str,
        model_a_name: str = "a",
        model_b_name: str = "b",
        judge_model: str = "anthropic:claude-opus-4-5-20251101",
        query_model: str = "openrouter:meta-llama/llama-3.1-70b-instruct",
        buffer_size: int = 5,
        candidates_per_iter: int = 80,
        output_dir: str = "diff_output",
        model_a_max_tokens: int = 4096,
        model_b_max_tokens: int = 12288,
        model_a_temperature: float = 0.7,
        model_b_temperature: float = 0.7,
        target_concurrency: int = 50,
        query_concurrency: int = 50,
        judge_concurrency: int = 20,
        use_thinking: bool = True,
        thinking_budget: int = 10000,
        early_stop_threshold: float = 10.0,
    ):
        # Load rubric
        self.rubric = load_rubric(rubric_path)
        self.principle_specific_details = get_principle_from_rubric(self.rubric)
        self.topic_guidance = self.rubric.get("topic_guidance")

        # Configurable dimensions and divergence types (fall back to defaults)
        self.dimension_names = self.rubric.get("dimensions", DEFAULT_DIMENSIONS)
        self.divergence_types = self.rubric.get("divergence_types", DEFAULT_DIVERGENCE_TYPES)

        if not self.principle_specific_details:
            raise ValueError(f"No principle_specific_details found in {rubric_path}")

        # Store config
        self.attributes_source = attributes
        self.buffer_size = buffer_size
        self.candidates_per_iter = candidates_per_iter
        self.output_dir = Path(output_dir)
        self.model_a_name = model_a_name
        self.model_b_name = model_b_name
        self.early_stop_threshold = early_stop_threshold

        # Initialize attribute loader
        self.attribute_loader = AttributeFileLoader(attributes)

        # Initialize replay buffer
        self.replay_buffer = ReplayBuffer(buffer_size=buffer_size)

        # Initialize TWO target models
        self.model_a = ModelResource.from_string(
            model_a,
            max_concurrency=target_concurrency,
            max_tokens=model_a_max_tokens,
            temperature=model_a_temperature,
        )
        self.model_b = ModelResource.from_string(
            model_b,
            max_concurrency=target_concurrency,
            max_tokens=model_b_max_tokens,
            temperature=model_b_temperature,
        )

        # Initialize judge model
        self.judge_model = ModelResource.from_string(
            judge_model,
            max_concurrency=judge_concurrency,
            max_tokens=16384 if use_thinking else 4096,
            temperature=1.0,
        )
        self.use_thinking = use_thinking
        self.thinking_budget = thinking_budget

        # Initialize query generation model
        self.query_model = ModelResource.from_string(
            query_model,
            max_concurrency=query_concurrency,
            max_tokens=512,
            temperature=1.0,
            stop=[QUERY_GEN_STOP_TOKEN],
        )

        # Results streamer
        self.streamer = JSONStreamer(str(self.output_dir))

        # Failures log
        self.failures_path = self.output_dir / "failures.jsonl"
        self._failures_file = None

        # State
        self.iteration = 0
        self.run_id = None
        self.quiet = False
        self._query_gen_failures = 0

        # Progress tracking
        self.status = {
            "iteration": 0,
            "phase": "init",
            "top_score": 0.0,
            "buffer_top": 0.0,
            "progress": "",
        }

    def _log(self, msg: str):
        if not self.quiet:
            print(msg)

    def _log_failure(self, failure_type: str, response: str, attributes: List[str]):
        if self._failures_file is None:
            self._failures_file = open(self.failures_path, "a")
        record = {
            "iteration": self.iteration,
            "type": failure_type,
            "response_preview": response[:500] if response else None,
            "attributes": attributes[:3] if attributes else [],
        }
        self._failures_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._failures_file.flush()

    def _try_resume(self) -> bool:
        """Try to resume from existing state. Returns True if already complete."""
        summary_path = self.output_dir / "summary.jsonl"
        results_path = self.output_dir / "results.jsonl"

        if not summary_path.exists():
            return False

        last_iteration = 0
        with open(summary_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if "iteration" in entry and entry.get("type") != "final_summary":
                        last_iteration = max(last_iteration, entry["iteration"])
                except json.JSONDecodeError:
                    continue

        if last_iteration == 0:
            return False

        self._log(f"Resuming from iteration {last_iteration}...")

        if results_path.exists():
            buffer_candidates = []
            with open(results_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if "reward_score" in entry:
                            buffer_candidates.append(entry)
                        elif "scores" in entry:
                            # Recompute reward_score from dimension scores
                            entry["reward_score"] = compute_reward_score(entry["scores"])
                            buffer_candidates.append(entry)
                    except json.JSONDecodeError:
                        continue

            if buffer_candidates:
                buffer_candidates.sort(key=lambda x: x.get("reward_score", 0), reverse=True)
                self.replay_buffer.add_batch(buffer_candidates)
                self._log(f"  Restored replay buffer with {len(self.replay_buffer)} entries")

        self.iteration = last_iteration
        buffer_scores = self.replay_buffer.get_scores()
        self.status["iteration"] = last_iteration
        self.status["buffer_top"] = buffer_scores[0] if buffer_scores else 0.0
        self.status["phase"] = "resumed"
        self._log(f"  Will continue from iteration {last_iteration + 1}")
        return False

    async def _generate_query(self, attributes: List[str]) -> Optional[str]:
        """Generate a query from attributes. Same as EMLoop."""
        if not attributes:
            return None

        attributes_text = "\n".join(f"- {attr}" for attr in attributes)

        if self.topic_guidance:
            prompt = render_jinja(
                TOPIC_GUIDED_QUERY_GEN_PROMPT,
                attributes_text=attributes_text,
                topic_guidance=self.topic_guidance,
            )
        else:
            prompt = render_jinja(
                QUERY_GEN_PROMPT,
                attributes_text=attributes_text,
            )

        try:
            response = await self.query_model.call(prompt)
            if not response:
                self._log_failure("empty_response", "", attributes)
                return None

            for tag in ["query_1", "query", "Query_1", "Query"]:
                query = parse_xml_tags_optional(response, tag)
                if query:
                    return query.strip()

            if "<query_1>" in response:
                start = response.find("<query_1>") + len("<query_1>")
                end = response.find("</query_1>", start)
                if end == -1:
                    extracted = response[start:].strip()
                    if extracted:
                        return extracted
                else:
                    return response[start:end].strip()

            if "<query>" in response:
                start = response.find("<query>") + len("<query>")
                end = response.find("</query>", start)
                if end == -1:
                    extracted = response[start:].strip()
                    if extracted:
                        return extracted

            if "<query_1" in response and "<query_1>" not in response:
                start = response.find("<query_1") + len("<query_1")
                extracted = response[start:].strip()
                if extracted:
                    return extracted

            self._query_gen_failures += 1
            self._log_failure("parse_failed", response, attributes)
            return None

        except Exception as e:
            self._query_gen_failures += 1
            self._log_failure(f"exception: {e}", "", attributes)
            return None

    async def _get_model_response(self, model: ModelResource, query: str) -> Optional[str]:
        """Get response from a model."""
        try:
            return await model.call(query)
        except Exception as e:
            self._log(f"  Warning: {model.model} error: {e}")
            return None

    async def _score_pair(
        self,
        query: str,
        response_a: str,
        response_b: str,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """
        Score a pair of responses with the paired judge.

        Returns (parsed_result, metadata) or None on failure.
        """
        prompt = render_jinja(
            PAIRED_SCORE_PROMPT,
            principle_specific_details=self.principle_specific_details,
            query=query,
            response_a=response_a,
            response_b=response_b,
            dimensions_score_block=build_dimensions_score_block(self.dimension_names),
            divergence_types_block=build_divergence_types_block(self.divergence_types),
        )

        try:
            if self.use_thinking:
                text, thinking = await self.judge_model.call_with_thinking(
                    prompt,
                    thinking_budget=self.thinking_budget,
                )
            else:
                text = await self.judge_model.call(prompt)
                thinking = None

            parsed = parse_paired_scores(text, dimension_names=self.dimension_names)
            if parsed is None:
                return None

            metadata = {"raw_response": text}
            if thinking:
                metadata["thinking"] = thinking

            return parsed, metadata

        except Exception as e:
            return None

    async def run_single_iteration(self) -> Dict[str, Any]:
        """Run a single iteration of the diff EM loop."""
        self.iteration += 1
        self.status["iteration"] = self.iteration
        self.status["phase"] = "sampling"

        run_prefix = f"[Run {self.run_id}] " if self.run_id else ""
        self._log(f"\n{'='*60}")
        self._log(f"{run_prefix}Diff Iteration {self.iteration}")
        self._log(f"{'='*60}")

        # Build weighted attribute pool
        weighted_pool = build_weighted_attribute_pool(self.replay_buffer.get_buffer())
        self._log(f"Weighted pool size: {len(weighted_pool)}")

        # Sample attributes
        self._log(f"Sampling attributes for {self.candidates_per_iter} candidates...")
        candidate_attrs = [
            sample_attributes_for_candidate(
                weighted_pool=weighted_pool,
                attribute_loader=self.attribute_loader,
                max_attributes=5,
            )
            for _ in range(self.candidates_per_iter)
        ]

        # Generate queries
        self.status["phase"] = "query_gen"
        self._log("Generating queries...")
        self._query_gen_failures = 0
        query_tasks = [self._generate_query(c["attributes"]) for c in candidate_attrs]

        def update_query_progress(completed, total):
            self.status["progress"] = f"{completed}/{total}"

        queries = await tqdm_gather(
            query_tasks, desc="Query generation", disable=self.quiet,
            on_progress=update_query_progress,
        )

        # Filter valid queries
        valid_candidates = []
        for query, attrs in zip(queries, candidate_attrs):
            if isinstance(query, Exception) or query is None or not query.strip():
                continue
            valid_candidates.append({"query": query, **attrs})

        self._log(f"Valid queries: {len(valid_candidates)}/{self.candidates_per_iter}")

        if not valid_candidates:
            return self._empty_stats()

        # Get responses from BOTH models in parallel
        self.status["phase"] = "target"
        self._log("Getting responses from both models...")

        async def get_pair(candidate):
            query = candidate["query"]
            resp_a, resp_b = await asyncio.gather(
                self._get_model_response(self.model_a, query),
                self._get_model_response(self.model_b, query),
            )
            return resp_a, resp_b

        pair_tasks = [get_pair(c) for c in valid_candidates]

        def update_target_progress(completed, total):
            self.status["progress"] = f"{completed}/{total}"

        pairs = await tqdm_gather(
            pair_tasks, desc="Model responses", disable=self.quiet,
            on_progress=update_target_progress,
        )

        # Filter: both models must respond, neither degenerate
        scorable = []
        degenerate_count = 0
        for candidate, pair in zip(valid_candidates, pairs):
            if isinstance(pair, Exception):
                continue
            resp_a, resp_b = pair
            if resp_a is None or resp_b is None:
                continue
            a_degen = is_degenerate(resp_a)
            b_degen = is_degenerate(resp_b)
            if a_degen or b_degen:
                degenerate_count += 1
                # Log which model(s) were degenerate with full responses
                which = []
                if a_degen:
                    which.append(self.model_a_name)
                if b_degen:
                    which.append(self.model_b_name)
                self._log_failure(
                    f"degenerate:{'+'.join(which)}",
                    "",
                    candidate.get("attributes", []),
                )
                # Write detailed degenerate entry to failures file
                if self._failures_file is None:
                    self._failures_file = open(self.failures_path, "a")
                degen_record = {
                    "iteration": self.iteration,
                    "type": "degenerate_detail",
                    "degenerate_models": which,
                    "query": candidate.get("query", "")[:300],
                    "response_a_raw_len": len(resp_a),
                    "response_a_stripped_len": len(strip_think_tags(resp_a).strip()),
                    "response_a_preview": resp_a[:500],
                    "response_b_raw_len": len(resp_b),
                    "response_b_stripped_len": len(strip_think_tags(resp_b).strip()),
                    "response_b_preview": resp_b[:500],
                }
                self._failures_file.write(json.dumps(degen_record, ensure_ascii=False) + "\n")
                self._failures_file.flush()
                continue
            candidate["response_a_raw"] = resp_a
            candidate["response_b_raw"] = resp_b
            scorable.append(candidate)

        self._log(f"Scorable pairs: {len(scorable)} (filtered {degenerate_count} degenerate)")

        if not scorable:
            return self._empty_stats()

        # Randomize response order for judge and score
        self.status["phase"] = "scoring"
        self._log("Scoring with paired judge...")

        async def score_candidate(candidate):
            resp_a = candidate["response_a_raw"]
            resp_b = candidate["response_b_raw"]

            # Randomize order
            if random.random() < 0.5:
                judge_order = [self.model_a_name, self.model_b_name]
                judge_resp_a, judge_resp_b = resp_a, resp_b
            else:
                judge_order = [self.model_b_name, self.model_a_name]
                judge_resp_a, judge_resp_b = resp_b, resp_a

            result = await self._score_pair(
                candidate["query"], judge_resp_a, judge_resp_b,
            )
            if result is None:
                return None

            parsed, metadata = result
            return {
                "parsed": parsed,
                "metadata": metadata,
                "judge_order": judge_order,
            }

        score_tasks = [score_candidate(c) for c in scorable]

        def update_scoring_progress(completed, total):
            self.status["progress"] = f"{completed}/{total}"

        score_results = await tqdm_gather(
            score_tasks, desc="Scoring", disable=self.quiet,
            on_progress=update_scoring_progress,
        )

        # Assemble final results (filter None = judge errors)
        scored_candidates = []
        judge_errors = 0
        for candidate, score_result in zip(scorable, score_results):
            if isinstance(score_result, Exception) or score_result is None:
                judge_errors += 1
                continue

            parsed = score_result["parsed"]
            reward = compute_reward_score(parsed["scores"])

            entry = {
                "query": candidate["query"],
                "response_a": candidate["response_a_raw"],
                "response_b": candidate["response_b_raw"],
                "scores": parsed["scores"],
                "reward_score": reward,
                "divergence_type": parsed["divergence_type"],
                "primary_divergence": parsed["primary_divergence"],
                "score_metadata": {
                    "reasoning": parsed["reasoning"],
                    **({"thinking": score_result["metadata"].get("thinking")} if score_result["metadata"].get("thinking") else {}),
                },
                "judge_order": score_result["judge_order"],
                "model_a_name": self.model_a_name,
                "model_b_name": self.model_b_name,
                "attributes": candidate.get("attributes", []),
                "weighted_attributes": candidate.get("weighted_attributes", []),
                "random_attributes": candidate.get("random_attributes", []),
                "source_index": candidate.get("source_index"),
            }
            scored_candidates.append(entry)

        if judge_errors > 0:
            self._log(f"  Judge errors: {judge_errors}/{len(scorable)}")

        self._log(f"Successfully scored: {len(scored_candidates)}")

        # Sort by reward score
        scored_candidates.sort(key=lambda x: x["reward_score"], reverse=True)

        if scored_candidates:
            top_scores = [round(c["reward_score"], 1) for c in scored_candidates[:5]]
            self._log(f"Top 5 scores: {top_scores}")

        # Update replay buffer
        self.replay_buffer.add_batch(scored_candidates)

        buffer_scores = self.replay_buffer.get_scores()
        self.status["buffer_top"] = buffer_scores[0] if buffer_scores else 0
        self.status["phase"] = "done"

        # Compute iteration stats
        mean_reward = sum(c["reward_score"] for c in scored_candidates) / len(scored_candidates) if scored_candidates else 0

        # Divergence type distribution
        type_counts = {}
        for c in scored_candidates:
            dt = c.get("divergence_type", "UNKNOWN")
            type_counts[dt] = type_counts.get(dt, 0) + 1

        stats = {
            "num_candidates": len(scored_candidates),
            "mean_reward": round(mean_reward, 2),
            "max_reward": scored_candidates[0]["reward_score"] if scored_candidates else 0,
            "degenerate_filtered": degenerate_count,
            "judge_errors": judge_errors,
            "divergence_types": type_counts,
            "dimensions": self.dimension_names,
        }

        # Stream results
        await self.streamer.stream_iteration(
            iteration=self.iteration,
            candidates=scored_candidates,
            buffer_state=self.replay_buffer.get_buffer(),
            stats=stats,
            run_id=self.run_id,
        )

        self._log(f"\nBuffer state: {self.replay_buffer}")

        return {
            "iteration": self.iteration,
            "candidates": self.candidates_per_iter,
            "valid_queries": len(valid_candidates),
            "scored": len(scored_candidates),
            "mean_reward": mean_reward,
            "top_score": scored_candidates[0]["reward_score"] if scored_candidates else 0,
            "buffer_top_score": buffer_scores[0] if buffer_scores else 0,
            "degenerate_filtered": degenerate_count,
            "judge_errors": judge_errors,
            "divergence_types": type_counts,
        }

    def _empty_stats(self) -> Dict[str, Any]:
        return {
            "iteration": self.iteration,
            "candidates": self.candidates_per_iter,
            "valid_queries": 0,
            "scored": 0,
            "mean_reward": 0,
            "top_score": 0,
            "buffer_top_score": self.replay_buffer.get_scores()[0] if self.replay_buffer.get_scores() else 0,
            "degenerate_filtered": 0,
            "judge_errors": 0,
            "divergence_types": {},
        }

    async def run_loop(self, num_iterations: int = 20) -> Dict[str, Any]:
        """Run the full diff EM loop."""
        is_complete = self._try_resume()
        if is_complete:
            self._log("Run already complete, skipping.")
            return {"status": "already_complete", "iteration": self.iteration}

        start_iteration = self.iteration
        remaining = num_iterations - start_iteration

        if remaining <= 0:
            self._log(f"Already completed {start_iteration} iterations, nothing to do.")
            return {"status": "already_complete", "iteration": self.iteration}

        self._log(f"\nStarting Diff EM loop for {remaining} more iterations")
        self._log(f"Model A ({self.model_a_name}): {self.model_a.provider_model.model}")
        self._log(f"Model B ({self.model_b_name}): {self.model_b.provider_model.model}")
        self._log(f"Judge: {self.judge_model.provider_model.model}")
        self._log(f"Candidates per iteration: {self.candidates_per_iter}")
        self._log(f"Buffer size: {self.buffer_size}")
        self._log(f"Output dir: {self.output_dir}")

        iteration_stats = []

        try:
            while self.iteration < num_iterations:
                stats = await self.run_single_iteration()
                iteration_stats.append(stats)

                # Early stopping: if no meaningful divergence found
                if self.iteration >= 10 and len(iteration_stats) >= 5:
                    recent_top = [s.get("top_score", 0) for s in iteration_stats[-5:]]
                    if max(recent_top) < self.early_stop_threshold:
                        self._log(f"\nNo divergence above {self.early_stop_threshold} in last 5 iterations, stopping early")
                        break

        finally:
            summary = {
                "total_iterations": self.iteration,
                "final_buffer_size": len(self.replay_buffer),
                "final_buffer_scores": self.replay_buffer.get_scores(),
                "iteration_stats": iteration_stats,
                "model_a": self.model_a.provider_model.model,
                "model_b": self.model_b.provider_model.model,
                "model_a_name": self.model_a_name,
                "model_b_name": self.model_b_name,
                "dimensions": self.dimension_names,
            }
            await self.streamer.write_final_summary(summary)
            await self.streamer.close()
            await self.shutdown()

        return summary

    async def shutdown(self):
        """Shutdown all model resources."""
        await self.model_a.shutdown()
        await self.model_b.shutdown()
        await self.query_model.shutdown()
        await self.judge_model.shutdown()
        if self._failures_file is not None:
            self._failures_file.close()
            self._failures_file = None
