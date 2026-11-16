import json
import re
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from landing_lib import seam

MERGE_REQUESTED = "merge requested by the queue"

WAITING_FOR_CHECKS = "waiting for required checks before merging"

BASE_MOVED_RETRIES = 3

BASE_MOVED_WAIT = 2

ABSENT_CHECKS_GRACE = timedelta(minutes=5)

RULESET_MAX_PAGES = 50

BLOCKER_QUERY = (
    'def check_name: .name // .context; '
    '[.reviewDecision // "", '
    '([(.statusCheckRollup // [])[] | select((.status // "COMPLETED") != "COMPLETED" or (.state // "") == "PENDING")] | length), '
    '([(.statusCheckRollup // [])[] | select((.conclusion // "") as $c | (.state // "") as $s '
    '| ($c == "FAILURE" or $c == "CANCELLED" or $c == "TIMED_OUT" or $s == "FAILURE" or $s == "ERROR")) '
    '| check_name] | join(",")), '
    '((.statusCheckRollup // []) | length), '
    '(.autoMergeRequest != null), '
    '(.headRefOid // ""), '
    '([(.statusCheckRollup // [])[] | check_name | select(type == "string" and . != "")] | tojson)] | @tsv'
)

ADVISORY_RESULTS = "advisoryChecks"

def blocker_query(suffix):
    """BLOCKER_QUERY when suffix is empty.

    Otherwise checks whose name ends in suffix leave the pending count and the failed names,
    and an eighth field lists each of them as [name, result, kind] with kind pending, failed, or passed.
    """
    if not suffix:
        return BLOCKER_QUERY
    pending = '((.status // "COMPLETED") != "COMPLETED" or (.state // "") == "PENDING")'
    failed = ('((.conclusion // "") as $c | (.state // "") as $s '
              '| ($c == "FAILURE" or $c == "CANCELLED" or $c == "TIMED_OUT" or $s == "FAILURE" or $s == "ERROR"))')
    result = ('(if (.conclusion // "") != "" then .conclusion elif (.state // "") != "" then .state '
              'else (.status // "") end)')
    return (
        'def check_name: .name // .context; '
        f'def advisory: check_name | type == "string" and endswith({json.dumps(suffix)}); '
        '[.reviewDecision // "", '
        f'([(.statusCheckRollup // [])[] | select(advisory | not) | select({pending})] | length), '
        f'([(.statusCheckRollup // [])[] | select(advisory | not) | select({failed}) | check_name] | join(",")), '
        '((.statusCheckRollup // []) | length), '
        '(.autoMergeRequest != null), '
        '(.headRefOid // ""), '
        '([(.statusCheckRollup // [])[] | check_name | select(type == "string" and . != "")] | tojson), '
        f'([(.statusCheckRollup // [])[] | select(advisory) | [check_name, {result}, '
        f'(if {pending} then "pending" elif {failed} then "failed" else "passed" end)]] | tojson)] | @tsv'
    )

def classify_rollup(review, pending, failed, posted):
    """The gate for one check read. Absent means GitHub has posted nothing yet."""
    if review in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
        return "review", review
    if failed:
        return "failed", failed
    if pending > 0:
        return "pending", ""
    if posted == 0:
        return "absent", ""
    return "", ""

def unposted_merge_refusal(stderr):
    """True when stderr contains the base branch policy prohibition."""
    return "the base branch policy prohibits the merge" in (stderr or "")

def base_moved_refusal(stderr):
    return "Base branch was modified" in (stderr or "")

def merge_requested(row):
    return row["note"] == MERGE_REQUESTED

OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

PR_STATES = ("OPEN", "CLOSED", "MERGED")

CheckRead = namedtuple("CheckRead", "blocker detail armed head names advisory")

def absent_since(store, ident, candidate):
    """When this entry first saw no posted check for this candidate.

    A missing, legacy, unreadable, or other-candidate marker records now and
    returns that moment. A second read in the same run finds the stored record.
    """
    raw = store.contract.get("absentDrain") or {}
    record = raw.get(str(ident))
    since = None
    if isinstance(record, dict) and record.get("candidate") == candidate:
        try:
            since = datetime.fromisoformat(record.get("since") or "")
        except (ValueError, TypeError):
            since = None
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    if since is None:
        since = seam.now()
        with store.tx() as db:
            row = db.execute("SELECT value FROM contract WHERE key = 'absentDrain'").fetchone()
            raw = json.loads(row["value"]) if row else {}
            raw[str(ident)] = {"since": seam.stamp(since), "candidate": candidate}
            db.execute("INSERT OR REPLACE INTO contract VALUES ('absentDrain', ?)", (json.dumps(raw),))
            store.set_entry(db, ident, "awaiting-merge", note=WAITING_FOR_CHECKS)
    return since

