# 🧪 Prompt Evaluation (promptfoo)

Evaluasi kualitas prompt bot memakai [promptfoo](https://promptfoo.dev) (MIT,
dev-time tooling) dengan provider **Python** yang menjalankan pipeline **nyata**
bot: `AIFallbackEngine` (OpenRouter free → Groq → Gemini → ...) + template
sintesis + parsing JSON agent confidence.

## Mengapa eval ini penting

- **Cegah regresi kualitas**: ubah prompt (`prompts/*.txt`), jalankan eval,
  lihat skor sebelum/after — bukan asumsi.
- **Validasi JSON agent**: agent confidence memakai `clean_json_response` +
  `json.loads`; eval memastikan model masih mengembalikan JSON dengan skema
  benar (`overall` 0-1, `level`, `assessment`) lintas provider.
- **Deteksi markdown rusak**: bot menampilkan plain text — `**` yang lolos dari
  model akan tampil mentah di Telegram; assertion `not-contains "**"` menangkapnya.

## Cara pakai

```bash
# Dari direktori ini (promptfoo/)
cd app/bot-telegram/promptfoo

# Prasyarat: minimal satu API key AI
export OPENROUTER_API_KEY=sk-or-v1-...    # hanya model free (gratis)
# (Windows PowerShell: $env:OPENROUTER_API_KEY="...")

# Jalankan evaluasi (unduh promptfoo otomatis via npx)
npx promptfoo eval

# Buka dashboard hasil di browser
npx promptfoo view
```

Tanpa `npx`/Node terpasang? `npm install -g promptfoo` lalu `promptfoo eval`.

## Isi

| File | Fungsi |
|---|---|
| `promptfooconfig.yaml` | Konfigurasi provider, prompt, test & assertion |
| `agent_provider.py` | Provider Python → pipeline nyata bot (`call_api` = sintesis, `call_confidence` = agent JSON) |
| `tests/` (opsional) | Bisa dipindah ke file YAML terpisah bila test makin banyak |

## Biaya & kuota

- Eval memakai **model gratis** (OpenRouter `:free` / Groq free tier) — biaya $0,
  tapi perhatikan rate limit: tiap test = 1 panggilan LLM (ditambah 1 lagi untuk
  agent confidence). Batch kecil dulu (2-3 test) lalu naikkan.
- `call_api` memakai `use_cache=False` agar tiap eval benar-benar menguji model
  (bukan cache). `call_confidence` memakai cache internal (seperti produksi).

## Tambahan: penilaian rubric (opsional)

Untuk menilai *kualitas* jawaban (bukan hanya format), aktifkan blok komentar
`defaultTest.options.rubric` di `promptfooconfig.yaml` lalu tambahkan assertion
`- type: llm-rubric` ke test. Judge memakai model gratis OpenRouter — pastikan
`OPENROUTER_API_KEY` terisi.
