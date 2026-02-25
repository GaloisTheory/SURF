#!/usr/bin/env python3
"""SURF Diff Results Viewer — local HTTP server + single-page app for model comparison results."""

import argparse
import json
import math
import webbrowser
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_DIMENSIONS = ["substance", "safety", "accuracy", "reasoning", "persona"]


def get_dimensions_from_results(results):
    """Discover dimension names from the first result entry's scores dict."""
    for r in results:
        scores = r.get("scores", {})
        if scores:
            return list(scores.keys())
    return DEFAULT_DIMENSIONS


def load_all_data(results_dir: Path):
    """Load all results.jsonl and summary.jsonl files from a results directory."""
    results = []
    summaries = defaultdict(list)
    run_ids = []

    # Look for run directories
    runs_dir = results_dir / "runs"
    if not runs_dir.exists():
        if any(d.is_dir() and d.name.startswith("run_") for d in results_dir.iterdir()):
            runs_dir = results_dir

    if runs_dir.exists() and any(d.is_dir() and d.name.startswith("run_") for d in runs_dir.iterdir()):
        for run_dir in sorted(runs_dir.iterdir()):
            if not run_dir.is_dir() or not run_dir.name.startswith("run_"):
                continue
            run_id = run_dir.name
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

    # Assign stable IDs
    for i, r in enumerate(results):
        r["_id"] = i

    # Sort by reward descending
    results.sort(key=lambda x: x.get("reward_score", 0), reverse=True)

    return results, dict(summaries), run_ids


def compute_overview(results, dimensions=None):
    """Compute overview statistics: dimension distributions, divergence type counts, stats."""
    dims = dimensions or DEFAULT_DIMENSIONS
    total = len(results)
    rewards = [r.get("reward_score", 0) for r in results]
    degenerate = sum(1 for r in results if r.get("reward_score", 0) == 0)

    # Divergence type counts
    div_counts = defaultdict(int)
    for r in results:
        dt = r.get("divergence_type", "UNKNOWN")
        div_counts[dt] += 1

    # Dimension distribution: for each dim, count how many 0s, 1s, 2s, 3s, 4s, 5s
    dim_dist = {}
    for dim in dims:
        counts = [0, 0, 0, 0, 0, 0]  # index = score value (0-5)
        for r in results:
            score = r.get("scores", {}).get(dim, 0)
            if 0 <= score <= 5:
                counts[score] += 1
        dim_dist[dim] = counts

    return {
        "total_pairs": total,
        "mean_reward": round(sum(rewards) / total, 1) if total else 0,
        "max_reward": max(rewards) if rewards else 0,
        "degenerate_rate": round(degenerate / total * 100, 1) if total else 0,
        "divergence_types": dict(div_counts),
        "dimension_distribution": dim_dist,
    }


def compute_attributes(results):
    """Compute attribute statistics from results."""
    attr_stats = defaultdict(lambda: {"count": 0, "total_reward": 0, "div_types": defaultdict(int)})
    for r in results:
        reward = r.get("reward_score", 0)
        div_type = r.get("divergence_type", "UNKNOWN")
        for attr in r.get("attributes", []):
            s = attr_stats[attr]
            s["count"] += 1
            s["total_reward"] += reward
            s["div_types"][div_type] += 1

    out = []
    for attr, s in attr_stats.items():
        top_div = max(s["div_types"].items(), key=lambda x: x[1])[0] if s["div_types"] else "UNKNOWN"
        out.append({
            "attribute": attr,
            "count": s["count"],
            "avg_reward": round(s["total_reward"] / s["count"], 1) if s["count"] else 0,
            "top_divergence_type": top_div,
        })
    out.sort(key=lambda x: x["avg_reward"], reverse=True)
    return out


