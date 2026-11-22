#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== vkcapture-consumer setup ==="

# 1. Verify ffmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo "[!] Error: ffmpeg is not installed or not in PATH."
    echo "    Please install ffmpeg (e.g., sudo pacman -S ffmpeg, sudo apt install ffmpeg)."
    exit 1
fi
echo "[✓] ffmpeg found: $(command -v ffmpeg)"

# 2. Check Python version (>= 3.10 required)
PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "${PYTHON_BIN}" &>/dev/null; then
    echo "[!] Error: Python 3 not found."
    exit 1
fi

PY_VER=$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "[✓] Python version: ${PY_VER}"

# 3. Create virtual environment
VENV_DIR="${ROOT_DIR}/venv"
if [ ! -d "${VENV_DIR}" ]; then
    echo "[*] Creating virtual environment at ${VENV_DIR}..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
    echo "[✓] Existing virtual environment found at ${VENV_DIR}."
fi

# 4. Install package in editable mode
echo "[*] Upgrading pip and installing vkcapture-consumer in editable mode..."
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -e "${ROOT_DIR}"

echo ""
echo "=== Setup complete! ==="
echo "To activate environment and start capturing:"
echo "  source venv/bin/activate"
echo "  vkcapture --out ~/clips --clip-len 15"
