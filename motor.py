"""Motor de la llamada: señalización, procesos FFmpeg y registro.

Estados:  LIBRE -> LLAMANDO -> EN_LLAMADA -> FINALIZANDO -> LIBRE   (quien llama)
          LIBRE -> ENTRANTE -> EN_LLAMADA -> FINALIZANDO -> LIBRE   (quien contesta)

La GUI recibe eventos con notificar(evento, datos), invocado desde cualquier hilo:
  "estado" {estado, texto, inicio} | "entrante" {ip, nombre} | "log" {nivel, texto}
  | "fin" {motivo, error}
Cada llamada tiene un número de generación (_gen); al terminar se incrementa, lo que
invalida temporizadores e hilos que aún pertenezcan a la llamada anterior.
"""

import csv
import json
import os
import re
import socket
import threading
import time

from . import medios
from .config import COMPARTIDOS, DIR_APP
from .senal import Canal
from .utilidades import info_wifi, puerto_udp_libre, ruta_hacia, validar_ip

LIBRE, LLAMANDO, ENTRANTE, EN_LLAMADA, FINALIZANDO = (
    "libre", "llamando", "entrante", "en_llamada", "finalizando")
DIR_LOGS = os.path.join(DIR_APP, "logs")
DIR_CAPTURAS = os.path.join(DIR_APP, "datos", "capturas")
FIN_REMOTO = {"OCUPADO": "El otro equipo está ocupado en otra llamada",
              "RECHAZAR": "El otro equipo no aceptó la llamada",
              "CANCELAR": "El otro equipo canceló la llamada",
              "COLGAR": "El otro equipo colgó"}
CAMPOS = [
    "id_llamada", "id_prueba", "inicio", "fin", "duracion_s", "rol",
    "equipo_local", "ip_local", "interfaz", "equipo_remoto", "ip_remota",
    "puerto_tx_destino", "puerto_rx_local", "transporte_tx", "transporte_rx",
    "ssid", "banda", "canal", "frecuencia", "senal_pct", "distancia_m", "ubicacion",
    "resolucion", "fps", "gop", "vcodec", "vbitrate", "qp", "acodec", "abitrate", "afrecuencia",
    "tamano_paquete", "motivo_fin", "error", "archivo_log", "archivo_captura",
    "cmd_tx", "cmd_rx", "observaciones", "valoracion_subjetiva",
]


def _id_seguro(texto):
    """Identificador apto para nombres de archivo (también si viene del otro equipo)."""
    return re.sub(r"[^\w.-]+", "_", texto)[:80]


class _Cancelado(Exception):
    """La llamada terminó mientras se estaba estableciendo."""