class DiffViewerHandler(SimpleHTTPRequestHandler):
    results = []
    summaries = {}
    run_ids = []
    overview = {}
    attributes = []
    results_dir = None
    html_path = None
    model_a_name = "A"
    model_b_name = "B"
    dimensions = DEFAULT_DIMENSIONS

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self.send_file(self.html_path, "text/html")
        elif path == "/api/meta":
            self.send_json({
                "experiment": self.results_dir.name,
                "model_a_name": self.model_a_name,
                "model_b_name": self.model_b_name,
                "run_count": len(self.run_ids),
                "run_ids": self.run_ids,
                "total_entries": len(self.results),
                "dimensions": self.dimensions,
            })
        elif path == "/api/overview":
            self.send_json(self.overview)
        elif path == "/api/timeline":
            self.send_json(self.summaries)
        elif path.startswith("/api/results"):
            self.handle_results(params)
        elif path.startswith("/api/result/"):
            self.handle_single_result(path)
        elif path == "/api/attributes":
            self.send_json(self.attributes)
        else:
            self.send_error(404)

    def handle_results(self, params):
        page = int(params.get("page", ["0"])[0])
        size = int(params.get("size", ["50"])[0])
        run_filter = params.get("run", [""])[0]
        min_reward = float(params.get("min_reward", ["0"])[0])
        div_type = params.get("divergence_type", [""])[0]
        dim_sort = params.get("dim_sort", [""])[0]
        search = params.get("search", [""])[0].lower()
        attribute = params.get("attribute", [""])[0]

        filtered = self.results
        if run_filter:
            filtered = [r for r in filtered if r["run_id"] == run_filter]
        if min_reward > 0:
            filtered = [r for r in filtered if r.get("reward_score", 0) >= min_reward]
        if div_type:
            filtered = [r for r in filtered if r.get("divergence_type", "") == div_type]
        if attribute:
            filtered = [r for r in filtered if attribute in r.get("attributes", [])]
        if search:
            filtered = [r for r in filtered if search in r.get("query", "").lower()
                        or search in r.get("response_a", "").lower()
                        or search in r.get("response_b", "").lower()]

        # Sort by dimension if specified, otherwise default (reward desc)
        if dim_sort and dim_sort in self.dimensions:
            filtered = sorted(filtered, key=lambda x: x.get("scores", {}).get(dim_sort, 0), reverse=True)

        total = len(filtered)
        start = page * size
        end = start + size
        page_results = filtered[start:end]

        compact = [{
            "_id": r["_id"],
            "query": r.get("query", "")[:120],
            "reward_score": r.get("reward_score", 0),
            "divergence_type": r.get("divergence_type", ""),
            "scores": r.get("scores", {}),
            "iteration": r.get("iteration", 0),
            "run_id": r.get("run_id", ""),
        } for r in page_results]

        self.send_json({
            "results": compact,
            "total": total,
            "page": page,
            "pages": math.ceil(total / size) if size else 1,
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
        if args and "404" in str(args[0]):
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description="SURF Diff Results Viewer")
    parser.add_argument("path", type=Path, help="Results directory")
    parser.add_argument("--port", type=int, default=8891, help="Server port (default: 8891)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    results_dir = args.path.resolve()
    if not results_dir.exists():
        print(f"Error: {results_dir} does not exist")
        return 1

    html_path = Path(__file__).parent / "diff_viewer.html"
    if not html_path.exists():
        print(f"Error: {html_path} not found")
        return 1

    print(f"Loading data from {results_dir}...")
    results, summaries, run_ids = load_all_data(results_dir)
    print(f"  {len(results)} results across {len(run_ids)} runs")

    # Detect model names from first entry
    model_a_name = "A"
    model_b_name = "B"
    if results:
        model_a_name = results[0].get("model_a_name", "A")
        model_b_name = results[0].get("model_b_name", "B")
    print(f"  Models: {model_a_name} vs {model_b_name}")

    # Discover dimensions from data
    dimensions = get_dimensions_from_results(results)
    print(f"  Dimensions: {dimensions}")

    print("Computing statistics...")
    overview = compute_overview(results, dimensions=dimensions)
    attributes = compute_attributes(results)

    DiffViewerHandler.results = results
    DiffViewerHandler.summaries = summaries
    DiffViewerHandler.run_ids = run_ids
    DiffViewerHandler.overview = overview
    DiffViewerHandler.attributes = attributes
    DiffViewerHandler.results_dir = results_dir
    DiffViewerHandler.html_path = html_path
    DiffViewerHandler.model_a_name = model_a_name
    DiffViewerHandler.model_b_name = model_b_name
    DiffViewerHandler.dimensions = dimensions

    server = HTTPServer(("127.0.0.1", args.port), DiffViewerHandler)
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
