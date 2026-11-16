import json
import re
import time
from collections import namedtuple
from landing_lib import seam
from . import required
from .forge import _CLIENT_ABSENT_REF, delete_queue_branch, remote_branch_is_gone
from .rollup import OBJECT_ID, PR_STATES, merge_blocker, merge_requested, record_advisory

Line = namedtuple("Line", "entries stale tip")

def trunk_commit(store):
    return seam.git("rev-parse", seam.trunk_ref(store.contract), cwd=store.repo).stdout.strip()

def fetch_trunk(store):
    contract = store.contract
    seam.git("fetch", contract["remote"], contract["trunk"], cwd=store.repo)
    return trunk_commit(store)

def same_tree(repo, first, second):
    found = seam.git("rev-parse", f"{first}^{{tree}}", f"{second}^{{tree}}", cwd=repo, check=False)
    trees = found.stdout.split()
    return found.returncode == 0 and len(trees) == 2 and trees[0] == trees[1]

def read_tree(store, commit):
    """The tree of the commit with this full id, or None when git cannot read it.

    After a failed read the id is fetched from the contract remote and read once more."""
    for fetched in (False, True):
        found = seam.git("rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}", cwd=store.repo, check=False)
        tree = found.stdout.strip()
        if found.returncode == 0 and OBJECT_ID.fullmatch(tree):
            return tree
        if not fetched:
            seam.git("fetch", store.contract["remote"], commit, cwd=store.repo, check=False)
    return None

def built_on(store, entry, trunk):
    if entry["onto"]:
        return entry["onto"]
    base = seam.git("merge-base", entry["candidate"], trunk, cwd=store.repo, check=False)
    return base.stdout.strip() if base.returncode == 0 else ""

def merge_method(store):
    return store.contract.get("mergeMethod") or "merge"

def line_of(store, trunk):
    waiting = store.entries("awaiting-merge")
    built = [row for row in waiting if row["candidate"]]
    first = next((row for row in built if same_tree(store.repo, built_on(store, row, trunk), trunk)), None)
    entries = [first] if first else []
    behind = {row["onto"]: row for row in reversed(built)}
    if merge_method(store) != "rebase":
        while entries and entries[-1]["candidate"] in behind:
            entries.append(behind[entries[-1]["candidate"]])
    inside = {row["id"] for row in entries}
    stale = [row for row in waiting if row["id"] not in inside and row["pr"] and not merge_requested(row)]
    return Line(entries, stale, entries[-1]["candidate"] if entries else trunk)

def disarm_auto_merge(store, url, armed):
    """Turn auto-merge off when the check read showed an autoMergeRequest.

    An empty return means it is off or was never requested. A string is a failure of
    gh pr merge --disable-auto."""
    if not armed:
        return ""
    result = seam.gh("pr", "merge", url, "--disable-auto", cwd=store.repo)
    if result.returncode == 0:
        return ""
    return (result.stderr or result.stdout or "gh pr merge --disable-auto failed").strip()

def close_pr_with_reason(store, url, reason, state=""):
    """Close an open PR with reason as a one-line comment. Returns (closed, warning).

    A PR that is closed or merged is left as it is, which returns (False, ""). state is a state the
    caller already read, and is read here when it is empty."""
    if not url:
        return False, ""
    if not state:
        try:
            state = read_pr_state(store, url)[0]
        except seam.Infrastructure as problem:
            return False, f"could not read the state of {url}: {problem}"
    if state != "OPEN":
        return False, ""
    closed = seam.gh("pr", "close", url, "--comment", " ".join(reason.split()), cwd=store.repo)
    if closed.returncode != 0:
        return False, f"could not close {url}: {(closed.stderr or closed.stdout or 'gh pr close failed').strip()}"
    return True, ""

def delete_landing_branch(store, branch, entry):
    """Delete branch from the contract remote and drop its local copies. Returns (deleted, warning). A push that succeeds counts as deleted. For an entry with a pull request, a push that fails counts as deleted only when remote_branch_is_gone says the branch is gone. For an entry with no pull request, Git reporting an absent ref counts as deleted. An empty listing alone is not proof. An entry with no pull request never asks the forge. A local branch git cannot delete is named in warning."""
    remote = store.contract["remote"]
    pushed = seam.git_push(remote, "--delete", branch, cwd=store.repo)
    if pushed.returncode == 0:
        return True, seam.forget_local_branch(store, branch)
    stderr = pushed.stderr or ""
    if not entry["pr"]:
        if _CLIENT_ABSENT_REF.search(stderr) is not None:
            return True, seam.forget_local_branch(store, branch)
        return False, f"could not delete {branch} from {remote}: {seam.git_reason(pushed.stderr)}"
    if not remote_branch_is_gone(store, entry, branch, stderr):
        return False, f"could not delete {branch} from {remote}: {seam.git_reason(pushed.stderr)}"
    return True, seam.forget_local_branch(store, branch)

