#!/usr/bin/env python3
"""Land many agents' work on one repository through one queue.

Each repository (one git common directory) gets a contract, a lease table
that keeps writers off each other's paths, and a queue that is the only
writer to trunk. The queue is a lock, not a process: whoever runs `land`
while holding it drains the queue. State lives in SQLite so every change is
one transaction. Heavy commands take a slot from a machine-wide governor.
"""

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

import sys as _sys
import types as _types
import uuid as _uuid
from pathlib import Path as _Path
_libdir = _Path(__file__).resolve().parent / "landing_lib"


def _drop_cached_landing(*names):
    """Drop every cached module under one of these names, so a load cannot reuse another load's copy."""
    for _n in [n for n in _sys.modules if any(n == name or n.startswith(name + ".") for name in names)]:
        del _sys.modules[_n]


_prior_landing = {n: m for n, m in _sys.modules.items() if n == "landing_lib" or n.startswith("landing_lib.")}
_drop_cached_landing("landing_lib")
_pkg = "_landing_ns_" + _uuid.uuid4().hex
_alias = _types.ModuleType(_pkg)
_alias.__path__ = [str(_libdir)]
_sys.modules[_pkg] = _alias
_sys.modules["landing_lib"] = _alias
_bw = _sys.dont_write_bytecode
_sys.dont_write_bytecode = True
try:
    from landing_lib import seam as _seam_bootstrap
    import landing_lib.foundation as _foundation_mod
    globals().update({k: v for k, v in vars(_foundation_mod).items() if not k.startswith("__") and k != "seam"})
    _seam_bootstrap.bind(globals())
    import landing_lib.governor as _governor_mod
    import landing_lib.store as _store_mod
    import landing_lib.policy as _policy_mod
    import landing_lib.sizes as _sizes_mod
    import landing_lib.genmap as _genmap_mod
    import landing_lib.leases as _leases_mod
    import landing_lib.queue as _queue_mod
    import landing_lib.merge as _merge_mod
    import landing_lib.contests as _contests_mod
    import landing_lib.drain as _drain_mod
    import landing_lib.times as _times_mod
    import landing_lib._land.parser as _parser_mod
    import landing_lib._land.prune as _prune_mod
    for _mod in (_governor_mod, _store_mod, _policy_mod, _sizes_mod, _genmap_mod, _leases_mod, _queue_mod,
                _merge_mod, _contests_mod, _drain_mod, _times_mod):
        globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__") and k != "seam"})
    _build_parser = _parser_mod.build_parser
    _run_prune = _prune_mod.prune
finally:
    _sys.dont_write_bytecode = _bw
    _drop_cached_landing("landing_lib", _pkg)
    _sys.modules.update(_prior_landing)
del _bw, _drop_cached_landing, _pkg, _alias, _libdir, _types, _uuid, _prior_landing
del _foundation_mod, _governor_mod, _store_mod, _policy_mod, _sizes_mod, _genmap_mod, _leases_mod, _queue_mod
del _merge_mod, _contests_mod, _drain_mod, _times_mod, _seam_bootstrap, _mod, _parser_mod, _prune_mod

def parser():
    return _build_parser(__doc__.splitlines()[0], exclusive_cap=EXCLUSIVE_CAP, modes=MODES,
                         legacy_modes=LEGACY_MODES, merge_methods=MERGE_METHODS,
                         author_policies=AUTHOR_POLICIES)


