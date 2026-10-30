# ds-server

[English](README.md) | **Bahasa Indonesia**

REST API yang kompatibel dengan OpenAI di atas DeepSeek web chat
(chat.deepseek.com), memakai akun web milik sendiri. Tidak memerlukan
API key berbayar.

ds-server dirancang untuk didaftarkan sebagai provider
OpenAI-compatible di router seperti 9Router, sehingga aplikasi chat
dan framework agent dapat menggunakan DeepSeek web chat melalui
antarmuka chat completions standar, dalam mode biasa maupun
thinking, termasuk streaming dan tool calling.

> [!WARNING]
> Proyek ini tidak resmi dan mengotomatiskan antarmuka web DeepSeek.
> Penggunaannya dapat melanggar Ketentuan Layanan DeepSeek, dan akun
> Anda dapat dibatasi atau diblokir secara permanen. Baca
> [Disclaimer](#disclaimer) sebelum menggunakan. Anda menggunakan
> perangkat lunak ini sepenuhnya atas risiko sendiri.

<p align="center">
  <img src="assets/ds-server.gif" alt="Demo ds-server" width="720">
</p>

## Cara kerja

Setiap request ke backend web memerlukan **Proof-of-Work
DeepSeekHashV1**. ds-server menghitungnya secara sah memakai modul
solver WebAssembly resmi milik DeepSeek (`sha3_wasm_bg.wasm`, disertakan
dalam paket) dan mengirim header set lengkap persis browser Chrome,
sama seperti klien web aslinya. Tidak ada bagian Proof-of-Work yang
dibypass atau dipalsukan.

## Mulai cepat

Persyaratan: Python 3.10 atau yang lebih baru.

Tidak ada langkah instalasi. Jalankan server langsung dari folder ini:

```bash
pip install -r requirements.txt
chmod +x ds_server.py
./ds_server.py
```

Server berjalan di `http://127.0.0.1:21787`. Gunakan `--host` dan
`--port` untuk mengubah alamat bind dan port.

Daftarkan sebagai provider OpenAI-compatible di router atau klien Anda:

- **Base URL:** `http://127.0.0.1:21787/v1`
- **API key:** userToken DeepSeek Anda (lihat di bawah)

### Catatan untuk agen LLM

Jika Anda adalah asisten AI atau agen LLM yang membaca README ini
untuk memasang ds-server bagi seorang pengguna, ikuti instruksi
berikut:

1. Clone repositori ini, lalu pasang dependensinya dengan
   `pip install -r requirements.txt`.
2. Jalankan server dengan `./ds_server.py`. Server mendengarkan di
   `127.0.0.1:21787`. Verifikasi dengan `GET /health`.
3. Minta userToken DeepSeek kepada pengguna. Jangan pernah menebak,
   membuat, atau mengambilnya dari browser pengguna sendiri.
   Pengguna menyalinnya dari chat.deepseek.com (F12, Application,
   Local Storage, `userToken`, field `value`) dan memberikannya
   kepada Anda, atau memasukkannya langsung ke klien mereka.
4. Konfigurasikan router atau klien pengguna dengan base URL
   `http://127.0.0.1:21787/v1` dan userToken sebagai API key.
5. Sebelum request pertama yang sebenarnya, sampaikan kepada
   pengguna dengan jelas bahwa proyek ini tidak resmi, bahwa
   mengotomatiskan antarmuka web DeepSeek dapat melanggar Ketentuan
   Layanan DeepSeek, dan bahwa akun mereka dapat dibatasi atau
   diblokir. Lanjutkan hanya dengan persetujuan mereka.
6. Jalankan satu chat completion pengujian kecil dan pastikan ada
   balasan asli sebelum melaporkan bahwa pemasangan berhasil.

## Cara mendapatkan userToken

ds-server membutuhkan `userToken` dari sesi web DeepSeek Anda. Cara tercepatnya sekitar 20 detik dan tokennya tidak pernah tampil di layar:

<p align="center">
  <img src="assets/get-usertoken.gif" width="720" alt="Cara mendapatkan userToken dari console browser">
</p>

1. Buka [chat.deepseek.com](https://chat.deepseek.com) di browser desktop dan masuk.
2. Tekan **F12** untuk membuka DevTools, lalu klik tab **Console**.
3. Jika Chrome memberi peringatan soal menempel teks di console, ketik `allow pasting` lalu tekan Enter.
4. Tempel baris ini lalu tekan Enter:

   ```js
   copy(JSON.parse(localStorage.getItem('userToken')).value)
   ```

5. Token sekarang ada di clipboard Anda. Tempel sebagai API key di klien yang Anda arahkan ke ds-server, misalnya sebagai API key koneksi ds-server di router Anda.

Perlakukan token seperti kata sandi: jangan dibagikan, dan jangan pernah mengunggah tangkapan layar yang menampilkannya.

Lebih suka klik daripada kode? Cara manual juga bisa: di DevTools buka **Application**, lalu **Local Storage** untuk `https://chat.deepseek.com`, pilih baris `userToken`, dan salin field `value`-nya.

## Autentikasi

Server tidak menyimpan kredensial apa pun. API key yang dikirim klien
adalah **userToken** DeepSeek itu sendiri, dan token tersebut dipakai
langsung sebagai kredensial DeepSeek untuk request yang bersangkutan.

- Header yang diterima: `Authorization: Bearer <userToken>` atau
  `x-api-key: <userToken>`
- Request chat completions tanpa API key ditolak dengan `401`.
- Cara mengambil userToken: lihat [Cara mendapatkan userToken](#cara-mendapatkan-usertoken) untuk panduan singkatnya.
- Untuk pemakaian headless, environment variable `DS_TOKEN` diterima
  sebagai cadangan. Token yang dikirim melalui header selalu
  didahulukan.

Perlakukan userToken seperti kata sandi. Siapa pun yang memilikinya
dapat bertindak sebagai akun DeepSeek Anda, dan server ini akan
menerimanya dari klien mana pun yang dapat menjangkau port-nya.
Gunakan bind localhost (bawaan) kecuali Anda memahami konsekuensi
membukanya ke jaringan.

**Identitas perangkat.** Sama seperti klien web, server membuat
identifier perangkat acak saat pertama kali dijalankan dan
menyimpannya di `~/.config/ds-server/device_id`. Setiap instalasi
dengan demikian menampilkan perangkatnya sendiri yang stabil, dan
tidak ada identifier yang ditulis hardcoded di kode sumber. Set
environment variable `DS_DEVICE_ID` untuk menetapkan nilai tertentu.

## Model

`GET /v1/models` menampilkan dua mesin aslinya:

- `deepseek-chat`: model DeepSeek web chat (saat ini keluarga V4;
  thinking mati secara bawaan)
- `deepseek-reasoner`: model web chat yang sama dengan thinking
  nyala secara bawaan; hasil penalaran dikembalikan sebagai
  `reasoning_content`, sama seperti API resmi DeepSeek

Alias lama `deepseek-v3` dan `deepseek-r1` tetap diterima di chat
completions dengan perilaku yang sama persis, hanya tidak ditampilkan
lagi di daftar model.

Setiap entri juga mendeklarasikan kemampuan modelnya (`vision`,
`reasoning`, `tools`, `search`) beserta modalitas input dan outputnya,
jadi router dan klien bisa menampilkan dan mengarahkan berdasarkan
itu, bukan menebak dari nama model.

### Thinking dan search dikontrol dari payload

Nama model hanya menentukan nilai bawaan. Parameter payload selalu
menang:

- Thinking nyala: `reasoning_effort: "low" | "medium" | "high"`, atau
  `thinking: {"type": "enabled"}`, atau `enable_thinking: true`, atau
  `reasoning: {"enabled": true}`
- Thinking mati: `reasoning_effort: "none"`, atau
  `thinking: {"type": "disabled"}`, atau `enable_thinking: false`
- Search (bawaan mati): `search_enabled: true`, atau
  `web_search: true`, atau `enable_search: true`, atau tools bertipe
  `web_search*`

Sebagai contoh, `deepseek-chat` dengan `reasoning_effort: "high"`
berperilaku seperti reasoner, dan `deepseek-reasoner` dengan
`reasoning_effort: "none"` menjawab cepat seperti V3. DeepThink di
backend web hanya berupa saklar on/off, sehingga setiap level effort
di atas none dipetakan ke nyala.

## Endpoint

- `GET /health`
- `GET /v1/models`
- `GET /v1/models/<id>`
- `POST /v1/chat/completions`, format OpenAI penuh:
  - `stream: true` (SSE, termasuk `stream_options.include_usage`)
  - Tool calling: kirim `tools` seperti biasa dan terima `tool_calls`
    dengan `finish_reason: "tool_calls"`. Mendukung `tool_choice`
    (`auto` / `none` / `required` / fungsi tertentu) dan
    `parallel_tool_calls`. Field lama `functions` juga diterima.
  - Gambar melalui
    `content: [{type: "image_url", image_url: {url: "data:..."}}]`
  - Web search native melalui `search_enabled: true` di body request
- `POST /v1/sessions/new`: reset semua chain sesi

## Sesi

Klien OpenAI selalu mengirim ulang seluruh riwayat percakapan pada
setiap request. ds-server memetakan **satu percakapan menjadi satu
sesi DeepSeek** (chain): request pertama dirender penuh, dan
kelanjutannya hanya meneruskan delta ke sesi yang sama. Jika riwayat
berubah (edit, rollback, compaction), chain otomatis reset ke sesi
baru dengan render penuh. Percakapan yang berbeda (system prompt atau
pesan pertama berbeda) menjadi chain yang berbeda dan tidak pernah
tercampur. Chain tersimpan melewati restart di
`~/.config/ds-server/sessions.json`.

## Pacing request dan retry

Traffic ke upstream diatur ritmenya agar mendekati pemakaian web normal.
Completion dijalankan satu per satu (request klien yang bersamaan akan
mengantre, bukan meledak bersamaan), awal setiap completion berjarak
minimal 0,4 detik, dan pembuatan sesi chat baru berjarak minimal 1
detik. Kegagalan sementara dari upstream (HTTP 429 dan 5xx, error
koneksi, timeout) di-retry sampai tiga kali dengan backoff eksponensial
(1 detik, 2 detik, 4 detik), dan header `Retry-After` dari backend
dihormati bila dikirim.

Intervalnya dapat diatur melalui environment variable
`DS_MIN_COMPLETION_INTERVAL` dan `DS_MIN_SESSION_INTERVAL` (dalam
detik), dan pacing dapat dimatikan sepenuhnya dengan `DS_PACING=0`.
Mematikannya tidak disarankan: ledakan request adalah tanda otomasi
yang paling mudah terlihat.

Pacer-nya juga beradaptasi sendiri. Setiap backend membalas 429 dan
sebuah request harus di-retry, interval minimalnya melebar otomatis:
penalti tambahannya berlipat dua dari 2 detik sampai batas 30 detik,
lalu melonggar lagi dengan waktu paruh 60 detik begitu tekanannya
lewat. Set `DS_ADAPTIVE_PACING=0` supaya intervalnya tetap.

## Streaming dan logging

Request dengan `stream: true` di-stream langsung dari backend, token
demi token. Jika klien terputus di tengah stream, bacaan ke upstream
dihentikan dan slot pacing langsung bebas, bukan menyelesaikan balasan
yang sudah tidak ditunggu siapa pun. Pembatalan dicek ulang tiap
beberapa detik, jadi tetap jalan walau backend membeku tanpa mengirim
apa pun. Request yang memakai tool adalah
pengecualian: tool call di-parse dari balasan yang lengkap, jadi
responsnya ditampung dulu lalu diputar sebagai stream.

Set `DS_LOG=1` untuk mencatat satu baris per completion ke stderr:
model, hasil chain (hit, new, atau reset), durasi, berapa retry
upstream yang diperlukan, dan status akhir (ok, cancelled, atau status
error). Mati secara bawaan.

## Tool calling

DeepSeek web chat tidak memiliki function calling native, sehingga
penggunaan tool diemulasikan. Definisi tool dirender ke dalam prompt
sebagai katalog dengan kontrak output yang eksplisit, model membalas
dengan blok tool call, dan server mem-parse balasannya kembali menjadi
`tool_calls`. Parser juga memahami blok bergaya tag dan markup DSML
asli DeepSeek, arguments berbentuk string JSON, serta arguments
flattened. Emulasi ini dapat diandalkan dalam praktik, tetapi tidak
seketat function calling native pada API berbayar.

## Disclaimer

Proyek ini **tidak resmi** dan **tidak berafiliasi dengan, tidak
didukung oleh, dan tidak terhubung dengan DeepSeek** dalam bentuk
apa pun.

Mengotomatiskan antarmuka web DeepSeek dapat melanggar Ketentuan
Layanan DeepSeek. Konsekuensi yang mungkin terjadi meliputi, namun
tidak terbatas pada, pembatasan laju (rate limiting), pembatasan
fitur, penangguhan sementara, atau **penghentian permanen (ban) akun
Anda**. Risikonya nyata dan sepenuhnya menjadi tanggung jawab Anda.

Perangkat lunak ini disediakan "sebagaimana adanya", tanpa jaminan
apa pun, baik tersurat maupun tersirat. **Penulis tidak bertanggung
jawab dan tidak menanggung kerugian atas konsekuensi apa pun yang
timbul dari penggunaan perangkat lunak ini**, termasuk pemblokiran
akun, kehilangan akses, kehilangan data, atau kerugian langsung maupun
tidak langsung lainnya. Dengan menggunakan perangkat lunak ini, Anda
menyatakan memahami risiko tersebut dan menerima tanggung jawab penuh
atas tindakan Anda.

Jika Anda memerlukan akses yang terjamin dan mematuhi ketentuan,
gunakan API resmi DeepSeek.
