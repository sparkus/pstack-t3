import json
import os
import shutil
from pathlib import Path
from landing_lib import seam
from landing_lib._merge.forge import github_owner_repo
from .errors import allowed_merge_methods, busy_queue_message, pick_merge_method, trunk_ref

GH_MODES = ("merge", "human")

def gh_program():
    """The gh command the queue runs. LAND_GH names another executable."""
    return os.environ.get("LAND_GH", "gh")

def refused_forge(repo, remote, mode):
    """Why merge or human mode cannot land this repository through pull requests, or None for push and local."""
    if mode not in GH_MODES:
        return None
    listed = seam.git("remote", "get-url", remote, cwd=repo, check=False)
    if not github_owner_repo(listed.stdout.strip()):
        return f"mode {mode} opens pull requests with gh, and remote {remote!r} is not a GitHub URL; use mode push or mode local"
    if shutil.which(gh_program()) is None:
        return f"mode {mode} opens pull requests with gh, and gh is not installed; install gh, or use mode push or mode local"
    auth_args = ["auth", "status", "--active", "--hostname", "github.com"]
    auth = seam.gh(*auth_args, cwd=repo)
    if auth.returncode != 0 and "unknown flag: --active" in auth.stderr:
        auth_args.remove("--active")
        auth = seam.gh(*auth_args, cwd=repo)
    if auth.returncode != 0:
        return (f"mode {mode} opens pull requests with gh, and gh {' '.join(auth_args)} failed; "
                "run gh auth login, or use mode push or mode local")
    return None

def generated_list(passed, stored):
    """The generated paths an init stores. None keeps the stored list. An empty value clears it."""
    if passed is None:
        return list(stored)
    if not passed.strip():
        return []
    return [seam.generated_path(part) for part in passed.split(",")]

def rebuild_command(passed, stored):
    """The rebuild command an init stores, or None. None keeps the stored value. An empty value clears it."""
    if passed is None:
        return stored
    return passed.strip() or None

def generated_note(generated, rebuild):
    """The contract's generated paths and rebuild command, or "" when it holds neither."""
    if not generated and not rebuild:
        return ""
    return f"generated: {', '.join(generated) if generated else 'none'}; rebuild: {rebuild or 'none'}"

def map_list(passed, stored, roots):
    """The map rows an init stores. None keeps the stored rows. An empty value clears them."""
    if passed is None:
        return list(stored)
    if not passed.strip():
        return []
    return seam.parse_map(passed, roots)

HEARTBEAT_MS = 3600000

MERGE_HEARTBEAT_MS = 900000

def refused_heartbeat(flag, value, floor):
    """Why an init heartbeat flag value is refused, or None. None passes."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < floor:
        return f"{flag} must be an integer at or above {floor}"
    return None

def heartbeat_note(contract):
    """The heartbeat intervals for repo status. Empty when both intervals hold their defaults."""
    every = contract.get("heartbeatMs", HEARTBEAT_MS)
    merge = contract.get("mergeHeartbeatMs", MERGE_HEARTBEAT_MS)
    if every == HEARTBEAT_MS and merge == MERGE_HEARTBEAT_MS:
        return ""
    return f"heartbeat everyMs {every} merge wait {merge}"

def refused_size_maximum(value):
    """Why an init size flag value is refused, or None. None passes."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return "--max-file-tokens must be an integer at or above 1"
    return None

def size_baseline_value(passed, stored):
    """The baseline path an init stores. None keeps the stored path, which may be none."""
    if passed is None:
        return stored
    text = passed.strip().replace("\\", "/").rstrip("/")
    parts = text.split("/")
    if not text or text.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise seam.LandError(f"size baseline {passed!r} must be a repository-relative path with no '.', '..', or empty component")
    return text

def size_note(contract):
    """The stored size limit for repo status. Empty when the contract sets none."""
    maximum = contract.get("sizeMaxTokens")
    if maximum is None:
        return ""
    return f"size limit {maximum} tokens via {contract.get('sizeBaseline') or seam.DEFAULT_BASELINE}"

