"""Interfaz gráfica (Tkinter): llamar, contestar, rechazar y colgar."""

import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import medios
from . import motor as M
from .config import DIR_APP
from .utilidades import ips_locales, verificar_entorno

COLORES = {M.LIBRE: "#6b7280", M.LLAMANDO: "#d97706", M.ENTRANTE: "#2563eb",
           M.EN_LLAMADA: "#16a34a", M.FINALIZANDO: "#6b7280"}
ULTIMA_IP = os.path.join(DIR_APP, ".ultima_ip")

# (clave, etiqueta, opciones; None = texto libre). Los combobox también aceptan valores
# escritos; lo que sigue a " (" es solo una etiqueta, p. ej. "352x288 (CIF)".
CAMPOS = [
    ("RESOLUCION", "Resolución", [f"{r} ({n})" for n, r in (
        ("SQCIF", "128x96"), ("QCIF", "176x144"), ("CIF", "352x288"), ("2CIF", "704x288"),
        ("4CIF", "704x576"), ("16CIF", "1408x1152"))]),
    ("FPS", "FPS", ["10", "15", "24", "30"]),
    ("GOP", "GOP (1-250)", ["15", "30", "60"]),
    ("VBITRATE", "Bitrate video (si QP vacío)", ["50k", "100k", "200k", "300k"]),
    ("QP", "QP fijo (1-51, vacío=bitrate)", ["", "20", "24", "28", "32", "36"]),
    ("ABITRATE", "Bitrate audio (8k-64k)", ["32k", "64k"]),
    ("AFRECUENCIA", "Frec. muestreo", ["8000", "16000", "44100", "48000"]),
    ("TRANSPORTE", "Transporte", list(medios.TRANSPORTES)),
    ("TAMANO_PAQUETE", "Tamaño paquete", ["188", "564", "1316"]),
    ("DURACION", "Duración (s, 0=∞)", ["0", "60", "120"]),
    ("ID_PRUEBA", "ID de prueba", None),
    ("DISTANCIA_M", "Distancia (m)", None),
    ("CAPTURAR", "Capturar .pcapng", ["0", "1"]),
]


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.eventos = queue.Queue()
        self.motor = M.Motor(cfg, lambda ev, d: self.eventos.put((ev, d)))
        self.inicio_llamada = None
        self.root = tk.Tk()
        self.root.title("Videoconferencia FFmpeg")
        self.root.minsize(560, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.salir)
        self._estilos()
        self._construir()
        try:
            self.motor.iniciar()
        except RuntimeError as e:
            messagebox.showerror("No se puede iniciar", str(e))
            raise SystemExit(1)
        self._actualizar_estado(M.LIBRE, "Libre")
        for sig in (signal.SIGINT, signal.SIGTERM):     # Ctrl+C en la terminal
            signal.signal(sig, lambda *_: self.root.after(0, self.salir))
        self.verificar()
        self.root.after(100, self._procesar_eventos)

    # ------------------------------------------------------------ interfaz
    def _estilos(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("Estado.TLabel", font=("TkDefaultFont", 15, "bold"))
        st.configure("Tiempo.TLabel", font=("TkFixedFont", 13))
        for nombre, color in (("Verde", "#16a34a"), ("Rojo", "#dc2626"), ("Azul", "#2563eb")):
            st.configure(f"{nombre}.TButton", foreground="white", background=color,
                         font=("TkDefaultFont", 11, "bold"), padding=(12, 6))
            st.map(f"{nombre}.TButton", background=[("disabled", "#cbd5e1"), ("active", color)],
                   foreground=[("disabled", "#f1f5f9")])

    def _construir(self):
        raiz = ttk.Frame(self.root, padding=12)
        raiz.pack(fill="both", expand=True)
        ips = ", ".join(f"{ip} ({i})" for i, ip in ips_locales()) or "sin red"
        ttk.Label(raiz, foreground="#475569", text=f"Este equipo: {self.motor.nombre}  —  "
                  f"IP: {ips}  —  señalización TCP {self.cfg['PUERTO_SENAL']}").pack(anchor="w")

        # Llamar
        fila = ttk.Frame(raiz)
        fila.pack(fill="x", pady=(10, 6))
        ttk.Label(fila, text="IP destino:").pack(side="left")
        ip = self.cfg["IP_REMOTA"]
        if not ip and os.path.isfile(ULTIMA_IP):
            with open(ULTIMA_IP) as fh:
                ip = fh.read().strip()
        self.var_ip = tk.StringVar(value=ip)
        self.ent_ip = ttk.Entry(fila, textvariable=self.var_ip, width=20, font=("TkDefaultFont", 12))
        self.ent_ip.pack(side="left", padx=6)
        self.ent_ip.bind("<Return>", lambda e: self.llamar())
        self.btn_llamar = ttk.Button(fila, text="📞 Llamar", style="Azul.TButton",
                                     command=self.llamar)
        self.btn_llamar.pack(side="left")

        # Estado
        caja = ttk.Frame(raiz, padding=(0, 8))
        caja.pack(fill="x")
        self.indicador = tk.Canvas(caja, width=18, height=18, highlightthickness=0)
        self.punto = self.indicador.create_oval(2, 2, 16, 16, width=0)
        self.indicador.pack(side="left", padx=(0, 8))
        self.lbl_estado = ttk.Label(caja, style="Estado.TLabel")
        self.lbl_estado.pack(side="left")
        self.lbl_tiempo = ttk.Label(caja, style="Tiempo.TLabel")
        self.lbl_tiempo.pack(side="right")

        # Acciones
        acc = ttk.Frame(raiz)
        acc.pack(fill="x", pady=(0, 8))
        self.btn_contestar = ttk.Button(acc, text="✔ Contestar", style="Verde.TButton",
                                        command=self.motor.contestar)
        self.btn_rechazar = ttk.Button(acc, text="✖ Rechazar", style="Rojo.TButton",
                                       command=self.motor.rechazar)
        self.btn_colgar = ttk.Button(acc, text="⏹ Colgar", style="Rojo.TButton",
                                     command=lambda: self.motor.colgar())
        for b in (self.btn_contestar, self.btn_rechazar, self.btn_colgar):
            b.pack(side="left", padx=(0, 8))

        # Parámetros
        marco = ttk.LabelFrame(raiz, text="Parámetros de transmisión", padding=8)
        marco.pack(fill="x", pady=4)
        self.vars, self.editables = {}, []
        for i, (clave, etiqueta, opciones) in enumerate(CAMPOS):
            f, col = divmod(i, 2)
            ttk.Label(marco, text=etiqueta + ":").grid(row=f, column=col * 2, sticky="e",
                                                       padx=(8, 4), pady=2)
            valor = next((o for o in opciones or [] if o.startswith(self.cfg[clave] + " (")),
                         self.cfg[clave])
            self.vars[clave] = var = tk.StringVar(value=valor)
            w = ttk.Combobox(marco, textvariable=var, values=opciones, width=13) if opciones \
                else ttk.Entry(marco, textvariable=var, width=15)
            w.grid(row=f, column=col * 2 + 1, sticky="w", pady=2)
            self.editables.append(w)
        n = (len(CAMPOS) + 1) // 2
        ttk.Label(marco, foreground="#64748b", text=(
            f"Códecs: video {medios.VCODEC} (H.264), audio {medios.ACODEC.upper()}. " +
            ("Al contestar se adoptan los parámetros de quien llama."
             if self.cfg.booleano("ADOPTAR_PARAMETROS_REMOTOS") else
             "Cada equipo transmite con sus propios parámetros."))).grid(
            row=n, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 0))
        b = ttk.Button(marco, text="Verificar equipo", command=self.verificar)
        b.grid(row=n + 1, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 0))
        self.editables.append(b)

        # Registro
        reg = ttk.LabelFrame(raiz, text="Registro", padding=4)
        reg.pack(fill="both", expand=True, pady=(8, 0))
        self.txt = tk.Text(reg, height=10, wrap="word", state="disabled", relief="flat",
                           font=("TkFixedFont", 9), background="#0f172a", foreground="#e2e8f0")
        sb = ttk.Scrollbar(reg, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(fill="both", expand=True)
        for nivel, color in (("error", "#f87171"), ("aviso", "#fbbf24"), ("ok", "#4ade80")):
            self.txt.tag_configure(nivel, foreground=color)

    # ------------------------------------------------------------- eventos
    def _procesar_eventos(self):
        while not self.eventos.empty():
            ev, d = self.eventos.get_nowait()
            if ev == "log":
                self.escribir(d["texto"], d["nivel"])
            elif ev == "estado":
                self._actualizar_estado(d["estado"], d["texto"], d.get("inicio"))
            elif ev == "entrante":
                self._llamada_entrante(d)
            elif ev == "fin" and d["error"]:
                messagebox.showwarning("Llamada finalizada", d["motivo"], parent=self.root)
        if self.inicio_llamada:
            s = int(time.time() - self.inicio_llamada)
            self.lbl_tiempo.config(text=f"{s // 60:02d}:{s % 60:02d}")
        self.root.after(200, self._procesar_eventos)

    def _actualizar_estado(self, estado, texto, inicio=None):
        self.lbl_estado.config(text=texto)
        self.indicador.itemconfig(self.punto, fill=COLORES[estado])
        for w in [self.btn_llamar, self.ent_ip] + self.editables:
            w.state(["!disabled" if estado == M.LIBRE else "disabled"])
        for w in (self.btn_contestar, self.btn_rechazar):
            w.state(["!disabled" if estado == M.ENTRANTE else "disabled"])
        self.btn_colgar.state(["!disabled" if estado in (M.LLAMANDO, M.EN_LLAMADA) else "disabled"])
        self.inicio_llamada = (inicio or time.time()) if estado == M.EN_LLAMADA else None
        if not self.inicio_llamada:
            self.lbl_tiempo.config(text="")

    def _llamada_entrante(self, d):
        self.escribir(f"📞 Llamada entrante de {d['nombre']} ({d['ip']})", "ok")
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(1500, lambda: self.root.attributes("-topmost", False))
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-u", "critical", "Videoconferencia",
                              f"Llamada entrante de {d['nombre']} ({d['ip']})"])

    def escribir(self, texto, nivel="info"):
        self.txt.configure(state="normal")
        self.txt.insert("end", f"{time.strftime('%H:%M:%S')}  {texto}\n", nivel)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    # ------------------------------------------------------------ acciones
    def _aplicar_parametros(self):
        """Valida los campos sobre una copia y, si son correctos, los aplica."""
        nueva = self.cfg.copia()
        for clave, var in self.vars.items():
            nueva[clave] = var.get().split(" (")[0].strip()
        medios.validar(nueva)
        self.cfg.update(nueva)

    def llamar(self):
        try:
            self._aplicar_parametros()
            self.motor.llamar(self.var_ip.get())
            with open(ULTIMA_IP, "w") as fh:
                fh.write(self.var_ip.get().strip())
        except (ValueError, RuntimeError) as e:
            messagebox.showerror("No se puede llamar", str(e), parent=self.root)

    def verificar(self):
        def tarea():
            self.eventos.put(("log", {"nivel": "info", "texto": "Verificando el equipo..."}))
            for ok, msg in verificar_entorno(self.cfg):
                nivel = "ok" if ok else ("aviso" if ok is None else "error")
                self.eventos.put(("log", {"nivel": nivel, "texto": ("✔ " if ok else "• ") + msg}))
        threading.Thread(target=tarea, daemon=True).start()

    def salir(self):
        if self.motor.estado == M.EN_LLAMADA and not messagebox.askyesno(
                "Salir", "Hay una llamada en curso. ¿Colgar y salir?", parent=self.root):
            return
        self.lbl_estado.config(text="Cerrando...")
        self.root.update_idletasks()
        self.motor.cerrar()
        self.root.destroy()
