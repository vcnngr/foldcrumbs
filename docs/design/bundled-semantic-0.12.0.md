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
   - setup: runtime check PRIMA di ogni altra cosa; scarica model_quantized
     + vocab.txt da HF (URL pinnati + SHA256 + size attesi nel codice) in
     staging .part, verifica ENTRAMBI, poi commit della coppia con rename
     sequenziali E rollback dei file già committati se una rename fallisce
     (transazionale di fatto: nessun mezzo bundle resta su disco).
     Salva nella STATE dir (machine-local, non nello store sincronizzato).
     [self-test end-to-end e marker di config: NON implementati — il marker
     è la presenza verificata dei file stessi; il self-test vive nel CI job
     semantic-extra. Correzione r2: la prima stesura li prometteva.]
   - status: cosa è installato, checksum, revisione, disponibilità.
   - remove: cancella modello e vocab.
4. Catena di risoluzione embed() — CHANNEL-FIRST (contratto r2, RT P0-1):
   endpoint esplicito > discovery loopback > **bundled** > lessicale.
   Una chiamata embed() serve TUTTI i testi in UN SOLO spazio vettoriale:
   il canale server prova per primo (cache+POST sotto la sua basis); solo
   se non risponde affatto, il bundled prende l'intera chiamata sotto la
   propria basis. Mai vettori di spazi diversi nello stesso ranking.
   Il bundled non fa MAI rete dopo il setup.
5. doctor: la riga `semantic` mostra il bundled — follow-up dichiarato
   (non in questa PR).

## Limiti dichiarati (nel README, onestamente)
- macOS Intel: SUPPORTATO fino a onnxruntime 1.23.x (wheel x86_64; pip
  risolve da solo — verificato in vivo su Xeon W-2140B con 1.23.2, E2E
  verde). Da 1.24+ solo arm64. [correzione r2: la prima stesura diceva
  "non installabile su Intel" — falso, smentito dai fatti sopra e dalla
  verifica PyPI 1.16→1.30]
- Python: extra supportato 3.10–3.13 (marker `python_version < '3.14'` su
  onnxruntime: nessuna wheel 3.14 pubblicata — verificato dal revisore su
  3.14.7 macOS x86_64). Core senza tetto.
- NESSUN claim di qualità prima della misura sul golden set parafrasato
  (disciplina Fase 0): il README dirà "local semantic channel", non
  "better recall", finché i numeri non ci sono.

## Test
- Import-guard: suite intera verde SENZA onnxruntime installato (CI core,
  946 test). _runtime_error cattura ImportError E OSError (dlopen rotto).
- Regressione channel-first: cache server + cache bundle mescolate → una
  chiamata ritorna un solo spazio (PoC del revisore pinnato come test).
- setup rifiuta: bundle valido su disco + runtime assente → False, non
  "already installed" (PoC del revisore pinnato).
- Transazionalità: sha del secondo file fallisce → zero residui nel bundle dir.
- Con extra installato (job CI dedicato, continue-on-error): setup reale,
  inferenza reale (cos parafrasi > 0.5, unrelated < 0.3), tokenizer contro
  ID veri del vocab.txt.
- Le regressioni P0 della PR #80 restano verdi in entrambe le configurazioni.

## Numeri E2E (dichiarati onestamente)
Misurati dal committer su iMac Intel Xeon W-2140B (py3.12, ort 1.23.2):
cos parafrasi 0.748 / unrelated −0.095; 189ms per 3 testi (cold), warm
cache 54ms. Il revisore, su host diverso (ubuntu), ha misurato 0.743 /
−0.020 e 291ms — stesso segno, soglie CI verdi in entrambi i casi. I
numeri assoluti dipendono dall'host: le SOGLIE (>0.5 / <0.3) sono il
contratto, non i valori esatti.

## Sequenza
PR stacked su feat/local-semantic DOPO il merge di PR #80 (r2 in review
ora): branch feat/bundled-semantic, stessa disciplina TDD+RT.
