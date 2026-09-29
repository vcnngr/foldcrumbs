# Design: `foldcrumbs[semantic]` — modello embedding bundled (extra OPZIONALE)

Data: 2026-09-29 · Stato: APPROVATO DAL TITOLARE ("hai il mio ok a procedere!
Il semantic è opzionale! Non è mai core… foldcrumbs ha il suo core stdlib e
resta quello!")

## Principio costituzionale (non negoziabile)
Il CORE di foldcrumbs resta **stdlib puro, zero dipendenze**. Tutto ciò che
è semantic è un EXTRA OPZIONALE: chi non lo installa ha un pacchetto
byte-identico a oggi. Nessun import di terze parti nel core path; l'extra
vive in un modulo separato con import guardato.

## Fatti verificati oggi (fonti: PyPI API, HF API, esecuzione reale)
- onnxruntime su macOS: wheel **x86_64 fino a 1.23.x**, arm64-only da 1.24+
  (verificato via PyPI API su 1.16→1.30). pip risolve automaticamente la
  più recente installabile per piattaforma. VERIFICATO IN VIVO su questo
  iMac Intel (Xeon W-2140B, py3.12): install ok (1.23.2), inferenza ok.
  [correzioni: una prima verifica cercava solo 'x86_64' su versioni con
  wheel universal2 → falso negativo; il pin >=1.22 funziona su Intel]
- Modello: Xenova/all-MiniLM-L6-v2, rev pinnata
  751bff37182d3f1213fa05d7196b954e230abad9, file onnx/model_quantized.onnx
  22.97MB sha256 afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1,
  vocab.txt 231508B sha256 07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3.
- Grafo ONNX verificato: input input_ids/attention_mask/token_type_ids
  int64 [B,S], output last_hidden_state [B,S,384]. Mean-pooling sui token
  reali + L2 norm (recipe sentence-transformers).
- Latenza reale misurata (iMac Intel, seq 64): prima run 6ms, media 5ms.
  20 candidati ≈ 100ms — DENTRO il gate <3s di 30×.
- Tokenizer WordPiece stdlib: validato 12/12 ID-per-ID contro il tokenizer
  ufficiale HF (tokenizers 0.23.2) su casi difficili (accenti, CJK, tab,
  punteggiatura attaccata, vuoto, overlong). Pinnato in tests/test_wordpiece.py.
- Costo extra totale: ~40MB wheel universal2 + numpy + 23MB modello.

## Architettura
1. `pyproject.toml`: `[project.optional-dependencies] semantic =
   ["onnxruntime>=1.22", "numpy>=1.26"]` — MAI nelle dependencies core.
2. `foldcrumbs/embeddings_local.py` (nuovo, importato SOLO dal CLI setup e
   dalla catena fallback): try-import onnxruntime → se assente, ogni
   funzione riporta "extra non installato" senza crash. Tokenizer
   WordPiece stdlib (vocab.txt dal bundle). Mean-pooling + L2 norm come da
   model card MiniLM.
3. `foldcrumbs embeddings setup|status|remove` (nuovo sottocomando CLI):
   - setup: scarica model_qint8 + vocab.txt da HF (URL pinnati + SHA256
     attesi nel codice), salva nella STATE dir (machine-local, non nello
     store sincronizzato), self-test end-to-end, scrive il marker di config.
   - status: cosa è installato, checksum, dimensione, self-test.
   - remove: cancella modello e marker.
4. Catena di risoluzione embed() (estende la PR #80):
   endpoint esplicito > discovery loopback > **bundled locale (se extra
   installato E modello presente)** > lessicale. Ogni gradino ha il suo
   gate e il suo fallback onesto; il bundled non fa MAI rete dopo il setup.
5. doctor: la riga `semantic` guadagna il caso "bundled (local model)".

## Limiti dichiarati (nel README, onestamente)
- macOS Intel: onnxruntime non pubblica wheel → `[semantic]` non
  installabile lì; la via documentata per Intel Mac resta la discovery
  verso ollama/llama-server (PR #80). Verificato su PyPI il 2026-09-29.
- Python 3.10: supportato solo con onnxruntime <=1.22 (pin da testare in
  CI matrix).
- NESSUN claim di qualità prima della misura sul golden set parafrasato
  (disciplina Fase 0): il README dirà "local semantic channel", non
  "better recall", finché i numeri non ci sono.

## Test
- Import-guard: suite intera verde SENZA onnxruntime installato (CI core).
- Con extra installato (job CI dedicato, non bloccante): tokenizer contro
  vettori noti di riferimento, self-test setup, catena di precedenza
  completa (mock), checksum rifiutato se corrotto.
- Le regressioni P0 della PR #80 restano verdi in entrambe le configurazioni.

## Sequenza
PR stacked su feat/local-semantic DOPO il merge di PR #80 (r2 in review
ora): branch feat/bundled-semantic, stessa disciplina TDD+RT.
