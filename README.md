# Track record

A public, tamper-evident record of paper-traded investment books. The rules are in [`PROTOCOL.md`](PROTOCOL.md).

## How it works

- Every session, before the market opens, each book's target weights are encrypted and committed in a daily manifest.
- The manifest is time-stamped by three RFC 3161 time-stamp authorities and by OpenTimestamps on the Bitcoin blockchain.
- The weights are revealed 63 sessions later. Anyone can then check that they are the ones that were committed, and
  recompute the results from public market data.
- Performance is published every day: a hypothetical calculation from the committed weights and public prices, and the
  books' paper-trading results.

| folder | what |
|---|---|
| `books.json` | the books in the record and their first sessions |
| `chain/` | the daily manifests, each with its time-stamp tokens (`.tsr`) and OpenTimestamps proof (`.ots`) |
| `sealed/` | the encrypted weights, one file per book and session |
| `keys/`, `weights/` | day keys and plaintext weights, published 63 sessions after each session |
| `returns/` | the per-instrument inputs of the public calculation, every session |
| `results/` | each book's daily results: the public calculation and paper trading |
| `tools/verify.py` | checks everything above (Python 3 and openssl) |

## Verify

```
git clone <this repository> && cd <it>
pip install opentimestamps          # optional: checks the Bitcoin attestations against a block explorer
python3 tools/verify.py
```

`verify.py` checks:
- the chain: each manifest names the previous one by its sha256;
- every time-stamp signature, and that each commitment was stamped before its session opened;
- the OpenTimestamps proofs;
- every reveal: the key opens the sealed file to the committed hash;
- that every session of every book is either committed or recorded as missing;
- the public calculation on the revealed sessions.

`PROTOCOL.md`, section 4, shows the same checks with only `sha256sum`, `openssl` and `ots`.

A scheduled GitHub workflow runs the same checks every session after the open, and every evening
(`.github/workflows/verify.yml`; see the repository's Actions tab).

Nothing here is investment advice or an offer. Paper and hypothetical results are not results of real money.
