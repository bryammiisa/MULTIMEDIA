"""Canal de señalización TCP con mensajes JSON (uno por línea).

  INVITAR   {nombre, id_llamada, puerto_rx, transporte, params}  quien llama -> quien recibe
  TIMBRANDO / OCUPADO                                 quien recibe -> quien llama
  ACEPTAR   {nombre, puerto_rx, transporte}           quien recibe -> quien llama
  RECHAZAR / CANCELAR / COLGAR  {motivo}              cualquiera
  PING {n} / PONG {n}                                 ambos, cada 2 s durante la llamada
"""

import json
import socket
import threading


class Canal:
    def __init__(self, sock, al_mensaje, al_cerrar):
        self.sock, self.al_mensaje, self.al_cerrar = sock, al_mensaje, al_cerrar
        self.remoto = sock.getpeername()[0]
        self.cerrado = False
        self._lock = threading.Lock()

    @classmethod
    def conectar(cls, ip, puerto, al_mensaje, al_cerrar):
        sock = socket.create_connection((ip, puerto), timeout=5)
        sock.settimeout(None)
        return cls(sock, al_mensaje, al_cerrar)

    def iniciar_lectura(self):
        threading.Thread(target=self._leer, daemon=True).start()

    def enviar(self, tipo, **datos):
        with self._lock:
            if self.cerrado:
                return
            try:
                self.sock.sendall((json.dumps(dict(datos, tipo=tipo)) + "\n").encode())
            except OSError:
                pass

    def _leer(self):
        motivo = "Conexión cerrada por el otro equipo"
        try:
            for linea in self.sock.makefile("r", encoding="utf-8", errors="replace"):
                try:
                    msg = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    self.al_mensaje(self, msg)
        except OSError as e:
            motivo = f"Error en la señalización: {e}"
        if not self.cerrado:
            self.cerrar()
            self.al_cerrar(self, motivo)

    def cerrar(self):
        with self._lock:
            if self.cerrado:
                return
            self.cerrado = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
