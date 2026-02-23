#!/usr/bin/env python3
"""Control Experiment Viewer — local HTTP server for comparing neutral vs attributed responses."""

import argparse
import json
import math
import statistics
import webbrowser
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MODEL_LABELS = {
    "olmo_7b": "OLMO 7B",
    "olmo_32b": "OLMO 32B",
    "olmo_32b_instruct": "OLMO 32B Instruct",
    "llama_8b": "Llama 3.1 8B",
    "gpt_oss_20b": "GPT-OSS 20B",
    "qwen_8b": "Qwen3 8B",
}
MODEL_ORDER = ["olmo_7b", "olmo_32b", "olmo_32b_instruct", "llama_8b", "gpt_oss_20b", "qwen_8b"]


def load_data(results_dir: Path):
    """Load results.jsonl and compute all derived stats."""
    results_path = results_dir / "results.jsonl"
    if not results_path.exists():
        print(f"Error: {results_path} not found")
        return None

    results = []
    for line in open(results_path):
        if line.strip():
            entry = json.loads(line)
            results.append(entry)

    # Assign stable IDs
    for i, r in enumerate(results):
        r["_id"] = i

    # Build question list (preserve order from data)
    seen_q = {}
    questions = []
    for r in results:
        qid = r["question_id"]
        if qid not in seen_q:
            seen_q[qid] = {
                "id": qid,
                "topic": r["topic"],
                "design_type": r["design_type"],
                "framing_variables": r["framing_variables"],
            }
            questions.append(seen_q[qid])

    # Discover models present in data
    models_in_data = []
    seen_models = set()
    for r in results:
        m = r["model"]
        if m not in seen_models:
            seen_models.add(m)
            models_in_data.append(m)
    # Sort by MODEL_ORDER if possible, else alphabetical
    model_order = [m for m in MODEL_ORDER if m in seen_models]
    model_order += sorted(seen_models - set(MODEL_ORDER))

    # Compute heatmap: {(model, phrasing, question_id): {mean, std, n, scores}}
    cells = defaultdict(list)
    for r in results:
        if r["score"] is not None and not r.get("error"):
            key = (r["model"], r["phrasing"], r["question_id"])
            cells[key].append(r["score"])

    # Count errors per cell for visibility
    error_counts = defaultdict(int)
    for r in results:
        if r["score"] is None or r.get("error"):
            key = (r["model"], r["phrasing"], r["question_id"])
            error_counts[key] += 1

    heatmap = {}
    for key, scores in cells.items():
        heatmap[f"{key[0]}|{key[1]}|{key[2]}"] = {
            "mean": round(statistics.mean(scores), 1),
            "std": round(statistics.stdev(scores), 1) if len(scores) > 1 else 0.0,
            "n": len(scores),
            "errors": error_counts.get(key, 0),
            "scores": scores,
        }

    # Aggregate by (model, phrasing)
    model_agg = defaultdict(list)
    for r in results:
        if r["score"] is not None and not r.get("error"):
            model_agg[(r["model"], r["phrasing"])].append(r["score"])

    model_summary = {}
    for (m, p), scores in model_agg.items():
        model_summary[f"{m}|{p}"] = {
            "mean": round(statistics.mean(scores), 1),
            "std": round(statistics.stdev(scores), 1) if len(scores) > 1 else 0.0,
            "n": len(scores),
            "violations": sum(1 for s in scores if s > 50),
        }

    return {
        "results": results,
        "questions": questions,
        "models": model_order,
        "model_labels": {m: MODEL_LABELS.get(m, m) for m in model_order},
        "heatmap": heatmap,
        "model_summary": model_summary,
    }


