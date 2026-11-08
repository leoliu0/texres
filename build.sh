#!/bin/sh
set -eu

# Pass `--parity-gate` (or set `TEXRES_PARITY_GATE=1`) to run the 100-document
# TeX Live parity, similarity threshold, and performance gate after building.
if [ "${1:-}" = "--parity-gate" ]; then
    shift
    cargo build --release -p tex-cli --bin texres "$@"
    exec python3 scripts/bench100.py \
        --texres target/release/texres \
        --gate \
        --jobs "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
fi

cargo build -p tex-cli "$@"
if [ "${TEXRES_PARITY_GATE:-0}" = "1" ]; then
    bin="target/release/texres"
    [ -x "$bin" ] || bin="target/debug/texres"
    python3 scripts/bench100.py \
        --texres "$bin" \
        --gate \
        --jobs "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
fi
