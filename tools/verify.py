#!/usr/bin/env python3
"""Verify this repository: the manifest chain, every time stamp, every reveal and, for revealed days, the public
calculation. Needs Python 3.10+ and openssl. Optional: `pip install opentimestamps` to check the Bitcoin
attestations against a public block explorer (otherwise use `ots verify` or https://opentimestamps.org).

    python3 tools/verify.py                 # everything
    python3 tools/verify.py --watchdog      # today's sessions only (the scheduled GitHub check)
    python3 tools/verify.py --no-network    # skip the block-explorer lookups
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CERTS = ROOT / "tools" / "certs"
DATA_DIRS = ("results", "returns", "keys", "weights", "sealed", "statements")
OPENS = {"B3": ("America/Sao_Paulo", dt.time(10, 0)), "NYSE": ("America/New_York", dt.time(9, 30))}

problems: list[str] = []
notes: list[str] = []


def problem(msg: str) -> None:
    problems.append(msg)
    print("PROBLEM", msg)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode()


def manifests() -> list[Path]:
    return sorted((ROOT / "chain").glob("*/*_*.json"), key=lambda p: (p.stem.split("_")[0], int(p.stem.split("_")[1])))


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def session_open(market: str, day: str) -> dt.datetime:
    tz, t = OPENS[market]
    return dt.datetime.combine(dt.date.fromisoformat(day), t, ZoneInfo(tz))


# ------------------------------------------------------------------ RFC 3161
def tsa_check(tsr: Path, data: Path) -> dt.datetime | None:
    """The token's time if its signature verifies and it signs this file, else None."""
    name = tsr.name.rsplit(".", 2)[-2]
    if name == "freetsa":
        ca = ["-CAfile", str(CERTS / "freetsa_cacert.pem"), "-untrusted", str(CERTS / "freetsa_tsa.crt")]
    else:
        ca = ["-CAfile", "/etc/ssl/certs/ca-certificates.crt"]
    v = subprocess.run(["openssl", "ts", "-verify", "-data", str(data), "-in", str(tsr), *ca], capture_output=True, text=True)
    if v.returncode or "Verification: OK" not in v.stdout:
        return None
    r = subprocess.run(["openssl", "ts", "-reply", "-in", str(tsr), "-text"], capture_output=True, text=True)
    m = re.search(r"Time stamp: (.+)", r.stdout)
    if not m:
        return None
    t = re.sub(r"\s+", " ", m.group(1).strip().replace(" GMT", ""))
    for fmt in ("%b %d %H:%M:%S.%f %Y", "%b %d %H:%M:%S %Y"):
        try:
            return dt.datetime.strptime(t, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return None


# ------------------------------------------------------------------ OpenTimestamps
def ots_check(manifest: Path, network: bool) -> str:
    proof = Path(str(manifest) + ".ots")
    if not proof.exists():
        problem(f"{rel(manifest)}: no OpenTimestamps proof")
        return "none"
    try:
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
        from opentimestamps.core.serialize import StreamDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile
    except ImportError:
        return "not checked (pip install opentimestamps)"
    with open(proof, "rb") as f:
        dtf = DetachedTimestampFile.deserialize(StreamDeserializationContext(f))
    if dtf.file_digest != hashlib.sha256(manifest.read_bytes()).digest():
        problem(f"{rel(manifest)}.ots: the proof is for another file")
        return "wrong file"
    blocks = [(msg, att.height) for msg, att in dtf.timestamp.all_attestations() if isinstance(att, BitcoinBlockHeaderAttestation)]
    if not blocks:
        return "pending (not yet in a Bitcoin block)"
    if not network:
        return f"bitcoin block {min(h for _, h in blocks)} (not checked: --no-network)"
    for msg, height in sorted(blocks, key=lambda x: x[1]):
        try:
            with urllib.request.urlopen(f"https://blockstream.info/api/block-height/{height}", timeout=30) as r:
                bh = r.read().decode().strip()
            with urllib.request.urlopen(f"https://blockstream.info/api/block/{bh}", timeout=30) as r:
                blk = json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            return f"bitcoin block {height} (explorer unavailable: {e})"
        if blk["merkle_root"] == msg[::-1].hex():
            t = dt.datetime.fromtimestamp(blk["timestamp"], dt.timezone.utc)
            return f"bitcoin block {height} at {t.isoformat()}"
        problem(f"{rel(manifest)}.ots: the merkle root does not match block {height}")
    return "mismatch"


# ------------------------------------------------------------------ chain
def check_chain(network: bool, verbose: bool) -> dict:
    prev, tree, out = None, {}, {}
    ms = manifests()
    for p in ms:
        raw = p.read_bytes()
        m = json.loads(raw)
        if raw != canonical(m):
            problem(f"{rel(p)}: not canonical JSON")
        want = {"path": rel(prev), "sha256": sha256(prev.read_bytes())} if prev else None
        if m.get("prev") != want:
            problem(f"{rel(p)}: broken link to the previous manifest")
        tree.update(m.get("changed", {}))
        for k in m.get("removed", []):
            tree.pop(k, None)
        if sha256(canonical(tree)) != m.get("tree_sha256"):
            problem(f"{rel(p)}: published-tree digest mismatch")
        times = [t for t in (tsa_check(x, p) for x in sorted(p.parent.glob(p.name + ".*.tsr"))) if t]
        first = min(times) if times else None
        if first is None:
            problem(f"{rel(p)}: no valid RFC 3161 time stamp")
        ots = ots_check(p, network)
        for k, c in m.get("commitments", {}).items():
            f = ROOT / c["sealed"]
            if not f.exists() or sha256(f.read_bytes()) != c["sealed_sha256"]:
                problem(f"{k}: sealed file missing or altered")
            if c.get("deadline_utc"):
                deadline = dt.datetime.fromisoformat(c["deadline_utc"])
                valid = first is not None and first < deadline
                if not valid:
                    problem(f"{k}: time-stamped {first} at or after the open {deadline}: does not count")
            else:
                valid = first is not None                      # a statement (fills): sealed after the session, no deadline
            out[(c["book"], c["session"])] = dict(c, manifest=rel(p), stamped=first, valid=valid)
        if verbose:
            print(f"{rel(p)}: tsa {first.isoformat() if first else None}; ots {ots}; "
                  f"{len(m.get('commitments', {}))} commitments, {len(m.get('missing', {}))} missing")
        prev = p
    # the files as they are now against the last manifest
    now = {}
    for d in DATA_DIRS:
        for f in sorted((ROOT / d).rglob("*")) if (ROOT / d).exists() else []:
            if f.is_file():
                now[rel(f)] = sha256(f.read_bytes())
    for k, v in now.items():
        if tree.get(k) != v:
            notes.append(f"{k}: newer than the last manifest (it will be committed by the next one)")
    for k in tree:
        if k not in now:
            problem(f"{k}: committed by a manifest, now missing")
    print(f"chain: {len(ms)} manifests, {len(out)} commitments")
    return out


def check_reveals(commits: dict) -> int:
    n = 0
    for kp in sorted((ROOT / "keys").rglob("*.json")) if (ROOT / "keys").exists() else []:
        k = json.loads(kp.read_text())
        c = commits.get((k["book"], k["session"]))
        if not c:
            problem(f"{rel(kp)}: a key for a session that was never committed")
            continue
        r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-K", k["key"], "-iv", k["iv"], "-in", str(ROOT / c["sealed"])],
                           capture_output=True)
        if r.returncode or sha256(r.stdout) != c["plain_sha256"]:
            problem(f"{rel(kp)}: does not open its sealed file to the committed weights")
        if c.get("kind") == "fills":
            wp = ROOT / "statements" / k["book"].split(".")[0] / k["session"][:4] / f"{k['session']}-fills.csv"
        else:
            wp = ROOT / "weights" / k["book"] / k["session"][:4] / f"{k['session']}.json"
        if wp.exists() and sha256(wp.read_bytes()) != c["plain_sha256"]:
            problem(f"{rel(wp)}: differs from the committed weights")
        n += 1
    print(f"reveals: {n} checked")
    return n


