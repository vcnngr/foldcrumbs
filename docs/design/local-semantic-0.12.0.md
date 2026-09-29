# Design: local semantic embeddings (via B) + understand→work→update loop — 0.12.0

Data: 2026-09-29 · Stato: PROPOSTA (design gate nel RT della PR)
Deciso dal titolare: via B (subprocess/server locale dell'utente, foldcrumbs
resta stdlib), niente onnxruntime in-process. Scope 0.12.0.

## 1. Verifica modelli embeddings (fatti raccolti oggi contro fonte)

Requisito del titolare: "modelli simili o assimilabili che possano fare anche
meglio senza variare troppo il costo in MB di RAM". Riferimento: MiniLM-L6-v2
quantizzato ONNX 23MB (la scelta MegaMemory), 384 dim, EN-only, 242M download
HF (verificato via API HF).

| Modello | Size | Dim | Lingue | Qualità (fonte) | Note |
|---|---|---|---|---|---|
| all-MiniLM-L6-v2 | 23MB (ONNX q) | 384 | EN | baseline MTEB EN, datato | la scelta MegaMemory |
| nomic-embed-text-v1.5 | ~137MB | 768 | EN | MTEB EN > MiniLM (dichiarato model card) | 13.5M download HF; in ollama |
| embeddinggemma-300m | ~123MB (q4 MatFormer) | 768/128 | **multi (100+)** | dichiarata forte su MTEB multilingual | 3.2M download HF; in ollama |
| qwen3-embedding-0.6b | ~600MB | 1024 | multi | **MTEB multilingual 70.58** (model card, ver. 05/06/2025) | 9.4M download; pesante |
| mxbai-embed-large | ~335MB (670MB f16) | 1024 | EN | MTEB EN forte | 15.2M pull ollama |
| bge-m3 | ~567MB | 1024 | multi | MTEB multi forte | pesante |

Scelta proposta (misurabile, non ideologica):
- **Default documentato EN: nomic-embed-text-v1.5** — 137MB è ~6× MiniLM ma
  resta "piccolo" in assoluto; qualità EN dichiarata superiore; è il modello
  embeddings più usato nell'ecosistema ollama.
- **Opzione multilingual documentata: embeddinggemma-300m** — stesso ordine
  di MB (~123MB), copre IT/ZH (utenti README trilingue).
- qwen3-0.6b solo menzionato come fascia alta (600MB: fuori dal vincolo
  "senza variare troppo i MB").
- **Gate di accettazione**: golden set parafrasato della Fase 0 (recall
  lessicale 30% misurato). Il canale locale deve battere il lessicale sulle
  parafrasi SENZA falsi positivi peggiori, misurato prima del merge della
  doc pubblica. I numeri vanno nel CHANGELOG/README solo se misurati da noi.

## 2. Architettura (via B)

Nessuna dipendenza nuova. foldcrumbs resta client HTTP stdlib di
`/v1/embeddings` (embeddings.py attuale, invariato). Cambia solo la
**risoluzione dell'endpoint**:

Stato attuale (config.py:108-113): SEMANTIC opt-in; EMBEDDING_ENDPOINT
default = LLM_ENDPOINT (localhost:8081, il default MLX distill).

Nuovo comportamento — **local discovery, opt-in, pigra e onesta**:
1. Ordine invariato: env > state-file override > default attuale.
2. Nuovo: `FOLDCRUMBS_EMBEDDING_AUTO=1` (opt-in esplicito) → se l'endpoint
   risolto non risponde al primo uso, si tenta UNA volta per processo una
   discovery di candidati locali noti, in ordine:
   - `http://127.0.0.1:11434/v1` (ollama — shim OpenAI nativo)
   - `http://127.0.0.1:8080/v1` (llama-server)
   - `http://localhost:8081/v1` (MLX/LM Studio compat, già default)
   Probe = POST /v1/embeddings con input ["ping"], timeout breve
   (EMBEDDING_PROBE_TIMEOUT, default 2s). Il primo che risponde vince;
   l'esito è cachato nel state dir (file `embedding-endpoint-discovered`)
   e riusabile; `FOLDCRUMBS_EMBEDDING_ENDPOINT` esplicito vince sempre su
   tutto (nessuna sorpresa per chi ha già configurato).