def init(path, trunk, mode, remote, base, checks, setup, batch, timeout, merge_method=None, cap=None, generated=None, rebuild=None,
         author_policy=None, pr_item_line=None, heartbeat_ms=None, merge_heartbeat_ms=None, generated_map=None,
         size_max_tokens=None, size_baseline=None):
    mode = seam.LEGACY_MODES.get(mode, mode)
    for flag, value, floor in (("--heartbeat-ms", heartbeat_ms, HEARTBEAT_MS),
                               ("--merge-heartbeat-ms", merge_heartbeat_ms, MERGE_HEARTBEAT_MS)):
        reason = refused_heartbeat(flag, value, floor)
        if reason:
            raise seam.LandError(reason)
    reason = refused_size_maximum(size_max_tokens)
    if reason:
        raise seam.LandError(reason)
    common = seam.common_dir(path)
    repo = common.parent if common.name == ".git" else Path(seam.git("rev-parse", "--show-toplevel", cwd=path).stdout.strip())
    directory = seam.state_home() / "landing" / seam.store_name(common)
    directory.mkdir(parents=True, exist_ok=True)

    def filled(stored, key, passed, default):
        if passed is not None:
            return passed
        return stored[key] if key in stored else default

    def merge_method_to_store(explicit, stored_method, mode, allowed):
        if explicit is not None:
            return pick_merge_method(explicit, allowed)
        if stored_method is None:
            return "merge" if mode != "merge" else pick_merge_method(None, allowed)
        if allowed is None or stored_method in allowed:
            return stored_method
        return pick_merge_method(None, allowed)

    stored = {}
    if (directory / "land.db").exists():
        with seam.Store(directory) as existing:
            stored = existing.contract
    stored_method = stored.get("mergeMethod")
    allowed = None
    if merge_method is not None or mode == "merge" or stored_method is not None:
        allowed = allowed_merge_methods(repo)
    # A refused method must fail before the store exists, so a first init leaves no database.
    merge_method_to_store(merge_method, stored_method, mode, allowed)
    fixed = {"commonDir": str(common), "trunk": trunk, "mode": mode, "remote": remote}

    def refuse_clash(current):
        current = {**current, "mode": seam.LEGACY_MODES.get(current.get("mode"), current.get("mode"))} if "mode" in current else current
        clash = [key for key in fixed if key in current and current[key] != fixed[key]]
        if clash:
            raise seam.LandError(f"the contract already sets {', '.join(f'{k}={current[k]!r}' for k in clash)}; "
                            "change the mode with land.py mode; changing trunk or remote needs a new repository contract")

    refuse_clash(stored)
    if mode in seam.REMOTE_MODES:
        if not seam.git("remote", "get-url", remote, cwd=repo, check=False).stdout.strip():
            raise seam.LandError(f"mode {mode} needs remote {remote!r}; use --mode local for a repository without one")
        reason = refused_forge(repo, remote, mode)
        if reason:
            raise seam.LandError(reason)
        seam.git("fetch", remote, trunk, cwd=repo)
    # A bad generated path must be refused before the store exists, so a first init leaves no database.
    if generated is not None:
        generated_list(generated, [])
    if generated_map is not None:
        early_roots = generated_list(generated, stored.get("generated") or [])
        seam.parse_map(generated_map, early_roots)
    # A refused size flag must fail the same way.
    baseline_to_store = size_baseline_value(size_baseline, stored.get("sizeBaseline"))
    with seam.Store(directory) as store:
        with store.tx() as db:
            current = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key, value FROM contract")}
            refuse_clash(current)
            baseline_to_store = size_baseline_value(size_baseline, current.get("sizeBaseline"))
            wanted = {"commonDir": str(common), "repo": str(repo), "trunk": trunk, "mode": mode, "remote": remote,
                      "checks": filled(current, "checks", checks, []),
                      "setup": filled(current, "setup", setup, ""),
                      "batch": filled(current, "batch", batch, 1),
                      "timeout": filled(current, "timeout", timeout, 1800),
                      "mergeMethod": merge_method_to_store(merge_method, current.get("mergeMethod"), mode, allowed),
                      "generated": generated_list(generated, current.get("generated") or []),
                      "rebuild": rebuild_command(rebuild, current.get("rebuild")),
                      "map": map_list(generated_map, current.get("map") or [], generated_list(generated, current.get("generated") or [])),
                      "authorPolicy": filled(current, "authorPolicy", author_policy, "any"),
                      "prItemLine": filled(current, "prItemLine", pr_item_line, False),
                      "heartbeatMs": filled(current, "heartbeatMs", heartbeat_ms, HEARTBEAT_MS),
                      "mergeHeartbeatMs": filled(current, "mergeHeartbeatMs", merge_heartbeat_ms, MERGE_HEARTBEAT_MS),
                      "paused": ""}
            for key, value in wanted.items():
                if key != "paused" or key not in current:
                    db.execute("INSERT OR REPLACE INTO contract VALUES (?, ?)", (key, json.dumps(value)))
            if cap is not None:
                set_cap(db, cap)
            # Size keys stay absent until a flag sets them, so an older contract keeps no size key.
            if size_max_tokens is not None:
                db.execute("INSERT OR REPLACE INTO contract VALUES ('sizeMaxTokens', ?)", (json.dumps(size_max_tokens),))
            if baseline_to_store is not None:
                db.execute("INSERT OR REPLACE INTO contract VALUES ('sizeBaseline', ?)", (json.dumps(baseline_to_store),))
        if mode == "local" and seam.git("rev-parse", "--verify", "--quiet", trunk_ref(wanted), cwd=repo, check=False).returncode != 0:
            if not base:
                raise seam.LandError(f"local mode lands on {trunk_ref(wanted)}; pass --base <commit> to create it")
            seam.git("update-ref", trunk_ref(wanted), seam.git("rev-parse", "--verify", f"{base}^{{commit}}", cwd=repo).stdout.strip(), "", cwd=repo)
        note = f"{'updated' if current else 'created'} {directory}"
        settings = generated_note(wanted["generated"], wanted["rebuild"])
        return f"{note}. {settings}" if settings else note

