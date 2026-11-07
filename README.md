# Indodax Momentum Bot — Paper Mode

Dashboard lokal Python untuk memindai pasangan IDR, menganalisis OHLC multi-timeframe, dan menguji aturan modal melalui paper trading. **Scanner dan dashboard tetap paper-only; tidak ada rute dashboard yang membuat atau membatalkan order nyata.**

## Menjalankan

1. Pastikan Python 3.10 atau lebih baru tersedia.
2. Dari folder project, jalankan `python bot.py`.
3. Buka `http://127.0.0.1:8765`.
4. Pilih **Mulai scanner**. Pemindaian dilakukan setiap 30 detik.

Dependensi runtime hanya standard library Python. Ledger paper disimpan lokal di `paper_trading.sqlite3`.

Host dan port bisa dikonfigurasi dengan `BOT_HOST`, `BOT_PORT`, dan `BOT_ALLOWED_HOST`. Default-nya hanya localhost.

## VPS Biznet Gio tanpa Nginx (paper mode sementara)

Konfigurasi systemd dan environment contoh tersedia di folder `deploy/`. Jalur ini membuat aplikasi Python melayani dashboard langsung pada `http://IP-VPS:8765`; **tidak memakai Vercel atau Nginx**.

> **Peringatan:** IP:port HTTP tidak mengenkripsi lalu lintas dan dashboard ini belum memakai login. Gunakan hanya sementara untuk paper mode, jangan masukkan API key/secret, dan tutup kembali akses publik setelah selesai. Private API preflight otomatis ditolak untuk koneksi non-lokal.

Asumsi: Ubuntu/Debian, repository GitHub sudah dibuat, dan SSH ke VPS berfungsi. Contoh di bawah ini mengikuti folder `/var/www/fix-bot-trading` yang digunakan saat clone.

1. Siapkan host:

   ```bash
   sudo apt update
   sudo apt install -y git python3 python3-venv
   sudo id indodaxbot >/dev/null 2>&1 || sudo useradd --system --user-group --create-home --home-dir /var/lib/indodax-bot --shell /usr/sbin/nologin indodaxbot
   sudo mkdir -p /var/www/fix-bot-trading
   sudo chown -R indodaxbot:indodaxbot /var/www/fix-bot-trading
   ```

2. Clone repository private dengan deploy key GitHub yang hanya memiliki akses baca:

   ```bash
   # Repository sudah di-clone? Gunakan folder itu; jangan clone ulang ke dalamnya.
   sudo -u indodaxbot git -C /var/www/fix-bot-trading remote -v
   sudo -u indodaxbot python3 -m venv /var/www/fix-bot-trading/.venv
   sudo -u indodaxbot sh -c 'cd /var/www/fix-bot-trading && .venv/bin/python -m unittest discover -s tests -v'
   ```

3. Siapkan konfigurasi service. Ganti `YOUR_VPS_PUBLIC_IP` dengan IP publik VPS yang sebenarnya:

   ```bash
   sudo cp /var/www/fix-bot-trading/deploy/indodax-bot.env.example /etc/indodax-bot.env
   sudo nano /etc/indodax-bot.env
   sudo chmod 600 /etc/indodax-bot.env
   sudo cp /var/www/fix-bot-trading/deploy/indodax-bot.service /etc/systemd/system/indodax-bot.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now indodax-bot
   sudo systemctl status indodax-bot
   ```

