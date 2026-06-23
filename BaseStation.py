import sys
import socket
import threading
import json
import cv2
import numpy as np
import struct
import time
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QLabel, QComboBox,
                             QPushButton, QTextEdit, QFrame)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QPointF
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont, QKeyEvent
from PyQt6.QtWebEngineWidgets import QWebEngineView


class FrameAssembler:
    def __init__(self):
        self.lock = threading.Lock()
        self.frames = {}
        self.MAX_FRAME_BUF = 30

    def add_chunk(self, fid, total, idx, data, frame_ts=None):
        with self.lock:
            if fid not in self.frames:
                if len(self.frames) >= self.MAX_FRAME_BUF:
                    del self.frames[min(self.frames)]
                self.frames[fid] = {'chunks': {}, 'total': total, 'ts': time.time(), 'frame_ts': None}
            if idx == 0 and frame_ts is not None:
                self.frames[fid]['frame_ts'] = frame_ts
            self.frames[fid]['chunks'][idx] = data

    def get_complete(self):
        with self.lock:
            for fid in sorted(self.frames):
                e = self.frames[fid]
                if len(e['chunks']) == e['total']:
                    raw = b''.join(e['chunks'][i] for i in range(e['total']))
                    frame_ts = e['frame_ts']
                    del self.frames[fid]
                    return raw, frame_ts
        return None, None

    def cleanup(self, max_age=2.0):
        now = time.time()
        with self.lock:
            stale = [f for f, e in self.frames.items() if now - e['ts'] > max_age]
            for f in stale:
                del self.frames[f]