def absence_waits(since, moment):
    """True while moment is still inside the grace after since."""
    return moment - since < ABSENT_CHECKS_GRACE

def tsv_field_text(field):
    """Reverse jq @tsv escapes in one field.

    A backslash, tab, line feed, or carriage return comes back as itself.
    An unknown escape or a trailing backslash returns None.
    """
    out = []
    index = 0
    size = len(field)
    while index < size:
        char = field[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= size:
            return None
        nxt = field[index + 1]
        if nxt == "\\":
            out.append("\\")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "n":
            out.append("\n")
        elif nxt == "r":
            out.append("\r")
        else:
            return None
        index += 2
    return "".join(out)

def json_field(field):
    """The JSON value in one @tsv field, or None when it does not parse."""
    text = tsv_field_text(field)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

def advisory_rows(value):
    """A tuple of (name, result, kind) from field 8, or None when it is not that shape."""
    if not isinstance(value, list):
        return None
    rows = []
    for item in value:
        if (not isinstance(item, list) or len(item) != 3 or not all(isinstance(part, str) for part in item)
                or item[2] not in ("pending", "failed", "passed")):
            return None
        rows.append(tuple(item))
    return tuple(rows)

def merge_blocker(store, url):
    """The check read, plus the posted names from that same rollup.

    names is a tuple of non-empty strings. Field 7 is the @tsv form of their JSON array.
    tsv_field_text reverses that escaping before the array is read.
    A field count other than 7, or a field 7 that is not a JSON list of strings after that, raises Infrastructure.
    With an advisorySuffix in the contract the query has an eighth field. A check from it is advisory only
    when the required contexts can be read and do not name it. Any other failed or pending one is a blocker.
    advisory holds the rows that stayed advisory.
    """
    suffix = store.contract.get("advisorySuffix") or ""
    view = seam.gh("pr", "view", url, "--json", "reviewDecision,statusCheckRollup,autoMergeRequest,headRefOid",
              "-q", blocker_query(suffix), cwd=store.repo)
    if view.returncode != 0:
        raise seam.Infrastructure(f"could not read checks for {url}: {view.stderr.strip() or 'gh pr view failed'}")
    fields = view.stdout.rstrip("\n").split("\t")
    names = advisory = None
    if len(fields) == (8 if suffix else 7):
        parsed = json_field(fields[6])
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            names = tuple(item for item in parsed if item)
        advisory = advisory_rows(json_field(fields[7])) if suffix else ()
    if (names is None or advisory is None or not fields[1].isdigit() or not fields[3].isdigit()
            or fields[4] not in ("true", "false")):
        raise seam.Infrastructure(f"could not read checks for {url}: unparseable check rollup")
    pending, failed = int(fields[1]), fields[2]
    contexts = seam.read_required_contexts(store) if advisory else ()
    kept = []
    for name, result, kind in advisory:
        if contexts is not None and name not in contexts:
            kept.append((name, result, kind))
        elif kind == "passed":
            continue
        elif kind == "failed":
            failed = f"{failed},{name}" if failed else name
        else:
            pending += 1
    blocker, detail = classify_rollup(fields[0], pending, failed, int(fields[3]))
    return CheckRead(blocker, detail, fields[4] == "true", fields[5], names, tuple(kept))

def record_advisory(store, ident, candidate, checks):
    """Keep the advisory results of this check read for status. A contract with no advisorySuffix writes nothing."""
    if not store.contract.get("advisorySuffix"):
        return
    with store.tx() as db:
        row = db.execute("SELECT value FROM contract WHERE key = ?", (ADVISORY_RESULTS,)).fetchone()
        raw = json.loads(row["value"]) if row else {}
        raw[str(ident)] = {"candidate": candidate, "checks": [list(item[:2]) for item in checks.advisory]}
        db.execute("INSERT OR REPLACE INTO contract VALUES (?, ?)", (ADVISORY_RESULTS, json.dumps(raw)))

def advisory_results(store, row):
    """[name, result] pairs last read for this entry's candidate, or [] while the contract sets no advisorySuffix."""
    if not store.contract.get("advisorySuffix"):
        return []
    record = (store.contract.get(ADVISORY_RESULTS) or {}).get(str(row["id"]))
    if not isinstance(record, dict) or record.get("candidate") != row["candidate"]:
        return []
    return [list(item) for item in record.get("checks") or []]
