"""Comandos FFmpeg/FFplay
---------------------------
    ffmpeg [opciones globales] [-opciones de ENTRADA -i origen]... [opciones de SALIDA] destino

Las opciones se aplican al archivo que viene DESPUÉS de ellas:
  * antes de un "-i"  -> configuran esa entrada (formato, fps, resolución...)
  * antes del destino -> configuran la salida (códec, bitrate, contenedor...)

Por eso cada comando se arma aquí en bloques con nombre, un par "opción, valor" por línea.
"""

import os
import re
import shlex
import signal
import subprocess
import threading

from .config import DIR_APP

# nombre: (muxer de FFmpeg, esquema de la URL de red)
TRANSPORTES = {
    "mpegts_udp": ("mpegts", "udp"),
    "rtp_mpegts": ("rtp_mpegts", "rtp"),
}
VCODEC, ACODEC = "libx264", "aac"          # codecs fijos (H.264 y AAC)


# ──────────────────────── Construcción de comandos ────────────────────────

def _plano(*bloques):
    """Une bloques de pares (opción, valor) en una sola lista de argumentos."""
    return [arg for bloque in bloques for par in bloque for arg in par]


def comando_transmisor(cfg, ip, puerto):
    """ffmpeg: cámara + micrófono -> H.264/AAC -> MPEG-TS -> red."""
    ancho, alto = cfg.resolucion("RESOLUCION")
    muxer, esquema = TRANSPORTES[cfg["TRANSPORTE"]]
    vbitrate = cfg["VBITRATE"]
    url = f"{esquema}://{ip}:{puerto}?pkt_size={cfg['TAMANO_PAQUETE']}"

    # 1) Opciones globales
    globales = [("ffmpeg", "-hide_banner"), ("-nostats",)]

    # 2) Entrada 0: cámara (V4L2)
    entrada_video = [
        ("-thread_queue_size", "512"),            # cola de paquetes antes de procesar
        ("-f", "v4l2"),                           # tipo de dispositivo de entrada
    ]
    if cfg["VIDEO_FORMATO_ENTRADA"] != "auto":
        entrada_video.append(("-input_format", cfg["VIDEO_FORMATO_ENTRADA"]))
    entrada_video += [
        ("-framerate", cfg["CAPTURA_FPS"]),       # fps que pide a la cámara
        ("-video_size", cfg["CAPTURA_RESOLUCION"]),
        ("-i", cfg["VIDEO_DISPOSITIVO"]),
    ]

    # 3) Entrada 1: micrófono
    entrada_audio = [
        ("-thread_queue_size", "1024"),
        ("-f", cfg["AUDIO_FORMATO"]),             # p. ej. alsa, pulse
        ("-i", cfg["AUDIO_DISPOSITIVO"]),
    ]

    # 4) Qué flujos van a la salida (entrada:tipo:índice)
    mapeo = [
        ("-map", "0:v:0"),                        # video de la entrada 0
        ("-map", "1:a:0"),                        # audio de la entrada 1
    ]

    # 5) Filtros de video: fps -> escala -> formato de píxel compatible
    filtros = [
        ("-vf", f"fps={cfg['FPS']},scale={ancho}:{alto},format=yuv420p"),
    ]

        # 6) Codificación de video (H.264, baja latencia)
    if cfg["QP"]:
        control_tasa = [("-qp", cfg["QP"])]       # cuantizador constante: calidad fija, bitrate variable
    else:
        control_tasa = [
            ("-b:v", vbitrate),                   # bitrate objetivo
            ("-maxrate", vbitrate),               # tope -> bitrate casi constante
            ("-bufsize", vbitrate),               # buffer del control de tasa
        ]
    codec_video = [
        ("-c:v", VCODEC),
        *control_tasa,
        ("-g", cfg["GOP"]),                       # un fotograma clave cada N cuadros
        ("-preset", "ultrafast"),
        ("-tune", "zerolatency"),
        ("-profile:v", "baseline"),
    ]

    # 7) Codificación de audio (AAC mono)
    codec_audio = [
        ("-c:a", ACODEC),
        ("-b:a", cfg["ABITRATE"]),
        ("-ar", cfg["AFRECUENCIA"]),              # frecuencia de muestreo
        ("-ac", "1"),                             # canales
    ]

    # 8) Contenedor y salida a la red
    salida = [
        ("-f", muxer),
        ("-flush_packets", "1"),                  # envía cada paquete sin acumular
        ("-muxdelay", "0"),
        ("-muxpreload", "0"),
        (url,),
    ]

    return _plano(globales, entrada_video, entrada_audio, mapeo,
                  filtros, codec_video, codec_audio, salida)


    """ffmpeg -hide_banner -nostats \
  -thread_queue_size 512 -f v4l2 \
  -framerate 30 -video_size 640x480 -i /dev/video0 \
  -thread_queue_size 1024 -f alsa -i default \
  -map 0:v:0 -map 1:a:0 \
  -vf fps=15,scale=640:360,format=yuv420p \
  -c:v libx264 -b:v 500k -maxrate 500k -bufsize 500k -g 30 \
  -preset ultrafast -tune zerolatency -profile:v baseline \
  -c:a aac -b:a 32k -ar 16000 -ac 1 \
  -f mpegts -flush_packets 1 -muxdelay 0 -muxpreload 0 \
  'udp://192.168.1.20:5004?pkt_size=1316'"""

