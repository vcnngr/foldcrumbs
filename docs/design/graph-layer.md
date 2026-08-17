# Design — Graph layer per foldcrumbs (serie 0.8.0)

Status: REV-2 — post red-team. Kimi K3 (card t_22f4feed) e GPT-5.6-sol
(card t_4fc70dde) hanno risposto entrambi: APPROVATO CON MODIFICHE.
I 3 bloccanti e gli 8 finding importanti sono assorbiti qui sotto come
decisioni esplicite. Nessuna riga di codice di G1 finché il titolare non
approva questa revisione.

## Contesto

foldcrumbs ricorda fatti; non collega abbastanza bene i fatti tra loro.
Il recall risponde a "cosa sappiamo di X?" ma non a "perché X?" né a
"come si collega A a B?" — le catene causali vivono in memorie che non
condividono parole chiave, e la similarità trova testo che si *somiglia*,
non fatti che si *collegano*.

Misurazione sul campo (2026-08-17, store reale del titolare): 5 domande
causali sul progetto foldcrumbs → 4 falliscono col recall attuale
(restituiscono frammenti simili ma scollegati o non c'entranti). Il
criterio di accettazione della serie è che queste domande inizino a
funzionare.

Assunzione strutturale (aggiornata 2026-08-17, scelta titolare): W4
(split di store.py/federation.py in pacchetti) è DEFERITO — il graph
layer è additivo (nuovo modulo graph.py + nuovi campi schema.py) e non
richiede lo split. Le interfacce pubbliche restano invariate. Se/...[truncated]

## Principi (non negoziabili)

1. **Le relazioni vivono nel frontmatter delle memorie** — file leggibili,
   diffabili, verificabili. Nessun database, nessun sidecar binario.
2. **Evidence obbligatoria** — vedi §"Trattamento dell'evidence" per la
   distinzione tra scritture umane e proposte LLM.
3. **Nessuna sovrascrittura silenziosa** — né logica (contraddizioni →
   coda `conflicts`) né fisica (lost update vietato, §Concorrenza).
4. **Nessun merge automatico di entità** — al più si suggerisce
   (`graph entities --similar`); decide l'umano.
5. **Determinismo** — stesso store, stesso output. Nessun LLM nei path di
   lettura (G0/G1); LLM solo in G2, opt-in.
6. **Stdlib-only** — nessun Neo4j, nessuna dipendenza nuova.
7. **Identità stabile** — gli archi forti puntano a `Memory.id`
   (immutabile), MAI al filename (derivato da slug(title): un retitle lo
   rinomina e lascerebbe riferimenti penzolanti silenziosi — schema.py:
   284-291). Il filename è solo display, risolto in lettura.

## G0 — Graph view read-only (nessun cambiamento di schema)

Deriva un grafo dalle relazioni GIÀ esistenti nello store:

- archi `superseded_by` (vecchia → nuova memoria, per id)
- coppie della reconciliation queue (flagged pairs)
- co-occorrenza di tag (arco DEBOLE, pesato, solo sopra soglia 2+
  memorie condivise; escluso dal budget delle path query, §BFS)

Output di `foldcrumbs graph`:

- testo (lista di archi, per pipe e test) — formato primario
- `--format mermaid` e `--format dot`
- `--out FILE.html` — pagina autocontenuta come la dashboard. Nota
  onesta: senza script è un REPORT tabellare, non una visualizzazione
  interattiva; la documentazione non la venderà come graph viz.

Scopo di G0: dimostrare che il grafo derivato dice qualcosa di utile
sullo store reale PRIMA di introdurre schema nuovo. Se G0 non dimostra
valore, la serie si ferma qui senza danni. G0 non tocca lo schema.

## G1 — Relazioni esplicite (schema nuovo, additivo)

### Storage: `relations_json`, non YAML annidato (bloccante GPT-F6)

Il parser frontmatter attuale (`_split_frontmatter`) accetta solo righe
piatte `chiave: valore`: uno schema YAML annidato verrebbe ignorato in
lettura e CANCELLATO alla prima riscrittura — violazione diretta del
principio 3. Decisione:

```
relations_json: [{"p":"caused_by","t":{"k":"m","id":"a1b2c3…"},"e":"il fornitore…","c":0.82,"d":"2026-08-08T10:00:00Z"}]
```

- una riga, JSON canonico (chiavi ordinate, nessun spazio superfluo) →
  determinismo del diff
- `p` predicato, `t` target tipizzato, `e` evidence, `c` confidence,
  `d` data creazione
- campi sconosciuti nel frontmatter preservati in riscrittura (fixture
  round-trip obbligatoria: parse → edit → write → parse, semanticamente
  identico; test byte-safe sul resto del file)

### Target tipizzati e discriminati (bloccante Kimi-F2, GPT-F2)

- `{"k":"m","id":<Memory.id>}` → ARCO FORTE verso una memoria. Unico
  target attraversabile di default. id inesistente al momento della
  scrittura → rifiuto esplicito (mai riferimenti penzolanti in ingresso);
  memoria rimossa dopo → arco marcato `dangling` in lettura, segnalato
  da `graph doctor`, mai seguito in silenzio.
- `{"k":"x","ns":<namespace>,"l":<label normalizzata>}` → entità
  esterna, ARCO DEBOLE, non attraversabile dalla BFS di default.
  Normalizzazione OBBLIGATORIA in scrittura: lowercase, trim, collasso
  spazi; label vuota → rifiuto. `foldcrumbs graph entities` elenca le
  entità esterne con suggerimento di possibili duplicati (solo
  suggerimento — principio 4).

### Vocabolario dei predicati — 8, chiuso

`caused_by`, `depends_on`, `supersedes`, `contradicts`, `supports`,
`refines`, `blocks`, **`precedes`** (aggiunto: il dominio temporale non
era coperto — esempio reale non esprimibile: "la migrazione Postgres è
avvenuta PRIMA del refactor ORM; il refactor ha funzionato solo perché
la migrazione era già chiusa"; forzare `caused_by` sarebbe semanticamente
falso e avvelenerebbe le query causali — Kimi-F1).

Tabella di semantica nel codice (docstring + fixture): per ogni
predicato: dominio, verso, inversa (`supersedes`↔`superseded_by`
esistente, `blocks`↔`depends_on` dichiarati inversi approssimati con
nota). Predicato sconosciuto → rifiuto esplicito.

Gap documentato, rinviato: relazioni di implementazione/conformità
("PaymentService implementa PaymentGateway" — GPT-F1) non hanno
predicato dedicato in 0.8.0. Decisione consapevole: vocabolario chiuso e
stretto batte vocabolario largo e ambiguo; l'assenza è visibile (errore
esplicito), la forzatura no. Se il collaudo sul campo mostra il bisogno,
`implements` entra in una 0.8.x con la stessa trafila (motivazione nel
CHANGELOG).