def clean_bounced(store, entry, reason, state=""):
    """Close the entry's PR with the reason, then delete its landing branch. Returns the warnings joined, or "".

    In local and push mode an entry with no pull request has no landing branch, so only the close runs. Otherwise both steps run when one fails. Each failure prints one warning line. An already closed PR and a branch `remote_branch_is_gone` reports gone are not failures."""
    warnings = []
    results = [close_pr_with_reason(store, entry["pr"], reason, state)]
    # Local and push modes never push a landing branch for an entry without a pull request.
    if store.contract["mode"] not in ("local", "push") or entry["pr"]:
        results.append(delete_landing_branch(store, seam.human_branch(entry), entry))
    for _done, warning in results:
        if warning:
            warnings.append(warning)
            print(f"warning: {warning}")
    return " ".join(warnings)

def bounce_open_pr(store, ident, url, reason, armed):
    """Bounce an open merge-mode PR. Disable auto-merge when it is on, then close the PR and delete its branch.

    When auto-merge cannot be disabled it raises and leaves the PR and the branch in place, because a PR whose
    auto-merge stays on can still merge without the queue, so the queue pauses and keeps tracking it."""
    problem = disarm_auto_merge(store, url, armed)
    if problem:
        raise seam.AutoMergeStillEnabled(
            f"{reason}. Auto-merge is still enabled: {problem}. "
            f"Turn auto-merge off on {url}, then run land.py resume and land.py land")
    clean_bounced(store, entry_row(store, ident), reason, "OPEN")
    with store.tx() as db:
        seam.settle_bounced(store, db, ident, reason)

def pause_for_review(store, url, detail, disarm, armed=False):
    problem = disarm_auto_merge(store, url, armed) if disarm else ""
    requirement = "has changes requested" if detail == "CHANGES_REQUESTED" else "needs an approving review"
    if problem:
        raise seam.Infrastructure(f"{url} {requirement}. Auto-merge is still enabled: {problem}")
    raise seam.Infrastructure(f"{url} {requirement}")

def entry_row(store, ident):
    return store.db.execute("SELECT * FROM entry WHERE id = ?", (ident,)).fetchone()

def read_pr_state(store, url):
    """State, head commit, and merge commit of a PR. Raises Infrastructure when gh fails or prints no known state."""
    view = seam.gh("pr", "view", url, "--json", "state,headRefOid,mergeCommit",
              "-q", '.state + " " + .headRefOid + " " + (.mergeCommit.oid // "")', cwd=store.repo)
    fields = (view.stdout.strip().split(" ") + ["", "", ""])[:3]
    if view.returncode != 0 or fields[0] not in PR_STATES:
        raise seam.Infrastructure(f"could not read the state of {url}. gh said {view.stderr.strip() or 'nothing'}")
    return fields

def open_pr_head(store, url):
    """The head commit of a PR that a state read shows open. Empty when the read fails or shows another state."""
    try:
        state, head, _merged = read_pr_state(store, url)
    except seam.Infrastructure:
        return ""
    return head if state == "OPEN" else ""

def head_changed_note(head):
    return f"PR head changed to {head[:12]} outside the queue"

def tree_mismatch_pause(store, entry, merged, trunk):
    """The pause text when trunk holds the merge commit and its tree is not the candidate's, and empty when trunk holds it and the trees are the same.

    Raises MergeNotOnTrunk when git says the trunk commit does not hold the merge commit. Raises UnverifiedMerge when
    the merge commit has no full id, either tree cannot be read, or git cannot tell whether trunk holds the merge commit."""
    label, remote, url = seam.entry_label(entry["id"]), store.contract["remote"], entry["pr"]
    unverified = (f"The merge of {label} could not be checked. {{cause}}, so the tree of the merge commit was not "
                  f"compared with the tree of the candidate. {label} stays awaiting merge. "
                  "{fix}, then run land.py resume and land.py land")
    if not OBJECT_ID.fullmatch(merged or ""):
        raise seam.UnverifiedMerge(unverified.format(
            cause=f"The state read of {url} gave no full commit id for the merge commit",
            fix=f"Check what gh pr view {url} --json mergeCommit prints"))
    trees = []
    for commit in (merged, entry["candidate"]):
        tree = read_tree(store, commit)
        if tree is None:
            raise seam.UnverifiedMerge(unverified.format(
                cause=f"Git could not read commit {commit} even after asking {remote} for it",
                fix=f"Check that {remote} serves that commit"))
        trees.append(tree)
    # The exit codes are those in rewound. Only 0 shows that the merge commit is on trunk.
    code = seam.git("merge-base", "--is-ancestor", merged, trunk, cwd=store.repo, check=False).returncode
    if code == 1:
        raise seam.MergeNotOnTrunk(f"{url} is recorded as merged at {merged[:12]}, and trunk at {trunk[:12]} does not hold "
                              f"that commit. {label} stays awaiting merge. Check what happened to trunk. Once trunk "
                              "holds that commit again, run land.py resume and land.py land")
    if code != 0:
        raise seam.UnverifiedMerge(unverified.format(
            cause=f"Git could not tell whether trunk at {trunk} holds merge commit {merged} "
                  f"(git merge-base --is-ancestor exited {code})",
            fix="Check that git can read both commits here"))
    if trees[0] == trees[1]:
        return ""
    return (f"{label} merged as {merged[:12]}, whose tree is not the checked candidate "
            f"{entry['candidate'][:12]}. Trunk holds a change no check ran against. Check trunk, then run land.py resume")

