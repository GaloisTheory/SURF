#!/usr/bin/env python3
"""CLI viewer for SURF model diff results.

Usage:
    uv run utils/diff_top.py results/olmo3_diff --n 20
    uv run utils/diff_top.py results/olmo3_diff --n 20 --dim accuracy
    uv run utils/diff_top.py results/olmo3_diff --dim safety --min-score 66
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_DIMENSIONS = ["substance", "safety", "accuracy", "reasoning", "persona"]


def get_dimensions(entries: list) -> list:
    """Discover dimension names from the first entry's scores dict."""
    for e in entries:
        scores = e.get("scores", {})
        if scores:
            return list(scores.keys())
    return DEFAULT_DIMENSIONS


def load_results(results_dir: str) -> list:
    """Load results from a directory (or multiple run dirs)."""
    results_path = Path(results_dir)
    entries = []

    # Check for results.jsonl directly
    direct = results_path / "results.jsonl"
    if direct.exists():
        entries.extend(_load_jsonl(direct))

    # Check for run_* subdirectories
    for run_dir in sorted(results_path.glob("run_*")):
        run_results = run_dir / "results.jsonl"
        if run_results.exists():
            entries.extend(_load_jsonl(run_results))

    # Check for runs/ subdirectory
    runs_dir = results_path / "runs"
    if runs_dir.exists():
        for run_dir in sorted(runs_dir.glob("run_*")):
            run_results = run_dir / "results.jsonl"
            if run_results.exists():
                entries.extend(_load_jsonl(run_results))

    return entries


def _load_jsonl(path: Path) -> list:
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def format_entry(rank: int, entry: dict, truncate: int = 300, dimensions: list = None) -> str:
    """Format a single result entry for display."""
    scores = entry.get("scores", {})
    reward = entry.get("reward_score", 0)
    div_type = entry.get("divergence_type", "UNKNOWN")
    primary = entry.get("primary_divergence", "")
    query = entry.get("query", "")
    resp_a = entry.get("response_a", "")
    resp_b = entry.get("response_b", "")
    judge_order = entry.get("judge_order", [])
    model_a_name = entry.get("model_a_name", "A")
    model_b_name = entry.get("model_b_name", "B")
    reasoning = ""
    if "score_metadata" in entry:
        reasoning = entry["score_metadata"].get("reasoning", "") or ""

    # Score line
    dims = dimensions or list(scores.keys()) or DEFAULT_DIMENSIONS
    score_parts = []
    for dim in dims:
        score_parts.append(f"{dim}:{scores.get(dim, '?')}")

    lines = [
        f"{'='*70}",
        f"#{rank} | Reward: {reward:.1f} | Type: {div_type}",
        f"   | {' '.join(score_parts)}",
    ]
    if primary:
        lines.append(f'   | "{primary}"')
    if judge_order:
        lines.append(f"   | Judge order: A={judge_order[0]}, B={judge_order[1]}")

    lines.append(f"{'-'*70}")
    lines.append(f"[Query]")
    lines.append(query[:truncate] + ("..." if len(query) > truncate else ""))

    lines.append(f"{'-'*70}")
    lines.append(f"[{model_a_name} Response]  (truncated to {truncate} chars)")
    lines.append(resp_a[:truncate] + ("..." if len(resp_a) > truncate else ""))

    lines.append(f"{'-'*70}")
    lines.append(f"[{model_b_name} Response]  (truncated to {truncate} chars)")
    lines.append(resp_b[:truncate] + ("..." if len(resp_b) > truncate else ""))

    if reasoning:
        lines.append(f"{'-'*70}")
        lines.append(f"[Judge reasoning]")
        lines.append(reasoning[:500] + ("..." if len(reasoning) > 500 else ""))

    lines.append(f"{'='*70}")
    return "\n".join(lines)


def print_summary(entries: list, dimensions: list = None):
    """Print summary statistics."""
    if not entries:
        print("No entries found.")
        return

    dims = dimensions or get_dimensions(entries)
    rewards = [e.get("reward_score", 0) for e in entries]

    # Divergence type distribution
    type_counts = {}
    for e in entries:
        dt = e.get("divergence_type", "UNKNOWN")
        type_counts[dt] = type_counts.get(dt, 0) + 1

    # Per-dimension statistics
    dim_stats = {}
    for dim in dims:
        dim_scores = [e.get("scores", {}).get(dim, 0) for e in entries if "scores" in e]
        if dim_scores:
            dim_stats[dim] = {
                "mean": sum(dim_scores) / len(dim_scores),
                "max": max(dim_scores),
                "nonzero": sum(1 for s in dim_scores if s > 0),
            }

    print(f"\n{'='*70}")
    print(f"SUMMARY: {len(entries)} total entries")
    print(f"{'='*70}")
    print(f"Reward scores: mean={sum(rewards)/len(rewards):.1f}, "
          f"max={max(rewards):.1f}, min={min(rewards):.1f}")
    print(f"Entries with reward > 40: {sum(1 for r in rewards if r > 40)}")
    print(f"Entries with reward > 60: {sum(1 for r in rewards if r > 60)}")
    print(f"Entries with reward > 80: {sum(1 for r in rewards if r > 80)}")

    print(f"\nDivergence types:")
    for dt, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {dt}: {count} ({100*count/len(entries):.1f}%)")

    print(f"\nPer-dimension stats:")
    for dim, stats in dim_stats.items():
        print(f"  {dim}: mean={stats['mean']:.2f}, max={stats['max']}, "
              f"nonzero={stats['nonzero']}/{len(entries)}")
    print()


def main():
    parser = argparse.ArgumentParser(description="View SURF model diff results")
    parser.add_argument("results_dir", help="Path to results directory")
    parser.add_argument("--n", type=int, default=10, help="Number of top results to show")
    parser.add_argument("--dim", help="Sort by a specific dimension instead of reward_score")
    parser.add_argument("--min-score", type=float, default=0, help="Minimum reward_score to include")
    parser.add_argument("--type", dest="div_type", help="Filter by divergence_type")
    parser.add_argument("--truncate", type=int, default=300, help="Truncate responses at N chars")
    parser.add_argument("--summary-only", action="store_true", help="Only show summary stats")
    args = parser.parse_args()

    entries = load_results(args.results_dir)
    if not entries:
        print(f"No results found in {args.results_dir}")
        sys.exit(1)

    # Discover dimensions from data
    dimensions = get_dimensions(entries)

    # Validate --dim against discovered dimensions
    if args.dim and args.dim not in dimensions:
        print(f"Error: dimension '{args.dim}' not found in data. Available: {dimensions}")
        sys.exit(1)

    # Filter
    if args.min_score > 0:
        entries = [e for e in entries if e.get("reward_score", 0) >= args.min_score]
    if args.div_type:
        entries = [e for e in entries if e.get("divergence_type", "").upper() == args.div_type.upper()]

    # Print summary
    print_summary(entries, dimensions=dimensions)

    if args.summary_only:
        return

    # Sort
    if args.dim:
        entries.sort(key=lambda e: e.get("scores", {}).get(args.dim, 0), reverse=True)
    else:
        entries.sort(key=lambda e: e.get("reward_score", 0), reverse=True)

    # Display top N
    for i, entry in enumerate(entries[:args.n], 1):
        print(format_entry(i, entry, truncate=args.truncate, dimensions=dimensions))
        print()


if __name__ == "__main__":
    main()
