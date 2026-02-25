#!/usr/bin/env bash
# run_diff_quick.sh — Quick timing run: 5 parallel diff runs, 5 iters x 20 candidates
#
# GPU allocation: all 8x B200 (4 per model via serve_olmo_pair.sh)
# Judge: Gemini 3 Flash (fast + cheap)
# Purpose: measure wall-clock time for diff runs
#
# Usage: bash scripts/run_diff_quick.sh
# Stop:  Ctrl-C

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

NUM_RUNS=5
ITERATIONS=5
CANDIDATES=20
JUDGE_MODEL="openrouter:google/gemini-3-flash-preview"
OUTPUT_BASE="results/olmo3_diff_quick_$(date +%Y%m%d_%H%M%S)"

MODEL_A="allenai/OLMo-3-1025-7B"
MODEL_B="allenai/OLMo-3-7B-Think-SFT"
PORT_A=8000
PORT_B=8001

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log() { echo -e "${GREEN}[runner]${NC} $*"; }
warn() { echo -e "${YELLOW}[runner]${NC} $*"; }
err() { echo -e "${RED}[runner]${NC} $*" >&2; }

# --- Check if servers are already running ---
servers_healthy() {
    curl -s http://localhost:${PORT_A}/health > /dev/null 2>&1 && \
    curl -s http://localhost:${PORT_B}/health > /dev/null 2>&1
}

if servers_healthy; then
    log "vLLM servers already running on ports $PORT_A and $PORT_B"
else
    log "Starting vLLM servers..."
    bash "$SCRIPT_DIR/serve_olmo_pair.sh" &
    SERVE_PID=$!

    # Wait for health
    elapsed=0
    while [[ $elapsed -lt 600 ]]; do
        if servers_healthy; then
            log "Both servers healthy after ${elapsed}s"
            break
        fi
        sleep 5
        elapsed=$((elapsed + 5))
        if (( elapsed % 30 == 0 )); then
            log "  Still waiting for servers... (${elapsed}s)"
        fi
    done

    if ! servers_healthy; then
        err "Servers failed to start within 600s"
        exit 1
    fi
fi

# --- Launch 5 parallel diff runs ---
log "=========================================="
log " Launching $NUM_RUNS parallel diff runs"
log " $ITERATIONS iters x $CANDIDATES candidates each"
log " Judge: $JUDGE_MODEL"
log " Output: $OUTPUT_BASE/"
log "=========================================="

mkdir -p "$OUTPUT_BASE"
START_TIME=$(date +%s)
PIDS=()

for i in $(seq 1 $NUM_RUNS); do
    run_dir="${OUTPUT_BASE}/run_${i}"
    log "Starting run $i -> $run_dir"

    uv run -m surf.cli.main run-diff \
        --rubric rubrics/model_diff.yaml \
        --attributes data/dolci-25k/pseudo_sae_attributes.jsonl \
        --model-a "http://localhost:${PORT_A}/v1:${MODEL_A}" \
        --model-b "http://localhost:${PORT_B}/v1:${MODEL_B}" \
        --model-a-name base --model-b-name sft \
        --judge-model "$JUDGE_MODEL" \
        --iterations "$ITERATIONS" \
        --candidates "$CANDIDATES" \
        --buffer-size 5 \
        --no-thinking \
        --target-concurrency 30 \
        --judge-concurrency 20 \
        --output-dir "$run_dir" \
        > "${run_dir}.log" 2>&1 &
    PIDS+=($!)
done

log "All $NUM_RUNS runs launched (PIDs: ${PIDS[*]})"
log "Logs: ${OUTPUT_BASE}/run_*.log"
log "Waiting for completion..."

# Wait for all runs
FAILED=0
for i in "${!PIDS[@]}"; do
    run_num=$((i + 1))
    if wait "${PIDS[$i]}"; then
        log "Run $run_num completed successfully"
    else
        err "Run $run_num failed (exit code $?)"
        FAILED=$((FAILED + 1))
    fi
done

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
MINUTES=$((ELAPSED / 60))
SECONDS=$((ELAPSED % 60))

echo ""
log "=========================================="
log " All runs finished in ${MINUTES}m ${SECONDS}s"
log " Failed: $FAILED / $NUM_RUNS"
log " Output: $OUTPUT_BASE/"
log "=========================================="
log ""
log "View results:"
log "  uv run utils/diff_top.py ${OUTPUT_BASE}/run_1 --n 10"
