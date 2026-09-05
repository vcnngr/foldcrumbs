# Design — Fleet learning per foldcrumbs (serie 0.11.0)

Stato: PROPOSTA — in attesa di doppio RT (Kimi + GPT) sul design.
Data: 2026-09-05
Autore: Qwen 3.8 Max
Dipende da: federation (roots registry, recall federato), ingest (provenance `imported`), recalls (sidecar).

## Contesto

La roadmap Fase 3 dichiara "condivisione della memoria a livello di flotta
(adozione tra store federati)". Oggi la federation esiste già in lettura:
`store.search(federated=True)` classifica insieme i ricordi di tutti i root
registrati, e `contested_by` permette a uno store di dichiarare obsoleta una
memoria straniera. Ma la lettura federata è volatile: ciò che un agente
impara dallo store di un altro non resta — ogni sessione riparte da zero.

Il mercato ha validato la categoria (Caura: fleet memory cloud, 12 tool MCP,
trust tiers, outcome loop). Caura conferma che "una flotta di agenti che
impara collettivamente" è un bisogno reale; la sua architettura — store
centrale multi-tenant in cloud — è esattamente ciò che foldcrumbs rifiuta.
Prendiamo il concetto, non l'architettura.

Domanda di design: come fa un agente a **tenere** ciò che ha imparato da un
altro store federato, senza introdurre sincronizzazione automatica, stato
centrale o servizi esterni?

## Principi (non negoziabili)

1. **Nessuno store centrale, nessun servizio.** La flotta è l'insieme dei
   root federati già registrati; non esiste un "server della flotta", né
   cloud, né gossip, né protocollo di rete. Tutto resta file locali.
2. **Adozione esplicita, mai auto-sync.** Una memoria straniera entra nel
   mio store solo con un comando umano/agente esplicito. Nessun background
   job, nessuna propagazione implicita, nessuna sorpresa.
3. **Provenienza tracciabile e immutabile.** Ogni memoria adottata dichiara
   da dove viene (`source: adopted:<root_id>:<memory_id>`) e resta legata
   all'originale per audit. La provenienza non è decorazione: è il confine
   di trust.
4. **L'adozione è una copia, non un riferimento.** Dopo l'adozione la
   memoria è mia: la modifico, la supero, la dimentico senza toccare
   l'originale. L'originale resta di proprietà del suo root — il vincolo
   esistente "solo l'istanza proprietaria scrive il proprio store"
   (store.search docstring) resta intatto.
5. **Outcome loop onesto.** Se adotto una memoria e si rivela sbagliata,
   lo registro. Il segnale negativo deve avere un effetto visibile e
   deterministico, non una penalità occulta.
6. **Stdlib, determinismo, fail-closed.** Come ovunque: nessuna dipendenza,
   stesso store → stessi byte, errori visibili mai silenziosi.

## F1 — `foldcrumbs adopt` (adozione esplicita)

### Forma del comando

```bash
foldcrumbs adopt <root_id>:<memory-ref> [--as-type TYPE] [--note EVIDENCE]
foldcrumbs adopt --search "query" --from <root_id> [--limit N]   # trova candidati, non adotta
```

- `<root_id>` è l'id a 16 hex del registry di federation (`iter_roots()`);
  `<memory-ref>` è filename o title, risolti nel root di partenza con le
  stesse regole di `store.get()`.
- Senza argomenti di ricerca il comando adotta UNA memoria per volta, come
  `graph transit`: l'adozione in batch è deliberatamente assente — adottare
  in massa è il comportamento di un sync, e il principio 2 lo vieta.

### Meccanica

1. Risolvi il root (`get_root`); se il root non è disponibile → errore
   visibile, nessuna scrittura (stessa postura di ingest: fail-before-write).
2. Leggi la memoria originale READ-ONLY dal root sorgente.
3. Costruisci la copia locale:
   - nuovo `MemoryRecord` con lo stesso contenuto, tipo e tag;
   - `provenance: imported` (peso 0.8 già in schema — deliberatamente NON
     `explicit_statement`: una memoria adottata è meno fidata di una
     dichiarata dall'utente in questa sessione);
   - `source: adopted:<root_id>:<memory_id>` — la coppia root+id è stabile
     (gli id non cambiano mai; i filename sì);
   - `confidence`: `min(originale.confidence, 0.8)` — il cap del peso
     `imported` garantisce che l'adozione non amplifichi mai la confidenza;
   - campi di stato dell'originale (`superseded`, `deleted`) → l'adozione
     RIFIUTA: non si adotta storia morta, si adotta conoscenza viva.
     Eccezione: nessuna. (Chi vuole la storia usa il recall federato, che
     già la vede.)
   - `relations_json`: DROPPATO. Le relazioni esplicite puntano a id di
     memorie del root sorgente che nel mio store non esistono — copiarle
     creerebbe archi penzolanti. La copia nasce senza relazioni; le mie
     le aggiungo io con `relate` se mi servono.
4. Deduplicazione fail-closed: se il mio store contiene già una memoria con
   `source: adopted:<stesso_root>:<stesso_id>`, il comando rifiuta
   ("already adopted as <filename>") a meno di `--force`, che supersede la
   copia vecchia con la nuova. Mai due copie vive dello stesso originale.
5. Scrittura atomica nel mio store (stesso percorso di `write_memory`).
6. L'originale non viene toccato in alcun modo — nessuna scrittura sul root
   sorgente, nemmeno un contatore.

### Trust boundary

- La memoria adottata passa per `redact.scrub` PRIMA della scrittura, come
  ingest: il root sorgente è attendibile quanto il suo proprietario, non più.
  Uno store federato può essere stato scritto da chiunque; l'adozione è un
  import, e gli import si sanitizzano.
- Chiave riservata: il campo `source` con prefisso `adopted:` è riconosciuto
  e preservato da `migrate` (non strippato: è provenienza, non stato
  locale), ma `import_store` da un root esterno NON può contrabbandare
  catene di adozione: una memoria il cui `source` dice `adopted:` ma che
  arriva da import normale mantiene il valore così com'è (è un fatto
  storico sul file, verificabile dal root_id) — la differenza con transit è
  che qui non c'è alcun potere speciale associato: `adopted:` non abilita
  nulla, documenta soltanto.