### Concorrenza: lock per memoria, fail-closed (bloccante GPT-F5)

- scrittura di `relations_json` = read-modify-write dello STESSO file
  memoria: due agenti concorrenti → l'ultimo writer cancella l'arco
  dell'altro. Vietato.
- Decisione: lock per memoria (stesso pattern mkdir-lock già in
  federation.py:140) tenuto per tutta la read-validate-write; il
  rifiuto-duplicati (stesso `p`+`t` normalizzato) è check-and-write
  sotto lo stesso lock.
- CAS/revision-digest: rinviato — il lock è coerente col codice
  esistente e basta per il caso multi-agente locale; se la federazione
  remota lo richiederà, si aggiungerà con le stesse fixture.
- Test obbligatorio: due processi aggiungono un arco diverso alla stessa
  memoria → o sopravvivono entrambi, o uno fallisce VISIBILMENTE
  (fail-closed; mai perdita silenziosa).

### Trattamento dell'evidence (coerenza Kimi/GPT)

- scrittura umana/agente senza evidence → accettata con confidence ≤ 0.5
  e provenance `inferred`: si registra l'incertezza, non la si nasconde
- proposta LLM (G2) senza evidence → SCARTATA dal parser: l'estrazione
  automatica deve provare, la scrittura diretta può dichiarare il dubbio.
  La differenza è intenzionale e documentata.

### BFS e `graph path` (Kimi-F3, GPT-F3)

- solo archi forti (default); `--weak` opt-in per includere i deboli
- esito TRI-STATO, mai ambiguo:
  - `FOUND` + cammino con evidence/confidence per arco
  - `NOT_FOUND_EXHAUSTIVE` — ricerca completata, il cammino non esiste;
    elenco dei nodi raggiunti (gap visibile)
  - `TRUNCATED:<motivo>` — budget depth o nodi esaurito; NON è una
    prova di assenza, e l'output lo dice