4. Pastikan aplikasi listen pada `0.0.0.0:8765` (`sudo ss -ltnp | grep ':8765'`). `BOT_ALLOWED_HOST` harus berisi IP publik VPS dan port, misalnya `103.58.101.236:8765`; ini memeriksa alamat tujuan, bukan membatasi IP perangkat yang membuka dashboard.

   Agar bisa dibuka dari HP maupun perangkat lain tanpa mencocokkan IP sumber, izinkan port tersebut dari semua alamat di UFW VPS:

   ```bash
   sudo ufw allow 8765/tcp
   sudo ufw status
   ```

   Di firewall/security group Biznet Gio, buat aturan inbound TCP port `8765` dengan sumber `0.0.0.0/0` (semua alamat IPv4). Jika memakai IPv6, tambahkan juga sumber `::/0`. Setelah itu buka `http://YOUR_VPS_PUBLIC_IP:8765` dari HP. Uji memakai data seluler atau Wi-Fi; alamat yang dipakai tetap IP VPS.

   **Peringatan:** aturan ini mempublikasikan dashboard tanpa login dan tanpa enkripsi kepada siapa pun di internet. Aplikasi hanya paper trading, tetapi pengunjung dapat mengubah state paper atau menjalankan scanner. Gunakan hanya sementara untuk tes dan hapus aturan UFW serta aturan Biznet setelah selesai (`sudo ufw delete allow 8765/tcp`).

Log service: `sudo journalctl -u indodax-bot -f`. Menghentikan service: `sudo systemctl disable --now indodax-bot`.

Jika repository berubah, jalankan `sudo -u indodaxbot git -C /var/www/fix-bot-trading pull --ff-only`, kemudian `sudo systemctl restart indodax-bot`. Jangan menaruh `.env` lokal, API key, secret, token GitHub, atau database paper di GitHub.

## Scanner dan analisis

