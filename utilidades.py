"""Red, Wi-Fi, puertos y verificación del equipo."""

import ipaddress
import os
import socket
import subprocess

from . import medios


def ejecutar(cmd, timeout=8):
    """Devuelve (código, salida) sin lanzar excepciones."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def validar_ip(texto):
    try:
        ip = ipaddress.IPv4Address(texto.strip())
    except ValueError:
        raise ValueError(f"'{texto.strip()}' no es una dirección IPv4 válida (ej. 192.168.1.11).")
    if not ip.is_private:
        raise ValueError(f"{ip} no es una IP privada: el experimento se limita a la red local.")
    return str(ip)


def ruta_hacia(ip):
    """(interfaz, ip_local) usados para llegar a ip, según `ip route get`."""
    p = ejecutar(["ip", "-o", "route", "get", ip])[1].split()
    dato = lambda k: p[p.index(k) + 1] if k in p else ""
    return dato("dev"), dato("src")


def ips_locales():
    """[(interfaz, ip)] IPv4 distintas de loopback."""
    out = ejecutar(["ip", "-o", "-4", "addr", "show"])[1]
    return [(c[1], c[3].split("/")[0]) for c in map(str.split, out.splitlines())
            if len(c) > 3 and c[1] != "lo"]


def info_wifi():
    """SSID, canal, frecuencia, banda y señal (%) de la red Wi-Fi activa (nmcli)."""
    out = ejecutar(["nmcli", "-t", "-f", "ACTIVE,SSID,CHAN,FREQ,SIGNAL",
                    "dev", "wifi", "list", "--rescan", "no"])[1]
    for linea in out.splitlines():
        c = [x.replace("\0", ":") for x in linea.replace("\\:", "\0").split(":")]
        if len(c) == 5 and c[0] in ("yes", "sí", "si"):
            mhz = int(c[3].split()[0]) if c[3].split() and c[3].split()[0].isdigit() else 0
            banda = "2.4 GHz" if mhz < 3000 else "5 GHz" if mhz < 5950 else "6 GHz"
            return {"ssid": c[1], "canal": c[2], "frecuencia": c[3],
                    "banda": banda if mhz else "", "senal_pct": c[4]}
    return {}


def puerto_udp_libre(puerto):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("0.0.0.0", puerto))
            return True
        except OSError:
            return False


def verificar_entorno(cfg):
    """Lista de (ok, mensaje); ok=None es una advertencia."""
    r = [(True, ejecutar(["ffmpeg", "-version"])[1].split("\n")[0])]
    dev = cfg["VIDEO_DISPOSITIVO"]
    if not os.path.exists(dev):
        r.append((False, f"No existe la cámara {dev}. Revise: v4l2-ctl --list-devices"))
    else:
        fmt = [] if cfg["VIDEO_FORMATO_ENTRADA"] == "auto" else \
            ["-input_format", cfg["VIDEO_FORMATO_ENTRADA"]]
        cod, out = ejecutar(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", *fmt,
                             "-video_size", cfg["CAPTURA_RESOLUCION"], "-i", dev,
                             "-frames:v", "1", "-f", "null", "-"])
        r.append((cod == 0, f"Cámara {dev} OK ({cfg['CAPTURA_RESOLUCION']})" if cod == 0 else
                  f"No se pudo abrir la cámara {dev}: {medios.explicar_error(out) or out}"))
    mic = f"{cfg['AUDIO_FORMATO']}:{cfg['AUDIO_DISPOSITIVO']}"
    cod, out = ejecutar(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", cfg["AUDIO_FORMATO"],
                         "-i", cfg["AUDIO_DISPOSITIVO"], "-t", "0.3", "-f", "null", "-"])
    r.append((cod == 0, f"Micrófono {mic} OK" if cod == 0 else
              f"No se pudo abrir el micrófono {mic}: {medios.explicar_error(out) or out}. "
              "Revise: arecord -l"))
    tono = medios.Alerta(cfg, None).archivo("entrante")
    r.append((os.path.isfile(tono), f"Tono de llamada entrante: {os.path.basename(tono)}"
              if os.path.isfile(tono) else f"No existe el tono {tono} (TONO_ENTRANTE)"))
    for clave in ("PUERTO_A_HACIA_B", "PUERTO_B_HACIA_A"):
        p = cfg.entero(clave)
        r.append((True, f"Puerto UDP {p} libre") if puerto_udp_libre(p) else
                 (None, f"Puerto UDP {p} ocupado por otro proceso"))
    if cfg.booleano("CAPTURAR"):
        cod, out = ejecutar(["dumpcap", "-L", "-i", "any"])   # abre la interfaz: exige permiso
        r.append((cod == 0, "dumpcap puede capturar paquetes" if cod == 0 else
                  "CAPTURAR=1 pero no se puede capturar: " +
                  (medios.explicar_error(out) or out[-200:])))
    return r
