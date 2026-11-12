"""Configuración: valores por defecto <- config.conf<- GUI."""

import os
import re

DIR_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    # Red y señalización
    "IP_REMOTA": "", "PUERTO_SENAL": "5100", "ROL": "",
    "PUERTO_A_HACIA_B": "5000", "PUERTO_B_HACIA_A": "5001",
    "TIMEOUT_SENAL": "15", "TIEMPO_TIMBRADO": "30",
    # Captura
    "VIDEO_DISPOSITIVO": "/dev/video1", "VIDEO_FORMATO_ENTRADA": "mjpeg",
    "CAPTURA_RESOLUCION": "640x480", "CAPTURA_FPS": "30",
    "AUDIO_FORMATO": "alsa", "AUDIO_DISPOSITIVO": "default",
    # Codificación
    "RESOLUCION": "352x288", "FPS": "15", "VBITRATE": "300k", "QP": "28", "GOP": "15",
    "ABITRATE": "64k", "AFRECUENCIA": "48000",
    # Transporte
    "TRANSPORTE": "mpegts_udp", "TAMANO_PAQUETE": "1316",
    # Recepción
    "BAJA_LATENCIA": "1", "RX_FIFO_SIZE": "50000", "RX_BUFFER_SIZE": "1048576",
    "RX_PROBESIZE": "500000", "RX_ANALYZEDURATION": "1000000", "RX_SYNC": "audio",
    # Llamada y experimentos
    "ADOPTAR_PARAMETROS_REMOTOS": "1", "DURACION": "0", "TONO_ENTRANTE": "tonos/tono.mp3",
    "ID_PRUEBA": "", "DISTANCIA_M": "", "UBICACION": "", "CAPTURAR": "0",
}

# Parámetros que quien llama envía y quien contesta puede adoptar.
COMPARTIDOS = ["RESOLUCION", "FPS", "VBITRATE", "QP", "GOP", "ABITRATE", "AFRECUENCIA", "TRANSPORTE", "TAMANO_PAQUETE", "DURACION", "ID_PRUEBA", "CAPTURAR",
               "DISTANCIA_M"]

# CLAVE=valor  |  CLAVE="valor con espacios"   (seguido opcionalmente de # comentario)
_LINEA = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(?:"([^"]*)"|([^\s#]*))\s*(#.*)?$')


class Config(dict):
    def __init__(self, archivo=None):
        super().__init__(DEFAULTS)
        candidatos = [archivo] if archivo else [os.path.join(DIR_APP, n) for n in
                                                ("config.conf", "config.example.conf")]
        self.archivo = next((r for r in candidatos if os.path.isfile(r)), None)
        if archivo and not self.archivo:
            raise OSError(f"No existe el archivo {archivo}")
        if self.archivo:
            with open(self.archivo, encoding="utf-8") as fh:
                for n, linea in enumerate(fh, 1):
                    if not linea.strip() or linea.lstrip().startswith("#"):
                        continue
                    m = _LINEA.match(linea)
                    if not m or m.group(1) not in DEFAULTS:
                        raise ValueError(f"{self.archivo}:{n}: línea no válida o clave "
                                         f"desconocida: {linea.strip()}")
                    self[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)

    def entero(self, clave):
        try:
            return int(self[clave])
        except ValueError:
            raise ValueError(f"{clave} debe ser un número entero (valor: {self[clave]!r})")

    def booleano(self, clave):
        return self[clave].lower() in ("1", "si", "sí", "true", "yes")

    def resolucion(self, clave):
        m = re.fullmatch(r"(\d+)x(\d+)", self[clave])
        if not m:
            raise ValueError(f"{clave} debe tener el formato ANCHOxALTO (valor: {self[clave]!r})")
        return int(m.group(1)), int(m.group(2))

    def copia(self):
        nueva = Config.__new__(Config)
        dict.update(nueva, self)
        nueva.archivo = self.archivo
        return nueva

    def compartidos(self):
        return {k: self[k] for k in COMPARTIDOS}

    def puerto_rx(self, soy_llamante):
        """Puerto UDP local de recepción. A -> B usa PUERTO_A_HACIA_B, B -> A PUERTO_B_HACIA_A.
        Sin ROL, quien llama actúa como A y quien contesta como B."""
        rol = self["ROL"].upper() if self["ROL"].upper() in ("A", "B") else \
            ("A" if soy_llamante else "B")
        return self.entero("PUERTO_B_HACIA_A" if rol == "A" else "PUERTO_A_HACIA_B")