def take_pr(store, entry, landed, bounced):
    try:
        state, head, merged = read_pr_state(store, entry["pr"])
    except seam.Infrastructure:
        if store.contract["mode"] == "merge":
            raise
        return False
    if state == "MERGED":
        checked, unchecked = head == entry["candidate"], ""
        if store.contract["mode"] == "merge":
            trunk = fetch_trunk(store)
            unchecked = tree_mismatch_pause(store, entry, merged, trunk) if checked else ""
        deleted, warning = delete_queue_branch(store, entry)
        if not deleted:
            return True
        with store.tx() as db:
            if checked:
                seam.settle_landed(store, db, entry["id"], merged, warning)
            else:
                seam.settle_bounced(store, db, entry["id"], f"merged at {merged[:12]} with head {head[:12]}, not the checked "
                                                       f"{entry['candidate'][:12]}; review what reached trunk")
            db.execute("INSERT OR REPLACE INTO contract VALUES ('tip', ?)", (json.dumps(merged),))
            if unchecked:
                seam.mark_paused(store, db, unchecked)
        (landed if checked else bounced).append(entry["id"])
        if unchecked:
            raise seam.UncheckedTrunk(unchecked)
        return True
    if state == "CLOSED":
        reason = "PR closed without merging"
        clean_bounced(store, entry, reason, "CLOSED")
        with store.tx() as db:
            seam.settle_bounced(store, db, entry["id"], reason)
        bounced.append(entry["id"])
        return True
    if state == "OPEN" and head and head != entry["candidate"]:
        with store.tx() as db:
            store.set_entry(db, entry["id"], "awaiting-merge", note=head_changed_note(head))
        return True
    return False

def poll(store, entry, out):
    merging = store.contract["mode"] == "merge"
    if not entry["pr"]:
        reason = seam.foreign_author(store, entry)
        if reason:
            clean_bounced(store, entry, reason)
            with store.tx() as db:
                seam.settle_bounced(store, db, entry["id"], reason)
            out.bounced.append(entry["id"])
            return
        outcome = seam.ensure_pr(store, entry)
        (out.adopted if outcome == "adopted" else out.opened).append(entry["id"])
        if merging:
            required.request_merge(store, entry["id"], out)
        entry = entry_row(store, entry["id"])
        if entry["state"] != "awaiting-merge":
            return
    if take_pr(store, entry, out.landed, out.bounced) or not merging:
        return
    if merge_requested(entry):
        checks = merge_blocker(store, entry["pr"])
        if take_pr(store, entry, out.landed, out.bounced):
            return
        if checks.head == entry["candidate"]:
            record_advisory(store, entry["id"], entry["candidate"], checks)
        if checks.blocker == "failed":
            bounce_open_pr(store, entry["id"], entry["pr"],
                           f"required checks failed on {entry['pr']}: {checks.detail}", checks.armed)
            out.bounced.append(entry["id"])
        elif checks.blocker == "review":
            pause_for_review(store, entry["pr"], checks.detail, disarm=True, armed=checks.armed)
        return
    # A merge command can return before pr view reports MERGED.
    reads = 4 if required.request_merge(store, entry["id"], out) else 1
    entry = entry_row(store, entry["id"])
    if entry["state"] != "awaiting-merge":
        return
    for attempt in range(reads):
        if take_pr(store, entry, out.landed, out.bounced):
            break
        entry = entry_row(store, entry["id"])
        if entry["state"] != "awaiting-merge" or attempt + 1 == reads:
            break
        time.sleep(0.5)

def walk(store, out, only=None):
    polled = set()
    while True:
        line = line_of(store, trunk_commit(store)).entries
        inside = {row["id"] for row in line}
        rows = [row for row in store.entries("awaiting-merge") if row["id"] not in inside] + line
        entry = next((row for row in rows if row["id"] not in polled and (only is None or row["id"] in only)), None)
        if entry is None:
            return
        polled.add(entry["id"])
        poll(store, entry, out)