def check_complete(commits: dict) -> None:
    """Every session of every book since its start is a commitment or a missing entry."""
    books = json.loads((ROOT / "books.json").read_text()) if (ROOT / "books.json").exists() else {}
    cal = json.loads((ROOT / "calendar.json").read_text()) if (ROOT / "calendar.json").exists() else {}
    gone = set()
    for p in manifests():
        for c in json.loads(p.read_text()).get("missing", {}).values():
            gone.add((c["book"], c["session"]))
    ms = manifests()
    if not ms:
        return
    last = dt.datetime.fromisoformat(json.loads(ms[-1].read_text())["created_utc"])
    for b, info in books.items():
        sessions = cal.get(info["market"], {}).get("sessions", [])
        for s in sessions:
            if s < info["start"] or session_open(info["market"], s) > last:
                continue
            if (b, s) not in commits and (b, s) not in gone:
                problem(f"{b} {s}: neither committed nor recorded as missing")


def watchdog(commits: dict, now: dt.datetime) -> None:
    """Today's sessions: each book must have a valid commitment, or a missing entry, 10 minutes after its open."""
    books = json.loads((ROOT / "books.json").read_text()) if (ROOT / "books.json").exists() else {}
    cal = json.loads((ROOT / "calendar.json").read_text())
    gone = {}
    for p in manifests():
        for c in json.loads(p.read_text()).get("missing", {}).values():
            gone[(c["book"], c["session"])] = c["reason"]
    for b, info in books.items():
        m = info["market"]
        today = now.astimezone(ZoneInfo(OPENS[m][0])).date().isoformat()
        if today not in cal.get(m, {}).get("sessions", []) or now < session_open(m, today) + dt.timedelta(minutes=10):
            continue
        c = commits.get((b, today))
        if c and c["valid"]:
            print(f"watchdog: {b} {today} committed, time-stamped {c['stamped'].isoformat()}")
        elif (b, today) in gone:
            problem(f"watchdog: {b} {today} recorded missing: {gone[(b, today)]}")
        else:
            problem(f"watchdog: {b} {today} has no valid commitment")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchdog", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    commits = check_chain(network=not a.no_network and not a.watchdog, verbose=not a.quiet)
    if a.watchdog:
        watchdog(commits, dt.datetime.now(dt.timezone.utc))
    else:
        check_reveals(commits)
        check_complete(commits)
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import public_calc
            for msg in public_calc.verify(ROOT):
                problem(msg)
        except ImportError:
            pass
    for n in notes[:20]:
        print("note", n)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
