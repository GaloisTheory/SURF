#!/usr/bin/env bash
# serve_olmo_pair.sh — Launch vLLM servers for OLMo3 base + SFT models
#
# GPU allocation (8x B200):
#   GPUs 0-3  port 8000  →  OLMo3 Base  (dp_size=4, with chat template)
#   GPUs 4-7  port 8001  →  OLMo3 Think SFT (dp_size=4)
#
# Usage: bash scripts/serve_olmo_pair.sh
# Stop:  Ctrl-C (trap handles cleanup)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODEL_A="allenai/OLMo-3-1025-7B"
MODEL_B="allenai/OLMo-3-7B-Think-SFT"
PORT_A=8000
PORT_B=8001
CHAT_TEMPLATE="${SCRIPT_DIR}/chat_template.jinja"
HEALTH_TIMEOUT=600  # 10 minutes

# vLLM binary from SURF's own venv (Python 3.10)
VLLM="${VLLM:-${SCRIPT_DIR}/../.venv/bin/vllm}"

# Ensure ninja (needed by FlashInfer JIT compilation) is on PATH
export PATH="${SCRIPT_DIR}/../.venv/bin:$PATH"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[serve]${NC} $*"; }
warn() { echo -e "${YELLOW}[serve]${NC} $*"; }
err() { echo -e "${RED}[serve]${NC} $*" >&2; }

# --- Phase 0: Cleanup stale processes ---
cleanup_stale() {
    log "Checking for stale vLLM processes..."
    local found=0
    for port in $PORT_A $PORT_B; do
        local pid
        pid=$(lsof -ti tcp:"$port" 2>/dev/null || true)
        if [[ -n "$pid" ]]; then
            warn "Killing stale process on port $port (PID: $pid)"
            kill -9 $pid 2>/dev/null || true
            found=1
        fi
    done
    # Kill any lingering vLLM engine processes
    if pkill -0 -f "VLLM::" 2>/dev/null; then
        warn "Killing stale VLLM engine processes"
        pkill -9 -f "VLLM::" 2>/dev/null || true
        found=1
    fi
    if [[ $found -eq 1 ]]; then
        sleep 2
        log "Stale processes cleaned up"
    fi
}

# --- Trap-based cleanup ---
PIDS=()
cleanup() {
    log "Shutting down vLLM servers..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            # Kill the entire process group
            kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        fi
    done
    # Final sweep for engine processes
    pkill -9 -f "VLLM::" 2>/dev/null || true
    log "Cleanup complete"
}
trap cleanup EXIT INT TERM

# --- Health check ---
wait_for_health() {
    local port=$1
    local name=$2
    local elapsed=0
    log "Waiting for $name (port $port) to be healthy..."
    while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
        if curl -s "http://localhost:${port}/health" > /dev/null 2>&1; then
            log "$name is healthy! (${elapsed}s)"
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
        if (( elapsed % 30 == 0 )); then
            log "  Still waiting for $name... (${elapsed}s)"
        fi
    done
    err "$name failed to start within ${HEALTH_TIMEOUT}s"
    return 1
}

# --- Main ---
cleanup_stale

log "Launching OLMo3 Base on GPUs 0-3 (port $PORT_A)..."
CUDA_VISIBLE_DEVICES=0,1,2,3 setsid "$VLLM" serve "$MODEL_A" \
    --data-parallel-size 4 \
    --port "$PORT_A" \
    --chat-template "$CHAT_TEMPLATE" \
    --enable-prefix-caching \
    --max-model-len 8192 \
    --dtype bfloat16 \
    > /tmp/vllm_model_a.log 2>&1 &
PIDS+=($!)
log "  PID: ${PIDS[-1]}, log: /tmp/vllm_model_a.log"

log "Launching OLMo3 Think SFT on GPUs 4-7 (port $PORT_B)..."
CUDA_VISIBLE_DEVICES=4,5,6,7 setsid "$VLLM" serve "$MODEL_B" \
    --data-parallel-size 4 \
    --port "$PORT_B" \
    --enable-prefix-caching \
    --max-model-len 16384 \
    --dtype bfloat16 \
    > /tmp/vllm_model_b.log 2>&1 &
PIDS+=($!)
log "  PID: ${PIDS[-1]}, log: /tmp/vllm_model_b.log"

log "Waiting for both servers to be healthy..."
wait_for_health $PORT_A "OLMo3 Base" &
WAIT_A=$!
wait_for_health $PORT_B "OLMo3 Think SFT" &
WAIT_B=$!

wait $WAIT_A || { err "OLMo3 Base failed to start"; exit 1; }
wait $WAIT_B || { err "OLMo3 Think SFT failed to start"; exit 1; }

echo ""
log "=========================================="
log " Both models ready!"
log "=========================================="
log ""
log "Model A (Base):      http://localhost:${PORT_A}/v1"
log "Model B (Think SFT): http://localhost:${PORT_B}/v1"
log ""
log "Copy-paste for smoke test:"
log ""
echo "cd projects/SURF && uv run -m surf.cli.main run-diff \\"
echo "    --rubric rubrics/model_diff.yaml \\"
echo "    --attributes data/dolci-25k/pseudo_sae_attributes.jsonl \\"
echo "    --model-a \"http://localhost:${PORT_A}/v1:${MODEL_A}\" \\"
echo "    --model-b \"http://localhost:${PORT_B}/v1:${MODEL_B}\" \\"
echo "    --model-a-name base --model-b-name sft \\"
echo "    --iterations 3 --candidates 20 \\"
echo "    --output-dir results/olmo3_diff_smoke"
log ""
log "Press Ctrl-C to stop both servers"

# Wait for either server to exit
wait -n "${PIDS[@]}" 2>/dev/null || true
err "A vLLM server exited unexpectedly"
exit 1
