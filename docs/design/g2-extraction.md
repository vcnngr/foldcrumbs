# Design — G2: estrazione relazioni dal modello (serie 0.8.0)

Status: REV-1 — post doppio red-team (round 1).
Kimi: BOCCIATO nella forma attuale (4 bloccanti). GPT: APPROVATO CON
MODIFICHE (5 bloccanti). Questa revisione assorbe tutti i 9 bloccanti come
decisioni esplicite D1–D7. Nessuna riga di codice finché REV-1 non è
approvata.

## Diagnosi (confermata da entrambi i revisori — non in discussione)

G0+G1 sono meccanicamente corretti (percorso 3-hop, round-trip, locking) ma
il grafo resta vuoto: le relazioni oggi nascono solo da `foldcrumbs relate`
manuale, un atto consapevole che nessun utente CLI farà. Il collo di
bottiglia è il canale di popolamento, non il codice. La direzione — far
proporre relazioni al modello durante il distill — è quella giusta e
confermata da entrambi. Il round 1 è stato bocciato per COME, non per COSA.

## La tensione che G2 deve risolvere

Kimi ha messo a nudo il nodo reale: una coda di proposte che nessuno
approva non risolve il problema di popolamento — il grafo resta vuoto e si
torna al punto di partenza. Ma la scrittura diretta nello store (come
proposto nel DRAFT round-1) è esattamente ciò che il REV-2 vieta e ciò che
Kimi ha definito "bocciante da solo".

La risoluzione adottata in REV-1: le relazioni estratte entrano
AUTOMATICAMENTE ma come cittadini di serie B — confidence cappata,
provenienza marcata, ESCLUSE dal graph_path di default. Il contenimento
diventa reale (a livello di query, non di metadata) e il popolamento non
richiede approvazione per-arco. Il grafo si popola; il percorso resta
affidabile; la promozione è un gesto esplicito e raro, non la norma.

## Decisioni (assorbono i 9 bloccanti)

### D1 — Distill propone in una coda, non scrive nello store
[Assorbe: Kimi-B1, GPT-B1]

Il REV-2 stabilisce: distill → coda di proposte → scrittura. Il DRAFT
round-1 violava questo facendo scrivere a distill direttamente nello store.
REV-1 ripristina il contratto: distill scrive proposte nel file di coda
`relation_proposals.jsonl` (append-only, una proposta per riga). Nessun
tocco allo store delle memorie.

Promozione: `foldcrumbs graph doctor` mostra le proposte in sospeso;
`foldcrumbs doctor promote <id>` scrive la relazione nello store con
confidence e provenienza preservate. La promozione è l'UNICO percorso di
scrittura nello store dal canale distill.

### D2 — Identità per Memory.id, mai per titolo
[Assorbe: Kimi-B2, GPT-B2 parziale]

Il DRAFT identificava subject e object per titolo. Il REV-2 principio 7
stabilisce che l'identità è `Memory.id` — il titolo cambia col retitle e
lascia riferimenti penzolanti. REV-1: subject e object nella proposta sono
`Memory.id`. Il modello riceve un elenco di memorie esistenti con i loro id
e propone relazioni tra quegli id. Se il modello propone un id inesistente,
la proposta è scartata in coda (non entra mai nello store).

### D3 — Confidence cap e provenienza obbligatoria
[Assorbe: Kimi-B3, GPT-B4]

Ogni relazione scritta nello store porta:
- `confidence`: 0.0–1.0. Le relazioni estratte dal modello sono cappate a
  0.5 al momento della scrittura, indipendentemente da ciò che il modello
  restituisce. Le relazioni umane (CLI `relate`, MCP `relate`) non hanno
  cap.
- `prov`: `"inferred"` (dal modello) o `"manual"` (da umano).
  Obbligatorio; nessun default silenzioso.

Il cap 0.5 da solo NON è contenimento (vedi D4). È metadata per la
visibilità e per il doctor.

### D4 — Contenimento a livello di query: graph_path esclude inferred di default
[Assorbe: Kimi-B4 — il cap cosmetico]

Il punto più pesante del round 1: la BFS di `graph_path` attraversava ogni
arco forte senza guardare confidence né provenance. Un `caused_by`
allucinato a 0.5 produceva FOUND identico a uno manuale a 0.9.

REV-1: `graph_path` (CLI e MCP) attraversa di default SOLO archi con
`prov: manual`. Gli archi `prov: inferred` sono attraversati solo con
`--include-inferred` (CLI) o `include_inferred: true` (MCP). Il
contenimento è nel percorso, non nel metadata.

`graph doctor` mostra entrambi. `graph entities` mostra entrambi. La
differenza è solo nel path.

### D5 — Formato proposta eseguibile dal parser esistente
[Assorbe: GPT-B2]

Il DRAFT mostrava un envelope JSON che il parser frontmatter non accetta.
REV-1: le proposte in coda sono JSONL (una riga per proposta), non
frontmatter. Formato per riga:

