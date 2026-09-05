# Design — Fleet learning per foldcrumbs (serie 0.11.0)

Stato: REVISIONE 2 — risponde ai finding del RT GPT sul design (card t_6057c04e, RED: F1-F3 P0, F4-F6 P1). Kimi GREEN sul testo pre-revisione (card t_daa51061) — la revisione cambia la struttura di trust, quindi entrambi i revisori ri-verificano il delta.
Data: 2026-09-05 (rev 2)
Autore: Qwen 3.8 Max
Dipende da: federation (roots registry, recall federato), ingest (provenance `imported`), recalls (sidecar `.recalls.json` come precedente di ledger locale).

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
3. **Provenienza tracciabile; attestazione separata dalla dichiarazione.**
   Ogni memoria adottata DICHIARA da dove viene (`source:
   adopted:<root_id>:<memory_id>`) — ma la dichiarazione è documentazione
   inerte: da sola non abilita né blocca nulla (RT F1). Ciò che fa fede è
   l'ATTESTAZIONE locale: il ledger `.adoptions.json` del mio store, che
   solo il comando `adopt` scrive e che non viaggia con i file. La
   distinzione è il confine di trust: un file può mentire sulla propria
   provenienza; il ledger no, perché non è importabile.
4. **L'adozione è una copia, non un riferimento.** Dopo l'adozione la
   memoria è mia: la modifico, la supero, la dimentico senza toccare
   l'originale. L'originale resta di proprietà del suo root — il vincolo
   esistente "solo l'istanza proprietaria scrive il proprio store"
   (store.search docstring) resta intatto.
5. **Outcome loop onesto.** Se una memoria si rivela sbagliata, lo
   registro; l'effetto è dichiarato, persistente e verificabile — mai una
   penalità occulta né un boost fantasma (RT F5: gli effetti devono
   sopravvivere al round-trip su disco).
6. **Stdlib, determinismo, fail-closed.** Come ovunque: nessuna dipendenza,
   stesso store → stessi byte, errori visibili mai silenziosi, nessuna
   scrittura prima che ogni controllo sia passato.

## F1 — `foldcrumbs adopt` (adozione esplicita)

### Forma del comando

```bash
foldcrumbs adopt <root_id>:<memory-ref> [--as-type TYPE] [--note EVIDENCE]
foldcrumbs adopt --search "query" --from <root_id> [--limit N]   # trova candidati, non adotta
```

- `<root_id>` è l'id a 16 hex del registry di federation (`iter_roots()`);
  `<memory-ref>` è filename o title, risolti nel root di partenza con le
  stesse regole di `store.get()`.
- Il comando adotta UNA memoria per volta, come `graph transit`: l'adozione
  in batch è deliberatamente assente — adottare in massa è il comportamento
  di un sync, e il principio 2 lo vieta.
- `--force` NON esiste in FL-1 (RT F3): ogni collisione è un rifiuto
  esplicito. Una sostituzione storicizzata (supersede transazionale della
  vecchia copia) è lavoro per una fase successiva, se servirà.

### Identità dell'originale (RT F2)

Prima di qualsiasi scrittura, l'originale deve avere identità STABILE e
UNIVOCA nel suo root:

1. `id` presente e conforme alla grammatica degli id (stessa regex usata
   dallo schema). Una memoria legacy senza `id` riceve un UUID nuovo a ogni
   parse — lo schema lo segnala già col flag `id_missing` (schema.py:198):
   due letture dello stesso file produrrebbero due chiavi `source` diverse
   e la deduplicazione sarebbe impossibile. RIFIUTO su `id_missing`:
   "original has no stable id — ask its owner to re-save it in their
   store". Fail-before-write.
2. `id` univoco nel root sorgente: se due file dello stesso root dichiarano
   lo stesso id, RIFIUTO ("ambiguous id in source root").
3. La stringa `source` è costruita da adopt con il `root_id` del REGISTRY e
   l'id VERIFICATO dell'originale — mai ereditando il campo `source`
   preesistente del file originale.

### Stato vivo dell'originale (RT F4)

Si adotta solo conoscenza viva. RIFIUTO (fail-before-write, messaggio
esplicito) se l'originale è:

