"""Punto de entrada: python3 -m vc [-c archivo.conf]  (normalmente vía videoconferencia.sh)."""

import argparse
import sys

from .config import Config
from .gui import App

p = argparse.ArgumentParser(prog="videoconferencia.sh",
                            description="Videoconferencia bidireccional de audio y video con FFmpeg.")
p.add_argument("-c", "--config", help="archivo de configuración "
                                      "(por defecto config.conf o config.example.conf)")
try:
    cfg = Config(p.parse_args().config)
except (OSError, ValueError) as e:
    sys.exit(f"Error en la configuración: {e}")
App(cfg).root.mainloop()
