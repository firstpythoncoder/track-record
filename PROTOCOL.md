# Protocol, version 1

This repository is a public, tamper-evident record of paper-traded investment books. Every session, each book's target
weights are committed **before the market opens**, in encrypted form, and time-stamped by third parties the publisher
does not control. The weights are revealed 63 sessions later. Anyone can then check that the revealed weights are the
ones that were committed, and recompute the book's performance from public market data.

The publisher can neither backdate a commitment nor rewrite or drop a day without breaking the chain of daily
manifests and their third-party time stamps.

## 1. Books

`books.json` lists each book with its id, a description, its market, its paper-trading venue and the first session of
its record. A book stays in the record from that session on. If it is retired, a manifest records the retirement, and
its history stays here.

## 2. What is committed each session

For each book and each session `D` of its market, a **weights file** (canonical JSON: sorted keys, no spaces, ASCII,
one trailing newline) holds:

| field | meaning |
|---|---|
| `book`, `market`, `session` | the book and the session `D` the weights are for |
| `data_through` | the last session whose data the decision used (normally the previous session) |
| `weights` | `{instrument: weight}`. Each weight is a fraction of the book's net asset value (NAV) after the session's trades, rounded to 1e-6; negative means short. Omitted instruments are 0. |
| `hedge` (some books) | a futures hedge: `weight` (a fraction of NAV in a long-price position of the legs; negative = short price) and `legs` (`{contract: share}`, summing to 1) |
| `sub_books` (joint books) | per sub-book: `nav_share` and its own `weights`, each a fraction of the sub-book's NAV |
| `settings_hash`, `registered_params_sha256` | identify the frozen model settings; a change of settings shows up here |
| `salt` | 32 random bytes, so that two identical books on different days have different hashes |

Instrument ids: `B3.<ticker>` for B3-listed securities (funds, futures) and `US.<ticker>` for US-listed equities.

## 3. Sealing, the daily manifest and the time stamps

- **Sealing.** The weights file is encrypted with AES-256-CBC (PKCS#7 padding) under a key and IV that belong to one
  book and session. The result is `sealed/<book>/<year>/<D>.bin`.
- **Manifest.** A manifest `chain/<year>/<date>_<n>.json` (canonical JSON) commits to:
  - for each sealed book: `plain_sha256` (the weights file) and `sealed_sha256` (the encrypted file), the session and
    its deadline;
  - `prev`: the path and sha256 of the previous manifest, so the manifests form one chain;
  - `protocol_sha256` and `books_sha256`: this file and `books.json`;
  - `changed` and `removed`: every published data file (`results/`, `returns/`, `keys/`, `weights/`, `sealed/`,
    `statements/`) that changed since the previous manifest, with its sha256, and `tree_sha256`, a digest of all of
    them;
  - `missing`: sessions without a valid commitment (section 5).
- **Time stamps.** Each manifest is time-stamped by:
  - **RFC 3161 time-stamp authorities**: FreeTSA, DigiCert and Sectigo. Each token is
    `chain/<year>/<manifest>.<tsa>.tsr` and is signed within seconds.
  - **OpenTimestamps**: `chain/<year>/<manifest>.ots`. The manifest's hash is aggregated into a Bitcoin transaction,
    usually within a few hours. Once confirmed, the proof is completed with the block attestation.
- **Deadline.** A commitment for session `D` counts only if at least one valid RFC 3161 token on its manifest is dated
  **before the session's open**: B3 10:00 America/Sao_Paulo, NYSE 09:30 America/New_York.
- **Why both hashes.** AES-CBC does not commit to its key. The plaintext hash is what binds the reveal, so a reveal must
  match `plain_sha256`.

## 4. Reveal

When 63 further sessions of the book's market have passed after `D`, two files are published:
- `keys/<book>/<year>/<D>.json`: the key and IV;
- `weights/<book>/<year>/<D>.json`: the weights file itself.

To check a revealed day, without any code from this repository:

```
sha256sum weights/A2/2026/2026-11-18.json            # equals plain_sha256 in the manifest that committed it
openssl enc -d -aes-256-cbc -K <key> -iv <iv> -in sealed/A2/2026/2026-11-18.bin | sha256sum   # the same hash
openssl ts -verify -data chain/2026/2026-11-18_1.json -in chain/2026/2026-11-18_1.json.digicert.tsr \
        -CAfile /etc/ssl/certs/ca-certificates.crt   # the token's time is before the open
ots verify chain/2026/2026-11-18_1.json.ots          # or drop the .ots file on https://opentimestamps.org
```

The publisher may also hand a day's key privately to a counterparty (a sponsor's due diligence) before the public
reveal. That changes nothing in this record.