def run(argv):
    args = parser().parse_args(argv)
    if args.command == "slot":
        command = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not command:
            raise LandError("slot needs a command after --")
        if args.exclusive:
            if args.slots is not None:
                raise LandError("--slots cannot be combined with --exclusive")
            return None, exclusive_command(
                command, args.slot_repo or args.repo, args.slot_for, args.slot_reason)
        if args.slot_for is not None or args.slot_reason is not None:
            raise LandError("--for and --reason are only for slot --exclusive")
        count = _parse_slot_count(args.slots)
        with slot(repo=args.slot_repo or args.repo, command=" ".join(command), count=count):
            return None, subprocess.run(command).returncode
    if args.command == "init":
        pr_item_line = None if args.pr_item_line is None else args.pr_item_line == "on"
        text = init(args.repo, args.trunk, args.mode, args.remote, args.base, args.check, args.setup, args.batch, args.timeout,
                    args.merge_method, args.cap, args.generated, args.rebuild, args.author_policy, pr_item_line,
                    args.heartbeat_ms, args.merge_heartbeat_ms, args.generated_map,
                    size_max_tokens=args.max_file_tokens, size_baseline=args.size_baseline,
                    advisory_suffix=args.advisory_suffix)
        with Store.for_repo(args.repo) as store:
            warning = no_checks_note(store.contract)
        if warning:
            print(warning, file=sys.stderr)
        return text, 0
    if args.command == "size":
        if args.staged and args.all:
            raise LandError("size takes --staged or --all, not both")
        return size_report(args.repo, args.staged, args.all, args.shrink, args.baseline), 0
    if args.command == "times":
        return landing_times(args.repo, args.last, args.holder, args.json), 0
    with Store.for_repo(args.repo) as store:
        if args.command == "cap":
            return change_cap(store, args.count), 0
        if args.command == "owner":
            return raise_floor(store, args.prefix, args.generation), 0
        if args.command == "lease":
            if args.action == "claim":
                return lease_claim(store, args.holder, args.paths, args.ttl_hours, args.owner), 0
            if args.action == "check":
                return lease_check(store, args.holder, args.paths)
            if args.action == "list":
                return lease_list(store), 0
            if args.action == "reserve":
                return lease_reserve(store, args.prefix, args.paths, args.ruling, args.ttl_hours, args.owner), 0
            if args.action == "unreserve":
                return lease_unreserve(store, reservation_id(args.id), args.owner), 0
            if args.action == "widen":
                return lease_widen(store, lease_id(args.id), args.paths, args.ttl_hours, args.owner), 0
            number = lease_id(args.id)
            if args.action == "renew":
                return lease_renew(store, number, args.ttl_hours, args.if_live, args.owner), 0
            return lease_release(store, number, args.owner), 0
        if args.command == "mode":
            return change_mode(store, args.mode, args.merge_method), 0
        if args.command == "share":
            return change_share(store, args.prefix, args.count, args.clear, args.owner), 0
        if args.command == "contest":
            if (args.settle is None) != (args.first is None):
                raise LandError("--settle and --first go together")
            return contest(store, args.owner, args.holders, args.settle, args.first, args.cancel), 0
        if args.command == "submit":
            body = Path(args.body_file).read_text() if args.body_file else ""
            return submit(store, args.holder, args.branch, args.sha, args.lease, args.reviewer, args.title, body, args.owner), 0
        if args.command == "land":
            return land(store), 0
        if args.command == "resume":
            with store.tx() as db:
                if (store.contract.get("paused") or "").startswith(IDENTITY_PAUSE):
                    db.execute("DELETE FROM contract WHERE key = 'identity'")
                db.execute("INSERT OR REPLACE INTO contract VALUES ('paused', '\"\"')")
                db.execute("DELETE FROM contract WHERE key = 'tip'")
                store.log(db, "queue", 0, "resumed")
            return "queue resumed", 0
        if args.command == "prune":
            return _run_prune(store, args.dry_run), 0
        if args.holder:
            if args.id:
                raise LandError("status takes an entry id or --holder, not both")
            if args.json:
                return holder_json(store, args.holder, args.sha), 0
            return holder_status(store, args.holder, args.sha), 0
        if args.sha:
            raise LandError("status --sha needs --holder")
        if args.json:
            if args.id:
                raise LandError("status --json takes no id")
            payload = {"mode": store.contract.get("mode"), "trunk": store.contract.get("trunk"),
                       "generated": list(store.contract.get("generated") or []),
                       "rebuild": store.contract.get("rebuild"),
                       "map": list(store.contract.get("map") or [])}
            if store.contract.get("sizeMaxTokens") is not None:
                payload["sizeMaxTokens"] = store.contract.get("sizeMaxTokens")
                payload["sizeBaseline"] = store.contract.get("sizeBaseline")
            if store.contract.get("advisorySuffix"):
                payload["advisorySuffix"] = store.contract["advisorySuffix"]
            return json.dumps(payload, indent=2, sort_keys=True), 0
        line = status(store, args.id)
        if not args.id:
            note = generated_note(store.contract.get("generated") or [], store.contract.get("rebuild"))
            policy_note = author_policy_note(store.contract)
            item_note = pr_item_line_note(store.contract)
            heartbeat = heartbeat_note(store.contract)
            size = size_note(store.contract)
            advisory = advisory_suffix_note(store.contract)
            text = " ".join(part for part in (note, policy_note, item_note, heartbeat, size, advisory) if part)
            if text:
                line = f"{line} {text}"
            warning = no_checks_note(store.contract)
            if warning:
                print(warning, file=sys.stderr)
            return line, 0
        return line, 0


def main(argv=None):
    try:
        output, code = run(sys.argv[1:] if argv is None else argv)
    except LandError as error:
        print(f"land: {error}", file=sys.stderr)
        return 1
    if output is not None:
        print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
