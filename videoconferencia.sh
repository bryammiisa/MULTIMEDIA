#!/usr/bin/env bash
# Videoconferencia bidireccional de audio y video con FFmpeg (mismo programa en ambas laptops).
#   ./videoconferencia.sh [-c archivo.conf]
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

error() { echo "ERROR: $*" >&2; exit 1; }
for b in python3 ffmpeg ffplay; do
    command -v "$b" >/dev/null || error "Falta $b. Instale con: sudo apt install ffmpeg python3"
done
[[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] || error "No hay sesión gráfica (DISPLAY)."
python3 -c "import tkinter" 2>/dev/null || error "Falta Tkinter: sudo apt install python3-tk"

cd "$DIR"
exec python3 -m vc "$@"
