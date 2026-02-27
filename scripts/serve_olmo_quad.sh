#!/usr/bin/env bash
# serve_olmo_quad.sh — Launch 4 vLLM servers for OLMo3 base, SFT, r32, r64
#
# GPU allocation (8x B200, dp_size=2 each):
#   GPUs 0,1  port 8000  →  OLMo3 Base
#   GPUs 2,3  port 8001  →  OLMo3 Think SFT
#   GPUs 4,5  port 8002  →  OLMo3 Base + LoRA r32
#   GPUs 6,7  port 8003  →  OLMo3 Base + LoRA r64
#
# Usage:
#   bash scripts/serve_olmo_quad.sh           # all 4 models
#   bash scripts/serve_olmo_quad.sh --lora    # only r32 + r64 (ports 8002/8003)
# Stop: Ctrl-C (trap handles cleanup)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PETRI_DIR="${SCRIPT_DIR}/../../projects/olmo_peft/olmo_petri"

HEALTH_TIMEOUT=600  # 10 minutes

# vLLM binary: prefer olmo_peft venv, fall back to SURF venv
VLLM="${VLLM:-${SCRIPT_DIR}/../../projects/olmo_peft/.venv/bin/vllm}"
if [[ ! -x "$VLLM" ]]; then
    VLLM="${SCRIPT_DIR}/../.venv/bin/vllm"
fi

# Ensure ninja is on PATH
export PATH="${SCRIPT_DIR}/../.venv/bin:$PATH"

# Parse args
LORA_ONLY=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --lora) LORA_ONLY=true; shift ;;
        *) echo "Usage: $0 [--lora]"; exit 1 ;;
    esac
done

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[serve-quad]${NC} $*"; }
warn() { echo -e "${YELLOW}[serve-quad]${NC} $*"; }
err() { echo -e "${RED}[serve-quad]${NC} $*" >&2; }

# --- Cleanup stale processes ---
cleanup_stale() {
    log "Checking for stale vLLM processes..."
    local found=0
    local ports=(8000 8001 8002 8003)
    if $LORA_ONLY; then
        ports=(8002 8003)
    fi
    for port in "${ports[@]}"; do
        local pid
        pid=$(lsof -ti tcp:"$port" 2>/dev/null || true)
        if [[ -n "$pid" ]]; then
            warn "Killing stale process on port $port (PID: $pid)"
            kill -9 $pid 2>/dev/null || true
            found=1
        fi
    done
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
            kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        fi
    done
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

# --- Download LoRA adapters via olmo_petri's serve_model.sh logic ---
ADAPTER_DIR="${PETRI_DIR}/adapters"
download_adapter() {
    local name="$1"
    local hf_id="$2"
    local revision="$3"
    local dest="$ADAPTER_DIR/$name"

    if [[ -d "$dest" && -f "$dest/adapter_config.json" ]]; then
        log "Adapter '$name' already cached at $dest"
        return
    fi

    log "Downloading adapter '$name' from $hf_id (revision: $revision)..."
    mkdir -p "$dest"
    huggingface-cli download "$hf_id" --revision "$revision" --local-dir "$dest"
    log "Adapter '$name' downloaded to $dest"
}

# --- Main ---
cleanup_stale

CHAT_TEMPLATE="${SCRIPT_DIR}/chat_template.jinja"
MODEL_BASE="allenai/OLMo-3-1025-7B"
MODEL_SFT="allenai/OLMo-3-7B-Think-SFT"

WAIT_PIDS=()

if ! $LORA_ONLY; then
    log "Launching OLMo3 Base on GPUs 0,1 (port 8000)..."
    CUDA_VISIBLE_DEVICES=0,1 setsid "$VLLM" serve "$MODEL_BASE" \
        --data-parallel-size 2 \
        --port 8000 \
        --chat-template "$CHAT_TEMPLATE" \
        --enable-prefix-caching \
        --max-model-len 8192 \
        --dtype bfloat16 \
        > /tmp/vllm_base.log 2>&1 &
    PIDS+=($!)
    log "  PID: ${PIDS[-1]}, log: /tmp/vllm_base.log"

    log "Launching OLMo3 Think SFT on GPUs 2,3 (port 8001)..."
    CUDA_VISIBLE_DEVICES=2,3 setsid "$VLLM" serve "$MODEL_SFT" \
        --data-parallel-size 2 \
        --port 8001 \
        --enable-prefix-caching \
        --max-model-len 16384 \
        --dtype bfloat16 \
        > /tmp/vllm_sft.log 2>&1 &
    PIDS+=($!)
    log "  PID: ${PIDS[-1]}, log: /tmp/vllm_sft.log"

    wait_for_health 8000 "OLMo3 Base" &
    WAIT_PIDS+=($!)
    wait_for_health 8001 "OLMo3 Think SFT" &
    WAIT_PIDS+=($!)
fi

# Download LoRA adapters
download_adapter "r32" "GaloisTheory123/olmo3-7b-sampled-lora" "r32-a64"
download_adapter "r64" "GaloisTheory123/olmo3-7b-sampled-lora" "main"

log "Launching OLMo3 Base + LoRA r32 on GPUs 4,5 (port 8002)..."
CUDA_VISIBLE_DEVICES=4,5 setsid "$VLLM" serve "$MODEL_BASE" \
    --data-parallel-size 2 \
    --port 8002 \
    --chat-template "$CHAT_TEMPLATE" \
    --enable-prefix-caching \
    --max-model-len 8192 \
    --dtype bfloat16 \
    --enable-lora \
    --lora-modules "r32-lora=$ADAPTER_DIR/r32" \
    --max-lora-rank 64 \
    > /tmp/vllm_r32.log 2>&1 &
PIDS+=($!)
log "  PID: ${PIDS[-1]}, log: /tmp/vllm_r32.log"

log "Launching OLMo3 Base + LoRA r64 on GPUs 6,7 (port 8003)..."
CUDA_VISIBLE_DEVICES=6,7 setsid "$VLLM" serve "$MODEL_BASE" \
    --data-parallel-size 2 \
    --port 8003 \
    --chat-template "$CHAT_TEMPLATE" \
    --enable-prefix-caching \
    --max-model-len 8192 \
    --dtype bfloat16 \
    --enable-lora \
    --lora-modules "r64-lora=$ADAPTER_DIR/r64" \
    --max-lora-rank 64 \
    > /tmp/vllm_r64.log 2>&1 &
PIDS+=($!)
log "  PID: ${PIDS[-1]}, log: /tmp/vllm_r64.log"

wait_for_health 8002 "OLMo3 LoRA r32" &
WAIT_PIDS+=($!)
wait_for_health 8003 "OLMo3 LoRA r64" &
WAIT_PIDS+=($!)

# Wait for all health checks
for wpid in "${WAIT_PIDS[@]}"; do
    wait "$wpid" || { err "A model failed to start"; exit 1; }
done

echo ""
log "=========================================="
if $LORA_ONLY; then
    log " LoRA models ready! (r32 + r64)"
else
    log " All 4 models ready!"
fi
log "=========================================="
log ""
if ! $LORA_ONLY; then
    log "Base:      http://localhost:8000/v1"
    log "Think SFT: http://localhost:8001/v1"
fi
log "LoRA r32:  http://localhost:8002/v1"
log "LoRA r64:  http://localhost:8003/v1"
log ""
log "Press Ctrl-C to stop all servers"

# Wait for any server to exit
wait -n "${PIDS[@]}" 2>/dev/null || true
err "A vLLM server exited unexpectedly"
exit 1
