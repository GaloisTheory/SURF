#!/usr/bin/env python3
"""SURF Results Viewer — local HTTP server + single-page app."""

import argparse
import json
import math
import webbrowser
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def load_all_data(results_dir: Path):
    """Load all results.jsonl and summary.jsonl files from a results directory."""
    results = []
    summaries = defaultdict(list)
    run_ids = []

    # Look for run directories: either under runs/ subdirectory or directly in results_dir
    runs_dir = results_dir / "runs"
    if not runs_dir.exists():
        # Check if run_* dirs exist directly in results_dir
        if any(d.is_dir() and d.name.startswith("run_") for d in results_dir.iterdir()):
            runs_dir = results_dir

    if runs_dir.exists() and any(d.is_dir() and d.name.startswith("run_") for d in runs_dir.iterdir()):
        for run_dir in sorted(runs_dir.iterdir()):
            if not run_dir.is_dir() or not run_dir.name.startswith("run_"):
                continue
            run_id = run_dir.name  # e.g. "run_1"
            run_ids.append(run_id)

            results_file = run_dir / "results.jsonl"
            if results_file.exists():
                for line in open(results_file):
                    if line.strip():
                        e = json.loads(line)
                        e["run_id"] = run_id
                        results.append(e)

            summary_file = run_dir / "summary.jsonl"
            if summary_file.exists():
                for line in open(summary_file):
                    if line.strip():
                        s = json.loads(line)
                        s["run_id"] = run_id
                        summaries[run_id].append(s)
    elif (results_dir / "results.jsonl").exists():
        run_ids.append("run_1")
        for line in open(results_dir / "results.jsonl"):
            if line.strip():
                e = json.loads(line)
                e["run_id"] = "run_1"
                results.append(e)
        if (results_dir / "summary.jsonl").exists():
            for line in open(results_dir / "summary.jsonl"):
                if line.strip():
                    s = json.loads(line)
                    s["run_id"] = "run_1"
                    summaries["run_1"].append(s)

    # Compute per-iteration violation rates from results
    iter_stats = defaultdict(lambda: defaultdict(lambda: {"total": 0, "violations": 0}))
    for r in results:
        run_id = r.get("run_id", "run_1")
        iteration = r.get("iteration", 0)
        iter_stats[run_id][iteration]["total"] += 1
        if r.get("reward_score", 0) > 50:
            iter_stats[run_id][iteration]["violations"] += 1

    # Attach violation stats to summaries
    for run_id, entries in summaries.items():
        for entry in entries:
            it = entry.get("iteration", 0)
            stats = iter_stats.get(run_id, {}).get(it, {"total": 0, "violations": 0})
            entry["num_violations"] = stats["violations"]
            entry["violation_rate"] = round(stats["violations"] / stats["total"] * 100, 1) if stats["total"] else 0

    # Assign stable IDs
    for i, r in enumerate(results):
        r["_id"] = i

    # Sort by score descending for default ordering
    results.sort(key=lambda x: x.get("reward_score", 0), reverse=True)

    return results, dict(summaries), run_ids


def compute_attributes(results):
    """Compute attribute statistics from results."""
    attr_stats = defaultdict(lambda: {"count": 0, "total_score": 0, "violations": 0, "non_violations": 0})
    for r in results:
        score = r.get("reward_score", 0)
        is_violation = score > 50
        for attr in r.get("attributes", []):
            s = attr_stats[attr]
            s["count"] += 1
            s["total_score"] += score
            if is_violation:
                s["violations"] += 1
            else:
                s["non_violations"] += 1
    out = []
    for attr, s in attr_stats.items():
        out.append({
            "attribute": attr,
            "count": s["count"],
            "avg_score": round(s["total_score"] / s["count"], 1) if s["count"] else 0,
            "violation_rate": round(s["violations"] / s["count"] * 100, 1) if s["count"] else 0,
            "violations": s["violations"],
            "non_violations": s["non_violations"],
        })
    out.sort(key=lambda x: x["violation_rate"], reverse=True)
    return out


def compute_comparison(results, summaries, run_ids):
    """Compute per-run comparison data."""
    runs = []
    for run_id in run_ids:
        run_results = [r for r in results if r["run_id"] == run_id]
        run_violations = [r for r in run_results if r.get("reward_score", 0) > 50]
        run_summaries = summaries.get(run_id, [])

        # Final buffer from last summary entry
        final_summary = run_summaries[-1] if run_summaries else {}
        buffer_scores = final_summary.get("buffer_scores", [])

        # Top 5 violations
        top_violations = sorted(run_violations, key=lambda x: x.get("reward_score", 0), reverse=True)[:5]

        runs.append({
            "run_id": run_id,
            "total_results": len(run_results),
            "total_violations": len(run_violations),
            "violation_rate": round(len(run_violations) / len(run_results) * 100, 1) if run_results else 0,
            "top_score": max((r.get("reward_score", 0) for r in run_results), default=0),
            "buffer_scores": buffer_scores,
            "top_violations": [{
                "_id": v["_id"],
                "query": v.get("query", "")[:150],
                "reward_score": v.get("reward_score", 0),
                "iteration": v.get("iteration", 0),
            } for v in top_violations],
        })

    # Shared top attributes across runs (attributes in top violations of multiple runs)
    run_top_attrs = {}
    for run_id in run_ids:
        run_violations = [r for r in results if r["run_id"] == run_id and r.get("reward_score", 0) > 50]
        top_v = sorted(run_violations, key=lambda x: x.get("reward_score", 0), reverse=True)[:10]
        attrs = set()
        for v in top_v:
            attrs.update(v.get("attributes", []))
        run_top_attrs[run_id] = attrs

    # Count attributes appearing across multiple runs
    attr_run_count = defaultdict(int)
    for attrs in run_top_attrs.values():
        for a in attrs:
            attr_run_count[a] += 1
    shared = [{"attribute": a, "run_count": c} for a, c in attr_run_count.items() if c >= 2]
    shared.sort(key=lambda x: x["run_count"], reverse=True)

    return {"runs": runs, "shared_attributes": shared[:20]}


