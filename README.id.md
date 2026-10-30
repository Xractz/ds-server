# ds-server

[English](README.md) | **Bahasa Indonesia**

REST API yang kompatibel dengan OpenAI di atas DeepSeek web chat
(chat.deepseek.com), memakai akun web milik sendiri. Tidak memerlukan
API key berbayar.

ds-server dirancang untuk didaftarkan sebagai provider
OpenAI-compatible di router seperti 9Router, sehingga aplikasi chat
dan framework agent dapat menggunakan DeepSeek V3 dan R1 melalui
antarmuka chat completions standar, termasuk streaming dan tool
calling.

> [!WARNING]
> Proyek ini tidak resmi dan mengotomatiskan antarmuka web DeepSeek.
> Penggunaannya dapat melanggar Ketentuan Layanan DeepSeek, dan akun
> Anda dapat dibatasi atau diblokir secara permanen. Baca
> [Disclaimer](#disclaimer) sebelum menggunakan. Anda menggunakan
> perangkat lunak ini sepenuhnya atas risiko sendiri.

![Demo ds-server](ds-server.gif)

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

## Autentikasi

Server tidak menyimpan kredensial apa pun. API key yang dikirim klien
adalah **userToken** DeepSeek itu sendiri, dan token tersebut dipakai
langsung sebagai kredensial DeepSeek untuk request yang bersangkutan.

- Header yang diterima: `Authorization: Bearer <userToken>` atau
  `x-api-key: <userToken>`
- Request chat completions tanpa API key ditolak dengan `401`.
- Cara mengambil userToken: masuk ke chat.deepseek.com, tekan F12,
  buka tab Application, lalu Local Storage, cari key `userToken`, dan
  salin isi field `value`.
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

- `deepseek-chat`: DeepSeek V3 (thinking mati secara bawaan)
- `deepseek-reasoner`: DeepSeek R1 (thinking nyala secara bawaan;
  hasil penalaran dikembalikan sebagai `reasoning_content`, sama
  seperti API resmi DeepSeek)

Alias lama `deepseek-v3` dan `deepseek-r1` tetap diterima di chat
completions dengan perilaku yang sama persis, hanya tidak ditampilkan
lagi di daftar model.

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
