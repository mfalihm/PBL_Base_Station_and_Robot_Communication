# Sistem Kendali & Monitoring Robot (Base Station + Robot Simulator)

Aplikasi remote control dan monitoring robot berbasis PyQt6 yang berkomunikasi via UDP. Terdiri dari dua program terpisah yang berjalan sebagai proses/perangkat independen:

- **`base_station.py`** — GUI kendali di sisi operator (kirim perintah gerak, terima telemetri, video, GPS, dan metrik Network QoS).
- **`robot_simulator.py`** — GUI simulator robot (client) yang menerima perintah dan mengirim balik data sensor, video kamera, serta lokasi GPS.

## Fitur Utama

**Base Station**
- Kontrol arah robot (maju/mundur/kiri/kanan/stop) via tombol atau keyboard (W/A/S/D, arah panah, Space)
- Multi-robot: pilih target robot dari daftar IP, dengan indikator online/offline
- Tampilan video streaming real-time dari kamera robot
- Peta lokasi robot secara live (Leaflet + OpenStreetMap) berdasarkan GPS dari robot
- Monitoring sensor: jarak ultrasonik, 4 titik infrared, grafik riwayat jarak
- Pengukuran Network QoS end-to-end (throughput, latency, jitter, packet loss) terpisah untuk jalur telemetri dan video
- Estimasi one-way latency via mekanisme keepalive/pong (RTT)

**Robot Simulator**
- Mensimulasikan pengiriman data sensor (jarak, infrared) dengan beberapa skenario (otomatis, aman, waspada, bahaya)
- Streaming video dari webcam lokal (dipecah menjadi chunk UDP)
- Server GPS lokal berbasis browser (`http://localhost:8765`) yang membaca lokasi perangkat dan meneruskannya ke telemetri
- Kontrol frekuensi pengiriman (FPS video & interval sensor) yang bisa diatur langsung dari GUI
- Menampilkan status koneksi dan kalibrasi clock offset terhadap Base Station

## Arsitektur & Komunikasi (UDP)

| Jalur | Port | Arah | Keterangan |
|---|---|---|---|
| Perintah gerak + keepalive | `5005` | Base Station → Robot | JSON `{"cmd": ..., "ts": ...}` |
| Telemetri sensor + pong | `5006` | Robot → Base Station | JSON berisi jarak, infrared, GPS, timestamp |
| Video streaming | `9999` | Robot → Base Station | Frame JPEG terpecah (chunked) dengan header biner |
| GPS lokal (HTTP) | `8765` | Browser perangkat robot → Robot Simulator | Halaman web yang membaca `navigator.geolocation` |

Latency diestimasi menggunakan mekanisme **keepalive → pong** (mirip NTP sederhana): Base Station mengirim `ts` (waktu kirim), robot membalas dengan `pong`, lalu Base Station menghitung RTT dan menggunakan RTT/2 sebagai estimasi one-way latency (moving average 5 sampel).

## Requirements

Lihat `requirements.txt`. Ringkasannya:

- Python 3.10+ (disarankan, karena union type hint `float = None` gaya modern dan kompatibilitas PyQt6)
- [PyQt6](https://pypi.org/project/PyQt6/) — GUI
- [PyQt6-WebEngine](https://pypi.org/project/PyQt6-WebEngine/) — untuk `QWebEngineView` (peta Leaflet)
- [opencv-python](https://pypi.org/project/opencv-python/) — akses webcam & encode/decode JPEG
- [numpy](https://pypi.org/project/numpy/) — decode buffer gambar

## Instalasi

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

> Peta pada Base Station memuat tile OpenStreetMap dari internet (`https://{s}.tile.openstreetmap.org`) dan library Leaflet dari CDN (`unpkg.com`). Pastikan perangkat Base Station memiliki akses internet agar peta tampil dengan benar.

## Cara Menjalankan

1. **Jalankan Base Station** (di laptop operator):
   ```bash
   python base_station.py
   ```
   Pilih robot dari dropdown "Daftar Robot Aktif" (IP robot sudah didaftarkan di kode pada `self.daftar_ip_robot`, sesuaikan dengan IP aktual di jaringan Anda).

2. **Jalankan Robot Simulator** (di laptop/perangkat yang berperan sebagai robot):
   ```bash
   python robot_simulator.py
   ```
   Isi kolom "IP Base Station (Tujuan)" dengan IP dari perangkat Base Station, lalu klik **AKTIFKAN PENGIRIMAN DATA**.

3. **(Opsional) Aktifkan GPS**: buka `http://localhost:8765` di browser pada perangkat yang menjalankan Robot Simulator, izinkan akses lokasi. Lokasi akan otomatis ikut terkirim melalui payload telemetri ke Base Station.

4. Kendalikan robot dari Base Station menggunakan tombol arah di GUI atau keyboard (W/A/S/D, panah, Space untuk stop).

## Catatan

- Kedua aplikasi menggunakan port UDP tetap (`5005`, `5006`, `9999`) dan port HTTP `8765` — pastikan port-port ini tidak diblokir firewall dan tidak bentrok dengan aplikasi lain di jaringan yang sama.
- Robot Simulator butuh webcam yang terdeteksi sebagai device index `0` (`cv2.VideoCapture(0)`) agar fitur video berjalan.
- Skenario sensor pada Robot Simulator (Otomatis/Aman/Waspada/Bahaya) adalah data simulasi, bukan pembacaan sensor fisik sungguhan — cocok untuk pengujian tanpa hardware asli.
