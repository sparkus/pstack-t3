import argparse
from landing_lib.foundation import LEGACY_MODES as _DEFAULT_LEGACY_MODES
from landing_lib.foundation import MERGE_METHODS as _DEFAULT_MERGE_METHODS
from landing_lib.foundation import MODES as _DEFAULT_MODES
from landing_lib.governor import EXCLUSIVE_CAP as _DEFAULT_EXCLUSIVE_CAP
from landing_lib.store import AUTHOR_POLICIES as _DEFAULT_AUTHOR_POLICIES


def build_parser(description, exclusive_cap=_DEFAULT_EXCLUSIVE_CAP, modes=_DEFAULT_MODES,
                 legacy_modes=_DEFAULT_LEGACY_MODES, merge_methods=_DEFAULT_MERGE_METHODS,
                 author_policies=_DEFAULT_AUTHOR_POLICIES):
    top = argparse.ArgumentParser(prog="land.py", description=description)
    top.add_argument("--repo", default=".", help="any checkout of the repository (default: current directory)")
    sub = top.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write the repository's landing contract")
    p.add_argument("--trunk", required=True, help="branch on the remote, or a lane name in local mode")
    p.add_argument("--mode", choices=modes + tuple(legacy_modes), required=True,
                   help="human: PRs you merge; merge: PRs the queue merges; push: no PRs, the queue pushes trunk; local: a lane ref")
    p.add_argument("--remote", default="origin")
    p.add_argument("--base", default="", help="local mode: the commit the lane starts from")
    p.add_argument("--check", action="append", default=None,
                   help="command that must pass before landing; repeatable. Omitted, a rerun keeps the stored checks and a first init stores none")
    p.add_argument("--setup", default=None,
                   help="command run before checks in the clean integration worktree, such as npm ci. Omitted, a rerun keeps the stored setup and a first init stores none")
    p.add_argument("--batch", type=int, default=None,
                   help="entries checked together; a failed batch lands one at a time. Omitted, a rerun keeps the stored batch and a first init stores 1")
    p.add_argument("--timeout", type=int, default=None,
                   help="seconds before a check is killed. Omitted, a rerun keeps the stored timeout and a first init stores 1800")
    p.add_argument("--merge-method", choices=merge_methods, default=None,
                   help="merge mode: how the queue merges its PRs, checked against the repository when gh can report the "
                        "allowed methods, whenever it is set. "
                        "omitted in merge mode, a first init stores the only allowed method, or prefers merge then squash "
                        "when several are allowed, or merge when gh cannot say, and a rerun keeps the stored method while "
                        "the repository allows it")
    p.add_argument("--author-policy", choices=author_policies, default=None,
                   help="how the queue judges a reviewed commit's author. any skips the check. configured and credited "
                        "check only when the repository has a configured identity: configured requires that author, and "
                        "credited also lands an entry whose commits by another author each carry a Co-authored-by trailer "
                        "equal to that commit's author as name <email> and whose body has a line that reads Based on #<n> by "
                        "@<login>, optionally with a trailing period. Omitted, a rerun keeps the stored policy or stores any "
                        "when the contract has none, and a first init stores any. An unknown --author-policy value is refused "
                        "before the contract changes")
    p.add_argument("--pr-item-line", choices=("on", "off"), default=None,
                   help="whether the queue appends the Kind and Goal line to the PR body. on adds the line when the item has one, "
                        "after one blank line when the body has text or as the whole body otherwise. off sends the submitted title and body "
                        "unchanged. Omitted, a rerun keeps the stored setting and a first init stores off. An unknown value is refused before the contract changes")
    p.add_argument("--heartbeat-ms", type=int, default=None, dest="heartbeat_ms",
                   help="fallback schedule interval in milliseconds. Omitted, a rerun keeps the stored value and a first init stores 3600000. "
                        "A value below 3600000 is refused before the contract changes")
    p.add_argument("--merge-heartbeat-ms", type=int, default=None, dest="merge_heartbeat_ms",
                   help="merge wait schedule interval in milliseconds. Omitted, a rerun keeps the stored value and a first init stores 900000. "
                        "A value below 900000 is refused before the contract changes")
    p.add_argument("--advisory-suffix", default=None, dest="advisory_suffix",
                   help="merge mode: a posted check whose name ends in this text, such as (Observe), is advisory. Its failure "
                        "or pending run never bounces or holds an entry, unless the trunk requires that check by name or the required checks cannot be read. "
                        "status E<n> and status --holder --json show its result. An empty value clears it. Omitted, a rerun "
                        "keeps the stored suffix and a first init stores none, so every failed check bounces as before")
    p.add_argument("--cap", type=int, help="most changes in flight on the repository at once; 0 clears it")
    p.add_argument("--generated", metavar="PATHS", default=None,
                   help="comma-separated repository-relative paths the queue treats as generated output, such as skills,docs/cli. "
                        "An empty value clears the stored list. Omitted, a rerun keeps the stored list and a first init stores none")
    p.add_argument("--generated-map", metavar="MAP", default=None,
                   help="comma-separated src equals dst rules the fire expansion uses, with a trailing ! for index pages "
                        "and a trailing ^ to skip doc items, such as src=dst!^ for a catalog page. "
                        "An empty value clears the stored map. Omitted, a rerun keeps the stored map and a first init stores none")
    p.add_argument("--rebuild", metavar="COMMAND", default=None,
                   help="command that regenerates the generated paths while landing, such as \"python3 scripts/build.py\". "
                        "An empty value clears the stored command. Omitted, a rerun keeps the stored command and a first init stores none")
    p.add_argument("--max-file-tokens", type=int, default=None, dest="max_file_tokens",
                   help="token maximum using bytes // 4. "
                        "Omitted keeps the stored limit; a first init stores none")
    p.add_argument("--size-baseline", metavar="PATH", default=None, dest="size_baseline",
                   help="repository-relative baseline path. "
                        "Omitted keeps the stored path; the fallback is size-baseline.json")

    p = sub.add_parser("cap", help="set the most changes in flight on the repository at once; 0 clears it")
    p.add_argument("count", type=int)

    mode_help = "switch human, merge, or push when the queue is empty. --merge-method may change while entries are in flight"
    p = sub.add_parser("mode", help=mode_help, description=mode_help)
    p.add_argument("mode", choices=modes + tuple(legacy_modes))
    p.add_argument("--merge-method", choices=merge_methods)

    times_help = ("report submit-to-landed minutes for the last N landed entries, one line per entry then a summary line. "
                  "A line holds the entry's id, holder, PR, and minutes to one decimal, and the PR prints - when the entry has none. "
                  "The summary line is n=<count> median=<m> p90=<m> max=<m> skipped=<k>, where median and p90 are the "
                  "nearest-rank 50th and 90th percentiles. An entry in state landed with no landed log row is not a line and is "
                  "counted in skipped, even outside the --last window. --holder counts only that holder's entries. "
                  "With nothing landed the three statistics print - and the command exits 0")
    p = sub.add_parser("times", help=times_help, description=times_help)
    p.add_argument("--last", type=int, default=30, metavar="N",
                   help="report at most this many of the most recently landed entries (default: 30)")
    p.add_argument("--holder", metavar="PREFIX",
                   help="report this holder's entries; a value ending in / matches every holder under it")
    p.add_argument("--json", action="store_true",
                   help="print one JSON object instead of the lines; it holds entries, n, median, p90, max, and skipped, "
                        "an empty PR is null, and the statistics are null when nothing landed")

    owner_help = "<holder prefix>@<generation> of the coordinator writing; refused below that prefix's floor"
    ruling_help = "<prefix>@<generation> of the writer, such as .admin/@2; refused below that prefix's floor"
    p = sub.add_parser("owner", help="raise a holder prefix's generation floor; it never lowers")
    p.add_argument("--prefix", required=True, help="a holder prefix ending in /, such as docs/")
    p.add_argument("--generation", type=int, required=True)

    p = sub.add_parser("lease", help="claim, renew, widen, release, or list path leases")
    t = p.add_subparsers(dest="action", required=True)
    a = t.add_parser("claim")
    a.add_argument("--holder", required=True)
    a.add_argument("--paths", required=True, help="comma-separated files or directories; . is the whole repository")
    a.add_argument("--ttl-hours", type=float, default=6)
    a.add_argument("--owner", help=owner_help)
    a = t.add_parser("renew")
    a.add_argument("id")
    a.add_argument("--ttl-hours", type=float, default=6)
    a.add_argument("--if-live", action="store_true", help="refuse when the lease has expired instead of admitting it again")
    a.add_argument("--owner", help=owner_help)
    a = t.add_parser("widen", help="add paths to a live lease in place; its id and holder stay, and an expired lease is admitted again")
    a.add_argument("id")
    a.add_argument("--paths", required=True, help="comma-separated files or directories to add")
    a.add_argument("--ttl-hours", type=float, default=6)
    a.add_argument("--owner", help=owner_help)
    a = t.add_parser("release")
    a.add_argument("id")
    a.add_argument("--owner", help=owner_help)
    t.add_parser("list")
    a = t.add_parser("check", help="print what a claim would answer now; claims no lease and arms no reservation")
    a.add_argument("--holder", required=True)
    a.add_argument("--paths", required=True)
    a = t.add_parser("reserve", help="reserve paths for a holder prefix by a ruling; prints S<n>")
    a.add_argument("--for", dest="prefix", required=True, help="the winner's holder prefix, such as docs/")
    a.add_argument("--paths", required=True, help="comma-separated files or directories")
    a.add_argument("--ruling", required=True, help="the ruling this carries out, such as R4; a rerun returns the same S<n>")
    a.add_argument("--ttl-hours", type=float, default=2, help="hours the reservation stands once it arms")
    a.add_argument("--owner", required=True, help=ruling_help)
    a = t.add_parser("unreserve", help="lift a reservation")
    a.add_argument("id")
    a.add_argument("--owner", required=True, help=ruling_help)

    p = sub.add_parser("submit", help="queue a reviewed commit for landing")
    p.add_argument("--holder", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--sha", required=True, help="the exact SHA the review passed")
    p.add_argument("--lease", required=True)
    p.add_argument("--reviewer", required=True, help="provider/model of the reviewer that passed this SHA")
    p.add_argument("--title", default="", help="the PR title in merge and human mode (default: the last commit subject)")
    p.add_argument("--body-file", default="", help="a file holding the PR body in merge and human mode")
    p.add_argument("--owner", help=owner_help)

    p = sub.add_parser("share", help="set a holder prefix's share of the cap")
    p.add_argument("--for", dest="prefix", required=True, help="a holder prefix ending in /, such as docs/")
    p.add_argument("count", type=int)
    p.add_argument("--clear", action="store_true", help="remove the share; pass 0")
    p.add_argument("--owner", required=True, help=ruling_help)

    p = sub.add_parser("contest", help="hold two holders' entries, then order them; waits for a running land")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--holders", help="<a>,<b>: hold both holders' entries until settled; prints C<n>")
    g.add_argument("--settle", help="C<n>: with --first, land the first holder before the other")
    g.add_argument("--cancel", help="C<n>: remove the hold or the order")
    p.add_argument("--first", help="with --settle, the holder that lands first")
    p.add_argument("--owner", required=True, help=ruling_help)

    sub.add_parser("land", help="drain the queue unless another run holds it")
    sub.add_parser("resume", help="clear a pause after you did what its reason says")
    prune_help = ("close the open PR and delete the remote branch of every landed or bounced entry with a canonical branch still on the remote. "
                  "A canonical branch names landing/e<n> or landing/q<n> with n in [1-9][0-9]* in ASCII and no leading zero. "
                  "A branch with another name or with a missing entry or with an entry still in flight is left alone")
    p = sub.add_parser("prune", help=prune_help, description=prune_help)
    p.add_argument("--dry-run", action="store_true", help="print one line per settled branch and change nothing")
    p = sub.add_parser("status")
    p.add_argument("id", nargs="?")
    p.add_argument("--holder", help="list this holder's entries; a value ending in / matches every holder under it")
    p.add_argument("--sha", help="with --holder, only entries at exactly this commit")
    p.add_argument("--json", action="store_true",
                   help="print JSON instead of the lines. With --holder it is a list of that holder's entries, one object each holding id, state, holder, sha, and note, plus advisory, a list of name and result objects, only when the queue read advisory checks for that entry's candidate. Alone it is one object holding mode, trunk, generated, rebuild, map, plus size keys only while the contract sets a limit and advisorySuffix only while the contract sets one")

    size_help = ("check changed worktree file sizes; --staged reads changed index files; "
                 "--all reads tracked and unignored untracked files. Seed a missing baseline, except with --shrink, which refuses.\n\n"
                 "In a pstack-t3 checkout with no hook, run `h=$(git rev-parse --git-path hooks/pre-commit) && test ! -e \"$h\" && printf '#!/bin/sh\\nexec python3 skills/landing/scripts/land.py size --staged\\n' > \"$h\" && chmod +x \"$h\"`.")
    p = sub.add_parser("size", help=size_help, description=size_help)
    p.add_argument("--staged", action="store_true", help="judge staged files from the index instead of the worktree")
    p.add_argument("--all", action="store_true", help="judge tracked and unignored untracked files")
    p.add_argument("--shrink", action="store_true",
                   help="lower stored allowances that fell, never raising any, before judging")
    p.add_argument("--baseline", metavar="PATH", default=None,
                   help="baseline file to read and write instead of the contract one")
    p = sub.add_parser("slot", help="run a heavy command under a governor slot")
    p.add_argument("--exclusive", action="store_true", help="hold every slot: for benchmarks that need a quiet machine")
    p.add_argument("--slots", metavar="N", default=None,
                   help="take N worker slots in one admission. The default is 1. "
                        "A value above the governor's worker slots is refused")
    p.add_argument("--for", dest="slot_for", metavar="SECONDS", default=None,
                   help=f"cap this exclusive hold at SECONDS. The default is {exclusive_cap:g}. "
                        "The maximum is the governor exclusiveMax, 7200 by default. A value over the default needs --reason")
    p.add_argument("--reason", dest="slot_reason", metavar="TEXT", default=None,
                   help="why this exclusive hold may pass the default cap. Waiters see it")
    p.add_argument("--repo", dest="slot_repo", metavar="REPO", default=None,
                   help="the repository to name in the holder record (default: current directory)")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    return top
