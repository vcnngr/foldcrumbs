# Leggere la dashboard di foldcrumbs

La dashboard è una singola pagina HTML autocontenuta, generata al volo dallo
store del progetto da cui la lanci:

```bash
cd il-tuo-progetto
foldcrumbs dashboard          # si apre nel browser
```

Niente server, niente cache: ogni numero è calcolato nel momento in cui lanci
il comando, dalle stesse funzioni che usa la CLI. Ogni nome che porta un link
porta al file reale della memoria su disco — se un numero ti sorprende,
clicca e leggi la fonte.

Questa guida percorre la pagina dall'alto verso il basso.

---

## 1. L'hero — questo store è vivo, e come?

La fascia in cima risponde alla prima domanda con un'occhiata.

**Il battito.** Il cerchio luminoso respira. Il suo ritmo non è decorazione:
deriva dall'attività di recall reale dello store — uno store dormiente batte
ogni 6 secondi, e ogni recall registrato lo accelera, fino a 1,6 secondi. Se
lo store sembra affannato, è perché lo *è*. Il ritmo esatto è stampato nella
statistica `pulse`, e il numero da cui proviene è lo stesso totale `recalls`
mostrato lì accanto.

**Le statistiche.**

| stat | cosa dice |
|--|--|
| `memories` | ogni file nello store, qualunque sia il suo stato |
| `active` | quelle che rispondono davvero al recall e compaiono nell'indice |
| `recalls` | quante volte le memorie sono state richiamate nelle sessioni |
| `federated roots` | quante istanze agent condividono la vista di memoria di questo progetto |
| `pulse` | il ritmo del battito, in secondi (vedi sopra) |

**Il badge di salute** (angolo in alto a destra) è un verdetto calcolato da
tre condizioni reali:

| badge | significato | cosa fare |
|--|--|--|
| `current` | niente in decadimento, niente scadenze passate, nessun conflitto aperto | nulla — lo store è in ordine |
| `needs a sweep` | il decay ha candidati, o una scadenza è passata | lancia `foldcrumbs decay` (prima dry-run, poi `--apply`) |
| `attention` | la coda di riconciliazione non è vuota | lancia `foldcrumbs conflicts` e risolvi le coppie che elenca |

La directory dello store è stampata sotto le statistiche, così sai sempre
*di chi* stai guardando la memoria.

---

## 2. I pannelli, uno per uno

La griglia sotto l'hero è volutamente asimmetrica: i pannelli che di solito
chiedono lettura ricevono più spazio. Ogni pannello ha un accento colorato e
un badge — **verde** significa niente da fare, **ambra** significa che
qualcosa aspetta una passata, **rosso** significa che qualcosa aspetta *te*.

### Recall — reinforcement

Quali memorie continuano a servire, e quali non sono mai servite.

- La tabella elenca le memorie più richiamate con il loro conteggio — sono
  i fatti portanti del progetto.
- La riga sopra dice quante memorie non sono *mai* state richiamate. Un po'
  è normale (recenti, o davvero rare); un numero alto su uno store vecchio
  suggerisce memorie che non si sono mai guadagnate il posto — candidate a
  `decay` o `forget` dopo un'occhiata.

### Federation — parallel roots

Ogni istanza agent registrata su questa macchina, per questo progetto.

- Un **punto verde** segna l'istanza da cui stai guardando; i punti grigi
  sono le altre istanze la cui memoria è visibile in sola lettura.
- `entries` è quante memorie quell'istanza pubblica al momento; l'età dello
  shard dice quanto è fresca la pubblicazione. Uno shard molto vecchio
  significa che quell'istanza non fa una sessione da un po' (niente è rotto —
  le ultime voci pubblicate restano visibili, segnalate).

### Trust

La distribuzione di fiducia delle memorie attive, come istogramma, più la
media per tipo di memoria.

- La massa dovrebbe stare nelle fasce alte. Una fascia bassa gonfia è dove
  il `decay` va a cercare.
- Le medie per tipo mostrano su quali generi di memoria sei sicuro
  (decisioni, regole) e quali arrivano invece inferite e provvisorie.

### Decay

Cosa archivierebbe la passata *adesso* — lo stesso predicato di
`foldcrumbs decay` (dry-run).

- `expired` accanto a un nome significa che è scaduto per data; un numero
  significa che è decaduto per fiducia (sotto 0.3 e intoccato da 30 giorni).
- Archiviare non cancella mai: i file restano su disco, `restore <file>` ne
  riporta uno indietro, e `prune --apply` è l'atto separato ed esplicito che
  rimuove i file.

### Anti-rot

Le manopole di gestione del contesto per questa macchina.

- `context budget` e `checkpoint at` — dove foldcrumbs fa checkpoint e ti
  suggerisce `/compact` o `/clear`.
- `handoff age` — quanto è vecchio lo snapshot dello stato di lavoro; uno
  fresco significa che esiste un punto di ripresa sicuro per il `/clear`.
- `semantic channel` — se il recall semantico opzionale è attivo, e quanti
  vettori sono in cache locale.

### Superseded

Memorie sostituite, tenute su disco per audit. Ogni riga è una catena:
`vecchia → nuova`. Quando sei sicuro che la storia non serve più,
`foldcrumbs prune --apply` le rimuove.

### Expiry *(appare quando qualcosa ha una data)*

Memorie con un `expires_at`. `lapsed` conta quelle oltre la data (già
invisibili al recall); `next to expire` dice cosa sta arrivando. Se una
memoria scaduta vale ancora, modifica il file: sposta la data in avanti, o
rimuovi la riga.

### Conflicts *(appare quando la coda non è vuota)*

La coda di riconciliazione in numeri: coppie ambigue che l'LLM non ha saputo
giudicare, rivendicazioni di questo store su memorie di altre istanze,
rivendicazioni di altre istanze sulle nostre. Il pannello punta a
`foldcrumbs conflicts`, che elenca ogni voce con il comando esatto per
risolverla. L'ambiguità vive qui finché non decidi — non viene mai indovinata.

### Latest memories

Le memorie attive più recenti, dalla più nuova — la faccia sfogliabile dello
store. Ogni riga porta al file; data e tipo sono a destra.

---

## 3. Rigenerare e opzioni

```bash
foldcrumbs dashboard                 # genera + apre
foldcrumbs dashboard --no-open       # stampa solo il percorso del file
foldcrumbs dashboard --out ~/d.html  # scrive su un percorso a tua scelta
foldcrumbs dashboard --json          # i dati sottostanti, non la pagina
```

Rilanciala quando vuoi: la pagina riflette sempre lo store com'è *adesso*.