```json
{"src_id": "...", "dst_id": "...", "predicate": "caused_by",
 "evidence": "...", "confidence": 0.5, "prov": "inferred",
 "session": "...", "ts": "..."}
```

Il prompt al modello fornisce: il summary della sessione corrente E un
elenco di memorie esistenti (id + titolo, max 50 per non esplodere il
contesto). Il modello propone relazioni tra quegli id. Se il modello
propone un id inesistente o un predicato fuori vocabolario, la proposta è
scartata alla lettura della coda (non entra mai nello store). La coda è
JSONL letta con `json.loads` per riga — nessun nuovo parser.

### D6 — Ciclo di vita degli archi: memorie superseded/expired escluse dal percorso
[Assorbe: GPT-B3]

Il DRAFT non specificava cosa succede alle relazioni quando una memoria
scade o viene superseded. REV-1 adotta la stessa logica di contenimento di
D4 — esclusione dal percorso, nessuna duplicazione dei dati:

- `expire` e `supersede`: le relazioni restano nel frontmatter della
  memoria (dati preservati, mai cancellati) ma la memoria è ESCLUSA dal
  `graph_path` di default. Gli archi che la toccano non vengono attraversati.
- Visibilità: `graph doctor` e `graph entities` le mostrano comunque;
  `graph_path --include-superseded` riattiva l'attraversamento per
  ispezione esplicita.
- Nessuna copia di relazioni sulla memoria successore: la duplicazione
  introdurrebbe archi potenzialmente scorretti (B supersede A ma il contesto
  è cambiato) e complicherebbe il tri-stato di D4. La catena causale si
  ricostruisce via supersede (l'arco G0 già derivato) + relazioni della
  memoria storica, ispezionabili on demand.

Questa scelta scarta il "cascade con copia" considerato nel round interno:
coerente con D4 (contenimento semplice a livello di query), nessun nuovo
valore di `prov` oltre manual/inferred, zero rischio di archi ereditati
sbagliati.

### D7 — Criteri di accettazione falsificabili e preregistrati
[Assorbe: GPT-B5]

Il DRAFT aveva un criterio ("le domande causali che oggi falliscono devono
iniziare a rispondere") che è inattaccabile a posteriori: ogni fallimento
si spiega con "i fatti non esistono". REV-1 preregistra:

**Fixture positive** (prima dell'implementazione): dato uno store con
memorie A, B, C e una sessione che le collega causalmente, il distill deve
proporre almeno una relazione A→B o B→C con `prov: inferred`.

**Fixture negative**: dato uno store con memorie non collegate e una
sessione che non le collega, il distill NON deve proporre relazioni.

**Misura separata** (post-implementazione, sul campo):
1. Copertura: quante memorie hanno almeno una relazione dopo N sessioni
2. Precision: quante proposte sono corrette (verifica umana su campione)
3. Path: quante domande causali rispondono con `--include-inferred`
4. Promozione: quante proposte vengono promosse a manual

Il criterio di accettazione è: fixture positive passano, fixture negative
passano, copertura > 0. La precision è misurata ma non è un gate (è un
dato, non un criterio binario).

## Canali di popolamento (REV-1)

| Canale | Chi scrive | Provenienza | Confidence |
|---|---|---|---|
| CLI `relate` | umano | manual | libera |
| MCP `relate` | agente | manual | libera |
| Distill → coda → doctor promote | modello → umano | inferred → manual | cap 0.5 → libera |
| Distill → coda (non promossa) | modello | inferred | cap 0.5 |

Il canale distill popola il grafo automaticamente. Il canale doctor
promote è l'unico modo per elevare una relazione inferred a manual. Non
esiste scrittura automatica diretta nello store dal canale modello.

## Fuori scope (come REV-2)

- Nessuna dipendenza nuova (no Neo4j, no vector DB, no Cypher)
- Nessun merge automatico di entità
- Nessuna sovrascrittura silenziosa di relazioni esistenti
- Nessuna automazione del decay
- Nessun modello specifico richiesto (model-agnostic)
- Nessuna scrittura diretta nello store dal canale distill (D1)

## Dipendenze da G0+G1

G2 usa:
- `Memory.id` (G1, schema.py) — identità degli archi
- `relations_json` nel frontmatter (G1) — storage delle relazioni
- `graph_path` BFS (G1, relations.py) — D4 modifica il filtro, non la BFS
- `distill()` (distill.py) — D5 aggiunge il prompt di estrazione relazioni

G2 NON modifica la BFS di `find_path`. Aggiunge un filtro di provenienza
prima della BFS.

## Ordine di implementazione

1. D5: formato proposta JSONL + parser coda (nessun modello, solo formato)
2. D1: distill scrive proposte in coda (prompt + scrittura)
3. D4: graph_path filtro provenienza (modifica CLI + MCP)
4. D3: confidence cap e prov nella scrittura doctor promote
5. D6: cascade supersede/expire
6. D7: fixture preregistrate (prima di ogni codice, come da gate 4)
7. Doctor promote UI (graph doctor mostra proposte)