- `status != active` (superseded, deleted, provisional, archived);
- oppure attivo MA scaduto (`expires_at` nel passato) — un active scaduto è
  invisibile al recall (`_visible`), adottarlo lo "rianimerebbe" nel mio
  store: mai.

La correzione al testo rev-1 è deliberata: rev-1 diceva "il recall federato
già vede la storia morta" — falso, `store.search` filtra `_visible`. La
storia morta si consulta con `graph path`/transit nel root che la possiede,
non si adotta.

### Meccanica (ordine operativo)

1. Risolvi il root (`get_root`); root non disponibile → errore visibile,
   nessuna scrittura.
2. Leggi l'originale READ-ONLY; verifica identità (sopra) e stato vivo.
3. **Collision check (RT F3)**: calcola il filename di destinazione
   (`tipo-slug(title)`, le regole di `write_memory`) e verifica che non
   esista già un file con quel nome nel mio store per una memoria DIVERSA
   (id diverso). Collisione → RIFIUTO esplicito ("local memory <file> would
   be overwritten; rename or supersede it first"). `write_memory` usa
   `os.replace`: senza questo check l'adozione di un omonimo straniero
   distruggerebbe una memoria locale indipendente. Mai.
4. **Dedup sul LEDGER, non sul source (RT F1)**: se `.adoptions.json`
   contiene già una voce `(root_id, memory_id)` con copia locale viva,
   RIFIUTO ("already adopted as <filename>"). La dedup NON consulta il
   campo `source` dei file: una dichiarazione `adopted:` contraffatta via
   import non occupa nessuna chiave operativa e non produce falsi
   "already adopted".
5. Costruisci la copia locale — contratto dei campi (RT F4/F5):
   - contenuto, tipo, tag: copiati;
   - **id: NUOVO** (la copia è una memoria mia, con identità propria e
     persistita);
   - `provenance: imported` (peso 0.8 — deliberatamente non
     `explicit_statement`: una memoria adottata è meno fidata di una
     dichiarata dall'utente qui);
   - `source: adopted:<root_id>:<memory_id>` — documentazione inerte
     (principio 3);
   - `confidence: min(originale.confidence, 0.8)` — claim ristretta (RT
     F5): il cap limita la confidence GREZZA; il peso effettivo usato nel
     ranking dipende da altri fattori (freshness dell'originale, stato
     contradicted): adopt non promette che il peso effettivo della copia
     sia ≤ quello dell'originale, promette che la copia nasce con
     confidence grezza ≤ 0.8 e `validation_count = 0`;
   - `validation_count: 0` — la copia non eredita validazioni altrui
     (RT F6: `import_store` oggi conserva validation_count; per ADOPT la
     ripartenza da zero è obbligatoria);
   - `created_at`: ora dell'adozione; `updated_at`: idem;
   - `expires_at`: se l'originale ha una scadenza FUTURA, preservata
     (l'adozione non elimina vincoli di freschezza); se assente, assente;
   - `contradiction_detected`: False (non si ereditano contenziosi altrui);
   - DROPPATI: `relations_json` (archi verso id del root sorgente
     penzolerebbero), `superseded_by`, `transit`, `outcome*`,
     `source_path`, ogni `extra_meta` operativo dell'originale;
   - `redact.scrub` sul contenuto PRIMA della scrittura (il root sorgente è
     attendibile quanto il suo proprietario, non più: l'adozione è un
     import e gli import si sanitizzano).
6. Scrittura atomica (`write_memory`) + append al ledger `.adoptions.json`
   sotto il lock del mio store. Ordine: PRIMA il file memoria, POI il
   ledger; se la scrittura del ledger fallisce, il file esiste senza
   attestazione → `adopt` di nuovo lo stesso originale produce il refusal
   di collisione al punto 3 (il file c'è) e il messaggio indica di
   verificare il ledger: nessuno stato corrotto silenzioso. (`doctor` può
   riconciliare ledger/file in una fase successiva.)
7. L'originale e il suo root non vengono toccati in alcun modo — nessuna
   scrittura sul root sorgente, nemmeno un contatore.

### Ledger `.adoptions.json`

- Sidecar locale nel mio store, stesso pattern di `.recalls.json`:
  `{ "<local_memory_id>": {"root_id": "...", "memory_id": "...",
  "adopted_at": "...", "filename": "..."} }`
- Non è un file memoria: non viene elencato da `load_all`, non è
  importabile, `import_store` e `migrate` non lo toccano e non lo creano.
  L'import di un intero store con ledger proprio è fuori scope: il ledger
  attesta le ADOZIONI DI QUESTO STORE, e un altro store non può attestare
  per me.
- Lettura tollerante come `.recalls.json`: ledger corrotto → `adopt`
  rifiuta fail-closed ("adoption ledger unreadable"), `recall` e tutto il
  resto funzionano come prima.

### Trust boundary — cosa fa fede e cosa no (RT F1)

| Dato | Dove vive | Chi può scriverlo | Potere operativo |
|---|---|---|---|
| `source: adopted:...` | frontmatter del file | chiunque (anche import) | NESSUNO — documentazione |
| `.adoptions.json` | sidecar del mio store | solo `adopt` locale | dedup, `outcome --list`, avvisi reputazionali locali |
| `outcome*` | frontmatter del file | solo `outcome` locale | effetto ranking dichiarato in F2 |

- `import_store` da root esterno: il campo `source` con prefisso `adopted:`
  arriva così com'è (fatto storico sul file) ma NON entra nel ledger: la
  copia importata non risulta "adottata" e non occupa chiavi dedup.
- `migrate`: stesso trattamento — `source` preservato come testo, ledger
  non popolato, `outcome*` strippato insieme a `transit`.
- Onestà (RT F1, chiusura della claim rev-1 "immutabile"): la provenienza
  dichiarata è garantita solo nei percorsi supportati (CLI/MCP/import/
  migrate). Chi modifica i file a mano può scrivere qualsiasi `source`:
  per questo il potere operativo sta nel ledger, non nel frontmatter.

## F2 — Outcome loop (`foldcrumbs outcome`)

### Forma

```bash
foldcrumbs outcome <memory-ref> good [--note EVIDENCE]
foldcrumbs outcome <memory-ref> bad  [--note EVIDENCE]
foldcrumbs outcome --list            # adozioni attestate con outcome
```

### Effetti reali e persistenti (RT F5 — il punto centrale)

Il RT ha dimostrato con sonde che:
- `contradiction_detected` NON è serializzato nel frontmatter: impostarlo
  in memoria e riscrivere PERDE il flag al round-trip;
- il ranking di `store.search` non usa `compute_confidence`/pesi
  provenienza: usa rilevanza lessicale, freshness/recalls, località.

Quindi FL-2 non può "riusare e basta": dichiara effetti propri.

- **`good`**: `validation_count += 1` (campo già serializzato),
  `outcome: good`, `outcome_at`, `outcome_note`. Effetto dichiarato: il
  boost `min(0.15, n*0.03)` in `compute_confidence` — che influenza i
  percorsi che usano il peso effettivo (answer/audit), NON l'ordine di
  `search`. Nessuna overclaim: il doc utente dirà esattamente questo.
- **`bad`**: FL-2 AGGIUNGE la serializzazione di `contradiction_detected`
  (campo esistente, oggi non round-trippato: cambiamento additivo di
  schema, file vecchi invariati) E scrive `outcome: bad`, `outcome_at`,
  `outcome_note`. Effetto dichiarato: `compute_confidence` →
  `max(0.1, confidence * 0.3)` sui percorsi a peso effettivo.
  Edge case dichiarato (RT F5): su confidence molto bassa il ramo
  `max(0.1, ...)` può ALZARE il valore effettivo — FL-2 lo corregge in
  `min(valore_pre_bad, max(0.1, confidence*0.3))`: una penalità non
  promuove mai.
- **Sequenze**: `bad` poi `good` → `outcome: good` ma
  `contradiction_detected` RESTA true (il contenuto fu contraddetto: la
  rivalidazione non cancella la storia; per quello esiste `supersede`).
  `good` ripetuto → validation_count cresce, outcome_at si aggiorna.
  Outcome su memoria non attiva → rifiuto esplicito.
- Outcome funziona su QUALSIASI memoria (anche non adottata): il loop ha
  senso anche per le mie.

### Persistenza e strip (RT F6)

- `outcome`, `outcome_at`, `outcome_note` e `contradiction_detected`
  (ora serializzato) sono CHIAVI RISERVATE: `import_store` e `migrate` li
  strippano insieme a `transit`. Nessuna validazione o contenzioso può
  essere contrabbandato dall'esterno.
- `validation_count` su import: comportamento ESISTENTE (import_store lo
  conserva — il RT lo ha misurato a 99). Non è una regressione di questo
  design; è debito pregresso. Decisione di scope: FL-2 NON cambia il
  comportamento generale di import (fuori scope, eviterebbe sorprese a chi
  lo usa oggi), ma le copie ADOPT partono sempre da `validation_count = 0`
  (F1 punto 5) e il ledger separa comunque l'attestazione. Se il RT lo
  ritiene P1, si apre una issue separata per import; il design fleet non
  lo nasconde.

### Effetto di flotta (onesto, minimo, tutto locale)

`adopt --search` consulta il LEDGER + gli outcome delle copie attestate:
se da un root ho già adottato memorie marcate `bad`, l'elenco candidati
mostra un avviso ("N memories previously adopted from this root were marked
bad"). Il segnale resta mio: nessuna scrittura sul root altrui, nessuna
reputazione centrale, nessun file condiviso. E poiché fa fede il ledger,
un `source: adopted:` contraffatto via import non genera avvisi
reputazionali contro nessun root (RT F1 chiuso anche qui).

### Campi schema nuovi (additivi)

- `outcome: str | None` (`good`|`bad`), `outcome_at: datetime | None`,
  `outcome_note: str | None` — assenti dal frontmatter se non impostati:
  zero rumore sui file esistenti.
- `contradiction_detected`: serializzazione nuova di un campo esistente
  (vedi sopra).
- Nessuna modifica ai campi esistenti; i file vecchi round-trippano
  invariati (stessa regola di `relations_json` in G1).

## F3 — Superficie MCP

Due tool nuovi (il server passa da 7 a 9):
- `adopt` — stessi argomenti della CLI, stessi rifiuti espliciti, stesso
  ledger; provenienza della chiamata `agent` registrata in `outcome_note`
  dell'adozione se `--note` assente.
- `outcome` — `good`/`bad` + note.
Postura identica a `relate`: l'MCP non ottiene poteri che la CLI non ha.

## Fuori scope (esplicito)

1. **Sync/propagazione automatica** in qualsiasi forma — violerebbe il
   principio 2. Chi lo vuole può scriptare `adopt` in loop; la libreria non
   lo fa da sola.
2. **Reputazione centrale dei root** — nessun registro condiviso di trust
   score; il segnale resta per-store, nel ledger locale (F2).
3. **Adozione di relazioni** (archi G1 tra memorie di root diversi) —
   richiederebbero risoluzione di identità cross-store; è il problema di
   entity-resolution che il progetto ha deciso di non aggredire. Fuori.
4. **Notifiche al root sorgente** ("la tua memoria è stata adottata") —
   sarebbe una scrittura sullo store altrui: vietato dal principio 4.
5. **Adozione via URL/cloud** — la sorgente è un root federato locale.
   Ingest copre già i documenti esterni.
6. **Merge/blend di memorie adottate multiple** — l'adozione è 1:1.
7. **`--force`/sostituzione storicizzata di copie precedenti** — differito
   (RT F3): richiede una transazione multi-file con recupero su errore;
   FL-1 rifiuta sempre.
8. **Cambiare il comportamento generale di `validation_count` su
   `import_store`** — debito pregresso, issue separata se il RT la chiede
   (RT F6); non è parte di questo design.

## Piano di esecuzione e gate

| Fase | Contenuto | Gate |
|---|---|---|
| FL-0 | Questo design (rev 2) | doppio RT sul delta rev2, PR dedicata |
| FL-1 | F1 `adopt` + ledger `.adoptions.json`. TDD obbligatorio: root morto; identità instabile/ambigua (legacy senza id, id duplicati); stato morto (matrice: superseded/deleted/provisional/archived/expired/active-con-scadenza-futura); collisione filename (omonimo locale, omonimi da root diversi, titolo che collide dopo slugify); dedup su ledger + source contraffatto via import che NON blocca; redact; relazioni e campi operativi droppati; source costruito da registry; nessuna scrittura sul root sorgente nei casi rifiutati; due letture dello stesso originale → stessa chiave | suite green + doppio RT codice |
| FL-2 | F2 `outcome` + serializzazione `contradiction_detected` + fix `min(pre_bad, ...)` + strip chiavi riservate su import/migrate. TDD: round-trip da disco del bad; good/bad/good; bassissima confidence non promossa; outcome su non-attiva rifiutato; strip verificato su import E migrate | suite green + doppio RT codice |
| FL-3 | F3 MCP parity (9 tool) + docs trilingue EN/IT/ZH (effetti outcome dichiarati con le limitazioni reali: search non riordinato) | suite green + doppio RT |
| FL-4 | Release 0.11.0 | gate release permanente (RT GPT obbligatorio) |

Ogni fase è una PR separata. FL-2 dipende da FL-1 per ledger e `adopt
--search`, non per lo schema.

## Risposta ai red-team (tracciabilità)

RT GPT design, card `t_6057c04e`, commit revisionato `c7ffcdf`, VERDETTO RED:

- **F1 (P0) — source dichiarato usato come attestazione**: CLOSED in rev 2.
  Separazione dichiarazione (frontmatter, inerte) / attestazione (ledger
  `.adoptions.json`, locale, non importabile). Dedup e avvisi reputazionali
  solo dal ledger. Tabella trust boundary esplicita. Claim "immutabile"
  sostituita con l'onestà sui percorsi supportati (principio 3, §Trust
  boundary). Test FL-1: source falso via import non produce already-adopted
  né supersessioni; nessuna attribuzione reputazionale dalla stringa.
- **F2 (P0) — identità instabile**: CLOSED in rev 2. §Identità
  dell'originale: rifiuto fail-before-write per originali senza id stabile,
  id non conformi o duplicati nel root; `source` costruito da registry +
  id verificato, mai ereditato. Test FL-1: due letture/due tentativi stesso
  originale, id duplicati, caratteri ostili.
- **F3 (P0) — collisione filename distruttiva**: CLOSED in rev 2.
  Collision check al punto 3 della meccanica (prima di ogni scrittura),
  rifiuto esplicito; `--force` rimosso da FL-1 e spostato fuori scope
  (punto 7). Test FL-1: omonimo locale, omonimi da root diversi, slug
  collidenti; vecchi byte/id/relazioni invariati nei rifiuti.
- **F4 (P1) — stato vivo e contratto copia**: CLOSED in rev 2. §Stato vivo:
  `status == active` AND non scaduto; correzione esplicita della claim
  errata sul recall federato. Contratto campi completo al punto 5
  (id nuovo, created_at/updated_at ora, validation_count 0, expires_at
  futuro preservato, contradiction_detected False, lista DROP).
- **F5 (P1) — effetti outcome non esistenti**: CLOSED in rev 2. §Effetti
  reali: FL-2 aggiunge serializzazione di contradiction_detected e fix
  `min(pre_bad, max(0.1, c*0.3))`; effetti dichiarati sui percorsi a peso
  effettivo, NON sul ranking di search; sequenze good/bad/good definite;
  claim confidence ristretta.
- **F6 (P1) — strip incompleto**: CLOSED in rev 2. §Persistenza e strip:
  tutte e quattro le chiavi (outcome, outcome_at, outcome_note,
  contradiction_detected) riservate e strippate su import E migrate;
  validation_count=0 sulle copie adopt; il comportamento generale di
  import_store su validation_count è dichiarato debito pregresso, fuori
  scope, issue separabile (punto 8 fuori scope).

RT Kimi design, card `t_daa51061`, VERDETTO GREEN sul testo rev-1: le
citazioni del codice erano corrette; la rev 2 non invalida quel lavoro ma
cambia la struttura di trust, quindi Kimi riverifica il delta.
