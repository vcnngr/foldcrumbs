# Design — G2: estrazione relazioni dal modello (serie 0.8.0)

Status: REV-2 + EMENDAMENTO 2026-08-19 (E1-E6, post round 3 red-team).
Kimi R3: APPROVATO CON MODIFICHE (3 bloccanti: B1-B3) — chiusi da E1/E2/E3.
GPT R3: APPROVATO CON MODIFICHE (3 bloccanti minimi) — chiusi da E4/E5/E6.
Prima convergenza dei due revisori in tre round; emendamento approvato
dal titolare. A1 decisa (promote umano = arco manual è l'intento, auditato).

EMENDAMENTO AL REV-2 (graph-layer.md) — APPROVATO DAL TITOLARE 2026-08-18:
il Fuori scope del REV-2 vietava "estrazione automatica senza coda di
approvazione". G2 mantiene la coda di approvazione come percorso di scrittura
nello store, ma introduce l'attraversamento in lettura degli archi inferiti
via flag esplicito (--include-inferred). Ogni risposta via archi inferiti è
una scelta umana consapevole per-query, non fiducia silenziosa.

=============================================================================

## Problema (invariato)

G0+G1 sono meccanicamente corretti ma il grafo resta vuoto: le relazioni
nascono solo da `foldcrumbs relate`, un atto manuale che nessun utente CLI
farà. Il collo di bottiglia non è il codice, è il canale di popolamento.
G2 aggiunge un canale automatico senza tradire il REV-2.

=============================================================================

## D1 — Tassonomia della provenienza (3 valori, chiusa)

[Assorbe: Kimi-N1, GPT-P0-2]

Ogni arco porta `prov` ∈ {manual, agent, inferred} e `confidence` ∈ [0,1].

| prov | chi scrive | confidence | attraversabile in default |
|---|---|---|---|
| manual | umano via CLI `relate` | 0-1 libera | SÌ |
| agent | agente via MCP `relate` | cap 0.5 | NO (solo --include-inferred) |
| inferred | distill automatico | cap 0.5 | NO (solo --include-inferred) |

Motivazione: il round 1 lasciava MCP relate trusted → un agente poteva
iniettare evidence inventata a confidence 0.95 nel percorso di massima
fiducia. Ora solo l'umano ha fiducia piena; agenti e distill sono cittadini
di serie B finché non promossi.

`graph_path` di default attraversa solo `prov: manual`. Con
`--include-inferred` attraversa anche agent e inferred. Il flag è esplicito
per-query, quindi ogni risposta via archi non-manual è una scelta umana
consapevole (emendamento approvato).

## D2 — Protocollo della coda di proposte

[Assorbe: Kimi-N2, GPT-P0-3]

File: `state/relation_proposals.jsonl` — una riga JSON per proposta.

Ogni proposta ha:
- `proposal_id`: uuid4, stabile, assegnato alla scrittura
- `subject_id`, `predicate`, `target`, `evidence`, `confidence`, `prov`
- `status` ∈ {pending, promoted, rejected}
- `created_at`, `decided_at` (null finché pending)

Proprietà garantite (specchiano il pattern conflicts.py):
- **dedup**: prima di scrivere, si cercano proposte pending con stesso
  (subject_id, predicate, target.id). Se esistono, non si scrive il
  duplicato.
- **rejected persistente**: un reject non cancella la riga; segna
  status=rejected + decided_at. Le righe restano per audit.
- **cap 10 proposte per sessione distill**: oltre il cap, le proposte
  successive vengono scartate (log warning, nessun errore).
- **gate env OFF**: `FOLDCRUMBS_G2=1` abilita l'estrazione; senza, distill
  salta la fase relazioni. Default OFF = nessun arco inferito finché
  l'utente non opta in.

## D3 — Stato machine delle memorie nel percorso

[Assorbe: GPT-P0-5, Kimi-I1]

Il codice ha `VALID_STATUS = {active, superseded, deleted, provisional}`
(schema.py:45). `expired` non è uno status ma la proprietà `is_expired`
(schema.py:237). `contested` non è uno status ma una categoria della coda
conflicts (conflicts.py:122).

Regole per graph_path:
- **Universo nodi = TUTTE le memorie con status active e non is_expired**,
  indipendentemente dai filtri di prov. I filtri restringono quali ARCHI si
  attraversano, mai quali NODI esistono. Questo preserva il tri-stato:
  MISSING (nodo assente) vs NOT_FOUND_EXHAUSTIVE (nodi presenti, nessun
  cammino) vs FOUND.
- Archi che puntano a memorie superseded/deleted/provisional vengono
  saltati nella BFS (il nodo non è nell'universo), ma l'arco resta nel
  frontmatter della memoria sorgente per audit.
- Nessun arco viene copiato, spostato o cancellato su supersede/expire
  (confermato da REV-1, D6).

### D3-bis — Transit-only per le memorie superseded (emendamento, field test
2026-08-23, approvato dal titolare come direzione per la 0.9.0)

[Assorbe: finding del field test G2 — catene causali reali si spezzano
attraverso nodi superseded; D3 li esclude dall'universo, quindi un path
A --precedes--> S --supersedes--> B risponde NOT_FOUND_EXHAUSTIVE anche
quando la catena esiste ed è interamente attestata.]

Il supersede è l'unico status che NON rappresenta un'obiezione alla memoria:
una memoria superseded è stata vera, poi sostituita — il suo contenuto e le
sue relazioni restano fattualmente corretti nel momento in cui furono scritti.
deleted/provisional/expired invece sì (ritrattato / mai confermato / non più
valido), e restano esclusi come oggi.

Regole:
- **Le memorie con status=superseded (non expired) entrano nell'universo
  della BFS come nodi transit-only**: archi entranti e uscenti sono
  attraversabili secondo le regole di prov vigenti (default manual-only,
  include_inferred opt-in), ma il nodo NON può essere estremo di query.
- **Gli estremi restano active+non-expired, come oggi**: `graph_path` con
  src o dst superseded risponde NOT_FOUND_EXHAUSTIVE con nota esplicativa
  (comportamento E3 invariato per gli estremi).
- **L'output dichiara il transito**: ogni step FOUND su nodo superseded
  porta `"status": "superseded"` e il rendering CLI/MCP lo marca (es.
  `(superseded — transit)`). L'utente vede sempre che il cammino attraversa
  una memoria sostituita; mai silenzioso.
- **D1 invariato**: il containment di prov è ortogonale. Un arco
  agent/inferred/legacy verso o attraverso un nodo superseded resta
  attraversabile solo con include_inferred.
- **Overlay proposte**: una pending proposal con estremo superseded è
  scartata da overlay_edges (gli estremi proposti devono essere active);
  il transito di overlay su nodo superseded esistente è permesso alle stesse
  condizioni di prov dell'overlay.
- **Nessun cambiamento agli archi**: nessun arco copiato/spostato/cancellato
  (D6 confermato). Gli archi da/verso memorie superseded esistono già nei
  frontmatter; prima erano semplicemente irraggiungibili.

Tri-stato: invariato. FOUND / NOT_FOUND_EXHAUSTIVE / TRUNCATED mantengono i
loro significati; cambia solo l'insieme dei nodi attraversabili.

Criterio di accettazione: le catene causali del field test (A --precedes-->
S_superseded --supersedes--> B) rispondono FOUND con i prov corretti, e
NOT_FOUND_EXHAUSTIVE se gli archi sono solo non-manual e il flag è assente.
Estremi superseded: ancora NOT_FOUND_EXHAUSTIVE con nota.

## D4 — Formato eseguibile dal parser esistente

[Assorbe: GPT-P0-4 da round 1, confermato in R2]

Le proposte sono JSONL, una riga per proposta, parsate con `json.loads`
per riga. Il modello riceve un elenco di memorie esistenti (id + titolo,
max 50) e propone relazioni tra quegli id. Se propone un id inesistente o
un predicato fuori vocabolario, la proposta viene scartata al parse (non
scritta). Il parser esistente (schema.py) gestisce il frontmatter delle
memorie; la coda è un file separato in state/ e non tocca lo schema.

## D5 — Criterio di accettazione misurabile

[Assorbe: GPT-B5 da round 1, confermato in R2]

Prima di dichiarare G2 chiuso, si misurano separatamente su uno store di
test con fixture preregistrate:
1. **copertura**: quante memorie hanno almeno una relazione (manual o
   promossa) dopo una sessione distill con G2 attivo.
2. **precision proposte**: su N proposte generate, quante hanno evidence
   reale e target valido.
3. **path**: le 3 domande causali che fallivano devono rispondere FOUND
   con --include-inferred (e NOT_FOUND_EXHAUSTIVE senza, se nessun arco
   manual le collega).
4. **promozione**: `graph doctor promote` sposta una proposta pending in
   store con prov=manual, e il path la attraversa di default.

=============================================================================

## EMENDAMENTO 2026-08-19 — post round 3 red-team (APPROVATO DAL TITOLARE)

Kimi R3 (t_8ac61f91): APPROVATO CON MODIFICHE, 3 bloccanti (B1-B3).
GPT R3 (t_a61ae776): APPROVATO CON MODIFICHE, 3 bloccanti minimi.
Prima convergenza dei due revisori in tre round; entrambi: "non serve un
REV-3 ampio". Tutti i claim sottostanti verificati contro il codice reale
(relations.py:152-154, relations.py:212-217, federation.py:233;
conflicts.py non ha lock). L'emendamento E1-E6 chiude i 6 bloccanti.

### E1 — Dedup su tutti gli status + suppression persistente
[Kimi-B1; GPT backlog dedup]
La dedup NON cerca solo pending: prima di scrivere si verifica, nell'ordine:
1. esiste già in STORE un arco (subject_id, predicate, target.id) con
   qualsiasi prov? → la proposta non si scrive (già decisa dal mondo reale).
2. esiste una proposta con la stessa chiave in status pending, promoted O
   rejected? → non si scrive.
Un reject è suppression persistente: la stessa tripla non viene ri-proposta
in sessioni successive. Riapertura solo tramite azione esplicita umana
(`graph doctor reopen <proposal_id>`), mai automatica.

### E2 — Concorrenza: file_lock, non il pattern conflicts.py
[Kimi-B2]
REV-2 D2 citava conflicts.py come garanzia; verifica sul codice: conflicts.py
non ha lock. La coda proposals scrive sotto `federation.file_lock`
(federation.py:233 — lo stesso lock che relations.py usa già per arco),
dedup inclusa: la sequenza leggi-verifica-scrivi è interamente sotto lock.
Nessuna scrittura read-then-write fuori lock.

### E3 — Semantica nodi non-active: NOT_FOUND_EXHAUSTIVE, dichiarato
[Kimi-B3]
Nodo con status != active o is_expired interrogato come src/dst:
restituisce NOT_FOUND_EXHAUSTIVE con nota esplicativa ("nodo presente ma
non attraversabile: status=superseded"), NON MISSING (il nodo esiste) e NON
InvalidRelation (romperebbe il tri-stato). MISSING resta riservato a id che
non esistono in load_all.

### E4 — Overlay coda→graph_path, definito per status
[GPT bloccante 1]
Con `--include-inferred`, graph_path costruisce un overlay read-only dalle
sole proposte PENDING con predicato valido ed endpoint vivi; lo store resta
byte-identico. rejected, promoted e malformate non entrano mai nell'overlay.
`promote` materializza UNA relazione manual in store e marca la proposta
promoted, atomico e idempotente sotto file_lock: dopo il promote l'arco
esiste una volta sola (da store), mai doppio arco queue+store.
Criteri fail-closed preregistrati:
- pending valida → default NOT_FOUND_EXHAUSTIVE, include-inferred FOUND,
  store byte-identico;
- rejected/malformata → mai FOUND in nessun modo;
- promoted → FOUND via store una sola volta, overlay vuoto per quella chiave.

### E4-bis — Protocollo promote crash-safe
[GPT-R4 P0-1; APPROVATO DAL TITOLARE 2026-08-19]
file_lock esclude concorrenti VIVI ma non rende atomiche due scritture
persistenti distinte (arco in store + riga JSONL). E4 prometteva atomicità
che il lock da solo non fornisce. Correzione: promote è un protocollo
recoverable e idempotente, non una transazione.

Ordine delle scritture, sempre sotto lock di coda; lock memoria acquisito
dentro il lock coda (ordine globale coda → memoria, mai invertito):
1. lock coda (federation.file_lock su state/relation_proposals.lock);
2. lettura proposta: se status != pending → no-op idempotente (già decisa);
3. scrittura dell'arco manual in store, con `proposal_id` dentro la
   relazione (dedup-store di E1: se un arco con lo stesso proposal_id
   esiste già, lo step è no-op);
4. marcatura della riga JSONL: status=promoted, decided_at;
5. rilascio lock.

Recovery deterministica, eseguita a ogni retry di promote e da
`graph doctor`:
- arco con proposal_id presente + proposta ancora pending → completare lo
  step 4 (converge a promoted; idempotente);
- arco con proposal_id assente + proposta pending → ri-eseguire dal passo 3;
- proposta promoted senza arco → impossibile by construction con l'ordine
  3→4; se mai rilevata, report ERROR esplicito, nessuna correzione
  automatica.
L'overlay di E4 esclude una proposta pending il cui proposal_id risulta già
materializzato in store (equivalente a promoted ai fini dell'attraversamento).

Criterio di chiusura fail-closed (test, non prosa):
- fault injection dopo ciascuna delle due scritture (interruzione simulata
  tra step 3 e step 4);
- al retry converge sempre a: esattamente una relazione manual + una
  proposta promoted; nessuna pending attraversabile duplicata;
- due promote concorrenti + un relate concorrente sulla stessa tripla
  convergono allo stesso stato; lock non acquisito → nessuna mutazione.

### E5 — Migrazione fail-closed degli archi senza prov (legacy)
[GPT bloccante 2]
Verificato nel codice: relations.py scrive prov="inferred" solo con evidence
vuota; gli archi con evidence (inclusi quelli creati con `relate`) non hanno
campo prov. Policy:
- prov assente = `legacy`, valore distinto, NON mappato silenziosamente a
  manual (sarebbe falsa attestazione dell'attore originario);
- legacy NON è default-traversable;
- `graph doctor` conta gli archi legacy e `graph doctor promote-legacy` li
  marca manual uno per uno, solo su azione umana esplicita (attestazione
  consapevole, non automatica);
- ogni nuovo write path (CLI relate, MCP relate, distill) scrive prov
  esplicito; nessun arco nuovo può uscire senza prov.

### E6 — D5 diventa criterio binario preregistrato
[GPT bloccante 3]
Prima del codice si preregistrano nella fixture: archi attesi e archi
vietati (subject_id, predicato, target, direzione, evidence verificabile),
il massimo output accettabile, e la soglia binaria di precision
(precision = proposte corrette / proposte totali; soglia iniziale 0.8,
cambiabile solo prima del codice, mai dopo). Protocol tests con model stub
separati dalla misura semantica del modello. Il report emette numeratore,
denominatore e verdict binario PASS/FAIL — non solo la misura.

### A1 — Decisa: promote umano = arco manual è l'intento, documentato
[Kimi-A1; posizione qwen-pro approvata dal titolare]
Agente propone (prov=agent/inferred) → umano promuove → arco manual.
Questo NON è un buco: è l'umano che decide consapevolmente, esattamente il
principio dell'emendamento al REV-2. L'audit trail lo rende verificabile:
la proposta originaria resta in coda con il suo prov originale e status
promoted; l'arco materializzato porta prov=manual + riferimento a
proposal_id + decided_at. La storia di provenienza non viene riscritta:
due record distinti e auditabili (coerenza D1/D2, punto GPT).

### Domande aperte R3 — risposte nette (GPT R3, condivise)
1. Cap 10/sessione: adeguato per il primo collaudo; si misura, non si alza.
2. TTL pending: no; eventuale stato stale con archiviazione auditabile in
   backlog, mai cancellazione silenziosa.
3. Env persistente per --include-inferred: NO. Contraddirebbe la scelta
   esplicita per-query approvata dal titolare: trasformerebbe l'opt-in in
   default ambientale.

=============================================================================

## Cosa NON cambia dal REV-1 (confermato da entrambi i revisori)

- Identità per `Memory.id`, mai titolo (D2 REV-1)
- Cap confidence 0.5 per estratte e agent (D3 REV-1)
- No copia/spostamento archi su supersede/expire (D6 REV-1)
- Contenimento default: graph_path attraversa solo manual (D4 REV-1)
- Vocabolario predicati chiuso (D5 REV-1)

=============================================================================

## Domande aperte per round 3 (se necessarie)

1. Il cap 10/sessione è giusto o troppo conservativo? (Kimi I2)
2. Serve un TTL sulle proposte pending? (Kimi I3)
3. Il flag --include-inferred deve essere anche un env var persistente?