"Cuando trabaja con QP"
"-c:v libx264 -qp 28 -g 15 -preset ultrafast -tune zerolatency -profile:v baseline"

def comando_receptor(cfg, puerto, transporte, titulo):
    """ffplay: red -> decodificación -> ventana de video + audio."""
    esquema = TRANSPORTES[transporte][1]
    url = (f"{esquema}://0.0.0.0:{puerto}?overrun_nonfatal=1"
           f"&fifo_size={cfg['RX_FIFO_SIZE']}&buffer_size={cfg['RX_BUFFER_SIZE']}")

    globales = [("ffplay", "-hide_banner"), ("-window_title", titulo)]

    analisis = [                                  # cuánto analiza antes de mostrar
        ("-probesize", cfg["RX_PROBESIZE"]),
        ("-analyzeduration", cfg["RX_ANALYZEDURATION"]),
        ("-sync", cfg["RX_SYNC"]),
    ]

    baja_latencia = []
    if cfg.booleano("BAJA_LATENCIA"):
        baja_latencia = [
            ("-fflags", "nobuffer+discardcorrupt"),
            ("-flags", "low_delay"),
            ("-framedrop",),
        ]

    return _plano(globales, analisis, baja_latencia, [("-i", url)])



    """ffplay -hide_banner -window_title Llamada \
  -probesize 32768 -analyzeduration 500000 -sync audio \
  -fflags nobuffer+discardcorrupt -flags low_delay -framedrop \
  -i 'udp://0.0.0.0:5004?overrun_nonfatal=1&fifo_size=1000000&buffer_size=1048576'"""


def comando_captura(interfaz, puertos, puerto_senal, archivo, comentario):
    """dumpcap (incluido con tshark) limitado a los puertos del proyecto."""
    filtro = " or ".join([f"udp port {p}" for p in puertos] + [f"tcp port {puerto_senal}"])
    return _plano([
        ("dumpcap", "-q"),
        ("-i", interfaz),
        ("-f", filtro),                           # filtro BPF
        ("-w", archivo),
        ("--capture-comment", comentario),
    ])


def puertos_udp(puerto, transporte):
    """Puertos que ocupa el receptor: RTP usa además el siguiente para RTCP."""
    return [puerto, puerto + 1] if transporte == "rtp_mpegts" else [puerto]


