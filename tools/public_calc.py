"""The public calculation of PROTOCOL.md, section 6, from the committed weights and the published returns files.

The publisher runs exactly this code to write results/<book>/public_daily.csv, and tools/verify.py runs it again on
the revealed sessions and compares. Standard library only.

returns/<market>/<year>/<D>.csv holds, for each instrument, the total return from the previous session's close to the
close of D (`r`). The row `RF` is the cash rate over the same period. B3 rows also carry the prices and corporate
actions they come from: r = (close * factor + cash) / prev_close - 1.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

COST = {"B3": 10e-4, "NYSE": 5e-4}          # per unit of turnover, securities
COST_FUT = 0.5e-4                            # DI1 legs, per unit of notional turnover
BORROW = {"B3": 0.040, "NYSE": 0.010}        # a year, on short positions, per session / 252
COLUMNS = ["session", "committed", "r_gross", "cost", "borrow", "r_net", "nav_gross", "nav_net", "turnover", "long", "short", "rf"]


def fmt(x) -> str:
    return x if isinstance(x, str) else (str(x) if isinstance(x, int) else f"{x:.10g}")


def read_returns(root: Path, market: str, session: str) -> dict[str, float] | None:
    p = root / "returns" / market / session[:4] / f"{session}.csv"
    if not p.exists():
        return None
    with open(p, newline="") as f:
        return {row["instrument_id"]: float(row["r"]) for row in csv.DictReader(f) if row["r"] != ""}


def check_returns_file(p: Path) -> list[str]:
    """Rows with prices must satisfy r = (close * factor + cash) / prev_close - 1."""
    errs = []
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("prev_close") and row.get("close") and row["r"] != "":
                want = (float(row["close"]) * float(row.get("factor") or 1) + float(row.get("cash") or 0)) / float(row["prev_close"]) - 1
                if abs(want - float(row["r"])) > 1e-9:
                    errs.append(f"{p.name} {row['instrument_id']}: r {row['r']} != {want:.10g} from its prices")
    return errs


def vectors(payload: dict) -> list[tuple[str, float, dict, dict]]:
    """(sub-book, nav share, security weights, futures weights) for a weights file."""
    if "sub_books" in payload:
        return [(k, float(v["nav_share"]), dict(v["weights"]), {}) for k, v in sorted(payload["sub_books"].items())]
    fut = {}
    h = payload.get("hedge")
    if h:
        fut = {leg: float(h["weight"]) * float(s) for leg, s in h["legs"].items()}
    return [("", 1.0, dict(payload["weights"]), fut)]


class Sub:
    """One weight vector's path: holdings after the last close, NAVs."""

    def __init__(self, market: str):
        self.market = market
        self.w: dict[str, float] = {}
        self.h: dict[str, float] = {}
        self.nav_g = self.nav_n = 1.0

    def step(self, R: dict[str, float], rf: float, target: tuple[dict, dict] | None) -> dict:
        w, h = self.w, self.h
        r_sec = sum(x * R.get(i, 0.0) for i, x in w.items())
        r_fut = sum(x * (R.get(j, 0.0) - rf) for j, x in h.items())
        r_g = r_sec + (1.0 - sum(w.values())) * rf + r_fut
        borrow = BORROW[self.market] * sum(-x for x in w.values() if x < 0) / 252.0
        # drifted weights at the close, before any trade
        g = 1.0 + r_g - borrow
        wd = {i: x * (1.0 + R.get(i, 0.0)) / g for i, x in w.items()}
        hd = {j: x * (1.0 + R.get(j, 0.0)) / g for j, x in h.items()}
        cost = turnover = 0.0
        if target is not None:
            tw, th = target
            turnover = sum(abs(tw.get(i, 0.0) - wd.get(i, 0.0)) for i in set(tw) | set(wd))
            tf = sum(abs(th.get(j, 0.0) - hd.get(j, 0.0)) for j in set(th) | set(hd))
            cost = COST[self.market] * turnover + COST_FUT * tf
            self.w, self.h = dict(tw), dict(th)
        else:
            self.w, self.h = wd, hd
        r_n = r_g - borrow - cost
        self.nav_g *= 1.0 + r_g
        self.nav_n *= 1.0 + r_n
        return {"r_gross": r_g, "cost": cost, "borrow": borrow, "r_net": r_n, "nav_gross": self.nav_g, "nav_net": self.nav_n,
                "turnover": turnover, "long": sum(x for x in self.w.values() if x > 0), "short": sum(-x for x in self.w.values() if x < 0)}

    def start(self, target: tuple[dict, dict]) -> dict:
        """The first session: from cash to the committed weights at its close."""
        tw, th = target
        turnover = sum(abs(x) for x in tw.values())
        cost = COST[self.market] * turnover + COST_FUT * sum(abs(x) for x in th.values())
        self.w, self.h = dict(tw), dict(th)
        self.nav_n *= 1.0 - cost
        return {"r_gross": 0.0, "cost": cost, "borrow": 0.0, "r_net": -cost, "nav_gross": self.nav_g, "nav_net": self.nav_n,
                "turnover": turnover, "long": sum(x for x in tw.values() if x > 0), "short": sum(-x for x in tw.values() if x < 0)}