- limiti: depth default 3 (max 4), nodi default 500 (hard cap
  documentato); flag `--depth`/`--max-nodes` espliciti
- ordine dei vicini: totale e deterministico (ordinato per id) → stessa
  store, stesso cammino
- tool MCP `graph_path(from, to, depth, max_nodes)` — stessa semantica

## G2 — Estrazione conservativa via distill (opt-in, solo se G0+G1 validati)

Il distill propone terne, NON le scrive:

- blocco "relations" nel JSON delle proposte distill; ogni terna richiede
  `evidence` (citazione) — senza citazione è scartata dal parser
- **idempotenza della coda** (Kimi-F4, GPT-F4): `proposal_id` stabile =
  hash(source-memory-id + predicate + target normalizzato + evidence
  span); dedup PRIMA dell'ingresso in coda; stato `rejected` persistente
  (una proposta rifiutata non viene riproposta al ciclo successivo);
  raggruppamento per target + cap per distillazione (max 10 proposte per
  sessione, le eccedenti scartate con contatore visibile) — una coda di
  40 proposte porta l'umano ad approvare in blocco o a ignorarla:
  entrambi gli esiti uccidono il grafo
- approvazione esplicita → la relazione entra con provenance
  `distill_proposed`; gate env default OFF come il semantico

## Fuori scope (esplicito)

- Neo4j / Memgraph / qualsiasi graph DB
- Cypher o linguaggi di query
- entity resolution / merging automatico di entità
- community detection e global search (GraphRAG-style): costo di
  indicizzazione incompatibile con uno store di progetto
- estrazione automatica senza coda di approvazione
- vettori/embedding per il grafo: il semantico esistente resta separato
- predicato `implements` (gap documentato, rinviato — v. Vocabolario)
- CAS/revision-digest (lock per ora sufficiente — v. Concorrenza)

## Piano di esecuzione e gate

1. ~~W4 (split moduli) → main~~ DEFERITO (scelta titolare 2026-08-17):
   il layer è additivo; lo split resta fattibile come lavoro dedicato
   futuro con riscrittura del contratto test (~70 patch-site privati)
2. ✅ Design red-team doppio → questa revisione → approvazione titolare
3. G0 → branch `feat/graph-view` — sbloccato (nessun prerequisito W4)
4. G1 → branch `feat/relations` — gate: le fixture obbligatorie
   (round-trip relations_json, multi-processo fail-closed, tri-stato
   path, rifiuto predicati/target invalidi) devono esistere PRIMA del
   codice di prodotto (TDD)
5. G2 → solo se 3+4 validati
6. **Collaudo sul campo**: le domande causali misurate (4/5 oggi
   falliscono) sullo store reale del titolare. Le PR si mergiano e la
   serie diventa 0.8.0 SOLO dopo validazione del titolare. Fino ad
   allora main non vede il graph layer e nessun tag viene creato
   (publish PyPI impossibile: scatta solo su release pubblicata).

## Risposta ai red-team (tracciabilità)

| # | Fonte | Severità | Esito |
|---|-------|----------|-------|
| 1 | Kimi F2b | bloccante | assorbito: archi forti su Memory.id, principio 7 |
| 2 | GPT F6 | bloccante | assorbito: relations_json + fixture round-trip |
| 3 | GPT F5 | bloccante | assorbito: lock per memoria + test multi-processo |
| 4 | Kimi F2a | importante | assorbito: target tipizzati + normalizzazione + graph entities |
| 5 | Kimi F3 | importante | assorbito: esito tri-stato + archi deboli fuori budget |
| 6 | Kimi F5 | importante | assorbito (stesso fix di GPT F5) |
| 7 | Kimi F1 | importante | assorbito: predicato `precedes` aggiunto |
| 8 | GPT F1 | importante | assorbito: tabella semantica; `implements` gap rinviato e documentato |
| 9 | GPT F2 | importante | assorbito: target discriminato, esterni non attraversabili |
| 10 | GPT F3 | importante | assorbito: TRUNCATED con motivo + ordine deterministico |
| 11 | Kimi F4 | nota | assorbito: idempotenza e cap della coda G2 |
| 12 | Kimi F6 | nota | assorbito: HTML dichiarato report, non viz |

Entrambi i controaltare: "G0 può partire; G1 non parte finché i
bloccanti non sono chiusi". I bloccanti sono ora chiusi NEL DESIGN;
l'implementazione dovrà chiuderli NEI TEST (gate 4).
