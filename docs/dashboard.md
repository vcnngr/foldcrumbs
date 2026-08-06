# Reading the foldcrumbs dashboard

The dashboard is one self-contained HTML page, generated on the fly from the
store of the project you run it from:

```bash
cd your-project
foldcrumbs dashboard          # opens in your browser
```

No server, no cache: every number is computed at the moment you run the
command, by the same functions the CLI uses. Every name that links somewhere
links to the actual memory file on disk — if a number surprises you, click
through and read the source.

This guide walks the page top to bottom.

---

## 1. The hero — is this store alive, and how?

The band at the top answers the first question at a glance.

**The pulse.** The glowing circle breathes. Its tempo is not decoration: it
is derived from the store's real recall activity — a dormant store beats
every 6 seconds, and each recorded recall quickens it, down to 1.6 seconds.
If the store feels busy, it is because it *is*. The exact tempo is printed
as the `pulse` statistic, and the number it comes from is the same `recalls`
total shown right next to it.

**The statistics.**

| stat | what it says |
|--|--|
| `memories` | every file in the store, whatever its status |
| `active` | the ones that actually answer recall and appear in the index |
| `recalls` | how many times memories were pulled into sessions |
| `federated roots` | how many agent instances share this project's memory view |
| `pulse` | the heartbeat tempo, seconds (see above) |

**The health badge** (top-right corner) is a verdict computed from three
real conditions:

| badge | meaning | what to do |
|--|--|--|
| `current` | nothing decaying, nothing lapsed, no open conflicts | nothing — the store is tidy |
| `needs a sweep` | decay has candidates, or an expiry has lapsed | run `foldcrumbs decay` (dry-run first, then `--apply`) |
| `attention` | the reconciliation queue is not empty | run `foldcrumbs conflicts` and resolve the pairs it lists |

The store directory is printed under the stats, so you always know *whose*
memory you are looking at.

---

## 2. The panels, one by one

The grid below the hero is deliberately asymmetric: panels that usually need
reading get more room. Each panel carries a coloured accent and a badge —
**green** means nothing to do, **amber** means something is waiting for a
sweep, **red** means something is waiting for *you*.

### Recall — reinforcement

Which memories keep being needed, and which have never been needed at all.

- The table lists the most-recalled memories with their count — these are
  the load-bearing facts of the project.
- The line above says how many memories were *never* recalled. Some of that
  is normal (recent, or genuinely rare); a large number on an old store
  suggests memories that never earned their keep — candidates for `decay`
  or `forget` after a look.

### Federation — parallel roots

Every agent instance registered on this machine, for this project.

- A **green dot** marks the instance you are looking from; grey dots are the
  other instances whose memory you can see read-only.
- `entries` is how many memories that instance currently publishes; the
  shard age tells you how fresh the publication is. A very old shard means
  that instance hasn't run a session in a while (nothing is broken — its
  last published entries stay visible, flagged).

### Trust

The confidence distribution of active memories, as a histogram, plus the
average per memory type.

- Most of the mass should sit in the upper buckets. A fat low end is where
  `decay` goes looking.
- The per-type averages show which kinds of memory you keep sure about
  (decisions, rules) versus which arrive inferred and tentative.

### Decay

What the sweep would archive *right now* — the same predicate as
`foldcrumbs decay` (dry-run).

- `expired` beside a name means it lapsed on its date; a number means it
  lapsed on trust (below 0.3 and untouched for 30 days).
- Archiving never deletes: files stay on disk, `restore <file>` brings one
  back, and `prune --apply` is the separate explicit act that removes files.

### Anti-rot

The context-management dials for this machine.

- `context budget` and `checkpoint at` — where foldcrumbs checkpoints and
  nudges you toward `/compact` or `/clear`.
- `handoff age` — how old the live working-state snapshot is; a fresh one
  means a recent `/clear`-safe resume point exists.
- `semantic channel` — whether the optional embedding recall is on, and how
  many vectors are cached locally.

### Superseded

Memories that were replaced and are kept on disk for audit. Each row is a
chain: `old → new`. When you are sure you no longer need the history,
`foldcrumbs prune --apply` clears them.

### Expiry *(appears when something has a date on it)*

Memories with an `expires_at`. `lapsed` counts the ones past their date
(already invisible to recall); `next to expire` tells you what is coming.
If a lapsed memory is still true, edit the file: move the date forward, or
remove the line.

### Conflicts *(appears when the queue is not empty)*

The reconciliation queue in numbers: ambiguous pairs the LLM could not
adjudicate, claims this store made on other instances' memories, claims
other instances made on yours. The panel points at `foldcrumbs conflicts`,
which lists each item with the exact command to resolve it. Ambiguity lives
here until you decide — it is never guessed away.

### Latest memories

The newest active memories, newest first — the browsable face of the store.
Each row links to the file; the date and type are on the right.

---

## 3. Regenerating and options

```bash
foldcrumbs dashboard                 # generate + open
foldcrumbs dashboard --no-open       # print the file path only
foldcrumbs dashboard --out ~/d.html  # write to a path you choose
foldcrumbs dashboard --json          # the underlying data, not the page
```

Re-run it any time: the page always reflects the store as it is *now*.
