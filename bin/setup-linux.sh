#!/usr/bin/env bash
# One-shot setup for a fresh Linux box (incl. WSL Ubuntu).
# Idempotent: safe to re-run. Installs apt prereqs, creates .venv,
# editable-installs cataanbot with dev+bridge extras, runs --help to verify.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PY_MIN="3.11"

need_apt=()
have() { command -v "$1" >/dev/null 2>&1; }

# Pick a python >= 3.11 from what's installed; prefer python3.12, then 3.11, then python3 if it qualifies.
pick_python() {
    for cand in python3.12 python3.11; do
        if have "$cand"; then echo "$cand"; return; fi
    done
    if have python3; then
        local v
        v="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
        if [ "$(printf '%s\n' "$PY_MIN" "$v" | sort -V | head -1)" = "$PY_MIN" ]; then
            echo python3; return
        fi
    fi
    echo ""
}

PY="$(pick_python)"

if [ -z "$PY" ]; then
    need_apt+=(python3.11 python3.11-venv python3.11-dev)
fi
have git || need_apt+=(git)

# python3-venv is needed even if python3 itself is present (Ubuntu splits
# it). `import venv` succeeds without it — the actual gate is ensurepip,
# which Ubuntu ships in the python3.x-venv package.
if [ -n "$PY" ] && ! "$PY" -m ensurepip --version >/dev/null 2>&1; then
    need_apt+=("${PY#python}-venv" "${PY#python}-dev")
fi

if [ "${#need_apt[@]}" -gt 0 ]; then
    if have apt-get; then
        echo ">> installing apt packages: ${need_apt[*]}"
        sudo apt-get update
        sudo apt-get install -y "${need_apt[@]}"
        PY="$(pick_python)"
    else
        echo "missing: ${need_apt[*]} — install them and re-run." >&2
        exit 1
    fi
fi

if [ -z "$PY" ]; then
    echo "could not find python >= $PY_MIN after install" >&2
    exit 1
fi

echo ">> using $PY ($($PY --version))"

# A failed prior run can leave a stub .venv/ with no bin/activate. Treat
# the activate script as the marker for "venv finished" and rebuild if
# missing.
if [ ! -f .venv/bin/activate ]; then
    if [ -d .venv ]; then
        echo ">> .venv is incomplete — rebuilding"
        rm -rf .venv
    fi
    echo ">> creating .venv"
    "$PY" -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate
python -m pip install -U pip wheel
pip install -e '.[dev,bridge]'

echo ">> verifying"
./bin/cataanbot --help >/dev/null
echo "ok — activate with: source .venv/bin/activate"
echo "run with:           ./bin/cataanbot --help"
