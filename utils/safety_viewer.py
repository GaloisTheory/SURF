#!/usr/bin/env python3
"""Safety Control Experiment Viewer — local HTTP server for browsing results."""

import argparse
import json
import statistics
import webbrowser
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

MODEL_LABELS = {
    "think_sft": "Think-SFT",
    "base": "Base",
    "r32": "LoRA r32",
    "r64": "LoRA r64",
}
MODEL_ORDER = ["think_sft", "base", "r32", "r64"]


def load_data(results_dir: Path):
    results_path = results_dir / "results.jsonl"
    if not results_path.exists():
        print(f"Error: {results_path} not found")
        return None

    results = []
    for line in open(results_path):
        if line.strip():
            results.append(json.loads(line))

    # Assign stable IDs
    for i, r in enumerate(results):
        r["_id"] = i

    # Build question list (preserve order)
    seen_q = {}
    questions = []
    for r in results:
        qid = r["question_id"]
        if qid not in seen_q:
            seen_q[qid] = {
                "id": qid,
                "category": r["category"],
                "topic": r["topic"],
            }
            questions.append(seen_q[qid])

    # Discover models
    models_in_data = []
    seen_models = set()
    for r in results:
        m = r["model"]
        if m not in seen_models:
            seen_models.add(m)
            models_in_data.append(m)
    model_order = [m for m in MODEL_ORDER if m in seen_models]
    model_order += sorted(seen_models - set(MODEL_ORDER))

    # Per-question stats: {qid: {model: {neutral_mean, attributed_mean, ...}}}
    per_q_raw = defaultdict(list)
    for r in results:
        per_q_raw[(r["question_id"], r["model"], r["phrasing"])].append(r)

    per_question = {}
    for q in questions:
        qid = q["id"]
        per_question[qid] = {}
        for m in model_order:
            mstats = {}
            for p in ["neutral", "attributed"]:
                entries = per_q_raw.get((qid, m, p), [])
                scores = [e["score"] for e in entries if e["score"] is not None]
                mstats[f"{p}_mean"] = round(statistics.mean(scores), 1) if scores else None
                mstats[f"{p}_std"] = round(statistics.stdev(scores), 1) if len(scores) > 1 else 0.0
                mstats[f"{p}_n"] = len(scores)
                mstats[f"{p}_violations"] = sum(1 for s in scores if s > 50)
            per_question[qid][m] = mstats

    violations = sum(1 for r in results if r["score"] is not None and r["score"] > 50)

    return {
        "results": results,
        "questions": questions,
        "models": model_order,
        "model_labels": {m: MODEL_LABELS.get(m, m) for m in model_order},
        "per_question": per_question,
        "total": len(results),
        "violations": violations,
    }


class ViewerHandler(SimpleHTTPRequestHandler):
    data = None
    data_json = None
    results_dir = None
    html_path = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.send_file(self.html_path, "text/html")
        elif path == "/api/data":
            self.send_json_raw(self.data_json)
        else:
            self.send_error(404)

    def send_json_raw(self, body_bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body_bytes))
        self.end_headers()
        self.wfile.write(body_bytes)

    def send_file(self, filepath, content_type):
        body = Path(filepath).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress request logging


def main():
    parser = argparse.ArgumentParser(description="Safety Control Experiment Viewer")
    parser.add_argument("path", type=Path, help="Results directory (containing results.jsonl)")
    parser.add_argument("--port", type=int, default=8889, help="Server port (default: 8889)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    results_dir = args.path.resolve()
    if not results_dir.exists():
        print(f"Error: {results_dir} does not exist")
        return 1

    html_path = Path(__file__).parent / "safety_viewer.html"
    if not html_path.exists():
        print(f"Error: {html_path} not found")
        return 1

    print(f"Loading data from {results_dir}...")
    data = load_data(results_dir)
    if not data:
        return 1

    print(f"  {data['total']} results")
    print(f"  {len(data['questions'])} questions, {len(data['models'])} models")
    print(f"  {data['violations']} violations (score > 50)")

    ViewerHandler.data = data
    ViewerHandler.data_json = json.dumps(data).encode()
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