class NetworkWorker(QObject):
    sensor_received = pyqtSignal(dict)
    video_received = pyqtSignal(QImage, str)
    qos_updated = pyqtSignal(dict)
    gps_from_robot = pyqtSignal(dict)
    calibration_updated = pyqtSignal(float, int)

    def __init__(self, gui_parent):
        super().__init__()
        self.gui = gui_parent
        
        # State QoS Sensor
        self._last_seq = None
        self._lost_packets = 0
        self._total_expected = 0
        self._last_latency_sensor = None
        self._jitter_sum = 0.0
        self._jitter_count = 0
        self._bytes_sensor = 0
        self._last_tp_time_sensor = time.time()
        self._throughput_sensor = 0.0
        
        # State QoS Video
        self._last_latency_video = None
        self._jitter_sum_video = 0.0
        self._jitter_count_video = 0
        self._last_video_fid = None
        self._lost_video_frames = 0
        self._total_expected_video = 0
        self._bytes_video = 0
        self._last_tp_time_video = time.time()
        self._throughput_video = 0.0

        # RTT clock offset
        self._clock_offset = 0.0
        self._offset_history = []
        self._offset_lock = threading.Lock()

        self._last_sensor_recv_ts = None
        self._keepalive_send_ts = None
        self._keepalive_lock = threading.Lock()

        self._active_lock = threading.Lock()
        self._active_ip = None

        self._presence_lock = threading.Lock()
        self._last_seen = {}

    def _mark_seen(self, ip):
        with self._presence_lock:
            self._last_seen[ip] = time.time()

    def is_online(self, ip, timeout=3.0):
        with self._presence_lock:
            ts = self._last_seen.get(ip)
        return ts is not None and (time.time() - ts) <= timeout

    def set_active_ip(self, ip):
        with self._active_lock:
            self._active_ip = ip
            self._last_seq = None
            self._lost_packets = 0
            self._total_expected = 0
            self._last_latency_sensor = None
            self._jitter_sum = 0.0
            self._jitter_count = 0
            self._bytes_sensor = 0
            
            self._last_latency_video = None
            self._jitter_sum_video = 0.0
            self._jitter_count_video = 0
            self._last_video_fid = None
            self._lost_video_frames = 0
            self._total_expected_video = 0
            self._bytes_video = 0

    def get_active_ip(self):
        with self._active_lock:
            return self._active_ip

    def clear_video_buffer(self):
        assembler = getattr(self, "_video_assembler", None)
        if assembler is not None:
            with assembler.lock:
                assembler.frames.clear()

    def _handle_pong(self, echo_ts: float, recv_pong_ts: float, t2: float = None, t3: float = None):
        with self._keepalive_lock:
            t_send = self._keepalive_send_ts
        if echo_ts is None or t_send is None:
            return

        # Buang pong "basi" — balasan untuk ronde keepalive sebelumnya yang
        # baru tiba setelah BS mengirim keepalive ronde baru (t_send sudah
        # ditimpa). Tanpa guard ini, echo_ts (ronde lama) dan t_send (ronde
        # baru) akan tercampur dan menghasilkan sample offset/RTT yang salah.
        if abs(echo_ts - t_send) > 1e-6:
            return

        rtt = recv_pong_ts - t_send
        if rtt < 0 or rtt > 5.0:
            return

        if t2 is not None and t3 is not None:
            # Rumus NTP 4-timestamp: T1=t_send(BS), T2=robot recv, T3=robot send, T4=recv_pong_ts(BS)
            # offset = (BS_clock - robot_clock) supaya tanda-nya cocok dengan koreksi di _sensor_listener
            offset_sample = ((t_send - t2) + (recv_pong_ts - t3)) / 2.0
        else:
            # fallback lama (TIDAK akurat — cuma RTT/2, dibiarkan sebagai jaga-jaga saja)
            offset_sample = recv_pong_ts - echo_ts - (rtt / 2.0)

        with self._offset_lock:
            self._offset_history.append(offset_sample)
            if len(self._offset_history) > 5:
                self._offset_history.pop(0)
            self._clock_offset = sum(self._offset_history) / len(self._offset_history)
            n = len(self._offset_history)
        self.calibration_updated.emit(self._clock_offset, n)

    def start_listeners(self):
        threading.Thread(target=self._sensor_listener, daemon=True).start()
        threading.Thread(target=self._video_listener, daemon=True).start()

    def _sensor_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 5006))
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                self._mark_seen(addr[0])

                active_ip = self.get_active_ip()
                if active_ip is not None and addr[0] != active_ip:
                    continue

                recv_ts = time.time()
                
                # --- Hitung Throughput Sensor ---
                self._bytes_sensor += len(data)
                if recv_ts - self._last_tp_time_sensor >= 1.0:
                    self._throughput_sensor = (self._bytes_sensor * 8) / 1000.0 # Kbps
                    self._bytes_sensor = 0
                    self._last_tp_time_sensor = recv_ts

                self._last_sensor_recv_ts = recv_ts
                payload = json.loads(data.decode("utf-8"))

                if payload.get("type") == "pong":
                    self._handle_pong(payload.get("echo_ts"), recv_ts,
                                       payload.get("t2"), payload.get("t3"))
                    continue

                latency_ms = None
                jitter_ms = None
                packet_loss_pct = None

                epoch_ts = payload.get("epoch_ts")
                if epoch_ts is not None:
                    with self._offset_lock:
                        clock_offset = self._clock_offset
                    raw_latency_ms = (recv_ts - epoch_ts) * 1000.0
                    latency_ms = raw_latency_ms - (clock_offset * 1000.0)

                    if self._last_latency_sensor is not None:
                        diff = abs(latency_ms - self._last_latency_sensor)
                        self._jitter_sum += diff
                        self._jitter_count += 1
                    self._last_latency_sensor = latency_ms
                    jitter_ms = (self._jitter_sum / self._jitter_count) if self._jitter_count > 0 else 0.0

                seq = payload.get("seq")
                if seq is not None:
                    if self._last_seq is not None:
                        gap = seq - self._last_seq - 1
                        if gap > 0:
                            self._lost_packets += gap
                        self._total_expected += (seq - self._last_seq)
                        packet_loss_pct = (self._lost_packets / self._total_expected * 100.0) if self._total_expected > 0 else 0.0
                    self._last_seq = seq

                self.sensor_received.emit(payload)

                gps = payload.get("gps")
                if gps and gps.get("lat") is not None and gps.get("lon") is not None:
                    self.gps_from_robot.emit({
                        "lat": gps.get("lat"),
                        "lon": gps.get("lon"),
                        "accuracy": gps.get("accuracy"),
                        "ip": addr[0]
                    })

                if latency_ms is not None:
                    self.qos_updated.emit({
                        "type": "sensor",
                        "throughput_kbps": self._throughput_sensor,
                        "latency_ms": latency_ms,
                        "jitter_ms": jitter_ms,
                        "packet_loss_pct": packet_loss_pct
                    })
            except Exception:
                pass

    def _video_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        sock.bind(("0.0.0.0", 9999))
        assembler = FrameAssembler()
        self._video_assembler = assembler
        HEADER_FORMAT = '>I H H I d'
        HEADER_SZ = struct.calcsize(HEADER_FORMAT)

        while True:
            try:
                packet, addr = sock.recvfrom(65535)

                active_ip = self.get_active_ip()
                if active_ip is not None and addr[0] != active_ip:
                    continue

                if len(packet) < HEADER_SZ:
                    continue
                    
                # --- Hitung Throughput Video ---
                recv_ts = time.time()
                self._bytes_video += len(packet)
                if recv_ts - self._last_tp_time_video >= 1.0:
                    self._throughput_video = (self._bytes_video * 8) / 1000.0 # Kbps
                    self._bytes_video = 0
                    self._last_tp_time_video = recv_ts
                    
                fid, total_c, chunk_id, dlen, frame_ts = struct.unpack(HEADER_FORMAT, packet[:HEADER_SZ])
                
                # --- Hitung Video Frame Loss ---
                if self._last_video_fid is not None and fid != self._last_video_fid:
                    if fid > self._last_video_fid: 
                        gap = fid - self._last_video_fid - 1
                        if gap > 0:
                            self._lost_video_frames += gap
                        self._total_expected_video += (fid - self._last_video_fid)
                        self._last_video_fid = fid
                elif self._last_video_fid is None:
                    self._last_video_fid = fid
                    
                video_loss_pct = (self._lost_video_frames / self._total_expected_video * 100.0) if self._total_expected_video > 0 else 0.0

                assembler.add_chunk(fid, total_c, chunk_id, packet[HEADER_SZ:HEADER_SZ + dlen], frame_ts)
                assembler.cleanup()

                raw, stored_frame_ts = assembler.get_complete()
                if raw:
                    recv_ts = time.time()
                    arr = np.frombuffer(raw, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        frame = cv2.resize(frame, (500, 350))
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        h, w, ch = rgb.shape
                        bytes_per_line = ch * w
                        qt_img = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                        self.video_received.emit(qt_img, addr[0])

                        if stored_frame_ts is not None:
                            raw_video_lat = (recv_ts - stored_frame_ts) * 1000.0
                            with self._offset_lock:
                                clock_offset = self._clock_offset
                            video_latency_ms = raw_video_lat - (clock_offset * 1000.0)
                            
                            # Kalkulasi Jitter Video
                            video_jitter_ms = 0.0
                            if self._last_latency_video is not None:
                                diff = abs(video_latency_ms - self._last_latency_video)
                                self._jitter_sum_video += diff
                                self._jitter_count_video += 1
                                video_jitter_ms = (self._jitter_sum_video / self._jitter_count_video)
                            self._last_latency_video = video_latency_ms

                            self.qos_updated.emit({
                                "type": "video",
                                "throughput_kbps": self._throughput_video,
                                "latency_ms": video_latency_ms,
                                "jitter_ms": video_jitter_ms,
                                "packet_loss_pct": video_loss_pct
                            })
            except Exception:
                pass


class GraphWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.history = []
        self.setStyleSheet("background-color: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 5px;")

    def update_data(self, history):
        self.history = list(history)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if len(self.history) < 2:
            return
        w, h = self.width(), self.height()
        maks = max(self.history) or 1
        n = len(self.history)

        points = [QPointF(int(i / (n - 1) * (w - 10)) + 5, int((1 - v / maks) * (h - 10)) + 5) for i, v in enumerate(self.history)]
        pen = QPen(QColor("#1976D2"), 2)
        painter.setPen(pen)
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])
        painter.setBrush(QColor("#F57C00"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(points[-1], 4, 4)


class BaseStationGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sistem Kendali Robot - Base Station Pamdal")
        self.setGeometry(100, 100, 1400, 900)
        self.setMinimumSize(1250, 850)

        self.daftar_ip_robot = {
            "Select Robot": "100.100.100.100",
            "Robot 1 (Laptop Falih)": "100.100.1.7",
            "Robot 2 (Laptop Faris)": "100.100.1.4",
            "Robot 3 (Laptop Dwi)": "100.100.1.5",
            "Local Test (Satu Laptop)": "100.100.1.3"
        }

        self.cmd_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.held_keys = set()
        self.hold_perintah = None
        self.history_jarak = []
        self.current_speed = 0.0
        self.target_speed = 0.0
        self._map_initialized = False
        self._pending_gps = None
        self._last_video_image = None

        self.setStyleSheet("""
            QMainWindow { background-color: #f4f6f9; }
            QFrame { background-color: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; color: #333333; }
            QLabel { color: #333333; font-family: 'Segoe UI', Arial; border: none; }
            QComboBox { background-color: #ffffff; color: #333333; padding: 5px; border: 1px solid #cccccc; border-radius: 5px; }
            QTextEdit { background-color: #ffffff; color: #2E7D32; font-family: 'Courier New'; border: 1px solid #cccccc; border-radius: 5px; }
        """)

        self._build_ui()
        self.worker = NetworkWorker(self)
        self.worker.sensor_received.connect(self._update_sensor_ui)
        self.worker.video_received.connect(self._update_video_ui)
        self.worker.qos_updated.connect(self._update_qos_ui)
        self.worker.gps_from_robot.connect(self._update_map_from_robot)
        self.worker.calibration_updated.connect(self._on_calibration_updated)
        self.worker.set_active_ip(self.get_current_ip())
        self.worker.start_listeners()

        self.timer_cmd = QTimer()
        self.timer_cmd.timeout.connect(self._auto_repeat_cmd)
        self.timer_cmd.start(100)

        self.timer_speed = QTimer()
        self.timer_speed.timeout.connect(self._simulate_speed_data)
        self.timer_speed.start(50)

        self.timer_map = QTimer()
        self.timer_map.timeout.connect(self._apply_pending_gps)
        self.timer_map.start(1000)

        self.timer_status = QTimer()
        self.timer_status.timeout.connect(self._refresh_robot_status)
        self.timer_status.start(1000)
        self._refresh_robot_status()

        self.timer_keepalive = QTimer()
        self.timer_keepalive.timeout.connect(self._kirim_keepalive)
        self.timer_keepalive.start(1000)
        QTimer.singleShot(200, self._kirim_keepalive)

        self.log_pesan("☑ Base Station Siap. Mengirim keepalive ke robot untuk kalibrasi clock offset...")

    def _build_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        lbl_header = QLabel("BASE STATION REMOTE CONTROL AND MONITORING TELEMETRI")
        lbl_header.setFont(QFont("Courier New", 18, QFont.Weight.Bold))
        lbl_header.setStyleSheet("color: #282828; background: transparent;")
        lbl_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(lbl_header)

        panel_layout = QHBoxLayout()
        main_layout.addLayout(panel_layout)

        # ── KOLOM KIRI ──
        left_frame = QFrame()
        left_layout = QVBoxLayout(left_frame)
        panel_layout.addWidget(left_frame, 1)

        left_layout.addWidget(QLabel("Daftar Robot Aktif", font=QFont("Arial", 10, QFont.Weight.Bold)))
        self.robot_selector = QComboBox()
        for nama in self.daftar_ip_robot.keys():
            self.robot_selector.addItem(nama)
        for i in range(self.robot_selector.count()):
            self.robot_selector.setItemData(i, self.robot_selector.itemText(i), Qt.ItemDataRole.UserRole)
        self.robot_selector.setCurrentIndex(
            self.robot_selector.findData("Select Robot", Qt.ItemDataRole.UserRole)
        )
        self.robot_selector.currentIndexChanged.connect(self._on_robot_change)
        left_layout.addWidget(self.robot_selector)

        calib_frame = QFrame()
        calib_frame.setStyleSheet("background-color: #f0f4ff; border: 1px solid #c7d2fe; border-radius: 6px; padding: 4px;")
        calib_layout = QVBoxLayout(calib_frame)
        calib_layout.setSpacing(2)
        lbl_calib_title = QLabel("🕐 Status Kalibrasi Clock Offset (RTT)")
        lbl_calib_title.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        lbl_calib_title.setStyleSheet("color: #3730a3; border: none;")
        calib_layout.addWidget(lbl_calib_title)
        self.lbl_calib_offset = QLabel("Offset: -- ms")
        self.lbl_calib_offset.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        self.lbl_calib_offset.setStyleSheet("color: #9ca3af; border: none;")
        calib_layout.addWidget(self.lbl_calib_offset)
        self.lbl_calib_status = QLabel("⏳ Menunggu pong dari robot...")
        self.lbl_calib_status.setFont(QFont("Arial", 8))
        self.lbl_calib_status.setStyleSheet("color: #9ca3af; border: none;")
        calib_layout.addWidget(self.lbl_calib_status)
        left_layout.addWidget(calib_frame)

        left_layout.addWidget(QLabel("Tombol Kontrol", font=QFont("Arial", 10, QFont.Weight.Bold)))

        dpad_container = QHBoxLayout()
        dpad_container.addStretch()
        grid_dpad = QGridLayout()
        grid_dpad.setSpacing(10)
        dpad_container.addLayout(grid_dpad)
        dpad_container.addStretch()
        left_layout.addLayout(dpad_container)

        self.btn_maju = QPushButton("↑")
        self.btn_maju.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; border-radius: 5px; border: none; font-size: 20px; font-weight: bold;} QPushButton:hover { background-color: #43A047; } QPushButton:pressed { background-color: #2E7D32; border: 2px inset #1B5E20; }")
        self.btn_maju.pressed.connect(lambda: self._start_hold("MAJU"))
        self.btn_maju.released.connect(self._stop_hold)

        self.btn_kiri = QPushButton("←")
        self.btn_kiri.setStyleSheet("QPushButton { background-color: #2196F3; color: white; border-radius: 5px; border: none; font-size: 20px; font-weight: bold;} QPushButton:hover { background-color: #1E88E5; } QPushButton:pressed { background-color: #1565C0; border: 2px inset #0D47A1; }")
        self.btn_kiri.pressed.connect(lambda: self._start_hold("KIRI"))
        self.btn_kiri.released.connect(self._stop_hold)

        self.btn_stop = QPushButton("■")
        self.btn_stop.setStyleSheet("QPushButton { background-color: #F44336; color: white; border-radius: 5px; border: none; font-size: 16px; } QPushButton:hover { background-color: #E53935; } QPushButton:pressed { background-color: #C62828; border: 2px inset #b71c1c; }")
        self.btn_stop.pressed.connect(lambda: self._start_hold("STOP"))
        self.btn_stop.released.connect(self._stop_hold)

        self.btn_kanan = QPushButton("→")
        self.btn_kanan.setStyleSheet("QPushButton { background-color: #2196F3; color: white; border-radius: 5px; border: none; font-size: 20px; font-weight: bold;} QPushButton:hover { background-color: #1E88E5; } QPushButton:pressed { background-color: #1565C0; border: 2px inset #0D47A1; }")
        self.btn_kanan.pressed.connect(lambda: self._start_hold("KANAN"))
        self.btn_kanan.released.connect(self._stop_hold)

        self.btn_mundur = QPushButton("↓")
        self.btn_mundur.setStyleSheet("QPushButton { background-color: #FFC107; color: #333333; border-radius: 5px; border: none; font-size: 20px; font-weight: bold; } QPushButton:hover { background-color: #FFB300; } QPushButton:pressed { background-color: #FF8F00; border: 2px inset #FF6F00; color: white; }")
        self.btn_mundur.pressed.connect(lambda: self._start_hold("MUNDUR"))
        self.btn_mundur.released.connect(self._stop_hold)

        for btn in [self.btn_maju, self.btn_kiri, self.btn_stop, self.btn_kanan, self.btn_mundur]:
            btn.setFixedSize(70, 45)

        grid_dpad.addWidget(self.btn_maju, 0, 1)
        grid_dpad.addWidget(self.btn_kiri, 1, 0)
        grid_dpad.addWidget(self.btn_stop, 1, 1)
        grid_dpad.addWidget(self.btn_kanan, 1, 2)
        grid_dpad.addWidget(self.btn_mundur, 2, 1)

        lbl_hint = QLabel("⌨ W A S D / ↑↓←→ / SPACE=STOP")
        lbl_hint.setStyleSheet("color: #9e9e9e; font-size: 10px; background: transparent;")
        lbl_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_layout.addWidget(lbl_hint)

        left_layout.addWidget(QLabel("Data Sensor Real-Time", font=QFont("Arial", 10, QFont.Weight.Bold)))

        sensor_layout = QHBoxLayout()
        box_speed = QVBoxLayout()
        lbl_kecepatan = QLabel("Kecepatan", font=QFont("Arial", 8))
        lbl_kecepatan.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_speed.addWidget(lbl_kecepatan)
        self.lbl_val_speed = QLabel("0.0 m/s")
        self.lbl_val_speed.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        self.lbl_val_speed.setStyleSheet("background-color: #f8f9fa; border: 1px solid #e0e0e0; padding: 5px; color: #1976D2;")
        self.lbl_val_speed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_speed.addWidget(self.lbl_val_speed)
        sensor_layout.addLayout(box_speed)

        box_jarak = QVBoxLayout()
        lbl_jarak = QLabel("Ultrasonik", font=QFont("Arial", 8))
        lbl_jarak.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_jarak.addWidget(lbl_jarak)
        self.lbl_val_jarak = QLabel("-- cm")
        self.lbl_val_jarak.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        self.lbl_val_jarak.setStyleSheet("background-color: #f8f9fa; border: 1px solid #e0e0e0; padding: 5px;")
        self.lbl_val_jarak.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_jarak.addWidget(self.lbl_val_jarak)
        sensor_layout.addLayout(box_jarak)

        left_layout.addLayout(sensor_layout)

        self.lbl_status_jarak = QLabel("Status Jarak: Menunggu...")
        self.lbl_status_jarak.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_layout.addWidget(self.lbl_status_jarak)

        left_layout.addWidget(QLabel("Infrared (4 titik)", font=QFont("Arial", 10, QFont.Weight.Bold)))
        ir_frame = QFrame()
        ir_frame.setStyleSheet("background-color: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 5px;")
        ir_layout = QHBoxLayout(ir_frame)
        self.ir_ui = {}
        ir_keys = ["ir_kiri", "ir_tengah_kiri", "ir_tengah_kanan", "ir_kanan"]
        ir_names = ["Kiri", "T-Kiri", "T-Kanan", "Kanan"]

        for k, name in zip(ir_keys, ir_names):
            v_box = QVBoxLayout()
            bulat = QLabel("⬤")
            bulat.setFont(QFont("Arial", 20))
            bulat.setStyleSheet("color: #cccccc; border: none; background: transparent;")
            bulat.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_name = QLabel(name)
            lbl_name.setFont(QFont("Arial", 8))
            lbl_name.setStyleSheet("border: none; background: transparent;")
            lbl_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v_box.addWidget(bulat)
            v_box.addWidget(lbl_name)
            ir_layout.addLayout(v_box)
            self.ir_ui[k] = bulat

        left_layout.addWidget(ir_frame)

        left_layout.addWidget(QLabel("Riwayat Jarak", font=QFont("Arial", 10, QFont.Weight.Bold)))
        self.graph_widget = GraphWidget()
        self.graph_widget.setMinimumHeight(80)
        left_layout.addWidget(self.graph_widget)

        # ==========================================
        # 5. Network QoS (Bersebelahan Kiri & Kanan)
        # ==========================================
        left_layout.addWidget(QLabel("Network QoS (End-to-End)", font=QFont("Arial", 10, QFont.Weight.Bold)))
        qos_horizontal_layout = QHBoxLayout()

        def _qos_label(text):
            lbl = QLabel(text)
            lbl.setFont(QFont("Courier New", 8))
            lbl.setStyleSheet("color: #555555; border: none;")
            return lbl

        def _qos_value(text="--", color="#1976D2"):
            lbl = QLabel(text)
            lbl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
            lbl.setStyleSheet(f"color: {color}; border: none;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return lbl

        # ─── BAGIAN KIRI (QoS TELEMETRI / SENSOR) ───
        qos_sensor_vbox = QVBoxLayout()
        qos_sensor_vbox.addWidget(QLabel("📡 Telemetri", font=QFont("Arial", 9, QFont.Weight.Bold)))
        
        qos_sensor_frame = QFrame()
        qos_sensor_frame.setStyleSheet("background-color: #f8f9fa; padding: 4px; border: 1px solid #e0e0e0; border-radius: 5px;")
        qos_sensor_layout = QGridLayout(qos_sensor_frame)
        qos_sensor_layout.setSpacing(2)

        qos_sensor_layout.addWidget(_qos_label("Thr:"), 0, 0)
        self.lbl_qos_thr_sensor = _qos_value(color="#00796B")
        qos_sensor_layout.addWidget(self.lbl_qos_thr_sensor, 0, 1)

        qos_sensor_layout.addWidget(_qos_label("Lat:"), 1, 0)
        self.lbl_qos_lat_sensor = _qos_value()
        qos_sensor_layout.addWidget(self.lbl_qos_lat_sensor, 1, 1)

        qos_sensor_layout.addWidget(_qos_label("Jit:"), 2, 0)
        self.lbl_qos_jitter_sensor = _qos_value(color="#F57C00")
        qos_sensor_layout.addWidget(self.lbl_qos_jitter_sensor, 2, 1)

        qos_sensor_layout.addWidget(_qos_label("Loss:"), 3, 0)
        self.lbl_qos_loss_sensor = _qos_value(color="#D32F2F")
        qos_sensor_layout.addWidget(self.lbl_qos_loss_sensor, 3, 1)

        qos_sensor_vbox.addWidget(qos_sensor_frame)
        qos_horizontal_layout.addLayout(qos_sensor_vbox)

        # ─── BAGIAN KANAN (QoS VIDEO FEED) ───
        qos_video_vbox = QVBoxLayout()
        qos_video_vbox.addWidget(QLabel("🎞️ Video", font=QFont("Arial", 9, QFont.Weight.Bold)))
        
        qos_video_frame = QFrame()
        qos_video_frame.setStyleSheet("background-color: #f8f9fa; padding: 4px; border: 1px solid #e0e0e0; border-radius: 5px;")
        qos_video_layout = QGridLayout(qos_video_frame)
        qos_video_layout.setSpacing(2)

        qos_video_layout.addWidget(_qos_label("Thr:"), 0, 0)
        self.lbl_qos_thr_video = _qos_value(color="#00796B")
        qos_video_layout.addWidget(self.lbl_qos_thr_video, 0, 1)

        qos_video_layout.addWidget(_qos_label("Lat:"), 1, 0)
        self.lbl_qos_lat_video = _qos_value(color="#7C4DFF")
        qos_video_layout.addWidget(self.lbl_qos_lat_video, 1, 1)

        qos_video_layout.addWidget(_qos_label("Jit:"), 2, 0)
        self.lbl_qos_jitter_video = _qos_value(color="#E040FB")
        qos_video_layout.addWidget(self.lbl_qos_jitter_video, 2, 1)

        qos_video_layout.addWidget(_qos_label("Loss:"), 3, 0)
        self.lbl_qos_loss_video = _qos_value(color="#D32F2F")
        qos_video_layout.addWidget(self.lbl_qos_loss_video, 3, 1)

        qos_video_vbox.addWidget(qos_video_frame)
        qos_horizontal_layout.addLayout(qos_video_vbox)

        left_layout.addLayout(qos_horizontal_layout)

        left_layout.addWidget(QLabel("Log Pengiriman", font=QFont("Arial", 10, QFont.Weight.Bold)))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        left_layout.addWidget(self.log_box, 1)

        # ── KOLOM KANAN ──
        right_main_frame = QFrame()
        right_layout = QVBoxLayout(right_main_frame)
        panel_layout.addWidget(right_main_frame, 3)

        lbl_vtitle = QLabel("TAMPILAN VIDEO KAMERA (LIVE)")
        lbl_vtitle.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        lbl_vtitle.setStyleSheet("background: transparent; border: none;")
        lbl_vtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(lbl_vtitle)

        self.lbl_ping = QLabel("Menunggu kiriman data masuk dari IP manapun...")
        self.lbl_ping.setStyleSheet("color: #757575; background: transparent; border: none;")
        self.lbl_ping.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.lbl_ping)

        self.lbl_video_feed = QLabel("[ AREA VIDEO STREAMING ]\n\nVideo otomatis muncul saat stream diaktifkan dari robot")
        self.lbl_video_feed.setStyleSheet("background-color: #eeeeee; border: 1px dashed #bdbdbd; color: #757575;")
        self.lbl_video_feed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_video_feed.setMinimumSize(1, 1)
        right_layout.addWidget(self.lbl_video_feed, 2)

        lbl_mtitle = QLabel("📍 Live Maps - Lokasi Robot (GPS dari Robot)")
        lbl_mtitle.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        lbl_mtitle.setStyleSheet("background: transparent; border: none; margin-top: 10px;")
        lbl_mtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(lbl_mtitle)

        self.lbl_geo_info = QLabel("Menunggu data lokasi...")
        self.lbl_geo_info.setStyleSheet("color: #757575; font-size: 10px; background: transparent; border: none;")
        self.lbl_geo_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.lbl_geo_info)

        self.map_view = QWebEngineView()
        self.map_view.setMinimumHeight(220)
        self.map_view.setHtml(self._build_map_html(None, None))
        right_layout.addWidget(self.map_view, 1)

    def _on_calibration_updated(self, offset_s: float, n_samples: int):
        offset_ms = offset_s * 1000.0
        self.lbl_calib_offset.setText(f"Offset: {offset_ms:+.2f} ms")
        if n_samples < 5:
            warna = "#d97706"
            status_txt = f"⏳ Kalibrasi awal... ({n_samples}/5 sample)"
        else:
            warna = "#16a34a"
            status_txt = "✅ Terkalibrasi (moving avg 5 sample)"
        self.lbl_calib_offset.setStyleSheet(f"color: {warna}; border: none; font-family: 'Courier New'; font-weight: bold;")
        self.lbl_calib_status.setText(status_txt)
        self.lbl_calib_status.setStyleSheet(f"color: {warna}; border: none; font-size: 8px;")

    def _build_map_html(self, lat, lon, label="Robot", accuracy=None):
        if lat is None or lon is None:
            return """
            <html><body style="margin:0;background:#eeeeee;display:flex;align-items:center;
            justify-content:center;height:100vh;color:#757575;font-family:Arial;font-size:13px;">
            <div style="text-align:center;">
                <div style="font-size:28px;margin-bottom:8px;">🗺️</div>
                Menunggu data GPS dari robot...<br>
                <small style="color:#9e9e9e">Buka <b style="color:#1976D2">http://localhost:8765</b> di robot untuk mengaktifkan GPS</small>
            </div></body></html>
            """
        radius = max(5, int(accuracy)) if accuracy and accuracy < 2000 else 30
        return f"""<!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8"/>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <style>
                body {{ margin:0; padding:0; background:#eeeeee; }}
                #map {{ width:100%; height:100vh; }}
            </style>
        </head>
        <body>
        <div id="map"></div>
        <script>
            var map = L.map('map', {{zoomControl:true}}).setView([{lat},{lon}], 17);
            L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                attribution:'© OpenStreetMap', maxZoom:19
            }}).addTo(map);
            var icon = L.divIcon({{
                html: '<div style="font-size:28px;line-height:1;">🤖</div>',
                iconSize:[30,30], iconAnchor:[15,30], className:''
            }});
            var robotMarker = L.marker([{lat},{lon}], {{icon:icon}})
                .addTo(map)
                .bindPopup('<b>{label}</b><br>Lat:{lat:.6f}<br>Lon:{lon:.6f}<br>±{radius}m')
                .openPopup();
            var robotCircle = L.circle([{lat},{lon}], {{
                color:'#1976D2', fillColor:'#1976D2',
                fillOpacity:0.12, radius:{radius}
            }}).addTo(map);

            window.updateRobotPosition = function(lat, lon, label, radius) {{
                var pos = [lat, lon];
                robotMarker.setLatLng(pos);
                robotCircle.setLatLng(pos);
                robotCircle.setRadius(radius);
                robotMarker.setPopupContent(
                    '<b>' + label + '</b><br>Lat:' + lat.toFixed(6) +
                    '<br>Lon:' + lon.toFixed(6) + '<br>±' + radius + 'm'
                );
                map.panTo(pos, {{animate: true, duration: 0.8}});
            }};
        </script>
        </body></html>
        """

    def _update_map_from_robot(self, geo: dict):
        self._pending_gps = geo

    def _apply_pending_gps(self):
        geo = self._pending_gps
        if geo is None:
            return
        lat = geo.get("lat")
        lon = geo.get("lon")
        accuracy = geo.get("accuracy")
        ip = geo.get("ip", "")
        nama_robot = self.get_current_robot_name()
        radius = max(5, int(accuracy)) if accuracy and accuracy < 2000 else 30

        acc_str = f"±{accuracy:.0f}m" if accuracy else ""
        self.lbl_geo_info.setText(
            f"📍 GPS Robot ({ip})  |  Lat: {lat:.6f}, Lon: {lon:.6f}  {acc_str}"
        )
        self.lbl_geo_info.setStyleSheet("color: #388E3C; font-size: 10px; background: transparent; border: none;")

        if not self._map_initialized:
            self.map_view.setHtml(self._build_map_html(lat, lon, label=nama_robot, accuracy=accuracy))
            self._map_initialized = True
        else:
            label_js = json.dumps(nama_robot)
            js = f"window.updateRobotPosition({lat}, {lon}, {label_js}, {radius});"
            self.map_view.page().runJavaScript(js)

        self.log_pesan(f"📍 GPS Robot → {lat:.6f}, {lon:.6f} {acc_str}")

    def log_pesan(self, teks):
        waktu = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{waktu}] {teks}")

    def get_current_robot_name(self):
        data = self.robot_selector.currentData(Qt.ItemDataRole.UserRole)
        return data if data is not None else self.robot_selector.currentText()

    def get_current_ip(self):
        nama_robot = self.get_current_robot_name()
        return self.daftar_ip_robot.get(nama_robot, "127.0.0.1")

    def _on_robot_change(self, index):
        if index < 0:
            return
        pilihan = self.robot_selector.itemData(index, Qt.ItemDataRole.UserRole)
        if pilihan is None:
            pilihan = self.robot_selector.itemText(index)
        ip_terpilih = self.daftar_ip_robot[pilihan]

        self.worker.set_active_ip(ip_terpilih)
        self.worker.clear_video_buffer()

        self.lbl_calib_offset.setText("Offset: -- ms")
        self.lbl_calib_offset.setStyleSheet("color: #9ca3af; border: none; font-family: 'Courier New'; font-weight: bold;")
        self.lbl_calib_status.setText("⏳ Menunggu pong dari robot...")
        self.lbl_calib_status.setStyleSheet("color: #9ca3af; border: none; font-size: 8px;")

        with self.worker._offset_lock:
            self.worker._offset_history.clear()
            self.worker._clock_offset = 0.0

        QTimer.singleShot(100, self._kirim_keepalive)

        self.log_pesan(f"🔄 Gerbang koneksi dipindah ke {pilihan} (IP: {ip_terpilih})")
        self.lbl_ping.setText(f"Menunggu kiriman data dari {ip_terpilih}...")
        self.lbl_ping.setStyleSheet("color: #757575; background: transparent; border: none;")
        self.lbl_video_feed.setText("[ AREA VIDEO STREAMING ]\n\nVideo otomatis muncul saat stream diaktifkan dari robot")
        self.lbl_video_feed.setStyleSheet("background-color: #eeeeee; border: 1px dashed #bdbdbd; color: #757575;")
        self.lbl_video_feed.setPixmap(QPixmap())
        self._last_video_image = None
        self.history_jarak.clear()
        self.graph_widget.update_data([])

        self.lbl_val_jarak.setText("-- cm")
        self.lbl_val_jarak.setStyleSheet("background-color: #f8f9fa; border: 1px solid #e0e0e0; padding: 5px;")
        self.lbl_status_jarak.setText("Status Jarak: Menunggu...")
        self.lbl_status_jarak.setStyleSheet("")
        for lbl in self.ir_ui.values():
            lbl.setStyleSheet("color: #cccccc; border: none; background: transparent;")
            
        # --- Reset Tampilan QoS ---
        self.lbl_qos_thr_sensor.setText("--")
        self.lbl_qos_lat_sensor.setText("--")
        self.lbl_qos_lat_sensor.setStyleSheet("color: #1976D2; border: none;")
        self.lbl_qos_jitter_sensor.setText("--")
        self.lbl_qos_loss_sensor.setText("--")
        self.lbl_qos_loss_sensor.setStyleSheet("color: #D32F2F; border: none;")
        
        self.lbl_qos_thr_video.setText("--")
        self.lbl_qos_lat_video.setText("--")
        self.lbl_qos_lat_video.setStyleSheet("color: #7C4DFF; border: none;")
        self.lbl_qos_jitter_video.setText("--")
        self.lbl_qos_loss_video.setText("--")
        self.lbl_qos_loss_video.setStyleSheet("color: #D32F2F; border: none;")

        self._map_initialized = False
        self._pending_gps = None
        self.map_view.setHtml(self._build_map_html(None, None))
        self.lbl_geo_info.setText("Menunggu data GPS dari robot...")
        self.lbl_geo_info.setStyleSheet("color: #757575; font-size: 10px; background: transparent; border: none;")

    def _refresh_robot_status(self):
        combo = self.robot_selector
        combo.blockSignals(True)
        try:
            for i in range(combo.count()):
                nama = combo.itemData(i, Qt.ItemDataRole.UserRole)
                if nama is None:
                    nama = combo.itemText(i)
                ip = self.daftar_ip_robot.get(nama, "")
                online = self.worker.is_online(ip) if hasattr(self, "worker") else False
                prefix = "🟢 " if online else "⚪ "
                combo.setItemText(i, prefix + nama)
        finally:
            combo.blockSignals(False)

    def _kirim_keepalive(self):
        ip_tujuan = self.get_current_ip()
        try:
            t_send = time.time()
            with self.worker._keepalive_lock:
                self.worker._keepalive_send_ts = t_send
            payload = json.dumps({
                "cmd": "KEEPALIVE",
                "ts": t_send
            }).encode("utf-8")
            self.cmd_socket.sendto(payload, (ip_tujuan, 5005))
        except Exception:
            pass

    def kirim_udp(self, pesan):
        ip_tujuan = self.get_current_ip()
        port = 5005
        try:
            t2 = self.worker._last_sensor_recv_ts
            t3 = time.time()
            if t2 is not None:
                pesan_rtt = f"{pesan}|t2={t2:.6f}|t3={t3:.6f}"
            else:
                pesan_rtt = pesan
            self.cmd_socket.sendto(pesan_rtt.encode("utf-8"), (ip_tujuan, port))
            if pesan != "KEEPALIVE":
                self.log_pesan(f"✉ Perintah sent → [{pesan}]")
        except Exception as e:
            if pesan != "KEEPALIVE":
                self.log_pesan(f"✗ Gagal kirim: {e}")

    def _start_hold(self, perintah):
        self.hold_perintah = perintah
        self.kirim_udp(perintah)
        if perintah in ["MAJU", "MUNDUR"]:
            self.target_speed = 2.5
        elif perintah in ["KIRI", "KANAN"]:
            self.target_speed = 1.2
        elif perintah == "STOP":
            self.target_speed = 0.0

    def _stop_hold(self):
        self.hold_perintah = None
        self.kirim_udp("STOP")
        self.target_speed = 0.0

    def _auto_repeat_cmd(self):
        if self.hold_perintah:
            self.kirim_udp(self.hold_perintah)

    def _simulate_speed_data(self):
        if self.current_speed < self.target_speed:
            self.current_speed += 0.2
            if self.current_speed > self.target_speed:
                self.current_speed = self.target_speed
        elif self.current_speed > self.target_speed:
            self.current_speed -= 0.3
            if self.current_speed < 0:
                self.current_speed = 0.0
        if self.current_speed < 0.1 and self.target_speed == 0:
            self.current_speed = 0.0

        warna = "#1976D2" if self.current_speed > 0 else "#757575"
        self.lbl_val_speed.setText(f"{self.current_speed:.1f} m/s")
        self.lbl_val_speed.setStyleSheet(f"background-color: #f8f9fa; border: 1px solid #e0e0e0; padding: 5px; color: {warna};")

    def keyPressEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        key, key_code = event.text().lower(), event.key()
        mapping = {'w': 'MAJU', 's': 'MUNDUR', 'a': 'KIRI', 'd': 'KANAN'}
        if key_code == Qt.Key.Key_Up:
            cmd = "MAJU"
        elif key_code == Qt.Key.Key_Down:
            cmd = "MUNDUR"
        elif key_code == Qt.Key.Key_Left:
            cmd = "KIRI"
        elif key_code == Qt.Key.Key_Right:
            cmd = "KANAN"
        elif key_code == Qt.Key.Key_Space:
            cmd = "STOP"
        elif key in mapping:
            cmd = mapping[key]
        else:
            return

        if cmd not in self.held_keys:
            self.held_keys.add(cmd)
            self._start_hold(cmd)
            if cmd == "MAJU": self.btn_maju.setDown(True)
            elif cmd == "MUNDUR": self.btn_mundur.setDown(True)
            elif cmd == "KIRI": self.btn_kiri.setDown(True)
            elif cmd == "KANAN": self.btn_kanan.setDown(True)
            elif cmd == "STOP": self.btn_stop.setDown(True)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        key, key_code = event.text().lower(), event.key()
        mapping = {'w': 'MAJU', 's': 'MUNDUR', 'a': 'KIRI', 'd': 'KANAN'}
        if key_code == Qt.Key.Key_Up:
            cmd = "MAJU"
        elif key_code == Qt.Key.Key_Down:
            cmd = "MUNDUR"
        elif key_code == Qt.Key.Key_Left:
            cmd = "KIRI"
        elif key_code == Qt.Key.Key_Right:
            cmd = "KANAN"
        elif key_code == Qt.Key.Key_Space:
            cmd = "STOP"
        elif key in mapping:
            cmd = mapping[key]
        else:
            return

        if cmd in self.held_keys:
            self.held_keys.remove(cmd)
            self._stop_hold()
            if cmd == "MAJU": self.btn_maju.setDown(False)
            elif cmd == "MUNDUR": self.btn_mundur.setDown(False)
            elif cmd == "KIRI": self.btn_kiri.setDown(False)
            elif cmd == "KANAN": self.btn_kanan.setDown(False)
            elif cmd == "STOP": self.btn_stop.setDown(False)

    def _update_sensor_ui(self, payload):
        jarak = payload.get("jarak_cm", 0)
        self.lbl_val_jarak.setText(f"{jarak:.1f} cm")

        if jarak < 20:
            self.lbl_val_jarak.setStyleSheet("background-color: #f8f9fa; border: 1px solid #e0e0e0; padding: 5px; color: #D32F2F;")
            self.lbl_status_jarak.setText("Status Jarak: ⚠ SANGAT DEKAT!")
            self.lbl_status_jarak.setStyleSheet("QLabel { color: #D32F2F; font-weight: bold; background: transparent; border: none; }")
        elif jarak < 60:
            self.lbl_val_jarak.setStyleSheet("background-color: #f8f9fa; border: 1px solid #e0e0e0; padding: 5px; color: #F57C00;")
            self.lbl_status_jarak.setText("Status Jarak: ⚡ DEKAT")
            self.lbl_status_jarak.setStyleSheet("QLabel { color: #F57C00; font-weight: bold; background: transparent; border: none; }")
        else:
            self.lbl_val_jarak.setStyleSheet("background-color: #f8f9fa; border: 1px solid #e0e0e0; padding: 5px; color: #388E3C;")
            self.lbl_status_jarak.setText("Status Jarak: ✓ AMAN")
            self.lbl_status_jarak.setStyleSheet("QLabel { color: #388E3C; font-weight: bold; background: transparent; border: none; }")

        ir = payload.get("infrared", {})
        for key, lbl in self.ir_ui.items():
            det = ir.get(key, {}).get("terdeteksi", False)
            if det:
                lbl.setStyleSheet("color: #D32F2F; border: none; background: transparent;")
            else:
                lbl.setStyleSheet("color: #cccccc; border: none; background: transparent;")

        self.history_jarak.append(jarak)
        if len(self.history_jarak) > 60:
            self.history_jarak.pop(0)
        self.graph_widget.update_data(self.history_jarak)

    def _update_video_ui(self, qt_image, sender_ip):
        self._last_video_image = qt_image
        self.lbl_video_feed.setStyleSheet("background-color: #000000; border: none; color: #757575;")
        self._render_video_frame()
        self.lbl_ping.setText(f"Sedang menerima data dari IP Robot: {sender_ip}")
        self.lbl_ping.setStyleSheet("color: #388E3C; background: transparent; border: none;")

    def _render_video_frame(self):
        qt_image = getattr(self, "_last_video_image", None)
        if qt_image is None:
            return
        pixmap = QPixmap.fromImage(qt_image)
        target_size = self.lbl_video_feed.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            return
        scaled = pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.lbl_video_feed.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_video_frame()

    def _update_qos_ui(self, qos: dict):
        qtype = qos.get("type")
        thr = qos.get("throughput_kbps")
        lat = qos.get("latency_ms")
        jit = qos.get("jitter_ms")
        loss = qos.get("packet_loss_pct")
        
        def _format_thr(val):
            if val is None: return "--"
            return f"{val/1000:.2f} Mbps" if val >= 1000 else f"{val:.1f} Kbps"

        if qtype == "sensor":
            self.lbl_qos_thr_sensor.setText(_format_thr(thr))
            
            if lat is not None:
                c = "#388E3C" if lat < 50 else ("#F57C00" if lat < 150 else "#D32F2F")
                self.lbl_qos_lat_sensor.setText(f"{lat:.1f} ms")
                self.lbl_qos_lat_sensor.setStyleSheet(f"color: {c}; border: none; font-family: 'Courier New'; font-weight: bold;")
            if jit is not None:
                self.lbl_qos_jitter_sensor.setText(f"{jit:.1f} ms")
            if loss is not None:
                c_loss = "#388E3C" if loss < 1.0 else ("#F57C00" if loss < 5.0 else "#D32F2F")
                self.lbl_qos_loss_sensor.setText(f"{loss:.2f} %")
                self.lbl_qos_loss_sensor.setStyleSheet(f"color: {c_loss}; border: none; font-family: 'Courier New'; font-weight: bold;")
                
        elif qtype == "video":
            self.lbl_qos_thr_video.setText(_format_thr(thr))
            
            if lat is not None:
                c = "#388E3C" if lat < 100 else ("#F57C00" if lat < 300 else "#D32F2F")
                self.lbl_qos_lat_video.setText(f"{lat:.1f} ms")
                self.lbl_qos_lat_video.setStyleSheet(f"color: {c}; border: none; font-family: 'Courier New'; font-weight: bold;")
            if jit is not None:
                self.lbl_qos_jitter_video.setText(f"{jit:.1f} ms")
            if loss is not None:
                c_loss = "#388E3C" if loss < 5.0 else ("#F57C00" if loss < 15.0 else "#D32F2F")
                self.lbl_qos_loss_video.setText(f"{loss:.2f} %")
                self.lbl_qos_loss_video.setStyleSheet(f"color: {c_loss}; border: none; font-family: 'Courier New'; font-weight: bold;")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = BaseStationGUI()
    window.show()
    sys.exit(app.exec())