def texto(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


# ───────────────────────── Diagnóstico de errores ─────────────────────────

# Traducción de mensajes de FFmpeg a explicaciones claras.
_ERRORES = [
    ("permission to capture", "dumpcap no tiene permiso para capturar: ejecute 'sudo dpkg-reconfigure wireshark-common' (responda Sí), 'sudo usermod -aG wireshark $USER' y cierre e inicie sesión"),
    ("Device or resource busy", "dispositivo ocupado por otra aplicación"),
    ("Cannot open video device", "no se pudo abrir la cámara"),
    ("No such file or directory", "no existe el dispositivo o archivo indicado"),
    ("annot open audio device", "no se pudo abrir el micrófono"),
    ("Address already in use", "el puerto ya está en uso por otro proceso"),
    ("bind failed", "no se pudo abrir el puerto local (¿ocupado?)"),
    ("Network is unreachable", "red inalcanzable (¿Wi-Fi desconectado?)"),
    ("No route to host", "no hay ruta hacia el equipo remoto"),
    ("Unknown encoder", "FFmpeg no tiene ese codificador"),
    ("Could not write header", "el contenedor no acepta esa combinación de códecs"),
    ("Permission denied", "permiso denegado (revise los grupos 'video', 'audio', 'wireshark')"),
]


def explicar_error(linea):
    return next((exp for patron, exp in _ERRORES if patron in linea), "")


# ───────────────────────────── Procesos hijos ─────────────────────────────

class Proceso:
    """Proceso hijo con lectura de stderr y cierre ordenado."""

    def __init__(self, nombre, cmd, al_linea, al_terminar):
        self.nombre, self.cmd = nombre, cmd
        self.al_linea, self.al_terminar = al_linea, al_terminar
        self.ultimas = []             # últimas líneas de stderr, para diagnosticar
        self.error = ""               # primer error reconocido en stderr
        self.abrio_entrada = False    # ffplay llegó a abrir el flujo ("Input #0")
        self.deteniendo = False
        try:
            self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True, errors="replace",
                                      start_new_session=True)
        except OSError as e:
            raise RuntimeError(f"No se pudo ejecutar '{cmd[0]}' ({e.strerror})")
        threading.Thread(target=self._leer, daemon=True).start()

    def _leer(self):
        for linea in self.p.stderr:       # en modo texto '\r' también separa líneas
            linea = linea.rstrip()
            if not linea:
                continue
            self.ultimas = (self.ultimas + [linea])[-15:]
            self.abrio_entrada |= linea.startswith("Input #")
            self.error = self.error or explicar_error(linea)
            self.al_linea(self.nombre, linea)
        self.p.wait()
        self.al_terminar(self)

    def diagnostico(self):
        ultima = next((l for l in reversed(self.ultimas)
                       if "rror" in l or "fail" in l or "annot" in l),
                      self.ultimas[-1] if self.ultimas else "")
        return " — ".join(x for x in (f"{self.nombre} terminó (código {self.p.returncode})",
                                      self.error, ultima and f"FFmpeg: {ultima}") if x)

    def detener(self):
        """'q' por stdin (FFmpeg cierra limpio y libera dispositivos) -> SIGINT -> SIGTERM
        -> SIGKILL, esperando entre cada paso."""
        self.deteniendo = True
        for sig in (None, signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            if self.p.poll() is not None:
                return
            try:
                if sig is None:
                    if self.cmd[0] != "ffmpeg":
                        continue
                    self.p.stdin.write("q\n")
                    self.p.stdin.flush()
                else:
                    os.killpg(self.p.pid, sig)
                self.p.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass


# ───────────────────────────── Alerta audible ─────────────────────────────

class Alerta:
    """Tono en bucle con ffplay: TONO_ENTRANTE al recibir una llamada y un tono
    generado de 425 Hz (1 s sonando, 3 s en silencio) al llamar."""

    SALIENTE = os.path.join(DIR_APP, "tonos", ".tono_saliente.wav")

    def __init__(self, cfg, log):
        self.cfg, self.log, self._p = cfg, log, None

    def archivo(self, tipo):
        if tipo == "entrante":
            return os.path.join(DIR_APP, self.cfg["TONO_ENTRANTE"])
        if not os.path.isfile(self.SALIENTE):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "aevalsrc=exprs='0.35*sin(2*PI*425*t)*lt(mod(t,4),1)':s=44100:d=4",
                            self.SALIENTE], capture_output=True)
        return self.SALIENTE

    def iniciar(self, tipo):
        self.detener()
        archivo = self.archivo(tipo)
        if not os.path.isfile(archivo):
            self.log("error", f"No se puede reproducir la alerta: no existe {archivo}")
            return
        self._p = subprocess.Popen(["ffplay", "-nodisp", "-loglevel", "quiet", "-loop", "0",
                                    archivo], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.log("info", f"Alerta audible ({tipo}): {os.path.basename(archivo)}")

    def detener(self):
        p, self._p = self._p, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()




 # ----------------------------------- Validacion ---------------------------

def validar(cfg):
    """Lanza ValueError si algún parámetro de transmisión no es válido."""
    if cfg["TRANSPORTE"] not in TRANSPORTES:
        raise ValueError(f"TRANSPORTE debe ser uno de: {', '.join(TRANSPORTES)}")
    cfg.resolucion("RESOLUCION")
    cfg.resolucion("CAPTURA_RESOLUCION")
    for k in ("FPS", "CAPTURA_FPS", "AFRECUENCIA", "TAMANO_PAQUETE"):
        if cfg.entero(k) <= 0:
            raise ValueError(f"{k} debe ser mayor que cero")
    if not 1 <= cfg.entero("GOP") <= 250:
        raise ValueError("GOP debe ser un entero entre 1 y 250")
    qp = cfg["QP"]
    if qp and not (qp.isdigit() and 1 <= int(qp) <= 51):
        raise ValueError("QP debe estar entre 1 y 51 (o vacío para usar VBITRATE)")
    if cfg["CAPTURAR"] not in ("0", "1"):
        raise ValueError("CAPTURAR debe ser 0 o 1")
    if cfg.entero("DURACION") < 0:
        raise ValueError("DURACION no puede ser negativa")
    for k, minimo, maximo in (("VBITRATE", 10, 3000), ("ABITRATE", 8, 64)):
        m = re.fullmatch(r"(\d+)[kK]?", cfg[k])        # kbit/s, con o sin sufijo k
        if not m or not minimo <= int(m.group(1)) <= maximo:
            raise ValueError(f"{k} debe estar entre {minimo}k y {maximo}k (valor: {cfg[k]!r})")
        cfg[k] = f"{m.group(1)}k"
               