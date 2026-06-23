import sys
import socket
import threading
import json
import cv2
import numpy as np
import struct
import time
import random
from http.server import HTTPServer, BaseHTTPRequestHandler
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QFrame, QRadioButton, QButtonGroup, QTextEdit, QPushButton, QLineEdit, QComboBox)
from PyQt6.QtCore import Qt, pyqtSignal, QObject

class GpsLocalServer:
    PORT = 8765
    _callback = None
    _started = False

    HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>GPS Robot</title>
  <style>
    body { font-family: Arial, sans-serif; background: #0f111a; color: #eee;
           display: flex; flex-direction: column; align-items: center;
           justify-content: center; min-height: 100vh; margin: 0; }
    .card { background: #1a1d2e; border-radius: 12px; padding: 32px 40px;
            text-align: center; box-shadow: 0 4px 24px #0008; max-width: 440px; width: 90%; }
    h2 { color: #ff5e62; margin-bottom: 8px; }
    .status { font-size: 14px; margin: 16px 0 8px; min-height: 22px; }
    .ok   { color: #22c55e; }
    .err  { color: #f97316; }
    .wait { color: #aaa; }
    .coords { font-family: monospace; font-size: 12px; color: #38bdf8;
              background: #0b0c10; padding: 10px 14px; border-radius: 8px;
              margin-top: 8px; display: none; line-height: 1.7; }
    .pulse { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
             background: #22c55e; margin-right: 6px;
             animation: blink 1.2s infinite; }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.2} }
    button { margin-top: 18px; padding: 10px 28px; background: #ff5e62;
             color: white; border: none; border-radius: 6px; font-size: 14px;
             cursor: pointer; }
    button:hover { background: #e14448; }
    #btn-stop { background: #555; display: none; }
    #btn-stop:hover { background: #333; }
    .note { font-size: 11px; color: #555; margin-top: 14px; line-height: 1.6; }
    #count { font-size: 11px; color: #666; margin-top: 6px; }
  </style>
</head>
<body>
<div class="card">
  <h2>📍 GPS Robot</h2>
  <p style="color:#aaa;font-size:13px;margin:0">Tracking otomatis — lokasi dikirim ke Base Station via telemetri.</p>
  <div class="status wait" id="status">⏳ Klik Start untuk mulai tracking...</div>
  <div class="coords" id="coords"></div>
  <div id="count"></div>
  <br>
  <button id="btn-start" onclick="startWatch()">▶ Start GPS Tracking</button>
  <button id="btn-stop" onclick="stopWatch()">⏹ Stop</button>
  <p class="note">
    Izinkan akses lokasi saat browser meminta.<br>
    Halaman ini harus tetap terbuka agar tracking berjalan.<br>
    Data hanya dikirim ke localhost, tidak ke internet.
  </p>
</div>
<script>
var watchId = null;
var sendCount = 0;
function startWatch() {
  if (!navigator.geolocation) { setStatus('❌ Browser tidak mendukung Geolocation.', 'err'); return; }
  setStatus('⏳ Meminta izin GPS...', 'wait');
  document.getElementById('btn-start').style.display = 'none';
  document.getElementById('btn-stop').style.display = 'inline-block';
  watchId = navigator.geolocation.watchPosition(
    function(pos) {
      var lat = pos.coords.latitude; var lon = pos.coords.longitude; var acc = pos.coords.accuracy;
      sendCount++;
      document.getElementById('count').textContent = 'Update ke-' + sendCount;
      var co = document.getElementById('coords');
      co.style.display = 'block';
      co.innerHTML = '<span class="pulse"></span><b>Live Tracking</b><br>Lat: ' + lat.toFixed(6) + '<br>Lon: ' + lon.toFixed(6) + '<br>±' + Math.round(acc) + 'm';
      setStatus('✅ Tracking aktif — terkirim ke Base Station', 'ok');
      fetch('/location', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({lat: lat, lon: lon, accuracy: acc}) }).catch(function() { setStatus('⚠ Gagal kirim ke server lokal', 'err'); });
    },
    function(err) { var msg = {1:'Izin ditolak.', 2:'Posisi tidak tersedia.', 3:'Timeout.'}; setStatus('❌ ' + (msg[err.code] || err.message), 'err'); stopWatch(); },
    {enableHighAccuracy: true, timeout: 500, maximumAge: 0}
  );
}
function stopWatch() {
  if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; }
  document.getElementById('btn-start').style.display = 'inline-block';
  document.getElementById('btn-stop').style.display = 'none';
  setStatus('⏹ Tracking dihentikan.', 'wait');
}
function setStatus(msg, cls) { var el = document.getElementById('status'); el.textContent = msg; el.className = 'status ' + (cls || 'wait'); }
window.onload = function() { startWatch(); };
</script>
</body>
</html>"""

    @classmethod
    def start(cls, callback):
        if cls._started:
            return
        cls._callback = callback
        server = HTTPServer(("127.0.0.1", cls.PORT), cls._Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        cls._started = True

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            body = GpsLocalServer.HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            if self.path != "/location":
                self.send_response(404); self.end_headers(); return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                lat = float(data["lat"]); lon = float(data["lon"]); acc = float(data.get("accuracy", 0))
                if GpsLocalServer._callback:
                    GpsLocalServer._callback(lat, lon, acc)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception:
                self.send_response(400); self.end_headers()
        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()


class RobotWorker(QObject):
    command_received = pyqtSignal(str, str)
    telemetry_sent = pyqtSignal(str)
    gps_updated = pyqtSignal(float, float, float)
    
    calibration_updated = pyqtSignal(float, int)   

    def __init__(self):
        super().__init__()
        self.target_ip = "127.0.0.1"
        self.streaming_active = False
        self.running = True
        self.telemetry_mode = "otomatis"
        self._sensor_seq = 0
        self.current_gps = {"lat": None, "lon": None, "accuracy": None}
        self.video_fps = 10
        self.sensor_interval = 0.5

       
        self._last_epoch_ts_sent = None
        self._clock_offset = 0.0
        self._offset_history = []          
        self._offset_lock = threading.Lock()

  
        self._pong_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def on_gps_received(self, lat, lon, accuracy):
        self.current_gps = {"lat": lat, "lon": lon, "accuracy": accuracy}
        self.gps_updated.emit(lat, lon, accuracy)

    def start_listeners(self):
        threading.Thread(target=self._command_listener, daemon=True).start()
        threading.Thread(target=self._sensor_streamer, daemon=True).start()
        threading.Thread(target=self._video_streamer, daemon=True).start()

    def _send_pong(self, echo_ts: float, t2: float, dest_ip: str):
        try:
            t3 = time.time()
            pong_payload = json.dumps({
                "type": "pong",
                "echo_ts": echo_ts,
                "t2": t2,
                "t3": t3
            }).encode("utf-8")
            self._pong_sock.sendto(pong_payload, (dest_ip, 5006))
        except Exception:
            pass

    def _update_clock_offset(self, t1: float, t2: float, t3: float, t4: float):

        offset_sample = ((t2 - t1) + (t3 - t4)) / 2.0
        with self._offset_lock:
            self._offset_history.append(offset_sample)
            if len(self._offset_history) > 5:
                self._offset_history.pop(0)
            self._clock_offset = sum(self._offset_history) / len(self._offset_history)
            n = len(self._offset_history)
  
        self.calibration_updated.emit(self._clock_offset, n)

    def _command_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 5005))
        while self.running:
            try:
                data, addr = sock.recvfrom(1024)
                t4 = time.time()
                try:
                    payload = json.loads(data.decode("utf-8"))
                    cmd = payload.get("cmd", "")
                    ts  = payload.get("ts")  

                    if ts is not None:
                        self._send_pong(ts, t4, addr[0])

                    
                    t2_piggy = payload.get("t2")
                    t3_piggy = payload.get("t3")
                    if t2_piggy is not None and t3_piggy is not None:
                        with self._offset_lock:
                            t1 = self._last_epoch_ts_sent
                        if t1 is not None:
                            self._update_clock_offset(t1, t2_piggy, t3_piggy, t4)

                    if cmd and cmd != "KEEPALIVE":
                        self.command_received.emit(cmd, addr[0])

                except json.JSONDecodeError:
                    
                    raw = data.decode("utf-8")
                    parts = raw.split("|")
                    cmd = parts[0].strip()

                    if len(parts) == 3:
                        try:
                            t2 = float(parts[1].split("=")[1])
                            t3 = float(parts[2].split("=")[1])
                            with self._offset_lock:
                                t1 = self._last_epoch_ts_sent
                            if t1 is not None:
                                self._update_clock_offset(t1, t2, t3, t4)
                        except (IndexError, ValueError):
                            pass

                    if cmd != "KEEPALIVE":
                        self.command_received.emit(cmd, addr[0])
            except Exception:
                pass

    def _sensor_streamer(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        jarak_simulasi = 120.0
        while self.running:
            if self.streaming_active:
                if self.telemetry_mode == "otomatis":
                    jarak_simulasi += random.uniform(-15, 15)
                    jarak_simulasi = max(5, min(jarak_simulasi, 200))
                    ir_tengah = jarak_simulasi < 20
                elif self.telemetry_mode == "aman":
                    jarak_simulasi = random.uniform(80.0, 150.0)
                    ir_tengah = False
                elif self.telemetry_mode == "waspada":
                    jarak_simulasi = random.uniform(30.0, 55.0)
                    ir_tengah = False
                elif self.telemetry_mode == "bahaya":
                    jarak_simulasi = random.uniform(5.0, 18.0)
                    ir_tengah = True

                self._sensor_seq += 1
                epoch_ts_now = time.time()
                with self._offset_lock:
                    current_offset = self._clock_offset
                payload = {
                    "seq": self._sensor_seq,
                    "epoch_ts": epoch_ts_now,
                    "clock_offset": current_offset,
                    "jarak_cm": round(jarak_simulasi, 2),
                    "infrared": {
                        "ir_kiri": {"terdeteksi": jarak_simulasi < 30 and random.choice([True, False])},
                        "ir_tengah_kiri": {"terdeteksi": ir_tengah},
                        "ir_tengah_kanan": {"terdeteksi": ir_tengah},
                        "ir_kanan": {"terdeteksi": jarak_simulasi < 30 and random.choice([True, False])}
                    },
                    "gps": self.current_gps if self.current_gps.get("lat") is not None else None,
                    "timestamp": time.strftime("%H:%M:%S"),
                    "sensor_interval_s": self.sensor_interval
                }
                with self._offset_lock:
                    self._last_epoch_ts_sent = epoch_ts_now
                try:
                    data_string = json.dumps(payload, indent=2)
                    sock.sendto(data_string.encode("utf-8"), (self.target_ip, 5006))
                    self.telemetry_sent.emit(data_string)
                except Exception:
                    pass

            time.sleep(self.sensor_interval)

    def _video_streamer(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        fid = 0
        CHUNK_MAX_SZ = 60000
        HEADER_FORMAT = '>I H H I d'
        HEADER_SZ = struct.calcsize(HEADER_FORMAT)
        while self.running:
            loop_start = time.time()
            if self.streaming_active:
                ret, frame = cap.read()
                if ret:
                    ret, encoded_img = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
                    if ret:
                        raw_data = encoded_img.tobytes()
                        data_sz = len(raw_data)
                        total_chunks = (data_sz + CHUNK_MAX_SZ - 1) // CHUNK_MAX_SZ
                        frame_ts = time.time()
                        for i in range(total_chunks):
                            start = i * CHUNK_MAX_SZ
                            end = min(start + CHUNK_MAX_SZ, data_sz)
                            chunk_data = raw_data[start:end]
                            dlen = len(chunk_data)
                            header = struct.pack(HEADER_FORMAT, fid, total_chunks, i, dlen, frame_ts)
                            try:
                                sock.sendto(header + chunk_data, (self.target_ip, 9999))
                            except Exception:
                                pass
                        fid = (fid + 1) % 1000000
            fps = self.video_fps if self.video_fps > 0 else 1
            sleep_time = (1.0 / fps) - (time.time() - loop_start)
            if sleep_time > 0:
                time.sleep(sleep_time)
        cap.release()


class RobotSimulatorGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("● SIMULATOR ROBOT (Client)")
        self.setGeometry(150, 150, 500, 820)
        self.setStyleSheet("""
            QWidget { background-color: #121214; color: white; font-family: Arial; }
            QFrame { background-color: #1e1e24; border-radius: 8px; padding: 10px; border: 1px solid #2d2d35; }
            QLabel { font-size: 13px; }
            QPushButton { font-weight: bold; font-size: 13px; border-radius: 6px; }
            QLineEdit { background-color: #2d2d35; border: 1px solid #38bdf8; color: white; padding: 5px; border-radius: 4px;}
            QRadioButton { font-size: 12px; color: #cbd5e1; }
            QTextEdit { background-color: #0b0c10; color: #00ff66; font-family: 'Courier New', monospace; font-size: 11px; border: 1px solid #2d2d35; border-radius: 5px; }
            QComboBox { background-color: #2d2d35; border: 1px solid #38bdf8; color: white; padding: 4px; border-radius: 4px; }
        """)

        self.worker = RobotWorker()
        self._build_ui()
        self.worker.command_received.connect(self._on_command_received)
        self.worker.telemetry_sent.connect(self._on_telemetry_sent)
        self.worker.gps_updated.connect(self._on_gps_updated)
        self.worker.calibration_updated.connect(self._on_calibration_updated)
        self.worker.start_listeners()
        GpsLocalServer.start(callback=self.worker.on_gps_received)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        lbl_header = QLabel("🤖 SIMULATOR ROBOT MOBILE", alignment=Qt.AlignmentFlag.AlignCenter)
        lbl_header.setStyleSheet("font-size: 16px; font-weight: bold; color: #ff5e62; margin-bottom: 5px;")
        layout.addWidget(lbl_header)

        # ── IP BASE STATION ──
        frame_ip = QFrame()
        layout_ip = QHBoxLayout(frame_ip)
        layout_ip.addWidget(QLabel("IP Base Station (Tujuan):"))
        self.input_ip = QLineEdit("100.100.1.7")
        layout_ip.addWidget(self.input_ip)
        layout.addWidget(frame_ip)

        # ── KONTROL FPS VIDEO & INTERVAL SENSOR ──
        frame_rate = QFrame()
        layout_rate = QVBoxLayout(frame_rate)
        layout_rate.addWidget(QLabel("⚙️ Kontrol Frekuensi Pengiriman", styleSheet="color: #38bdf8; font-weight: bold;"))

        row_fps = QHBoxLayout()
        row_fps.addWidget(QLabel("🎞️ Video FPS:"))
        self.combo_fps = QComboBox()
        self.fps_options = [1, 3, 5, 10, 15, 20, 30, 60]
        for f in self.fps_options:
            self.combo_fps.addItem(f"{f} FPS")
        self.combo_fps.setCurrentIndex(self.fps_options.index(10))
        self.combo_fps.currentIndexChanged.connect(self._ubah_fps_video)
        row_fps.addWidget(self.combo_fps)
        layout_rate.addLayout(row_fps)

        row_sensor = QHBoxLayout()
        row_sensor.addWidget(QLabel("📡 Interval Sensor JSON:"))
        self.combo_sensor = QComboBox()
        self.sensor_options = [
            ("0.2 SPS", 5.0),
            ("0.5 SPS", 2.0),
            ("1 SPS",   1.0),
            ("3 SPS",   0.3333),
            ("5 SPS",   0.2),
            ("10 SPS",  0.1),
            ("15 SPS",  0.0667),
            ("20 SPS",  0.05),
            ("30 SPS",  0.0333),
            ("60 SPS",  0.0167),
        ]
        for label, _ in self.sensor_options:
            self.combo_sensor.addItem(label)
        self.combo_sensor.setCurrentIndex(2)
        self.combo_sensor.currentIndexChanged.connect(self._ubah_interval_sensor)
        row_sensor.addWidget(self.combo_sensor)
        layout_rate.addLayout(row_sensor)

        layout.addWidget(frame_rate)

        # ── TOMBOL STREAM ──
        self.btn_toggle_stream = QPushButton("🔴 AKTIFKAN PENGIRIMAN DATA (STREAM ON)")
        self.btn_toggle_stream.setStyleSheet("background-color: #22c55e; color: white; height: 45px;")
        self.btn_toggle_stream.clicked.connect(self._toggle_robot_streaming)
        layout.addWidget(self.btn_toggle_stream)

        frame_net = QFrame()
        layout_net = QVBoxLayout(frame_net)
        self.lbl_status = QLabel("<b>Status internal:</b> Pengiriman Mati ⚪")
        layout_net.addWidget(self.lbl_status)
        self.lbl_gps = QLabel("<b>📍 GPS:</b> Belum ada data — buka http://localhost:8765 di browser")
        self.lbl_gps.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        layout_net.addWidget(self.lbl_gps)

        # ── BARU: Label status kalibrasi clock offset ──
        self.lbl_calibration = QLabel("🕐 Clock Offset: belum terkalibrasi (menunggu keepalive BS...)")
        self.lbl_calibration.setStyleSheet("color: #f97316; font-size: 11px;")
        layout_net.addWidget(self.lbl_calibration)

        layout.addWidget(frame_net)

        # ── SKENARIO SENSOR ──
        frame_telemetry = QFrame()
        layout_tel = QVBoxLayout(frame_telemetry)
        layout_tel.addWidget(QLabel("⚙️ Skenario Kondisi Sensor", styleSheet="color: #38bdf8; font-weight: bold;"))
        self.btn_group = QButtonGroup(self)
        self.radio_auto   = QRadioButton("Otomatis (Acak/Fluktuatif)")
        self.radio_safe   = QRadioButton("Aman (> 80 cm / Hijau)")
        self.radio_warn   = QRadioButton("Waspada (30 - 55 cm / Kuning)")
        self.radio_danger = QRadioButton("Bahaya / Tabrak (< 20 cm / Merah)")
        self.radio_auto.setChecked(True)
        for rb in [self.radio_auto, self.radio_safe, self.radio_warn, self.radio_danger]:
            self.btn_group.addButton(rb)
            layout_tel.addWidget(rb)
        self.radio_auto.toggled.connect(lambda: self._ubah_mode_telemetri("otomatis"))
        self.radio_safe.toggled.connect(lambda: self._ubah_mode_telemetri("aman"))
        self.radio_warn.toggled.connect(lambda: self._ubah_mode_telemetri("waspada"))
        self.radio_danger.toggled.connect(lambda: self._ubah_mode_telemetri("bahaya"))
        layout.addWidget(frame_telemetry)

        layout.addWidget(QLabel("<b>💾 JSON Data Telemetri Terkirim (Live):</b>", styleSheet="color: #00ff66;"))
        self.log_telemetri = QTextEdit()
        self.log_telemetri.setReadOnly(True)
        self.log_telemetri.setPlaceholderText("{ Kirim data dalam posisi OFF }")
        layout.addWidget(self.log_telemetri, 1)

        frame_cmd = QFrame()
        layout_cmd = QVBoxLayout(frame_cmd)
        layout_cmd.addWidget(QLabel("<b>Perintah Motor Terakhir (Dari BS):</b>", styleSheet="color: #ffb300;"))
        self.lbl_active_cmd = QLabel("■ STOP", alignment=Qt.AlignmentFlag.AlignCenter)
        self.lbl_active_cmd.setStyleSheet("font-size: 26px; font-weight: bold; color: #00ff66; font-family: Courier New;")
        layout_cmd.addWidget(self.lbl_active_cmd)
        layout.addWidget(frame_cmd)

    def _toggle_robot_streaming(self):
        self.worker.streaming_active = not self.worker.streaming_active
        if self.worker.streaming_active:
            self.worker.target_ip = self.input_ip.text().strip()
            self.input_ip.setEnabled(False)
            self.lbl_status.setText(f"<b>Status internal:</b> Mengirim ke {self.worker.target_ip}... 🔴")
            self.lbl_status.setStyleSheet("color: #ff5555; font-weight: bold;")
            self.btn_toggle_stream.setText("⏸️ MATIKAN PENGIRIMAN DATA (STREAM OFF)")
            self.btn_toggle_stream.setStyleSheet("background-color: #ef4444; color: white; height: 45px;")
        else:
            self.input_ip.setEnabled(True)
            self.lbl_status.setText("<b>Status internal:</b> Pengiriman Mati ⚪")
            self.lbl_status.setStyleSheet("color: white;")
            self.btn_toggle_stream.setText("🔴 AKTIFKAN PENGIRIMAN DATA (STREAM ON)")
            self.btn_toggle_stream.setStyleSheet("background-color: #22c55e; color: white; height: 45px;")
            self.log_telemetri.clear()

    def _ubah_mode_telemetri(self, mode_terpilih):
        self.worker.telemetry_mode = mode_terpilih

    def _ubah_fps_video(self, index):
        self.worker.video_fps = self.fps_options[index]

    def _ubah_interval_sensor(self, index):
        _, interval = self.sensor_options[index]
        self.worker.sensor_interval = interval

    def _on_command_received(self, cmd, sender_ip):
        if cmd in ["MAJU", "MUNDUR", "KIRI", "KANAN", "STOP"]:
            simbol_map = {"MAJU": "▲ MAJU", "MUNDUR": "▼ MUNDUR", "KIRI": "◀ KIRI", "KANAN": "▶ KANAN", "STOP": "■ STOP"}
            self.lbl_active_cmd.setText(simbol_map[cmd])

    def _on_telemetry_sent(self, json_text):
        self.log_telemetri.setText(json_text)

    def _on_gps_updated(self, lat, lon, accuracy):
        self.lbl_gps.setText(
            f"<b>📍 GPS:</b> Lat {lat:.6f}, Lon {lon:.6f} (±{accuracy:.0f} m) — akan dikirim ke Base Station"
        )
        self.lbl_gps.setStyleSheet("color: #22c55e; font-size: 11px;")

    def _on_calibration_updated(self, offset_s: float, n_samples: int):
        """Update label kalibrasi setiap kali offset baru dihitung."""
        offset_ms = offset_s * 1000.0
       
        if n_samples == 0:
            warna = "#f97316"
            status = "belum terkalibrasi"
        elif n_samples < 5:
            warna = "#fbbf24"
            status = f"kalibrasi awal ({n_samples}/5 sample)"
        else:
            warna = "#22c55e"
            status = "terkalibrasi (moving avg 5 sample)"
        self.lbl_calibration.setText(
            f"🕐 Clock Offset: {offset_ms:+.2f} ms — {status}"
        )
        self.lbl_calibration.setStyleSheet(f"color: {warna}; font-size: 11px;")

    def closeEvent(self, event):
        self.worker.running = False
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RobotSimulatorGUI()
    window.show()
    sys.exit(app.exec())