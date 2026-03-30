# Telegram Bot + Selenium (Threaded)

Setup ini membuat bot Telegram yang:

- Menjalankan Selenium di background thread (`ThreadPoolExecutor`)
- Tetap merespons user lain tanpa menunggu job user sebelumnya selesai
- Menyimpan profile Chrome per user Telegram (`data/driver_profiles/user_<id>`)

## Struktur folder

```text
.
|-- bot/                  # App Telegram, handlers, runtime, job processor
|-- lib/selenium/         # Logic Selenium worker
|-- utils/                # Config loader, job store, lock manager
|-- data/                 # Data runtime (driver profiles)
|-- assets/               # Asset statis jika diperlukan
|-- main.py               # Entry point
|-- requirements.txt
|-- .env.example
```

## 1. Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Konfigurasi

```bash
copy .env.example .env
```

Isi minimal:

- `TELEGRAM_BOT_TOKEN`

Opsional:

- `MAX_WORKERS` jumlah thread paralel
- `COST_PER_ACCOUNT` biaya credits per account (untuk tampilan stats)
- `ADMIN_USER_IDS` daftar ID Telegram admin, pisahkan koma
- `CREDIT_USER_IDS` daftar ID Telegram user yang punya credit, pisahkan koma
- `SELENIUM_PROFILE_DIR` folder profile Chrome
- `VOUCHER_DB_PATH` path file database voucher JSON
- `USER_DB_PATH` path file database user (VCC)
- `DOMAIN_DB_PATH` path file database default domain
- `SELENIUM_HEADLESS=true` jika ingin headless
- `CHROMEDRIVER_PATH` jika driver manual

## 3. Run Bot

```bash
python main.py
```

## 4. Perintah Bot

- `/start` melihat bantuan
- `/start` menampilkan ringkasan user/stats/config seperti dashboard
- Tombol `/start` akan menyesuaikan role admin/credit
- Admin Panel mendukung generate voucher redeem (`gen_voucher` flow)
- `/redeem KODE_VOUCHER` redeem credits
- Kirim langsung `VC-XXXXXXXXXX` juga bisa untuk redeem
- `/add_vcc NomorKartu|MM|YY|CVV` tambah VCC
- Menu `Vcc` mendukung add/edit/delete + bulk input
- `Set Password` menyimpan password default user untuk flow auto create
- Admin Panel mendukung bulk set password user (format `USER_ID|PASSWORD`)
- `/add_default_domain DOMAIN` tambah default domain (admin)
- `/remove_default_domain DOMAIN` hapus default domain (admin)
- `/run <url>` submit job Selenium baru
- `/status <job_id>` cek status job

Contoh:

```text
/run https://example.com
```

## Cara kerja thread

- Setiap `/run` langsung dapat respons "job diterima"
- Job dieksekusi di thread pool, jadi update lain tidak ikut menunggu
- Profile Selenium dipisah per user Telegram untuk mempertahankan session
- Ada lock per user profile agar 2 job dari user yang sama tidak bentrok profile

## Ganti flow Selenium

Edit fungsi `run_visit_job()` di `lib/selenium/worker.py` dengan automation yang Anda butuhkan.
Fungsi ini sudah mencontohkan:

- Inisialisasi Chrome dengan profile persisten
- `WebDriverWait` + `ExpectedConditions`
- Tanpa `time.sleep()`
