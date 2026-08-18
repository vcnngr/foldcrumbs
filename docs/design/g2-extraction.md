# Design — G2: relation extraction by model (serie 0.8.0)

Status: DRAFT — in red-team. Nessuna riga di codice finché non approvato.

## Problema

G0+G1 sono meccanicamente corretti (percorso 3-hop, round-trip, locking
verificati sul campo) ma il grafo resta vuoto perché le relazioni nascono
solo da `foldcrumbs relate` — un atto manuale che nessuno ha il tempo o il
momento di fare. Il test di oggi non può chiudersi perché il valore del
grafo non può emergere da un grafo vuoto.

**Diagnosi:** il collo di bottiglia non è il codice, è il canale di
popolamento. La soluzione è far generare le relazioni dal modello durante
la distillazione, senza alcun atto manuale.

## Principio guida

Il modello **propone** relazioni; lo store le registra con fiducia ridotta
e provenienza tracciabile. Mai scrittura silenziosa di relazioni ad alta
confidence. Mai merge automatico di entità. Le relazioni manuali restano
il canale di massima fiducia.

## Architettura

### Canale 1: distill propone relazioni

Il prompt di distill (EXTRACTION_HEADER/FOOTER) chiede al modello anche
le relazioni tra le memorie che sta estraendo e verso memorie esistenti
trovate via recall. Formato:

```json
{
  "relations": [
    {
      "subject": "titolo della memoria sorgente",
      "predicate": "caused_by",
      "object": {"kind": "entity", "name": "release delay"},
      "evidence": "supplier problem caused the release delay",
      "confidence": 0.74
    }
  ]
}
```

Regole ferree:
- predicate dal vocabolario chiuso G1 (8 predicati)
- evidence obbligatoria (citazione dal transcript)
- confidence proposta dal modello, ma lo store la **cappa** a
  `min(confidence, G2_MAX_INFERRED=0.5)` — una relazione proposta dal
  modello non può mai avere la fiducia di una manuale
- `prov: inferred` sempre impostato
- oggetto: o `{"kind": "memory", "id": ...}` verso memoria esistente,
  o `{"kind": "entity", "name": ...}` per entità esterna

### Canale 2: tool MCP `relate`

Gli agenti (Claude, Codex, OpenCode, Hermes) possono creare relazioni
mentre lavorano, nel momento in cui notano un collegamento. Aggiunto al
server MCP esistente:

```
relate(source_ref, predicate, target, evidence, confidence?)
```

- source/target risolti come in `graph_path` (id, titolo, filename-stem)
- stessa validazione del CLI `relate`
- stesso lock per memoria, fail-closed

### Canale 3: backfill via `graph doctor --suggest`

Passaggio opzionale che propone connessioni tra memorie esistenti simili
(per tag condivisi, supersede, titolo simile). Non scrive nulla di suo:
stampa le proposte che l'utente può approvare con `relate`. È un assist,
non un'automaticità.

## Schema frontmatter

Nessun nuovo campo. Le relazioni G2 usano `relations_json` esistente con
`prov: inferred`:

```yaml
relations_json: '[{"c":0.5,"e":"…","k":"m","o":"id…","p":"caused_by","prov":"inferred"}]'
```

## Cosa NON si fa

- Nessuna scrittura automatica di relazioni ad alta confidence
- Nessun merge automatico di entità (Moonshot/Moonshot AI/月之暗面 restano
  distinte finché non risolte manualmente)
- Nessun vettore/embedding per la risoluzione
- Nessun nuovo canale oltre i tre sopra
- Nessuna sovrascrittura di relazioni esistenti

## Criterio di accettazione misurabile

Le domande causali che oggi falliscono devono iniziare a rispondere dopo
che il modello ha processato le sessioni che contengono quei fatti:

1. "perché merge commit invece di squash?"
2. "come si collega il conflict della PR23 allo squash?"
3. "perché expires solo da utente?"

Se dopo G2 queste domande restano a vuoto su uno store dove i fatti
esistono, il design è sbagliato. Se i fatti non esistono, G2 non può
aiutare — quello è un problema di copertura, non di grafo.

## Dipendenze

- G0+G1 già in main (verificato)
- Nessun nuovo modulo oltre l'estensione di `distill.py` e `mcp_server.py`

## Punti aperti per il red-team

1. Il capping a 0.5 è troppo aggressivo? Una relazione inferita ma con
   evidence forte dovrebbe poter salire?
2. Il canale backfill rischia di rumoreggiare? Meglio tenerlo fuori da G2
   e farlo come lavoro separato?
3. Il formato `object` con `kind: entity` per entità esterne — è utile o
   complica senza beneficio?
