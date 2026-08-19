# Design — G2: estrazione relazioni dal modello (serie 0.8.0)

Status: REV-2 — post round 2 red-team.
Kimi R2: APPROVATO CON MODIFICHE (3 bloccanti: N1-N3).
GPT R2: BOCCIATO (5 P0).
I due convergono su 4 problemi reali; questo documento li chiude tutti.

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