## F2 — Outcome loop (`foldcrumbs outcome`)

### Forma

```bash
foldcrumbs outcome <memory-ref> good [--note EVIDENCE]
foldcrumbs outcome <memory-ref> bad  [--note EVIDENCE]
foldcrumbs outcome --list            # memorie adottate con outcome registrato
```

### Meccanica

- `good`: incrementa `validation_count` della memoria (il boost esistente
  `min(0.15, validation_count * 0.03)` nel ranking si applica già) e
  registra `outcome: good` + timestamp nel frontmatter.
- `bad`: imposta `contradiction_detected: true` (il moltiplicatore 0.3 sul
  peso esiste già in schema) e registra `outcome: bad` + note. Una memoria
  adottata che ha tradito resta visibile ma penalizzata — non viene
  cancellata: l'outcome è un dato, non una sentenza.
- L'outcome funziona su QUALSIASI memoria, non solo adottate: il loop ha
  senso anche per le mie. Ma il campo `outcome` nel frontmatter è scritto
  solo dal comando — `import_store` e `migrate` lo strippano insieme a
  `transit`: nessuno può contrabbandare validazioni dall'esterno.
- Effetto di flotta (onesto, minimo): `adopt --search` ordina i candidati
  del root sorgente anche per outcome registrati LOCALMENTE su copie già
  adottate da quello stesso root in passato (`source: adopted:<root_id>:*`
  con outcome `bad` → il root compare con un avviso "1 adopted memory from
  this root was marked bad"). Il segnale resta locale: non scrivo nulla sul
  root altrui, non esiste una reputazione centrale — solo la mia memoria
  delle mie adozioni.

### Campi schema nuovi (additivi)

- `outcome: str | None` — `good` | `bad`, default None (assente dal
  frontmatter se non impostato: zero rumore sui file esistenti).
- `outcome_at: datetime | None`, `outcome_note: str | None`.
- Nessuna modifica a campi esistenti; i file vecchi round-trippano
  invariati (stessa regola di `relations_json` in G1).

## F3 — Superficie MCP

Due tool nuovi (il server passa da 7 a 9):
- `adopt` — stessi argomenti della CLI, stessi rifiuti espliciti;
  provenienza della chiamata registrata come `agent` nella note se assente.
- `outcome` — `good`/`bad` + note.
Postura identica a `relate`: l'MCP non ottiene poteri che la CLI non ha.

## Fuori scope (esplicito)

1. **Sync/propagazione automatica** in qualsiasi forma — violerebbe il
   principio 2. Chi lo vuole può scriptare `adopt` in loop; la libreria non
   lo fa da sola.
2. **Reputazione centrale dei root** — nessun registro condiviso di
   trust score; il segnale resta per-store (F2).
3. **Adozione di relazioni** (archi G1 tra memorie di root diversi) — le
   relazioni adottate richiederebbero risoluzione di identità cross-store;
   è il problema di entity-resolution che il progetto ha già deciso di non
   aggredire. Fuori.
4. **Notifiche al root sorgente** ("la tua memoria è stata adottata") —
   sarebbe una scrittura sullo store altrui: vietato dal principio 4.
5. **Adozione via URL/cloud** — la sorgente è un root federato locale, non
   una rete. Ingest copre già i documenti esterni.
6. **Merge/blend di memorie adottate multiple** — l'adozione è 1:1.

## Piano di esecuzione e gate

| Fase | Contenuto | Gate |
|---|---|---|
| FL-0 | Questo design | doppio RT (Kimi + GPT) sul design, PR dedicata |
| FL-1 | F1 `adopt` (TDD: rifiuto root morto, stato morto, dedup fail-closed, redact, relations dropped, source stabile) | suite green + doppio RT codice |
| FL-2 | F2 `outcome` + campi schema additivi + strip su import/migrate | suite green + doppio RT codice |
| FL-3 | F3 MCP parity (9 tool) + docs trilingue EN/IT/ZH | suite green + doppio RT |
| FL-4 | Release 0.11.0 | gate release permanente (RT GPT obbligatorio) |

Ogni fase è una PR separata; FL-2 dipende da FL-1 solo per i test di
`adopt --search`, non per lo schema.

## Risposta ai red-team (tracciabilità)

Sezione vuota finché il doppio RT sul design non gira; ogni finding
(P0/P1) avrà qui la sua chiusura con riferimento alla card, come in
graph-layer.md.
