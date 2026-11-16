import json
import time
from landing_lib import seam
from .rollup import BASE_MOVED_RETRIES, BASE_MOVED_WAIT, MERGE_REQUESTED, RULESET_MAX_PAGES, WAITING_FOR_CHECKS, absence_waits, absent_since, base_moved_refusal, merge_blocker, record_advisory, unposted_merge_refusal
from . import settle

def _protection_contexts(store):
    """Contexts for the trunk from branch protection, or None.

    The gh path is repos/{owner}/{repo}/branches/<trunk>/protection/required_status_checks.
    gh fills {owner} and {repo} from the checkout, as forge_branch_status already does.
    Return a tuple of non-empty strings when gh exits 0 and the body is a JSON object whose contexts is a list of non-empty strings.
    An empty list returns ().
    Return None on a non-zero gh exit, on a body that is not that object, and on a contexts element that is not a non-empty string.
    Ignore checks.
    """
    result = seam.gh(
        "api",
        "repos/{owner}/{repo}/branches/" + store.contract["trunk"] + "/protection/required_status_checks",
        cwd=store.repo,
    )
    if result.returncode != 0:
        return None
    try:
        body = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    contexts = body.get("contexts") if isinstance(body, dict) else None
    if not isinstance(contexts, list):
        return None
    if any(not isinstance(item, str) or item == "" for item in contexts):
        return None
    return tuple(contexts)

def _ruleset_contexts(store):
    """Contexts for the trunk from rulesets, or None.

    The gh path is repos/{owner}/{repo}/rules/branches/<trunk> with per_page 100.
    gh fills {owner} and {repo} from the checkout, as forge_branch_status already does.
    Every page is read until a short page, up to 50 pages.
    Return a tuple of non-empty strings from rules carrying required status checks when every page exits 0 and each body is a JSON list of dicts or a JSON object holding a rules list of dicts. An endpoint with no required status check rule returns (). Return None on a non-zero gh exit on any page, on stdout that is not valid JSON, on a body that is neither that list nor that object, on a rule that is not a dict, on a context that is not a non-empty string, and when 50 full pages arrive with no short page.
    """
    found = []
    per_page = 100
    base = "repos/{owner}/{repo}/rules/branches/" + store.contract["trunk"]
    for page in range(1, RULESET_MAX_PAGES + 1):
        result = seam.gh(
            "api",
            f"{base}?per_page={per_page}&page={page}",
            cwd=store.repo,
        )
        if result.returncode != 0:
            return None
        try:
            body = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if isinstance(body, dict) and isinstance(body.get("rules"), list):
            rules = body["rules"]
        elif isinstance(body, list):
            rules = body
        else:
            return None
        for rule in rules:
            if not isinstance(rule, dict):
                return None
            params = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
            is_checks = rule.get("type") == "required_status_checks"
            checks_value = None
            if isinstance(params.get("required_status_checks"), list):
                checks_value = params["required_status_checks"]
                is_checks = True
            elif isinstance(rule.get("required_status_checks"), list):
                checks_value = rule["required_status_checks"]
                is_checks = True
            elif is_checks and isinstance(params.get("contexts"), list):
                checks_value = params["contexts"]
            if not is_checks:
                continue
            if checks_value is None:
                continue
            if not isinstance(checks_value, list):
                return None
            for item in checks_value:
                if isinstance(item, str):
                    if item == "":
                        return None
                    found.append(item)
                elif isinstance(item, dict):
                    context = item.get("context")
                    if not isinstance(context, str) or context == "":
                        return None
                    found.append(context)
                else:
                    return None
        if len(rules) < per_page:
            return tuple(found)
    return None

def read_required_contexts(store):
    """Contexts for store.contract["trunk"] from protection and rulesets, or None.

    The gh paths are repos/{owner}/{repo}/branches/<trunk>/protection/required_status_checks and repos/{owner}/{repo}/rules/branches/<trunk>.
    gh fills {owner} and {repo} from the checkout, as forge_branch_status already does.
    Union both sources with ordered dedup. Return None only when both return None.
    An empty union returns ().
    """
    first = _protection_contexts(store)
    second = _ruleset_contexts(store)
    if first is None and second is None:
        return None
    seen = []
    for item in list(first or ()) + list(second or ()):
        if item not in seen:
            seen.append(item)
    return tuple(seen)

