# Indodax Momentum Bot — Paper Mode

Dashboard lokal Python untuk memindai pasangan IDR, menganalisis OHLC multi-timeframe, dan menguji aturan modal melalui paper trading. **Scanner dan dashboard tetap paper-only; tidak ada rute dashboard yang membuat atau membatalkan order nyata.**

## Menjalankan

1. Pastikan Python 3.10 atau lebih baru tersedia.
2. Dari folder project, jalankan `python bot.py`.
3. Buka `http://127.0.0.1:8000`.
4. Pilih **Mulai scanner**. Pemindaian dilakukan setiap 30 detik.

Dependensi runtime hanya standard library Python. Ledger paper disimpan lokal di `paper_trading.sqlite3`.

Host dan port bisa dikonfigurasi dengan `BOT_HOST`, `BOT_PORT`, dan `BOT_ALLOWED_HOST`. Default-nya hanya localhost.

## VPS Biznet Gio tanpa Nginx (paper mode sementara)

Konfigurasi systemd dan environment contoh tersedia di folder `deploy/`. Jalur ini membuat aplikasi Python melayani dashboard langsung pada `http://IP-VPS:8000`; **tidak memakai Vercel atau Nginx**.

> **Peringatan:** IP:port HTTP tidak mengenkripsi lalu lintas. Gunakan hanya sementara untuk paper mode, jangan masukkan API key/secret, dan batasi akses port 8000 ke IP Anda melalui firewall/security group Biznet Gio. Private API preflight otomatis ditolak untuk koneksi non-lokal.

Asumsi: Ubuntu/Debian, repository GitHub sudah dibuat, dan SSH ke VPS berfungsi.

1. Siapkan host:

   ```bash
   sudo apt update
   sudo apt install -y git python3 python3-venv
   sudo adduser --system --group --home /opt/indodax-bot indodaxbot
   sudo mkdir -p /opt/indodax-bot
   sudo chown indodaxbot:indodaxbot /opt/indodax-bot
   ```

2. Clone repository private dengan deploy key GitHub yang hanya memiliki akses baca:

   ```bash
   sudo -u indodaxbot git clone git@github.com:USERNAME/fix-bot-trading.git /opt/indodax-bot
   sudo -u indodaxbot python3 -m venv /opt/indodax-bot/.venv
   sudo -u indodaxbot /opt/indodax-bot/.venv/bin/python -m unittest discover -s /opt/indodax-bot/tests -v
   ```

3. Siapkan konfigurasi service. Ganti `YOUR_VPS_PUBLIC_IP` dengan IP publik VPS yang sebenarnya:

   ```bash
   sudo cp /opt/indodax-bot/deploy/indodax-bot.env.example /etc/indodax-bot.env
   sudo nano /etc/indodax-bot.env
   sudo chmod 600 /etc/indodax-bot.env
   sudo cp /opt/indodax-bot/deploy/indodax-bot.service /etc/systemd/system/indodax-bot.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now indodax-bot
   sudo systemctl status indodax-bot
   ```

4. Di pengaturan firewall/security group Biznet Gio, izinkan TCP port `8000` **hanya dari IP publik Anda**. Jangan membuka port tersebut ke seluruh internet. Setelah itu buka `http://YOUR_VPS_PUBLIC_IP:8000`.

Log service: `sudo journalctl -u indodax-bot -f`. Menghentikan service: `sudo systemctl disable --now indodax-bot`.

Jika repository berubah, jalankan `sudo -u indodaxbot git -C /opt/indodax-bot pull --ff-only`, kemudian `sudo systemctl restart indodax-bot`. Jangan menaruh `.env` lokal, API key, secret, token GitHub, atau database paper di GitHub.

## Scanner dan analisis

- Membaca seluruh pasangan IDR dan ticker publik Indodax; mengambil kandidat paling relevan untuk dianalisis lebih lanjut.
- Mengambil histori candle resmi 1 menit, 15 menit, 1 jam, 4 jam, dan 1 hari untuk sampai enam kandidat per siklus. Ini menjaga jumlah permintaan di bawah batas publik 180 permintaan/menit yang didokumentasikan Indodax.
- Menghitung perubahan harga jangka pendek, EMA 9/21, RSI 14, ATR, volume relatif, support/resistance 20 candle, ruang menuju resistance, dan indikasi false breakout.
- Hanya kandidat dengan data OHLC memadai dan tanpa kondisi false breakout/resistance terlalu dekat yang dapat membuka posisi **paper**. Kegagalan data teknis membuat kandidat menunggu; bukan fallback ke entry tanpa verifikasi.
- Memindai pasangan aktif, mengecualikan BTC, ETH, SOL, XRP, BNB, LINK, DOT, HYPE, AAVE, HBAR, ALGO, NEAR, dan ONDO, serta menerapkan batas harga Rp50, spread maksimum 1,5%, dan konteks volume 24 jam.
- API publik yang dipakai: [Indodax Public REST API](https://github.com/btcid/indodax-official-api-docs/blob/master/Public-RestAPI.md).

Kriteria momentum awal adalah konfigurasi pengujian, bukan rekomendasi atau jaminan hasil. Histori candles tersedia dari exchange; perubahan ticker rolling lokal tetap memerlukan pengamatan scanner. Pola grafik/candle kompleks, berita proyek, risiko delisting, dan kedalaman order book belum menjadi bagian dari skor entry.

## Batas modal paper

- Modal simulasi awal Rp10.000.000: kas trading virtual Rp8.000.000 dan cadangan manual Rp2.000.000.
- Maksimal dua posisi, anggaran entry tetap Rp1.500.000 per posisi termasuk simulasi biaya 0,2%.
- Cadangan DCA manual Rp2.500.000 per posisi tidak dapat dipakai untuk entry. Simulasi DCA hanya menghitung hasil dan tidak mengubah saldo/posisi.
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