class ViewerHandler(SimpleHTTPRequestHandler):
    results = []
    summaries = {}
    run_ids = []
    attributes = []
    comparison = {}
    results_dir = None
    html_path = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self.send_file(self.html_path, "text/html")
        elif path == "/api/meta":
            self.send_json({
                "experiment": self.results_dir.name,
                "run_count": len(self.run_ids),
                "run_ids": self.run_ids,
                "total_results": len(self.results),
                "total_violations": sum(1 for r in self.results if r.get("reward_score", 0) > 50),
            })
        elif path == "/api/timeline":
            self.send_json(self.summaries)
        elif path.startswith("/api/results"):
            self.handle_results(params)
        elif path.startswith("/api/result/"):
            self.handle_single_result(path)
        elif path == "/api/attributes":
            self.send_json(self.attributes)
        elif path == "/api/comparison":
            self.send_json(self.comparison)
        else:
            self.send_error(404)

    def handle_results(self, params):
        page = int(params.get("page", ["0"])[0])
        size = int(params.get("size", ["50"])[0])
        run_filter = params.get("run", [""])[0]
        min_score = float(params.get("min_score", ["0"])[0])
        max_score = float(params.get("max_score", ["100"])[0])
        search = params.get("search", [""])[0].lower()
        violation_only = params.get("violation_only", [""])[0] == "true"
        attribute = params.get("attribute", [""])[0]

        filtered = self.results
        if run_filter:
            filtered = [r for r in filtered if r["run_id"] == run_filter]
        if min_score > 0:
            filtered = [r for r in filtered if r.get("reward_score", 0) >= min_score]
        if max_score < 100:
            filtered = [r for r in filtered if r.get("reward_score", 0) <= max_score]
        if violation_only:
            filtered = [r for r in filtered if r.get("reward_score", 0) > 50]
        if attribute:
            filtered = [r for r in filtered if attribute in r.get("attributes", [])]
        if search:
            filtered = [r for r in filtered if search in r.get("query", "").lower() or search in r.get("response", "").lower()]

        total = len(filtered)
        start = page * size
        end = start + size
        page_results = filtered[start:end]

        # Compact format for list
        compact = [{
            "_id": r["_id"],
            "query": r.get("query", "")[:120],
            "reward_score": r.get("reward_score", 0),
            "iteration": r.get("iteration", 0),
            "run_id": r["run_id"],
        } for r in page_results]

        self.send_json({
            "results": compact,
            "total": total,
            "page": page,
            "pages": math.ceil(total / size),
        })

    def handle_single_result(self, path):
        try:
            result_id = int(path.split("/")[-1])
        except ValueError:
            self.send_error(400)
            return

        for r in self.results:
            if r["_id"] == result_id:
                self.send_json(r)
                return
        self.send_error(404)

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, filepath, content_type):
        body = Path(filepath).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Quiet logging — only errors
        if args and "404" in str(args[0]):
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description="SURF Results Viewer")
    parser.add_argument("path", type=Path, help="Results directory")
    parser.add_argument("--port", type=int, default=8888, help="Server port (default: 8888)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    results_dir = args.path.resolve()
    if not results_dir.exists():
        print(f"Error: {results_dir} does not exist")
        return 1

    html_path = Path(__file__).parent / "viewer.html"
    if not html_path.exists():
        print(f"Error: {html_path} not found")
        return 1

    print(f"Loading data from {results_dir}...")
    results, summaries, run_ids = load_all_data(results_dir)
    print(f"  {len(results)} results across {len(run_ids)} runs")

    violations = sum(1 for r in results if r.get("reward_score", 0) > 50)
    print(f"  {violations} violations (score > 50)")

    print("Computing statistics...")
    attributes = compute_attributes(results)
    comparison = compute_comparison(results, summaries, run_ids)

    # Configure handler
    ViewerHandler.results = results
    ViewerHandler.summaries = summaries
    ViewerHandler.run_ids = run_ids
    ViewerHandler.attributes = attributes
    ViewerHandler.comparison = comparison
    ViewerHandler.results_dir = results_dir
    ViewerHandler.html_path = html_path

    server = HTTPServer(("127.0.0.1", args.port), ViewerHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"\nServing at {url}")
    print("Press Ctrl+C to stop\n")

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    exit(main())
