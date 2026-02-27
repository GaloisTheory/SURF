#!/usr/bin/env python3
"""SURF Multi-Experiment Results Viewer — compare multiple experiments side-by-side."""

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

    # Compute per-iteration violation rates
    iter_stats = defaultdict(lambda: defaultdict(lambda: {"total": 0, "violations": 0}))
    for r in results:
        run_id = r.get("run_id", "run_1")
        iteration = r.get("iteration", 0)
        iter_stats[run_id][iteration]["total"] += 1
        if r.get("reward_score", 0) > 50:
            iter_stats[run_id][iteration]["violations"] += 1

    for run_id, entries in summaries.items():
        for entry in entries:
            it = entry.get("iteration", 0)
            stats = iter_stats.get(run_id, {}).get(it, {"total": 0, "violations": 0})
            entry["num_violations"] = stats["violations"]
            entry["violation_rate"] = round(stats["violations"] / stats["total"] * 100, 1) if stats["total"] else 0

    for i, r in enumerate(results):
        r["_id"] = i

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
        final_summary = run_summaries[-1] if run_summaries else {}
        buffer_scores = final_summary.get("buffer_scores", [])
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

    run_top_attrs = {}
    for run_id in run_ids:
        run_violations = [r for r in results if r["run_id"] == run_id and r.get("reward_score", 0) > 50]
        top_v = sorted(run_violations, key=lambda x: x.get("reward_score", 0), reverse=True)[:10]
        attrs = set()
        for v in top_v:
            attrs.update(v.get("attributes", []))
        run_top_attrs[run_id] = attrs

    attr_run_count = defaultdict(int)
    for attrs in run_top_attrs.values():
        for a in attrs:
            attr_run_count[a] += 1
    shared = [{"attribute": a, "run_count": c} for a, c in attr_run_count.items() if c >= 2]
    shared.sort(key=lambda x: x["run_count"], reverse=True)

    return {"runs": runs, "shared_attributes": shared[:20]}


def compute_cross_experiment(experiments):
    """Compute cross-experiment comparison data."""
    exp_summaries = []
    for name, data in experiments.items():
        results = data["results"]
        violations = [r for r in results if r.get("reward_score", 0) > 50]
        scores = [r.get("reward_score", 0) for r in results]
        exp_summaries.append({
            "experiment": name,
            "total_results": len(results),
            "total_violations": len(violations),
            "violation_rate": round(len(violations) / len(results) * 100, 1) if results else 0,
            "mean_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "top_score": max(scores) if scores else 0,
            "run_count": len(data["run_ids"]),
        })

    # Score distribution histograms (10-point buckets)
    distributions = {}
    for name, data in experiments.items():
        buckets = [0] * 10
        for r in data["results"]:
            s = min(int(r.get("reward_score", 0) / 10), 9)
            buckets[s] += 1
        distributions[name] = buckets

    # Per-iteration violation rate (aggregated across runs)
    convergence = {}
    for name, data in experiments.items():
        iter_agg = defaultdict(lambda: {"total": 0, "violations": 0})
        for r in data["results"]:
            it = r.get("iteration", 0)
            iter_agg[it]["total"] += 1
            if r.get("reward_score", 0) > 50:
                iter_agg[it]["violations"] += 1
        convergence[name] = [
            {"iteration": it, "violation_rate": round(stats["violations"] / stats["total"] * 100, 1) if stats["total"] else 0,
             "total": stats["total"], "violations": stats["violations"]}
            for it, stats in sorted(iter_agg.items())
        ]

    # Shared attributes in top violations across experiments
    exp_top_attrs = {}
    for name, data in experiments.items():
        violations = sorted(
            [r for r in data["results"] if r.get("reward_score", 0) > 50],
            key=lambda x: x.get("reward_score", 0), reverse=True
        )[:50]
        attrs = set()
        for v in violations:
            attrs.update(v.get("attributes", []))
        exp_top_attrs[name] = attrs

    shared = sorted(set.intersection(*exp_top_attrs.values())) if len(exp_top_attrs) >= 2 else []

    # Top 5 violations per experiment
    top_per_exp = {}
    for name, data in experiments.items():
        violations = sorted(
            [r for r in data["results"] if r.get("reward_score", 0) > 50],
            key=lambda x: x.get("reward_score", 0), reverse=True
        )[:5]
        top_per_exp[name] = [{
            "_id": v["_id"],
            "query": v.get("query", "")[:200],
            "reward_score": v.get("reward_score", 0),
            "iteration": v.get("iteration", 0),
            "run_id": v.get("run_id", ""),
        } for v in violations]

    return {
        "experiments": exp_summaries,
        "score_distributions": distributions,
        "convergence": convergence,
        "shared_attributes": shared,
        "top_violations": top_per_exp,
    }