def set_cap(db, cap):
    if cap < 0:
        raise seam.LandError(f"cap {cap} is negative; pass 0 to clear it")
    if cap:
        db.execute("INSERT OR REPLACE INTO contract VALUES ('cap', ?)", (json.dumps(cap),))
    else:
        db.execute("DELETE FROM contract WHERE key = 'cap'")

def change_cap(store, cap):
    with store.tx() as db:
        if cap > 0:
            rows = db.execute("SELECT prefix, count FROM share ORDER BY prefix").fetchall()
            total = sum(row["count"] for row in rows)
            if total > cap:
                listed = ", ".join(f"{row['prefix']} {row['count']}" for row in rows)
                raise seam.LandError(f"shares add up to {total} ({listed}), over the cap of {cap}")
        set_cap(db, cap)
        store.log(db, "queue", 0, f"cap {cap}")
    return f"repository cap is now {cap} changes in flight" if cap else "repository cap cleared"

def change_mode(store, mode, merge_method):
    """Switch remote modes while nothing is in flight. A merge method may change while entries wait, because it only affects the next gh pr merge."""
    mode = seam.LEGACY_MODES.get(mode, mode)
    seen = store.contract["mode"]
    if "local" in (mode, seen) and mode != seen:
        raise seam.LandError("local mode lands on refs/landing/<trunk>, not the remote trunk; switching to or from it needs a new contract")
    reason = refused_forge(store.repo, store.contract["remote"], mode)
    if reason:
        raise seam.LandError(reason)
    allowed = None
    if merge_method or mode == "merge":
        allowed = allowed_merge_methods(store.repo)
        if merge_method:
            pick_merge_method(merge_method, allowed)
    with store.tx() as db:
        # The read above can go stale while gh runs. This one decides the write.
        current = json.loads(db.execute("SELECT value FROM contract WHERE key = 'mode'").fetchone()["value"])
        current = seam.LEGACY_MODES.get(current, current)
        if "local" in (mode, current) and mode != current:
            raise seam.LandError("local mode lands on refs/landing/<trunk>, not the remote trunk; switching to or from it needs a new contract")
        busy = db.execute("SELECT count(*) FROM entry WHERE state IN ('queued', 'landing', 'awaiting-merge')").fetchone()[0]
        if busy and (mode != current or not merge_method):
            raise seam.LandError(busy_queue_message(busy))
        db.execute("INSERT OR REPLACE INTO contract VALUES ('mode', ?)", (json.dumps(mode),))
        method = merge_method
        if not method and mode == "merge" and allowed is not None:
            # Check the method stored now, not the one stored before gh ran.
            method = None if (store.contract.get("mergeMethod") or "merge") in allowed else pick_merge_method(None, allowed)
        if method:
            db.execute("INSERT OR REPLACE INTO contract VALUES ('mergeMethod', ?)", (json.dumps(method),))
        store.log(db, "queue", 0, f"mode {mode}", f"was {current}")
        holders = seam.holders_of(seam.in_flight(db))
    return (f"landing mode is now {mode}" + (f", merging with --{method}" if method else "")
            + (f"; leases held by {holders}" if holders else ""))

def repo_root(path):
    """Toplevel of the repository holding this path."""
    return Path(seam.git("rev-parse", "--show-toplevel", cwd=path).stdout.strip())