- Membaca seluruh pasangan IDR dan ticker publik Indodax; mengambil kandidat paling relevan untuk dianalisis lebih lanjut.
- Mengambil histori candle resmi 15 menit, 1 jam, dan 4 jam untuk sampai enam kandidat per siklus. Strategi entry intraday memakai momentum candle 15m Indodax (tanpa menunggu 15 menit pengamatan lokal setelah restart), konfirmasi EMA 1h, dan tren EMA 4h.
- Request data publik yang gagal sementara dicoba ulang maksimal tiga kali dengan jeda singkat. Jika tetap gagal, scanner menampilkan error dan tidak membuat entry dari data lama.
- Menghitung perubahan harga 15m/1h/4h, EMA 9/21, RSI 14, ATR, volume relatif 15m, support/resistance 20 candle, ruang menuju resistance, dan indikasi false breakout.
- Sebelum entry/DCA, memvalidasi ask terhadap last trade dan low/high 24 jam. Quote yang menyimpang lebih dari 1,5% atau berada di bawah low 24 jam ditolak untuk mencegah entry dari data ticker yang tidak konsisten.
- Hanya kandidat dengan data OHLC memadai, momentum 15m, tren 1h/4h yang terkonfirmasi, dan tanpa kondisi false breakout/resistance terlalu dekat yang dapat membuka posisi **paper**. Kegagalan data teknis membuat kandidat menunggu; bukan fallback ke entry tanpa verifikasi.
- Memindai pasangan aktif, mengecualikan BTC, ETH, SOL, XRP, BNB, LINK, DOT, HYPE, AAVE, HBAR, ALGO, NEAR, dan ONDO, serta membuang sepenuhnya aset berharga **Rp50 atau lebih rendah** dari daftar pantauan dan aksi beli/DCA. Spread maksimum 1,5% dan konteks volume 24 jam juga diterapkan.
- API publik yang dipakai: [Indodax Public REST API](https://github.com/btcid/indodax-official-api-docs/blob/master/Public-RestAPI.md).

Kriteria momentum awal adalah konfigurasi pengujian, bukan rekomendasi atau jaminan hasil. Histori candles tersedia dari exchange; perubahan ticker rolling lokal tetap memerlukan pengamatan scanner. Pola grafik/candle kompleks, berita proyek, risiko delisting, dan kedalaman order book belum menjadi bagian dari skor entry.

## Batas modal paper

- Modal simulasi awal Rp10.000.000: kas trading virtual Rp8.000.000 dan cadangan manual Rp2.000.000.
- Maksimal tiga posisi aktif: dua posisi otomatis dan satu posisi manual terpisah. Entry otomatis tetap Rp1.500.000 per posisi termasuk simulasi biaya 0,2%; satu siklus scan dapat membuka sampai jumlah slot otomatis yang tersedia jika sinyal valid mencukupi.
- Cadangan DCA Rp2.500.000 per posisi (maksimal Rp5.000.000) dapat dipakai untuk DCA paper manual; jika nominal DCA melebihi saldo DCA yang tersedia, sisa diambil dari cadangan manual.
- Cadangan manual awal Rp2.000.000 dapat dipakai untuk membuka posisi paper dengan nominal pilihan pengguna atau untuk DCA paper manual. Setiap penggunaan mengurangi saldo cadangan yang sama; penjualan mengembalikan hasil sesuai sumber modal posisi.
- Pembelian manual paper memakai ask publik terbaru. DCA paper manual menambah kuantitas dan memperbarui harga rata-rata posisi. Keduanya **memperbarui ledger simulasi saja** dan tidak mengirim order ke Indodax.
- Tabel posisi menampilkan modal masuk kumulatif per aset, dirinci menjadi nominal entry awal dan seluruh nominal DCA paper yang tercatat.
- Tidak ada DCA otomatis atau penjualan rugi otomatis. Posisi yang rugi masuk status `SCALPING — UNDER REVIEW`.
- Take-profit paper memakai tahap 40%–40%–20% saat sinyal melemah dan posisi masih untung setelah biaya.
- Profit bersih paper hanya dikunci secara akuntansi 50:50 untuk BTC/ETH ketika semua posisi paper sudah tertutup. Tidak ada pembelian BTC/ETH nyata.

Saldo pada dashboard adalah saldo **virtual**, bukan bukti saldo Indodax. Koneksi private API hanya dipakai jika pemilik mengisi kredensial lokal dan menjalankan pemeriksaan read-only.

## Kunci API dan kesiapan live

**Jangan kirim API key atau secret ke chat, issue, screenshot, atau file yang akan dibagikan.** Saat diperlukan, salin `.env.example` menjadi `.env` lokal dan isi nilainya. `.env` sudah dikecualikan dari Git dan dibaca lokal oleh program.

Untuk pemeriksaan akun, buat key Indodax dengan izin baca dan spot trade sesuai kebutuhan. Pastikan izin **crypto withdrawal dan IDR withdrawal nonaktif**; gunakan IP whitelist bila tersedia. Tombol **Periksa koneksi API** hanya membaca izin akun, saldo IDR, dan open orders.

Implementasi private API mengikuti [dokumen resmi Indodax Trade API 2.0](https://github.com/btcid/indodax-official-api-docs/blob/master/INDODAX-TradeAPI-2.md). Pemeriksaan akun bersifat read-only; client order limit yang ditandatangani HMAC-SHA256 juga masih terisolasi dan tidak dipakai oleh strategi/dashboard. Mutasi API membutuhkan dua gerbang lokal `INDODAX_LIVE_TRADING=YES` dan `INDODAX_LIVE_ACK=I_ACCEPT_REAL_ORDERS`, serta capital-lock executor yang saat ini sengaja belum tersedia. **Jangan aktifkan gerbang tersebut:** eksekusi order belum direkonsiliasi end-to-end terhadap ledger live dan belum layak dipakai untuk trading nyata. Jika respons pembuatan order tidak diketahui, status harus direkonsiliasi menurut client order ID; order tidak boleh dikirim ulang begitu saja.

Sebelum mode live dapat dipertimbangkan, masih perlu menyelesaikan dan menguji ledger modal berbasis saldo akun nyata, rekonsiliasi partial fill/open order, manajemen status tidak diketahui, batas harga/kuantitas per pasangan, simulasi slippage/biaya, dan persetujuan pemilik. Pembukuan terpisah adalah kontrol aplikasi, bukan pemisahan dana fisik; perubahan saldo eksternal harus memicu freeze dan rekonsiliasi.

## Pengujian

Jalankan `python -m unittest discover -s tests -v`. Tes API memakai mock lokal dan tidak melakukan request order ke exchange.