class Motor:
    def __init__(self, cfg, notificar):
        self.cfg, self.notificar = cfg, notificar
        self.nombre = socket.gethostname()
        self.estado = LIBRE
        self._lock = threading.RLock()
        self._gen = 0
        self._servidor = self.canal = None
        self.ll = {}                  # datos de la llamada en curso
        self.c = None                 # copia de la configuración usada en la llamada
        self.tx = self.rx = self.cap = None
        self._ultimo_rx = self._ultimo_estado = 0.0
        self._log_llamada = None
        os.makedirs(DIR_LOGS, exist_ok=True)
        self._log_app = open(os.path.join(DIR_LOGS, "videoconferencia.log"), "a",
                             encoding="utf-8", buffering=1)
        self.alerta = medios.Alerta(cfg, self._log)

    # ------------------------------------------------------------------ util
    def _log(self, nivel, texto, ui=True):
        linea = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{nivel.upper()}] {texto}\n"
        for f in (self._log_app, self._log_llamada):
            if f and not f.closed:
                f.write(linea)
        if ui:
            self.notificar("log", {"nivel": nivel, "texto": texto})

    def _linea_proceso(self, nombre, linea):
        # La línea de estado de ffplay ("12.3 A-V: 0.010 ...") llega varias veces por
        # segundo: se guarda una vez por segundo como medida de sincronización.
        if "A-V:" in linea:
            if time.time() - self._ultimo_estado < 1:
                return
            self._ultimo_estado = time.time()
            linea = "ESTADO " + linea.strip()
        f = self._log_llamada
        if f and not f.closed:
            f.write(f"{time.strftime('%H:%M:%S')} [{nombre}] {linea}\n")

    def _estado(self, estado, texto, **extra):
        self.estado = estado
        self.notificar("estado", dict(extra, estado=estado, texto=texto))

    @staticmethod
    def _hilo(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _temporizar(self, segundos, gen, estado, motivo):
        """Finaliza la llamada tras `segundos` si sigue siendo la misma y en `estado`."""
        def expirar():
            if gen == self._gen and self.estado == estado:
                self.finalizar(motivo)
        t = threading.Timer(segundos, expirar)
        t.daemon = True
        t.start()

    # ------------------------------------------------------------- servidor
    def iniciar(self):
        puerto = self.cfg.entero("PUERTO_SENAL")
        self._servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._servidor.bind(("0.0.0.0", puerto))
        except OSError as e:
            raise RuntimeError(f"No se pudo abrir el puerto TCP {puerto} para recibir llamadas "
                               f"({e.strerror}). ¿Hay otra instancia de la aplicación abierta?")
        self._servidor.listen(4)
        self._hilo(self._aceptar)
        self._log("info", f"Escuchando llamadas en TCP {puerto} (equipo {self.nombre})")

    def _aceptar(self):
        while True:
            try:
                conn, _ = self._servidor.accept()
            except OSError:
                return                                    # servidor cerrado
            Canal(conn, self._al_mensaje, self._al_cerrar).iniciar_lectura()

    # --------------------------------------------------------- acciones GUI
    def llamar(self, ip):
        """Lanza ValueError/RuntimeError si no se puede llamar."""
        ip = validar_ip(ip)
        with self._lock:
            if self.estado != LIBRE:
                raise RuntimeError("Ya hay una llamada en curso.")
            self._gen += 1
            self.c = self.cfg.copia()
            self.ll = {"rol": "llamante", "ip_remota": ip, "equipo_remoto": ip,
                       "puerto_rx_local": self.c.puerto_rx(soy_llamante=True),
                       "id_llamada": _id_seguro(f"{time.strftime('%Y%m%d_%H%M%S')}_"
                                                f"{self.c['ID_PRUEBA'] or 'llamada'}")}
            self._estado(LLAMANDO, f"Conectando con {ip}...")
            self._hilo(self._llamar, ip, self._gen)

    def _llamar(self, ip, gen):
        c = self.c
        error = self._error_previo(self.ll["puerto_rx_local"], c["TRANSPORTE"])
        if not error:
            try:
                canal = Canal.conectar(ip, c.entero("PUERTO_SENAL"),
                                       self._al_mensaje, self._al_cerrar)
            except ConnectionRefusedError:
                error = (f"{ip} rechazó la conexión: la aplicación no está abierta en el otro "
                         f"equipo o un firewall bloquea el puerto TCP {c['PUERTO_SENAL']}.")
            except socket.timeout:
                error = f"{ip} no responde: IP incorrecta o equipo fuera de la red."
            except OSError as e:
                error = (f"No se pudo contactar a {ip} ({e.strerror or e}): verifique la IP y "
                         "que ambos equipos estén en el mismo punto de acceso.")
        if error:
            self._log("error", error)
            if gen == self._gen:
                self.finalizar(error, enviar=False, error=True)
            return
        with self._lock:
            if gen != self._gen:                          # se colgó mientras conectaba
                return canal.cerrar()
            self.canal = canal
            canal.iniciar_lectura()
            canal.enviar("INVITAR", nombre=self.nombre, id_llamada=self.ll["id_llamada"],
                         puerto_rx=self.ll["puerto_rx_local"],
                         transporte=c["TRANSPORTE"], params=c.compartidos())
            self._estado(LLAMANDO, f"Llamando a {ip}...")
            self.alerta.iniciar("saliente")
        self._log("info", f"Invitación enviada a {ip}; recibiré en UDP {self.ll['puerto_rx_local']}")
        self._temporizar(c.entero("TIEMPO_TIMBRADO"), gen, LLAMANDO, "No contestaron")

    def contestar(self):
        with self._lock:
            if self.estado != ENTRANTE:
                return
            if self.c.booleano("ADOPTAR_PARAMETROS_REMOTOS"):
                cambios = []
                for k, v in self.ll["params"].items():
                    if k in COMPARTIDOS and self.c[k] != str(v):
                        self.c[k] = str(v)
                        cambios.append(f"{k}={v}")
                if cambios:
                    self._log("info", "Parámetros adoptados de quien llama: " + ", ".join(cambios))
            self.ll["puerto_rx_local"] = self.c.puerto_rx(soy_llamante=False)
            self.alerta.detener()
            self._estado(ENTRANTE, "Contestando...")
            self._hilo(self._establecer, self._gen)

    def rechazar(self):
        if self.estado == ENTRANTE:
            self.finalizar("Rechazada por el usuario")

    def colgar(self, motivo="Llamada finalizada por el usuario"):
        if self.estado not in (LIBRE, FINALIZANDO):
            self.finalizar(motivo)

    # -------------------------------------------------------- señalización
    def _al_mensaje(self, canal, msg):
        tipo = msg.get("tipo")
        with self._lock:
            if canal is not self.canal:
                if tipo == "INVITAR":
                    self._invitacion(canal, msg)
                return
            self._ultimo_rx = time.time()
            if tipo == "PING":
                canal.enviar("PONG", n=msg.get("n"))
            elif tipo == "TIMBRANDO" and self.estado == LLAMANDO:
                self._estado(LLAMANDO, f"Timbrando en {self.ll['ip_remota']}...")
            elif tipo == "ACEPTAR" and self.estado == LLAMANDO:
                self.ll.update(equipo_remoto=str(msg.get("nombre", canal.remoto)),
                               puerto_tx_destino=int(msg.get("puerto_rx", 0)),
                               transporte_rx=msg.get("transporte", "mpegts_udp"))
                self.alerta.detener()
                self._log("info", f"{self.ll['equipo_remoto']} contestó la llamada")
                self._hilo(self._establecer, self._gen)
            elif tipo in FIN_REMOTO and self.estado not in (LIBRE, FINALIZANDO):
                motivo = FIN_REMOTO[tipo] + (f": {msg['motivo']}" if msg.get("motivo") else "")
                self._hilo(self.finalizar, motivo, False)

    def _invitacion(self, canal, msg):
        ip, nombre = canal.remoto, str(msg.get("nombre", canal.remoto))
        if self.estado != LIBRE:
            canal.enviar("OCUPADO")
            canal.cerrar()
            return self._log("aviso", f"Llamada de {nombre} ({ip}) rechazada: ocupado")
        self._gen += 1
        self.canal, self.c = canal, self.cfg.copia()
        self.ll = {"rol": "llamado", "ip_remota": ip, "equipo_remoto": nombre,
                   "puerto_tx_destino": int(msg.get("puerto_rx", 0)),
                   "transporte_rx": msg.get("transporte", "mpegts_udp"),
                   "params": msg.get("params", {}),
                   "id_llamada": _id_seguro(str(msg.get("id_llamada", ""))
                                            or time.strftime("%Y%m%d_%H%M%S_llamada"))}
        canal.enviar("TIMBRANDO")
        self._estado(ENTRANTE, f"Llamada entrante de {nombre} ({ip})")
        self._log("info", f"Llamada entrante de {nombre} ({ip})")
        self.alerta.iniciar("entrante")
        self.notificar("entrante", {"ip": ip, "nombre": nombre})
        self._temporizar(self.c.entero("TIEMPO_TIMBRADO") + 5, self._gen, ENTRANTE,
                         "Llamada perdida (no se contestó)")

    def _al_cerrar(self, canal, motivo):
        with self._lock:
            if canal is self.canal and self.estado not in (LIBRE, FINALIZANDO):
                self._hilo(self.finalizar, motivo, False, self.estado == EN_LLAMADA)

    def _latido(self, gen):
        """PING numerado cada 2 s (el otro equipo responde PONG; sirve para medir el RTT en
        las capturas); si el otro equipo calla TIMEOUT_SENAL s, la llamada termina."""
        timeout, n = self.c.entero("TIMEOUT_SENAL"), 0
        while gen == self._gen and self.estado == EN_LLAMADA:
            n += 1
            self.canal.enviar("PING", n=n)
            if time.time() - self._ultimo_rx > timeout:
                return self.finalizar(f"Sin respuesta del otro equipo durante {timeout} s "
                                      "(conexión perdida)", error=True)
            time.sleep(2)

    # ---------------------------------------------------------------- medios
    def _error_previo(self, puerto_rx, transporte):
        if not os.path.exists(self.c["VIDEO_DISPOSITIVO"]):
            return f"No existe la cámara {self.c['VIDEO_DISPOSITIVO']} (v4l2-ctl --list-devices)"
        try:
            medios.validar(self.c)
        except ValueError as e:
            return f"Parámetro inválido: {e}"
        for p in medios.puertos_udp(puerto_rx, transporte):
            if not puerto_udp_libre(p):
                return f"El puerto UDP {p} (recepción) está ocupado por otro proceso"
        return ""

    def _lanzar(self, gen, atributo, nombre, cmd):
        """Inicia un proceso solo si la llamada sigue vigente (evita procesos huérfanos)."""
        with self._lock:
            if gen != self._gen:
                raise _Cancelado()
            proc = medios.Proceso(nombre, cmd, self._linea_proceso, self._al_terminar_proceso)
            setattr(self, atributo, proc)
            return proc

    def _establecer(self, gen):
        c, ll = self.c, self.ll
        ip = ll["ip_remota"]
        error = self._error_previo(ll["puerto_rx_local"], ll["transporte_rx"])
        if not error and not ll["puerto_tx_destino"]:
            error = "El otro equipo no indicó su puerto de recepción"
        if error:
            self._log("error", error)
            return self.finalizar(error, error=True)

        ll["transporte_tx"] = c["TRANSPORTE"]
        ll["archivo_log"] = os.path.join(DIR_LOGS, ll["id_llamada"] + ".log")
        ll["interfaz"], ll["ip_local"] = ruta_hacia(ip)
        cmd_rx = medios.comando_receptor(c, ll["puerto_rx_local"], ll["transporte_rx"],
                                         f"Videoconferencia - {ll['equipo_remoto']} ({ip})")
        cmd_tx = medios.comando_transmisor(c, ip, ll["puerto_tx_destino"])
        ll["cmd_rx"], ll["cmd_tx"] = medios.texto(cmd_rx), medios.texto(cmd_tx)
        try:
            with self._lock:
                if gen != self._gen:
                    return
                self._log_llamada = open(ll["archivo_log"], "a", encoding="utf-8", buffering=1)
            self._log("info", f"RX: {ll['cmd_rx']}", ui=False)
            self._log("info", f"TX: {ll['cmd_tx']}", ui=False)
            if c.booleano("CAPTURAR"):
                self._iniciar_captura(gen)
            self._lanzar(gen, "rx", "RX", cmd_rx)
            with self._lock:
                if gen != self._gen:
                    raise _Cancelado()
                if ll["rol"] == "llamado":
                    self.canal.enviar("ACEPTAR", nombre=self.nombre, puerto_rx=ll["puerto_rx_local"],
                                      transporte=c["TRANSPORTE"])
            self._lanzar(gen, "tx", "TX", cmd_tx)
        except _Cancelado:
            return
        except (RuntimeError, OSError) as e:
            self._log("error", f"No se pudo iniciar: {e}")
            return self.finalizar(f"Error al iniciar los medios: {e}", error=True)

        with self._lock:
            if gen != self._gen:
                return
            ll["inicio_ts"], ll["inicio"] = time.time(), time.strftime("%Y-%m-%d %H:%M:%S")
            self._ultimo_rx = time.time()
            self._estado(EN_LLAMADA, f"En llamada con {ll['equipo_remoto']} ({ip})",
                         inicio=ll["inicio_ts"])
            self._log("info", f"Llamada establecida. TX -> {ip}:{ll['puerto_tx_destino']} "
                              f"(PID {self.tx.p.pid}), RX <- UDP {ll['puerto_rx_local']} "
                              f"(PID {self.rx.p.pid}). {c['RESOLUCION']}@{c['FPS']} GOP {c['GOP']} "
                              f"{medios.VCODEC} {c['VBITRATE']}, {medios.ACODEC} {c['ABITRATE']} "
                              f"{c['AFRECUENCIA']} Hz, {c['TRANSPORTE']}")
        self._hilo(self._latido, gen)
        if c.entero("DURACION") > 0:
            self._log("info", f"La llamada terminará automáticamente en {c['DURACION']} s")
            self._temporizar(c.entero("DURACION"), gen, EN_LLAMADA, "Duración de prueba completada")
        ll.update(info_wifi())
        if ll.get("ssid"):
            self._log("info", f"Wi-Fi: {ll['ssid']}, canal {ll['canal']} ({ll['banda']}), "
                              f"señal {ll['senal_pct']}%")

    def _iniciar_captura(self, gen):
        ll, c = self.ll, self.c
        os.makedirs(DIR_CAPTURAS, exist_ok=True)
        archivo = os.path.join(DIR_CAPTURAS, f"{ll['id_llamada']}_{self.nombre}.pcapng")
        # Metadatos en el comentario del pcapng: la captura se analiza sin otros archivos.
        meta = {"id_llamada": ll["id_llamada"], "equipo": self.nombre, "rol": ll["rol"],
                "equipo_remoto": ll["equipo_remoto"], "ip_local": ll["ip_local"],
                "ip_remota": ll["ip_remota"],
                "puerto_rx": ll["puerto_rx_local"], "puerto_tx": ll["puerto_tx_destino"],
                "puerto_senal": c.entero("PUERTO_SENAL"), "transporte_tx": c["TRANSPORTE"],
                "transporte_rx": ll["transporte_rx"],
                **{k.lower(): c[k] for k in ("ID_PRUEBA", "DISTANCIA_M", "UBICACION", "RESOLUCION",
                                             "FPS", "GOP", "VBITRATE", "QP", "ABITRATE", "AFRECUENCIA",
                                             "TAMANO_PAQUETE")}}
        cmd = medios.comando_captura(ll["interfaz"] or "any",
                                     [ll["puerto_rx_local"], ll["puerto_tx_destino"]],
                                     c["PUERTO_SENAL"], archivo,
                                     "videoconferencia " + json.dumps(meta, ensure_ascii=False))
        cap = self._lanzar(gen, "cap", "CAP", cmd)
        ll["archivo_captura"] = archivo
        self._log("info", f"Capturando paquetes -> {os.path.relpath(archivo, DIR_APP)}")
        time.sleep(1)                         # la captura debe arrancar antes que los medios
        if cap.p.poll() is not None:
            raise RuntimeError(f"la captura no arrancó: {cap.error or cap.diagnostico()}")

    def _al_terminar_proceso(self, proc):
        if proc.deteniendo or self.estado in (LIBRE, FINALIZANDO):
            return
        if proc is self.rx and proc.p.returncode == 0 and proc.abrio_entrada and not proc.error:
            return self.finalizar("Se cerró la ventana de video")
        self._log("error", proc.diagnostico())
        if proc is not self.cap:              # sin captura la llamada puede continuar
            self.finalizar(f"Error en {proc.nombre}: {proc.diagnostico()}", error=True)

    # ------------------------------------------------------------- finalizar
    def finalizar(self, motivo, enviar=True, error=False):
        """Termina la llamada (idempotente) y libera cámara, micrófono y puertos."""
        with self._lock:
            if self.estado in (LIBRE, FINALIZANDO):
                return
            previo = self.estado
            self._gen += 1
            self._estado(FINALIZANDO, "Finalizando llamada...")
        self.alerta.detener()
        if enviar and self.canal:
            tipo = {LLAMANDO: "CANCELAR", ENTRANTE: "RECHAZAR"}.get(previo, "COLGAR")
            self.canal.enviar(tipo, motivo=motivo)
        for proc in (self.tx, self.rx, self.cap):   # primero TX: libera cámara y micrófono
            if proc:
                proc.detener()
        if self.tx or self.rx:
            self._log("info", "Procesos FFmpeg detenidos; cámara, micrófono y puertos liberados.")
        if self.canal:
            self.canal.cerrar()
        if self.ll.get("inicio_ts"):
            self._guardar_registro(motivo, error)
        self._log("error" if error else "info", f"Fin de la llamada: {motivo}")
        with self._lock:
            if self._log_llamada:
                self._log_llamada.close()
            self._log_llamada = self.canal = self.tx = self.rx = self.cap = None
            self.ll = {}
            self._estado(LIBRE, "Libre")
        self.notificar("fin", {"motivo": motivo, "error": error})

    def _guardar_registro(self, motivo, error):
        c, ll = self.c, self.ll
        fila = {k: ll.get(k, "") for k in CAMPOS}
        fila.update({k.lower(): c[k] for k in ("ID_PRUEBA", "DISTANCIA_M", "UBICACION", "RESOLUCION",
                                               "FPS", "GOP", "VBITRATE", "QP", "ABITRATE",
                                               "AFRECUENCIA", "TAMANO_PAQUETE")})
        fila.update(fin=time.strftime("%Y-%m-%d %H:%M:%S"), equipo_local=self.nombre,
                    vcodec=medios.VCODEC, acodec=medios.ACODEC,
                    duracion_s=f"{time.time() - ll['inicio_ts']:.1f}",
                    motivo_fin=motivo, error="si" if error else "no")
        for k in ("archivo_log", "archivo_captura"):
            if fila[k]:
                fila[k] = os.path.relpath(fila[k], DIR_APP)
        ruta = os.path.join(DIR_LOGS, "registro_llamadas.csv")
        try:
            nuevo = not os.path.exists(ruta)
            with open(ruta, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=CAMPOS)
                if nuevo:
                    w.writeheader()
                w.writerow(fila)
        except OSError as e:
            self._log("error", f"No se pudo guardar el registro de la llamada: {e}")

    def cerrar(self):
        """Cierre de la aplicación."""
        self.colgar("La aplicación se cerró")
        if self._servidor:
            self._servidor.close()
        self._log("info", "Aplicación cerrada", ui=False)