## 5. Missed sessions

If a session has no commitment time-stamped before its open (a system failure, a data delay, a holiday mismatch), the
next manifest lists it under `missing`, with the reason. Every session from a book's start appears either as a
commitment or as missing. For the public calculation, a missed session means **no trade**: the book keeps its previous
holdings, which drift with prices.

## 6. The public calculation (hypothetical, from public data)

The calculation uses only the committed weights and public data. It assumes:

1. **Rebalancing at the close.** At the official closing price of session `D`, the book trades to its committed weights
   `w_D`. On a missed session, its weights are the previous ones drifted by the session's returns.
2. **Daily return.** From the close of `D` to the close of the next session `D+1`:

   `r = Σ_i w_i · R_i  +  (1 − Σ_i w_i) · rf  +  Σ_j h_j · F_j  −  cost  −  borrow`

   - `R_i` is the instrument's total return from close to close, `(P_{D+1} · s + d) / P_D − 1`. `P` is the official
     close, `s` the split factor and `d` the cash distribution (dividend, fund income) per share whose ex date is
     `D+1`.
   - `1 − Σ_i w_i` is cash (negative = borrowed). It earns or pays `rf` for `D → D+1`:
     - BRL books: CDI compounded over the business days from `D` to `D+1`, normally one day's rate (Banco Central do
       Brasil, SGS series 12);
     - USD books: the federal funds effective rate of day `D` (FRED `DFF`), as `DFF/100/360` per calendar day.
   - A futures hedge has no cash weight. Its leg `j` has weight `h_j = hedge.weight · legs[j]`, and
     `F_j = PU_{j,D+1} / PU_{j,D} − 1 − rf`, from B3 settlement prices (PU). This is the B3 daily adjustment per unit
     of notional.
   - `cost` is charged once, at the rebalancing at the close of `D`: `k · Σ |w_i − w̃_i|`, where `w̃` are the drifted
     weights before the trade. `k` is:
     - 10 bp for B3 funds and equities;
     - 5 bp for US equities;
     - 0.5 bp of notional for DI1 legs.
   - `borrow` is charged per session on short positions: `b · Σ_i max(−w_i, 0) / 252`, with `b` = 4.0 % a year for B3
     securities and 1.0 % a year for US equities.
3. **Joint books.** A joint book's return is the `nav_share`-weighted sum of its sub-books' returns. Each sub-book is
   computed as above.
4. **Outputs.** Results are cumulative from each book's first session: `results/<book>/public_daily.csv` (gross and net
   of `cost` and `borrow`).

The per-instrument inputs used each day are published in `returns/<market>/<year>/<D>.csv` for the whole universe, not
only the holdings. For B3 they are the close, the split factor and the cash distribution, with their sources. Prices
come from B3's own files (COTAHIST and settlement prices); an instrument missing from them is priced from another
public source, named in its row. For US equities they are the daily total return. Each value can be checked against B3's COTAHIST files and settlement prices,
the funds' and companies' distribution notices, BCB, FRED or any public price source.

## 7. Paper results

Each book is also traded in a broker paper or demo account:
- the B3 books in a MetaTrader 5 demo account at XP, from BRL 1,000,000 each;
- the US book in an Interactive Brokers paper account, from USD 800,000.

`results/<book>/paper_daily.csv` gives:
- the paper NAV, marked at official closes, including fees and distributions;
- the share of orders filled;
- slippage against the session's VWAP and close.

Demo and paper accounts fill against the displayed quotes without queue position or market impact, so these results
measure execution plumbing, timing and spread, not capacity. Fills reveal positions, so they are sealed like the
weights and published after the same 63-session delay (`statements/`).

## 8. Revisions

- **What may change.** Results can be recomputed when public data is revised, such as a late distribution or a
  corrected settlement. Each new version of a results file is committed by the next manifest, so every version stays
  verifiable in this chain. `CHANGELOG.md` gives the reason for each revision.
- **What never changes.** Committed weights.
- **Changes to the protocol.** They are made only by a new version of this file. The manifests show from which date it
  applies, and results are kept under both versions.

## 9. What this record is not

- Paper and hypothetical results are not the results of real money: they ignore market impact and real borrow
  availability, and the publisher could have stopped trading a real book.
- Backtests published elsewhere are not verifiable here. This record starts at each book's first session.
- Nothing here is investment advice or an offer of any security or service.