def compute(root: Path, market: str, sessions: list[str], weights: dict[str, dict | None]) -> list[dict]:
    """Rows from the book's first session (sessions[0], which must be committed) through the last session whose
    returns file exists. weights: session -> weights file (dict), or None for a missed session."""
    first = weights.get(sessions[0])
    if first is None:
        return []
    subs = {k: (Sub(market), share) for k, share, _, _ in vectors(first)}
    rows = []
    for n, s in enumerate(sessions):
        p = weights.get(s)
        targets = {k: (wv, fv) for k, _, wv, fv in vectors(p)} if p else None
        if n == 0:
            per = {k: sub.start(targets[k]) for k, (sub, _) in subs.items()}
            rf = 0.0
        else:
            R = read_returns(root, market, s)
            if R is None or "RF" not in R:
                break
            rf = R["RF"]
            per = {k: sub.step(R, rf, targets[k] if targets else None) for k, (sub, _) in subs.items()}
        row = {"session": s, "committed": int(p is not None), "rf": rf}
        for c in ("r_gross", "cost", "borrow", "r_net", "turnover", "long", "short"):
            row[c] = sum(share * per[k][c] for k, (_, share) in subs.items())
        if n == 0:
            row["nav_gross"], row["nav_net"] = 1.0, 1.0 - row["cost"]
        else:
            row["nav_gross"] = rows[-1]["nav_gross"] * (1.0 + row["r_gross"])
            row["nav_net"] = rows[-1]["nav_net"] * (1.0 + row["r_net"])
        if len(subs) > 1:
            for k, (_, share) in subs.items():
                for c in ("r_gross", "r_net", "long", "short"):
                    row[f"{k}_{c}"] = per[k][c]
        rows.append(row)
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = COLUMNS + sorted({c for r in rows for c in r} - set(COLUMNS))
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([fmt(r.get(c, "")) for c in cols])


def book_sessions(root: Path, book: str, info: dict) -> list[str]:
    cal = json.loads((root / "calendar.json").read_text())
    return [s for s in cal[info["market"]]["sessions"] if s >= info["start"]]


def verify(root: Path) -> list[str]:
    """Recompute every book over its revealed sessions and compare with results/<book>/public_daily.csv."""
    errs = []
    for p in sorted((root / "returns").rglob("*.csv")) if (root / "returns").exists() else []:
        errs += check_returns_file(p)
    books = json.loads((root / "books.json").read_text()) if (root / "books.json").exists() else {}
    for book, info in books.items():
        wdir = root / "weights" / book
        revealed = {p.stem: json.loads(p.read_text()) for p in sorted(wdir.rglob("*.json"))} if wdir.exists() else {}
        if not revealed:
            continue
        missing = set()
        for m in (root / "chain").glob("*/*_*.json"):
            for c in json.loads(m.read_text()).get("missing", {}).values():
                if c["book"] == book:
                    missing.add(c["session"])
        sessions = book_sessions(root, book, info)
        last = max(revealed)
        # the revealed prefix: every session up to `last` must be revealed or recorded missing
        upto = [s for s in sessions if s <= last]
        if any(s not in revealed and s not in missing for s in upto):
            errs.append(f"{book}: a session before {last} is neither revealed nor missing")
            continue
        rows = compute(root, info["market"], upto, {s: revealed.get(s) for s in upto})
        pub = {}
        rp = root / "results" / book / "public_daily.csv"
        if rp.exists():
            with open(rp, newline="") as f:
                pub = {r["session"]: r for r in csv.DictReader(f)}
        for r in rows:
            q = pub.get(r["session"])
            if q is None:
                errs.append(f"{book} {r['session']}: not in results/{book}/public_daily.csv")
                continue
            for c in ("r_gross", "r_net", "nav_net"):
                if abs(float(q[c]) - r[c]) > 1e-8:
                    errs.append(f"{book} {r['session']}: {c} published {q[c]}, recomputed {r[c]:.10g}")
    return errs
