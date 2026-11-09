# Hosting EVKIN di Oracle Cloud Always Free

EVKIN berjalan sebagai satu proses Python dengan DuckDB pada disk server. Caddy
menyediakan HTTPS dan meminta kredensial operator sebelum meneruskan permintaan.
Instalasi baru memakai database kosong.

## 1. Siapkan server dan alamat

1. Masuk atau daftar di [Oracle Cloud](https://signup.oraclecloud.com/). Oracle
   meminta kartu untuk verifikasi pendaftaran. Tetap gunakan tipe akun gratis.
2. Di region utama akun, buat VM Ubuntu ARM64 `VM.Standard.A1.Flex` dengan
   label **Always Free**, 2 OCPU, 12 GB RAM, dan boot volume dalam kuota gratis.
   Pilih subnet publik, tetapkan IPv4 publik, dan pasang public key SSH Anda.
   Buka TCP 80 dan 443 pada aturan ingress Oracle dan firewall VM. Batasi TCP
   22 ke alamat IP pengelola. Port 8000 hanya dipakai di dalam Docker.
3. Buat DNS `A` ke IPv4 publik server. Anda bisa memakai domain sendiri atau
   subdomain gratis [Duck DNS](https://www.duckdns.org/). Gunakan hostname
   saja, misalnya `evkin-kantor.duckdns.org`, saat mengisi konfigurasi di bawah.

Oracle dapat mereklamasi VM yang terus menganggur dan kapasitas VM bergantung
pada region. Lihat [ketentuan Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

## 2. Jalankan EVKIN

Pasang Docker Engine dan plugin Compose sesuai [panduan Ubuntu dari Docker](https://docs.docker.com/engine/install/ubuntu/).
Jalankan perintah ini melalui SSH dengan akun yang bisa menjalankan Docker.

```bash
git clone https://github.com/diofajruliman-glitch/evkin.git
cd evkin
docker run --rm -it caddy:2-alpine caddy hash-password
```

Pilih kata sandi operator yang unik dan panjang saat diminta. Salin hasil hash
yang dimulai dengan `$2a$` atau `$2b$`. Buat konfigurasi satu kali, sebelum
menjalankan Compose. Petik tunggal di sekitar hash menjaga karakter `$`.

```bash
umask 077
read -r -p 'Hostname publik: ' evkin_host
read -r -p 'Hash kata sandi dari Caddy: ' evkin_password_hash
printf "EVKIN_HOST=%s\nEVKIN_API_KEY=%s\nEVKIN_OPERATOR_HASH='%s'\n" \
  "$evkin_host" "$(openssl rand -hex 32)" "$evkin_password_hash" > .env
unset evkin_password_hash
docker compose up -d --build
docker compose ps
```

Buka `https://<hostname>` dan masuk sebagai `operator`. Periksa bahwa jendela
privat tanpa kredensial mendapat HTTP `401`. Setelah masuk, pastikan aplikasi
menampilkan **Backend aktif** dan coba impor serta ekspor dengan data sintetis.
Pengguna yang masuk ke akun operator yang sama berbagi seluruh data dan hak
administrasi EVKIN. Berikan akses hanya kepada pengelola yang berwenang.

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://$evkin_host/api/info"
curl --user operator "https://$evkin_host/api/info"
docker compose logs --tail=60 evkin caddy
```

## 3. Perbarui dan cadangkan

`git pull --ff-only` lalu `docker compose up -d --build` memperbarui aplikasi.
Volume `evkin-data` menyimpan DuckDB, SQLite untuk pekerjaan, dan ekspor.
Gunakan `docker compose down` tanpa `-v` jika perlu menghentikan aplikasi dan
pertahankan volume. Untuk cadangan yang konsisten, hentikan aplikasi selama
menyalin volume ke lokasi cadangan privat di luar VM, lalu jalankan kembali.
Migrasi data dari PC perlu cadangan terpisah dan verifikasi sebelum mengganti
database cloud. Simpan `.env` dan file database di luar GitHub.