3. Nessun modello scelto da noi a runtime: il model name resta
   `EMBEDDING_MODEL` (l'utente fa `ollama pull nomic-embed-text` e lo
   dichiara). Se il modello non c'è sul server, l'embed ritorna None →
   fallback lessicale ESISTENTE (gate 2 di embeddings.py, invariato).
4. Privacy/trasparenza: la discovery tocca solo 127.0.0.1/localhost,
   mai indirizzi remoti; documentata nel README. Diagnostica:
   `foldcrumbs doctor` riporta l'endpoint semantico attivo e come è stato
   risolto (env/override/discovery/default).

Perché non di default (auto senza opt-in): un utente con SEMANTIC=1 e un
endpoint remoto lento non deve vedersi probe locali inaspettate; e probe
automatiche verso porte locali non dichiarate sono il tipo di "telefono a
casa al contrario" che la nostra filosofia vieta. Opt-in, documentato,
solo loopback.

## 3. Loop understand → work → update (AGENTS_MD_BLOCK)

Estensione del blocco installato (install.py:357-369), sempre
agent-agnostic (MCP tools nativi O CLI fallback, come da PR #70 F1):

```
## Memory (foldcrumbs)
... (testo attuale) ...
The loop: **understand → work → update**
- understand: before starting a task, recall it — load prior decisions,
  conventions, preferences. Do not re-ask what is already recorded.
- work: do the task. When you hit a recorded decision or constraint,
  follow it; when memory and code disagree, the code wins — fix the memory.
- update: when the task establishes something durable (decision, rule,
  preference, lesson), remember it before ending the session. Memory that
  is only in the transcript is lost.
```

Regole: idempotenza dell'install invariata (il marker "Memory (foldcrumbs)"
protegge i re-install); un AGENTS.md che contiene già il blocco vecchio NON
viene duplicato né riscritto (niente migrazione automatica di file
dell'utente — semmai `install --force` rigenera, comportamento esistente).
Parity: la stessa modifica va documentata in README x3 se il blocco vi è
citato.

## 4. Scope della PR unica (0.12.0 primo mattone)
- config.py: discovery opt-in (funzione pura, testabile senza rete:
  la probe è iniettata nei test)
- embeddings.py: nessuna modifica (riusa i gate esistenti)
- install.py: AGENTS_MD_BLOCK esteso + test idempotenza
- doctor: riga diagnostica endpoint semantico
- test red-first: discovery (mock probe), precedence env>override>discovery,
  cache state-file, loop AGENTS.md presente/idempotente
- README x3: sezione "Local semantic recall in 5 minutes" (ollama/llama.cpp)
  + parity check 23/23/46
- CHANGELOG [Unreleased]
- docs/design: questo file + benchmark golden set (seguono, gate pubblico)

Fuori scope (backlog 0.12.x): canale jev asincrono (kev:4b per distill
flag/index ranking), auto-migrazione blocchi AGENTS.md vecchi.

## 5. Rischi e mitigazioni
- Probe su porte locali: solo loopback, opt-in, timeout 2s, una volta per
  processo, cachata. Mai su reti remote.
- Falsi positivi di discovery (un server a caso su 11434 che risponde a
  /v1/embeddings con spazzatura): la validazione del payload esiste già in
  _post (vettori allineati, numerici) → None → lessicale.
- Modello embeddings assente: stesso fallback. Nessun crash, nessun blocco.
- Claim di qualità: NESSUN numero pubblico prima della misura sul golden
  set (disciplina Fase 0).