def missing_required(contexts, rollup_names):
    """Context strings that rollup_names does not contain, in contexts order."""
    posted = set(rollup_names)
    return tuple(context for context in contexts if context not in posted)

def refusal_waits(store, plain, checks):
    """True when this plain-merge stderr waits instead of pausing the queue.

    The policy phrase must be in plain. A blocker of absent waits and does not read required contexts.
    Any other blocker waits only when read_required_contexts returns a tuple and missing_required is non-empty.
    None does not wait. An empty tuple does not wait.
    Stderr without the policy phrase returns False.
    """
    if not unposted_merge_refusal(plain):
        return False
    if checks.blocker == "absent":
        return True
    contexts = read_required_contexts(store)
    if contexts is None or contexts == ():
        return False
    return missing_required(contexts, checks.names) != ()

def request_merge(store, ident, out):
    entry = settle.entry_row(store, ident)
    url = entry["pr"]
    line = settle.line_of(store, settle.trunk_commit(store)).entries
    ahead_of = {row["id"]: ahead for ahead, row in zip([None, *line], line)}
    if ident not in ahead_of:
        return False
    ahead = ahead_of[ident]
    checks = merge_blocker(store, url)
    if checks.head != entry["candidate"]:
        return False
    record_advisory(store, ident, entry["candidate"], checks)
    if checks.blocker == "review":
        settle.pause_for_review(store, url, checks.detail, disarm=False)
    waiting = checks.blocker == "pending"
    if checks.blocker == "absent":
        since = absent_since(store, ident, entry["candidate"])
        waiting = ahead or absence_waits(since, seam.now())
    if waiting:
        with store.tx() as db:
            store.set_entry(db, ident, "awaiting-merge", note=WAITING_FOR_CHECKS)
        return False
    if ahead:
        with store.tx() as db:
            store.set_entry(db, ident, "awaiting-merge", note=f"waiting for {seam.entry_label(ahead['id'])} to merge first")
        return False
    if checks.blocker == "failed":
        settle.bounce_open_pr(store, ident, url, f"required checks failed on {url}: {checks.detail}", checks.armed)
        out.bounced.append(ident)
        return False
    trunk = settle.fetch_trunk(store)
    if not settle.same_tree(store.repo, settle.built_on(store, entry, trunk), trunk):
        return False
    pinned = (f"--{settle.merge_method(store)}", "--match-head-commit", entry["candidate"])
    queued = seam.gh("pr", "merge", url, "--auto", *pinned, cwd=store.repo)
    if queued.returncode == 0:
        with store.tx() as db:
            store.set_entry(db, ident, "awaiting-merge", note=MERGE_REQUESTED)
        return True
    for asked in range(BASE_MOVED_RETRIES + 1):
        now_ = seam.gh("pr", "merge", url, *pinned, cwd=store.repo)
        if now_.returncode == 0 or not base_moved_refusal(now_.stderr) or asked == BASE_MOVED_RETRIES:
            break
        time.sleep(BASE_MOVED_WAIT)
    if now_.returncode != 0:
        plain = (now_.stderr or "").strip()
        if refusal_waits(store, plain, checks):
            with store.tx() as db:
                store.set_entry(db, ident, "awaiting-merge", note=WAITING_FOR_CHECKS)
            return False
        if base_moved_refusal(plain):
            return False
        head = settle.open_pr_head(store, url)
        if head and head != entry["candidate"]:
            with store.tx() as db:
                store.set_entry(db, ident, "awaiting-merge", note=settle.head_changed_note(head))
            raise seam.HeadChanged(
                f"GitHub refused to merge {url}, and its head is now {head[:12]}, not the checked candidate "
                f"{entry['candidate'][:12]}. {seam.entry_label(ident)} was not merged. Close that PR to bounce "
                f"{seam.entry_label(ident)}, or push {entry['candidate'][:12]} back to that PR's branch. "
                "Then run land.py resume and land.py land")
        raise seam.Infrastructure(f"GitHub refused to merge {url}: {plain or 'gh pr merge failed'}")
    with store.tx() as db:
        store.set_entry(db, ident, "awaiting-merge", note=MERGE_REQUESTED)
    return True