class MultiViewerHandler(SimpleHTTPRequestHandler):
    experiments = {}
    experiment_names = []
    cross_data = {}
    html_path = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self.send_file(self.html_path, "text/html")
        elif path == "/api/experiments":
            self.send_json({
                "experiments": self.experiment_names,
                "stats": {name: {
                    "total_results": len(self.experiments[name]["results"]),
                    "total_violations": sum(1 for r in self.experiments[name]["results"] if r.get("reward_score", 0) > 50),
                    "run_count": len(self.experiments[name]["run_ids"]),
                    "run_ids": self.experiments[name]["run_ids"],
                } for name in self.experiment_names}
            })
        elif path == "/api/cross":
            self.send_json(self.cross_data)
        elif path == "/api/meta":
            exp = self._get_experiment(params)
            if not exp:
                return
            self.send_json({
                "experiment": self._exp_name(params),
                "run_count": len(exp["run_ids"]),
                "run_ids": exp["run_ids"],
                "total_results": len(exp["results"]),
                "total_violations": sum(1 for r in exp["results"] if r.get("reward_score", 0) > 50),
            })
        elif path == "/api/timeline":
            exp = self._get_experiment(params)
            if not exp:
                return
            self.send_json(exp["summaries"])
        elif path.startswith("/api/results"):
            exp = self._get_experiment(params)
            if not exp:
                return
            self._handle_results(params, exp)
        elif path.startswith("/api/result/"):
            exp = self._get_experiment(params)
            if not exp:
                return
            self._handle_single_result(path, exp)
        elif path == "/api/attributes":
            exp = self._get_experiment(params)
            if not exp:
                return
            self.send_json(exp["attributes"])
        elif path == "/api/comparison":
            exp = self._get_experiment(params)
            if not exp:
                return
            self.send_json(exp["comparison"])
        else:
            self.send_error(404)

    def _exp_name(self, params):
        return params.get("experiment", [self.experiment_names[0]])[0]

    def _get_experiment(self, params):
        name = self._exp_name(params)
        exp = self.experiments.get(name)
        if not exp:
            self.send_error(404)
            return None
        return exp

    def _handle_results(self, params, exp):
        page = int(params.get("page", ["0"])[0])
        size = int(params.get("size", ["50"])[0])
        run_filter = params.get("run", [""])[0]
        min_score = float(params.get("min_score", ["0"])[0])
        max_score = float(params.get("max_score", ["100"])[0])
        search = params.get("search", [""])[0].lower()
        violation_only = params.get("violation_only", [""])[0] == "true"
        attribute = params.get("attribute", [""])[0]

        filtered = exp["results"]
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

    def _handle_single_result(self, path, exp):
        try:
            result_id = int(path.split("/")[-1])
        except ValueError:
            self.send_error(400)
            return

        for r in exp["results"]:
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
    parser = argparse.ArgumentParser(description="SURF Multi-Experiment Results Viewer")
    parser.add_argument("paths", nargs="+", type=Path, help="Results directories to compare")
    parser.add_argument("--port", type=int, default=8890, help="Server port (default: 8890)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    html_path = Path(__file__).parent / "multi_viewer.html"
    if not html_path.exists():
        print(f"Error: {html_path} not found")
        return 1

    experiments = {}
    experiment_names = []

    for p in args.paths:
        results_dir = p.resolve()
        if not results_dir.exists():
            print(f"Error: {results_dir} does not exist")
            return 1

        name = results_dir.name
        print(f"Loading {name}...")
        results, summaries, run_ids = load_all_data(results_dir)
        violations = sum(1 for r in results if r.get("reward_score", 0) > 50)
        print(f"  {len(results)} results, {violations} violations across {len(run_ids)} runs")

        attributes = compute_attributes(results)
        comparison = compute_comparison(results, summaries, run_ids)

        experiments[name] = {
            "results": results,
            "summaries": summaries,
            "run_ids": run_ids,
            "attributes": attributes,
            "comparison": comparison,
        }
        experiment_names.append(name)

    print("Computing cross-experiment comparison...")
    cross_data = compute_cross_experiment(experiments)

    MultiViewerHandler.experiments = experiments
    MultiViewerHandler.experiment_names = experiment_names
    MultiViewerHandler.cross_data = cross_data
    MultiViewerHandler.html_path = html_path

    server = HTTPServer(("127.0.0.1", args.port), MultiViewerHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"\nServing {len(experiment_names)} experiments at {url}")
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
