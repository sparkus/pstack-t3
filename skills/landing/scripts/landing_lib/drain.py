import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from landing_lib import seam

def land(store):
    handle = open(store.dir / ".queue.lock", "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return "queue busy: another run is landing and will take queued entries. Run land again later if entries stay queued."
    out = seam.Outcome([], [], [], [], [])
    try:
        if store.contract.get("paused"):
            return f"queue paused: {store.contract['paused']}"
        if reason := seam.identity_change(store):
            seam.pause(store, reason)
            return f"queue paused: {reason}"
        seam.reconcile(store)
        integration = seam.Integration(store)
        mode = store.contract["mode"]
        if mode in ("human", "merge"):
            if seam.rewound(store, seam.fetch_trunk(store)):
                return report(store, out)
            if mode == "merge":
                seam.walk(store, out)
                seam.walk(store, out, seam.build_line(store, integration, out))
            else:
                for entry in store.entries("awaiting-merge"):
                    seam.poll(store, entry, out)
                for entry in seam.unheld_queued(store):
                    trunk = seam.fetch_trunk(store)
                    seam.build(store, integration, entry, trunk, trunk, out)
            return report(store, out)
        single, rounds = False, 0
        while (queued := seam.unheld_queued(store)) and not store.contract.get("paused"):
            rounds += 1
            if rounds > 4 * len(queued) + 4:
                break
            size = 1 if single else max(1, int(store.contract.get("batch") or 1))
            done, bounced, requeued, _ = seam.attempt(store, integration, queued[:size])
            out.landed.extend(done)
            out.bounced.extend(bounced)
            single = bool(requeued)
        return report(store, out)
    except seam.Infrastructure as problem:
        with store.tx() as db:
            for entry in db.execute("SELECT id FROM entry WHERE state = 'landing'").fetchall():
                store.set_entry(db, entry["id"], "queued", candidate="", note=str(problem))
        if isinstance(problem, (seam.AutoMergeStillEnabled, seam.UncheckedTrunk, seam.UnverifiedMerge, seam.MergeNotOnTrunk, seam.HeadChanged)):
            seam.pause(store, str(problem))
        else:
            suggestion = seam.suggested_merge_method(store.repo, str(problem))
            if suggestion:
                seam.pause(store, f"{problem}. Run land.py mode merge --merge-method {suggestion}, then land.py resume")
            else:
                seam.pause(store, f"{problem}. Fix it, then run land.py resume")
        return report(store, out if store.contract["mode"] == "merge" else seam.Outcome([], [], [], [], []))
    finally:
        handle.close()


def report(store, out):
    rows = {row["id"]: row for row in store.db.execute("SELECT * FROM entry")}
    landed, bounced, adopted = out.landed, out.bounced, out.adopted
    opened, rebuilt = ([i for i in ids if rows[i]["state"] == "awaiting-merge"] for ids in (out.opened, out.rebuilt))
    lines = []
    if landed:
        parts = []
        for i in landed:
            text = f"{seam.entry_label(i)} ({rows[i]['holder']})"
            if rows[i]["note"].startswith("left local "):
                text += f": {rows[i]['note']}"
            parts.append(text)
        lines.append("landed " + ", ".join(parts))
    if opened:
        lead = "opened PRs that merge when their checks pass: " if store.contract["mode"] == "merge" else "opened PRs for "
        lines.append(lead + ", ".join(f"{seam.entry_label(i)} ({rows[i]['holder']}) {rows[i]['pr']}" for i in opened))
    lines += [f"adopted {seam.entry_label(i)} ({rows[i]['holder']}) {rows[i]['pr']}" for i in adopted]
    lines += [f"bounced {seam.entry_label(i)} ({rows[i]['holder']}): {rows[i]['note']}" for i in bounced]
    lines += [f"rebuilt {seam.entry_label(i)} ({rows[i]['holder']}) {rows[i]['pr']}" for i in rebuilt]
    held = seam.held_holders(store.db)
    waiting = []
    for row in rows.values():
        hold = held.get(row["holder"]) if row["state"] == "queued" else None
        if row["state"] in ("queued", "landing"):
            waiting.append(seam.entry_label(row["id"]) + (f" (held by C{hold})" if hold else ""))
    if waiting:
        lines.append("still queued: " + ", ".join(waiting))
    if store.contract.get("paused"):
        lines.append(f"queue paused: {store.contract['paused']}")
    return "\n".join(lines) or "nothing to land"


def holder_rows(store, holder, sha=None):
    """The rows of one holder, or of every holder under a prefix that ends in /, optionally only those at one commit."""
    if sha:
        # submit stores the full SHA rev-parse gives, so a short or uppercase SHA compares the same way.
        resolved = seam.git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", cwd=store.repo, check=False)
        sha = resolved.stdout.strip() if resolved.returncode == 0 else sha.lower()
    return [
        row for row in store.db.execute("SELECT * FROM entry ORDER BY id")
        if (row["holder"] == holder or (holder.endswith("/") and row["holder"].startswith(holder)))
        and (not sha or row["sha"] == sha)
    ]


def holder_status(store, holder, sha=None):
    """Entries of one holder, or of every holder under a prefix that ends in /, optionally only those at one commit."""
    lines = []
    for row in holder_rows(store, holder, sha):
        line = f"{seam.entry_label(row['id'])} {row['state']} ({row['holder']}, {row['sha'][:12]})"
        if row["state"] == "landed" and row["landed"]:
            line += f" as {row['landed'][:12]}"
        elif row["state"] == "awaiting-merge" and row["pr"]:
            line += f" {row['pr']}"
        elif row["state"] == "bounced":
            line += f": {row['note']}"
        lines.append(line)
    if sha and not lines:
        resolved = seam.git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", cwd=store.repo, check=False)
        sha = resolved.stdout.strip() if resolved.returncode == 0 else sha.lower()
    return "\n".join(lines) or f"no entries held by {holder}" + (f" at {sha}" if sha else "")


def holder_json(store, holder, sha=None):
    """One JSON object per entry of one holder, each holding its id, state, holder, sha, and note."""
    rows = []
    for row in holder_rows(store, holder, sha):
        item = {"id": seam.entry_label(row["id"]), "state": row["state"], "holder": row["holder"],
                "sha": row["sha"], "note": row["note"]}
        advisory = seam.advisory_results(store, row)
        if advisory:
            item["advisory"] = [{"name": name, "result": result} for name, result in advisory]
        rows.append(item)
    return json.dumps(rows, indent=2)


def entry_advisory_note(store, row):
    """The advisory check results last read for this entry, or empty."""
    advisory = seam.advisory_results(store, row)
    if not advisory:
        return ""
    return "advisory checks, never a blocker: " + "; ".join(f"{name} {result or 'unknown'}" for name, result in advisory)


def status(store, ident=None):
    if ident:
        number = seam.entry_id(ident)
        row = store.db.execute("SELECT * FROM entry WHERE id = ?", (number,)).fetchone()
        if not row:
            raise seam.LandError(f"no {seam.entry_label(number)}")
        detail = [f"landed as {row['landed'][:12]}" if row["landed"] else "", row["pr"], row["note"], entry_advisory_note(store, row)]
        return f"{seam.entry_label(row['id'])} {row['state']} ({row['holder']}, {row['branch']})" + "".join(f". {part}" for part in detail if part)
    states = dict(store.db.execute("SELECT state, count(*) FROM entry GROUP BY state").fetchall())
    live = seam.in_flight(store.db)
    leases = len(live)
    reserved = sum(seam.counts(row, live) for row in seam.standing(store.db))
    contract = store.contract
    parts = [f"{state}: {n}" for state, n in states.items()] + ([f"leases held: {leases}"] if leases else [])
    if contract.get("cap"):
        parts.append(f"changes in flight: {leases + reserved} of {contract['cap']}")
    paused = f" Paused: {contract['paused']}" if contract.get("paused") else ""
    return f"{contract['mode']} mode onto {seam.trunk_ref(contract)}. " + (", ".join(parts) or "empty") + "." + paused


def no_checks_note(contract):
    """A warning that push or local mode has no checks, so nothing gates a landing. Empty otherwise."""
    mode = contract.get("mode")
    if mode not in ("push", "local") or contract.get("checks"):
        return ""
    return f"Warning: {mode} mode has no checks, so nothing gates a landing. Add one with land.py init again with --check <command>."