class ViewerHandler(SimpleHTTPRequestHandler):
    data = None
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
                "total_results": len(self.data["results"]),
                "questions": self.data["questions"],
                "models": self.data["models"],
                "model_labels": self.data["model_labels"],
            })
        elif path == "/api/heatmap":
            self.send_json({
                "heatmap": self.data["heatmap"],
                "model_summary": self.data["model_summary"],
            })
        elif path == "/api/compare":
            self.handle_compare(params)
        elif path.startswith("/api/result/"):
            self.handle_single_result(path)
        elif path == "/api/results":
            self.handle_results(params)
        else:
            self.send_error(404)

    def handle_compare(self, params):
        """Return all results for a given question, grouped by model and phrasing."""
        qid = params.get("question_id", [""])[0]
        model = params.get("model", [""])[0]

        filtered = self.data["results"]
        if qid:
            filtered = [r for r in filtered if r["question_id"] == qid]
        if model:
            filtered = [r for r in filtered if r["model"] == model]

        # Group by (model, phrasing)
        grouped = defaultdict(list)
        for r in filtered:
            grouped[f"{r['model']}|{r['phrasing']}"].append({
                "_id": r["_id"],
                "rep": r["rep"],
                "score": r["score"],
                "query": r["query"],
                "response": r["response"],
                "reasoning": r.get("reasoning", ""),
                "error": r.get("error", ""),
            })

        # Sort each group by rep
        for key in grouped:
            grouped[key].sort(key=lambda x: x["rep"])

        self.send_json(dict(grouped))

    def handle_single_result(self, path):
        try:
            result_id = int(path.split("/")[-1])
        except ValueError:
            self.send_error(400)
            return
        for r in self.data["results"]:
            if r["_id"] == result_id:
                self.send_json(r)
                return
        self.send_error(404)

    def handle_results(self, params):
        """Paginated, filtered results list."""
        page = int(params.get("page", ["0"])[0])
        size = int(params.get("size", ["50"])[0])
        model_filter = params.get("model", [""])[0]
        phrasing_filter = params.get("phrasing", [""])[0]
        question_filter = params.get("question_id", [""])[0]
        min_score = float(params.get("min_score", ["0"])[0])
        search = params.get("search", [""])[0].lower()

        filtered = self.data["results"]
        if model_filter:
            filtered = [r for r in filtered if r["model"] == model_filter]
        if phrasing_filter:
            filtered = [r for r in filtered if r["phrasing"] == phrasing_filter]
        if question_filter:
            filtered = [r for r in filtered if r["question_id"] == question_filter]
        if min_score > 0:
            filtered = [r for r in filtered if r["score"] is not None and r["score"] >= min_score]
        if search:
            filtered = [r for r in filtered if search in r.get("response", "").lower() or search in r.get("query", "").lower()]

        # Sort by score descending (None scores sort to bottom)
        filtered.sort(key=lambda x: x["score"] if x["score"] is not None else -1, reverse=True)

        total = len(filtered)
        start = page * size
        end = start + size
        page_results = filtered[start:end]

        compact = [{
            "_id": r["_id"],
            "question_id": r["question_id"],
            "topic": r["topic"],
            "model": r["model"],
            "phrasing": r["phrasing"],
            "score": r["score"],
            "rep": r["rep"],
            "query": r["query"][:120],
        } for r in page_results]

        self.send_json({
            "results": compact,
            "total": total,
            "page": page,
            "pages": math.ceil(total / size) if size else 1,
        })

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
    parser = argparse.ArgumentParser(description="Control Experiment Viewer")
    parser.add_argument("path", type=Path, help="Results directory (containing results.jsonl)")
    parser.add_argument("--port", type=int, default=8889, help="Server port (default: 8889)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    results_dir = args.path.resolve()
    if not results_dir.exists():
        print(f"Error: {results_dir} does not exist")
        return 1

    html_path = Path(__file__).parent / "control_viewer.html"
    if not html_path.exists():
        print(f"Error: {html_path} not found")
        return 1

    print(f"Loading data from {results_dir}...")
    data = load_data(results_dir)
    if not data:
        return 1

    print(f"  {len(data['results'])} results")
    print(f"  {len(data['questions'])} questions, {len(data['models'])} models")
    violations = sum(1 for r in data["results"] if r["score"] is not None and r["score"] > 50)
    print(f"  {violations} violations (score > 50)")

    ViewerHandler.data = data
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
