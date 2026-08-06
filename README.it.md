# foldcrumbs

[![tests](https://github.com/vcnngr/foldcrumbs/actions/workflows/test.yml/badge.svg)](https://github.com/vcnngr/foldcrumbs/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/foldcrumbs.svg)](https://pypi.org/project/foldcrumbs/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) · **Italiano** · [中文](README.zh.md)

Memoria persistente cross-sessione per agent di coding — **niente Docker, niente vector DB, nessun servizio esterno**.

`/clear` e la compaction cancellano la conoscenza di Claude Code a ogni sessione. foldcrumbs mantiene una
piccola cartella di file di memoria tipizzati, così l'agent si riapre già conoscendo le tue decisioni, le
convenzioni e i fatti del codebase. Combatte anche il context rot: intorno al 45% del contesto fa un
checkpoint della memoria in background e ti suggerisce `/compact` o `/clear` — nulla va perso.

Più istanze CLI sullo stesso progetto (`claude`, `claude-work`, …) mantengono store propri
ma vedono la memoria delle altre, in sola lettura — vedi
[Federazione](#più-istanze-un-progetto-federazione).

## Come funziona

```
STORE     file markdown + indice MEMORY.md in
          ~/.claude/projects/<project>/memory/
RECALL    Grep/Read dello stesso Claude Code (no LLM, no vector DB)
          + SessionStart inietta l'indice
DISTILL   asincrono, solo LLM locale (MLX/Ollama/OpenRouter via env)
          a ~45% del contesto e a fine sessione → filtrato, deduplicato
ANTI-ROT  monitor PostToolUse → checkpoint + promemoria (no compaction forzata)
          PostCompact → re-inietta l'indice dopo la compaction
HANDOFF   ogni checkpoint scrive anche uno snapshot live dello stato di lavoro,
          re-iniettato a SessionStart → riprendi l'esatto task dopo un /clear
FEDERATE  ogni istanza registrata pubblica uno shard di indice; ogni sessione vede
          anche la memoria delle altre, read-only, con i path annunciati per il grep
```

Il motore di retrieval è l'agent stesso: fa grep sulla cartella quando è rilevante. L'LLM è usato
**solo** per la distillazione asincrona — quindi il recall è istantaneo e non dipende mai da un
modello attivo.

La distillazione esegue anche un **passaggio di contraddizione**: quando una nuova memoria copre
lo stesso argomento di una vecchia (una decisione ribaltata, una cosa "rinviata" che nel frattempo
è successa), all'LLM viene chiesto se la nuova rende obsoleta la vecchia — se sì, la vecchia memoria
è marcata superseded (file tenuto su disco, fuori dall'indice; `prune` la rimuove). Il solo dedup
non può catturarla: unisce solo testo quasi identico. Disattivalo con
`FOLDCRUMBS_NO_AUTO_SUPERSEDE=1`; senza LLM non cambia nulla.

Pura stdlib Python: gli script degli hook non falliscono mai per un import mancante.

L'indice `MEMORY.md` è scritto in un **ordine deterministico** (per tempo di creazione
immutabile, più recente prima all'interno di ogni tipo), così un bump di trust, un re-touch o una
re-distillazione non rimescolano mai le voci esistenti. Solo aggiungere o rimuovere una memoria
cambia il file. Questo mantiene identico tra sessioni il prefisso iniettato a SessionStart — così
cavalca la prompt cache dell'agent invece di invalidarla — e mantiene il file pulito nei diff per
tool di sync come Syncthing.

## Differenze rispetto a memanto

foldcrumbs parte da idee di [memanto](https://github.com/moorcheh-ai/memanto), ma assume
deliberatamente una forma diversa:

| | memanto | foldcrumbs |
|--|--|--|
| Retrieval | motore Moorcheh (chiuso) | il grep dello stesso agent — nessun motore |
| Footprint | Docker + motore + LLM + API REST | una cartella + hook |
| LLM | richiesto per retrieval e risposte | solo distillazione asincrona; il recall non lo richiede |
| Anti-rot | — | monitor del contesto + checkpoint vicino al 45% |
| Dipendenze | stack di servizi | zero dipendenze runtime (stdlib) |
| Ambito | servizio tool-agnostico | memoria per progetto, lato agent |

Il lavoro originale qui è l'architettura: recall basato su grep, lo store di file + indice, il
monitor anti-rot, l'installer merge-safe, gli hook e la CLI. Vedi **Crediti** per le parti
adattate da memanto.

## Avvio rapido

Trenta secondi da zero a uno store funzionante:

```bash
pip install foldcrumbs
cd il-tuo-progetto
foldcrumbs install          # collega hook di Claude Code + comandi slash
```

Fine. La prossima sessione di Claude Code parte con uno store vuoto ma attivo,
e le memorie iniziano ad accumularsi mentre lavori. Verifica con `foldcrumbs status`.

## Installazione

```bash
pip install foldcrumbs                  # da PyPI (oppure: pip install -e . da un checkout)
```

Poi collegalo al tuo agent:

```bash
foldcrumbs install                      # Claude Code, globale (~/.claude/settings.json)
foldcrumbs install --local              # Claude Code, progetto (.claude/settings.json)
foldcrumbs install --agent codex        # Codex: hooks.json + stampa lo snippet MCP per config.toml
foldcrumbs install --agent opencode     # OpenCode: MCP opencode.json + plugin + blocco AGENTS.md
```
L'installer è merge-safe e idempotente: aggiunge i propri gruppi di hook e lascia intatti
gli hook esistenti (GSD, graphify, …). Prima scrive un backup `.foldcrumbs-bak`.

Per Claude Code l'installer scrive anche quattro comandi slash — **`/remember`**,
**`/recall`**, **`/forget`**, **`/foldcrumbs`** (dashboard) — così la memoria diventa una capacità
in-sessione, non solo un layer di background. `/remember` senza argomenti distilla memorie durevoli
dalla conversazione live (con conferma) usando il modello della sessione stessa — non serve un
backend LLM. I file sono marcati come gestiti: modificane uno e rimuovi la riga marcatore per
prenderne possesso; `uninstall` rimuove solo i nostri. Riavvia le sessioni aperte per attivarli.
I comandi hook e MCP usano uno snapshot runtime autonomo sotto `~/.foldcrumbs/runtime`, così
checkout modificabili possono stare in cartelle protette di macOS come `~/Documents` senza rompere
i sottoprocessi dell'agent.

Su un TTY, install chiede **come distillare** (il recall non usa mai un LLM):

```
1) claude-cli   abbonamento Claude — `claude -p`, nessuna API key
2) codex        abbonamento Codex — `codex exec`, nessuna API key
3) openai       endpoint HTTP compatibile OpenAI (server locale o gateway remoto)
4) none         nessun LLM — solo euristica a keyword (ultima risorsa)
```

La scelta è salvata per macchina in `~/.foldcrumbs` (non sincronizzata), così uno store condiviso
può avere un indicizzatore con un modello locale e altri che usano il proprio abbonamento CLI. Salta
il prompt con `foldcrumbs install --backend codex` (o `--no-backend-prompt`), e cambialo in qualsiasi
momento con `foldcrumbs backend <name>` (`foldcrumbs backend` da solo mostra quello attuale).

Tutti gli agent condividono **un unico** store di memoria per progetto, così una decisione registrata
in Claude Code è richiamabile in Codex e OpenCode.

## Configurazione (env)

| var | default | significato |
|-----|---------|-------------|
| `FOLDCRUMBS_LLM_ENDPOINT` | `http://localhost:8081` | endpoint compatibile OpenAI (server MLX) |
| `FOLDCRUMBS_LLM_MODEL` | `gemma-4-26b-a4b-it` | nome del modello |
| `FOLDCRUMBS_LLM_API_KEY` | – | bearer token opzionale |
| `FOLDCRUMBS_CONTEXT_BUDGET` | `200000` | dimensione finestra di contesto (token) per il monitor |
| `FOLDCRUMBS_CONTEXT_PCT` | `0.45` | frazione a cui fare checkpoint + suggerimento |
| `FOLDCRUMBS_MIN_CONFIDENCE` | `0.7` | soglia minima del gate di scrittura |
| `FOLDCRUMBS_NO_AUTO_SUPERSEDE` | – | impostalo per disattivare il passaggio di contraddizione in distill |
| `FOLDCRUMBS_DIR` | derivato dal cwd | sovrascrive la directory di memoria |

Sostituisci l'LLM con un gateway remoto o OpenRouter cambiando `FOLDCRUMBS_LLM_ENDPOINT` — il recall
non ne risente.

## CLI

```bash
python3 -m foldcrumbs status
python3 -m foldcrumbs remember "Recall è grep, niente vector DB" --type decision --tag arch
python3 -m foldcrumbs remember "La licenza trial copre staging" --expires 2026-09-01   # oppure --expires 30d
python3 -m foldcrumbs recall "vector db" --type decision --tag arch   # filtri, ripetibili
python3 -m foldcrumbs index
python3 -m foldcrumbs distill transcript.txt    # distilla memorie durevoli (LLM)
python3 -m foldcrumbs checkpoint transcript.txt # scrive un handoff di ripresa (LLM)
python3 -m foldcrumbs handoff                   # stampa l'handoff corrente
python3 -m foldcrumbs answer "come funziona il recall?"
python3 -m foldcrumbs forget fact_wrong.md --apply   # soft-delete (--hard rimuove il file)
python3 -m foldcrumbs supersede decision_old.md --by decision_new.md
python3 -m foldcrumbs conflicts                      # coda di riconciliazione (coppie ambigue, rivendicazioni)
python3 -m foldcrumbs decay                          # archivia memorie a bassa fiducia (dry-run; --apply scrive)
python3 -m foldcrumbs restore fact_old.md            # recupera una memoria archiviata
python3 -m foldcrumbs import --from ~/.claude/projects/<slug>/memory --apply

python3 -m foldcrumbs profile list                   # tutti i profili registrati
python3 -m foldcrumbs profile add kimi --kind dedicated
python3 -m foldcrumbs profile env kimi               # l'unica riga env che lo seleziona
```

`decay` archivia — non cancella mai. Una memoria la cui fiducia è scesa sotto la soglia
(0.3) **e** che non viene toccata da 30 giorni passa a `status: archived`; esce dall'indice
e dal recall ma resta su disco. `restore <name>` la riporta indietro intera, e `prune --apply`
resta l'atto separato ed esplicito che rimuove i file definitivamente. Dry-run di default.

Alcune verità hanno una data — una trial che finisce, un rinvio "fino a settembre", una
scadenza. `remember --expires <data>` la imprime sulla memoria (`2026-09-01`,
`2026-09-01T12:00`, o relativo `30d`/`2w`/`6m`; una data senza ora significa la fine di
quel giorno). Passata la data, la memoria diventa invisibile ovunque lo sarebbe una
archiviata — indice, recall, federazione, dedup — mentre il file resta intatto su disco.
`decay` è poi la passata che la archivia (etichettandola `(expired)`), `status` mostra cosa
è scaduto e cosa scadrà dopo, e rimuovere o spostare la data nel file è il modo per dire
che vale ancora. Solo l'intento esplicito dell'utente imposta una scadenza: la distillazione
non indovina mai date, così nessuna memoria riceve mai un timer silenzioso che non ha chiesto.

### Profili — uno store per agent

Un **profilo** è una root di memoria registrata con un nome e una forma:

- **dedicated** — una directory di memoria condivisa da ogni progetto; quello che vuole un
  agent di lunga durata (un bot CI, un agent di review);
- **shared** — una directory di memoria *per progetto* sotto una config dir; come funziona un
  assistente interattivo come Claude Code (rispetta `CLAUDE_CONFIG_DIR`).

```bash
foldcrumbs profile add kimi-review --kind dedicated            # una dir, tutti i progetti
foldcrumbs profile add work   --kind shared --path ~/.claude-work
foldcrumbs profile env kimi-review
# → export FOLDCRUMBS_DIR=/Users/you/.foldcrumbs/profiles/kimi-review
```

Non esiste `profile use`. Quale store legge un processo è deciso dal suo ambiente
**prima che parta** — una CLI non può tornare indietro nella shell che l'ha lanciata. Quindi
`profile env` stampa l'unica riga che funziona, e tu la metti dove nasce il processo dell'agent
(un file rc della shell, l'env di un worker, il `.env` di un profilo Hermes). Punta un processo
su un profilo dedicated e ottiene una vista federata read-only di ogni store shared registrato
sulla macchina.

`profile import --agent hermes --apply` registra un profilo per ogni agent di un runtime
multi-agent, così ognuno ha una memoria propria (dry-run di default).
`profile remove` deregistra senza toccare le memorie.

## Curare lo store

Ogni memoria ha uno status: **active** → (**superseded** | **deleted** | **archived**) → *file rimosso*.
Solo le memorie attive compaiono in `MEMORY.md` e nel recall. I file non attivi restano su disco —
verificabili e recuperabili (`restore` rivitalizza una archiviata) — finché `foldcrumbs prune --apply`
non li rimuove davvero.

Tre modi in cui una memoria smette di essere vera:

**Dici tu che è sbagliata — `forget`.** Prende il nome file esatto mostrato in `MEMORY.md`
(o in un risultato di recall). Dry-run di default, come `prune`:

```bash
foldcrumbs forget fact_wrong.md                 # dry-run: mostra cosa succederebbe
foldcrumbs forget fact_wrong.md --apply         # marca status: deleted, file tenuto
foldcrumbs forget fact_wrong.md --apply --hard  # scollega il file immediatamente
foldcrumbs forget "deploy sbagliato"            # non è un nome file → elenca i file candidati
```

Gli agent MCP ottengono lo stesso via il tool `forget` (solo soft-delete).

**Qualcosa l'ha sostituita — `supersede`.** Punti a entrambi i lati; la vecchia memoria
mantiene un link `superseded_by` verso la nuova e la sua fiducia collassa a 0:

```bash
foldcrumbs supersede decision_pypi_deferred.md --by fact_published_to_pypi.md
```

**La distillazione se ne accorge da sola — il passaggio di contraddizione.** Il dedup unisce solo
testo *quasi identico*; una decisione ribaltata si legge in modo completamente diverso. Quindi al
momento del distill, quando una nuova memoria copre lo stesso argomento di una vecchia (una rozza
sovrapposizione di radici di parole sceglie i candidati), all'LLM viene posta una domanda: *la nuova
memoria rende obsoleta la vecchia?* Solo un sì esplicito fa un supersede. Esempio: una vecchia
decisione "la pubblicazione su PyPI è rinviata" viene auto-superseded quando viene distillato un
nuovo fatto "pubblicato su PyPI". Fail-soft (nessun LLM → non cambia nulla); disattiva con
`FOLDCRUMBS_NO_AUTO_SUPERSEDE=1`. Gli eventi di supersede sono loggati in
`~/.foldcrumbs/foldcrumbs.log`.

**Sfuma da sola — `decay`.** Una memoria di cui nessuno si fida e che nessuno tocca
non è sbagliata, è solo vecchia. `foldcrumbs decay` trova le memorie attive la cui
fiducia è scesa sotto 0.3 **e** che non sono state scritte o validate da 30 giorni, e le
sposta a `status: archived`. Le memorie archiviate escono dall'indice, dal recall e dagli shard
federati — le altre istanze smettono di vederle — ma il file resta su disco. `foldcrumbs restore <name>`
ne riporta una indietro. La passata è esplicita e dry-run di default; non è mai un effetto
collaterale di un recall, quindi la lettura non può mai cambiare silenziosamente ciò che lo store contiene.

## Più istanze, un progetto: federazione

Eseguire `claude`, `claude-work`, `claude-peo`, … significa un `CLAUDE_CONFIG_DIR`
ciascuno, quindi **uno store ciascuno** — una decisione registrata in uno è invisibile agli
altri. La federazione dà a ogni istanza una vista read-only di ciò che le altre hanno
imparato sullo stesso progetto, live e senza duplicare nulla. Gli store restano separati e
di proprietà separata: un'istanza scrive sempre e solo il proprio.

```bash
foldcrumbs install          # ogni istanza si autoregistra
foldcrumbs roots            # chi è federato, e dove vive la sua memoria
```

Ciò che ogni istanza vede a SessionStart: il proprio `MEMORY.md` esattamente come
prima, seguito da un blocco separato che elenca le directory di memoria delle altre istanze
e le loro voci, ciascuna con un path assoluto. `recall`, `answer` e i tool
MCP cercano in tutte, etichettando i risultati con la loro origine.

```
<foldcrumbs-federated>
Memoria dalle altre istanze agent di questo progetto. … READ-ONLY da qui …

- claude-work: /Users/you/.claude-work/projects/<project>/memory
- claude-peo:  /Users/you/.claude-peo/projects/<project>/memory

- [claude-work] Recall è grep, niente vector DB — il motore di retrieval è l'agent
  /Users/you/.claude-work/projects/<project>/memory/decision_recall_is_grep.md
</foldcrumbs-federated>
```

Tre proprietà sono deliberate, e ciascuna è costata qualcosa per essere fatta bene:

**Nulla è scritto in comune.** Ogni istanza pubblica un proprio shard di indice sotto
`~/.foldcrumbs/projects/<project>/roots/<root-id>.json`; i lettori li uniscono. Un unico indice
condiviso avrebbe significato due istanze che scansionano e riscrivono in parallelo, e una
sostituzione atomica previene un file strappato, non uno obsoleto. L'ordinamento è una chiave
totale (tipo, data, root id, nome file) così ogni istanza deriva lo stesso ordine senza un file
condiviso su cui accordarsi.

**`MEMORY.md` non è toccato.** La federazione non lo modifica mai, così resta
byte-identico mentre solo le altre istanze scrivono — ed è ciò che mantiene il prefisso
iniettato a cavallo della prompt cache dell'agent. La vista federata è aggiunta
dopo, nella regione che l'handoff già invalida a ogni sessione.

**Il read-only è applicato, non richiesto.** Il blocco dice al modello che quei file
appartengono a qualcun altro, ma `write_memory`, `upsert` e `mark_superseded_on_disk`
rifiutano anche un record estraneo a prescindere. Quando la distillazione trova una nuova memoria
che ne contraddice una nello store di un'altra istanza, registra la rivendicazione sul proprio
record e la vista federata marca quella voce come contestata — la loro istanza resta l'unica
a poter ritirare il loro file.

Esci dalla vista condivisa con `foldcrumbs roots remove <id>`; lo store stesso non è
toccato, e solo un `install` / `roots add` esplicito lo riporta.

Limiti da conoscere: la federazione è per macchina (le root si registrano in
`FOLDCRUMBS_STATE_DIR`, quindi istanze puntate su state dir diverse non possono vedersi
tra loro — `status` lo dice quando può rilevarlo); una root irraggiungibile mantiene le ultime
voci pubblicate, segnalate, invece di sembrare svuotata; e **dopo aver aggiornato il pacchetto,
esegui di nuovo `foldcrumbs install`** — gli hook girano da uno snapshot runtime preparato al
momento dell'installazione, quindi un aggiornamento da solo non li raggiunge.

## Condividere memoria tra store: `import`

Gli store sono con namespace **per istanza × per progetto**: la memoria vive in
`<config-dir>/projects/<encoded-cwd>/memory/`, dove `<config-dir>` rispetta
`CLAUDE_CONFIG_DIR`. Esegui più istanze (es. `~/.claude`, `~/.claude-work`) ed è
*strutturale* che uno store finisca ricco mentre un altro parte vuoto per lo
stesso progetto.

Due modi per colmare quel divario, e rispondono a domande diverse. La **federazione**
(sopra) lascia che un'istanza *veda* la memoria delle altre, live, senza copiare — è
ciò che vuoi la maggior parte delle volte. `import` la **adotta**: una decisione
maturata in `claude-work` diventa davvero tua, con un bump di fiducia al merge,
e sopravvive alla scomparsa di quell'istanza. La federazione mostra; import prende possesso.

I due lati del comando:

- **target** (dove si scrive) — lo store dell'istanza *che esegue il comando*, cioè
  il tuo `CLAUDE_CONFIG_DIR` (default `~/.claude`) + la directory da cui lo esegui;
- **sorgente** (`--from`) — qualsiasi path: direttamente una dir di memoria, o una dir di progetto
  risolta con la stessa convenzione.

```bash
# riempi lo store dell'istanza work da quello principale (esegui dalla dir del progetto):
CLAUDE_CONFIG_DIR=~/.claude-work foldcrumbs import \
  --from ~/.claude/projects/<slug>/memory --apply

# promuovi ciò che l'istanza work ha imparato di nuovo nel principale:
foldcrumbs import --from ~/.claude-work/projects/<slug>/memory --apply
```

Cosa fa — e cosa deliberatamente non fa:

| | |
|--|--|
| merge a livello di record | ogni memoria passa per `upsert`: nuova → creata, quasi duplicata → **valida** quella esistente (bump di fiducia, nessun doppio) |
| salta il rumore | `MEMORY.md`, `HANDOFF*`, file senza frontmatter, record superseded/deleted — la storia morta resta dov'è |
| prima dry-run | il default mostra il piano `{created, validated, skipped}`; `--apply` scrive e ricostruisce l'indice |
| idempotente | rieseguire valida soltanto — sicuro da usare come sync manuale periodico |
| unidirezionale | bidirezionale = eseguilo due volte, una per direzione |
| nessun LLM | il passaggio di contraddizione **non** gira su import (prevedibilità); una memoria importata che ne contraddice una locale coesiste finché un distill non la rivede o fai `supersede` a mano |

Contrapposto a `migrate --from`, che è una copia grezza di file per spostamenti una-tantum.
Se lo store *principale* è sincronizzato tra macchine (es. Syncthing), un pattern naturale
è hub-and-spoke: import nel principale solo da una macchina, aggiorna le istanze per-macchina
dal principale.

## Sopravvivere a `/clear` e `/compact`

Due livelli attraversano lo switch di contesto:

- **Memorie durevoli** (decisioni, regole, preferenze, fatti) — sempre re-iniettate via
  l'indice `MEMORY.md` a SessionStart / PostCompact.
- **Handoff dello stato di lavoro** — un singolo snapshot sovrascritto del task *corrente*, file
  in volo e prossimi passi, scritto a ogni checkpoint e re-iniettato così riprendi l'esatto
  task dopo un `/clear` netto.

A ~45% del contesto foldcrumbs ti suggerisce; scegli `/compact` (continua a lavorare) o `/clear`
(ricomincia da zero) — in entrambi i casi il turno successivo è re-innescato. Forza uno snapshot in
qualsiasi momento con `foldcrumbs checkpoint`.

## LLM locale

La distillazione richiede qualsiasi endpoint chat compatibile OpenAI — punta `FOLDCRUMBS_LLM_ENDPOINT`
su quello che esegui. È usato solo per la distillazione asincrona, quindi un caricamento a freddo del
modello è invisibile all'editor, e **il recall non richiede alcun modello**.

Server locali comuni (tutti espongono `/v1/chat/completions`):

```bash
# MLX — solo Apple Silicon, il più veloce su Mac
mlx_lm.server  --model <gemma-mlx-repo> --port 8081     # o mlx_vlm.server per i VLM

# Ollama — multipiattaforma (macOS / Linux / Windows)
ollama serve                                            # endpoint :11434/v1

# llama.cpp / LM Studio / vLLM — anch'essi compatibili OpenAI
```

Poi es. `export FOLDCRUMBS_LLM_ENDPOINT=http://localhost:11434 FOLDCRUMBS_LLM_MODEL=qwen2.5`.
Un gateway remoto o OpenRouter funziona allo stesso modo — cambia solo la variabile d'ambiente.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## Server MCP

foldcrumbs include un server MCP minimale (stdio, solo stdlib — nessuna dipendenza dall'SDK `mcp`)
che espone `remember`, `recall`, `answer` e `forget` a qualsiasi client MCP:

```bash
foldcrumbs-mcp            # oppure: python3 -m foldcrumbs.mcp_server
```
Codex e OpenCode sono collegati ad esso da `foldcrumbs install --agent …`. Usalo direttamente da
qualsiasi tool che parla MCP registrando il comando qui sopra.

## Come è collegato ogni agent

| Agent | Inietta all'avvio | Cattura | Note |
|-------|-------------------|---------|------|
| Claude Code | hook SessionStart | monitor PostToolUse + SessionEnd | hook a ciclo di vita completo |
| Codex | hook SessionStart (`additionalContext`) | hook Stop + PostToolUse | stessi script; + MCP per chiamate tool in-sessione |
| OpenCode | AGENTS.md → l'agent chiama `recall` (MCP) | plugin `session.idle`/`session.compacted` | nessun hook capace di iniettare, quindi recall guidato dal prompt |

## Roadmap

- **Fase 1 ✓** — Claude Code: store di file, recall grep, distillazione, anti-rot.
- **Fase 2 ✓** — Codex + OpenCode sullo stesso store via un server MCP stdlib + installer.
- **Fase 2.5 ✓** — federazione: più istanze CLI condividono una vista read-only di un
  progetto senza unire i loro store.
- **Fase 2.7 ✓** — ingegneria della memoria: rinforzo del recall e freschezza nel
  ranking, una passata di decay che archivia, profili con nome (uno store per agent) e
  comandi slash `/remember` `/recall` `/forget` `/foldcrumbs`.
- **Fase 3** — embeddings + vector DB aperto solo se la scala supera il grep; ingest di documenti via OCR.

Storico dei rilasci: [CHANGELOG.md](CHANGELOG.md).

## Crediti

foldcrumbs adatta alcune utility da [memanto](https://github.com/moorcheh-ai/memanto)
(MIT, © Moorcheh / Edge AI Innovations): le categorie di memoria tipizzata e il modello di
fiducia/decadimento, l'approccio di distillazione della sessione, l'helper di lettura del
transcript e l'idea di rendering del blocco di contesto. Questi sono reimplementati qui su uno
store di file; il motore di retrieval Moorcheh non è usato. Avviso completo in [LICENSE](LICENSE).
Grazie agli autori di memanto per averlo rilasciato sotto MIT.

## Licenza

MIT — vedi [LICENSE](LICENSE).
