import json
import os
import runpy
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "t3/added/brigade/scripts/brigade.py"
CODEX = "codex/gpt-6.1-sol"
CLAUDE = "claudeAgent/claude-opus-5-5"


def _wait_for_path(path, timeout=8):
    end = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > end:
            raise TimeoutError(path)
        time.sleep(0.005)


def _shown_root(path):
    """The project root walk prints, with the home directory as ~."""
    path = Path(path).resolve()
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    if not relative.parts:
        return "~"
    return "~/" + relative.as_posix()


def _owner_words(directory):
    """The --owner a coordinator reads from status, as the skill's service step 1 does."""
    try:
        meta = json.loads((Path(directory) / "restaurant.json").read_text())
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(meta, dict) or meta.get("generation") is None:
        return ()
    return ("--owner", f"{meta.get('thread') or ''}@{meta['generation']}")


def _race_child(mode, case, args):
    case = Path(case)
    if mode == "land-pause":
        land = runpy.run_path(str(ROOT / "t3/added/landing/scripts/land.py"))
        store = land["run"].__globals__["Store"]
        original_tx = store.tx

        def paused_tx(self):
            (case / "paused").touch()
            _wait_for_path(case / "proceed", timeout=60)
            return original_tx(self)

        store.tx = paused_tx
        sys.exit(land["main"](args))
    mod = runpy.run_path(str(SCRIPT))
    # run_path returns a copy. The live functions read the original globals.
    glob = mod["run"].__globals__
    if mode == "owner":
        original = glob["write_atomic"]

        def hooked(path, content):
            original(path, content)
            if Path(path).name == "restaurant.json":
                (case / "metadata-ready").touch()
                _wait_for_path(case / "owner-proceed")

        glob["write_atomic"] = hooked
    elif mode == "loser":
        original_wait = glob["wait_for_meta"]

        def hooked_wait(directory):
            meta = original_wait(directory)
            (case / "loser-saw-meta").touch()
            _wait_for_path(case / "loser-after-meta")
            return meta

        glob["wait_for_meta"] = hooked_wait
        original_write = glob["write_atomic"]

        def hooked_write(path, content):
            if Path(path).name in ("menu.md", "house-rules.md"):
                (case / "loser-at-scaffold").touch()
                _wait_for_path(case / "loser-scaffold-proceed")
            return original_write(path, content)

        glob["write_atomic"] = hooked_write
        original_save = glob["Restaurant"].save_rows

        def hooked_save(self, table, rows):
            if table == "rail.tsv" and rows == []:
                (case / "loser-at-scaffold").touch()
                _wait_for_path(case / "loser-scaffold-proceed")
            return original_save(self, table, rows)

        glob["Restaurant"].save_rows = hooked_save
    elif mode == "append":
        directory, label = args
        restaurant = glob["Restaurant"](directory)
        _wait_for_path(case / "go")
        for number in range(200):
            restaurant.append("log.tsv", {"at": glob["now"](), "kind": "ticket", "id": f"{label}{number}",
                                          "state": "waiting", "note": f"{label} row {number}"})
        sys.exit(0)
    elif mode == "die-before-publish":
        original = glob["write_atomic"]

        def hooked(path, content):
            if Path(path).parent.name == "inbox":
                os._exit(9)
            return original(path, content)

        glob["write_atomic"] = hooked
    elif mode == "fake-land":
        glob["LAND"] = case / "land.py"
    elif mode == "pause":
        original_locked = glob["Restaurant"].locked

        def paused_locked(self):
            if not getattr(self, "paused_once", False):
                self.paused_once = True
                (case / "paused").touch()
                _wait_for_path(case / "proceed", timeout=60)
            return original_locked(self)

        glob["Restaurant"].locked = paused_locked
    elif mode == "pause-after-finished":
        original_finished = glob["finished_by_coordinator"]

        def paused_finished(admin):
            finished = original_finished(admin)
            (case / "paused").touch()
            _wait_for_path(case / "proceed", timeout=60)
            return finished

        glob["finished_by_coordinator"] = paused_finished
    elif mode == "die-before-log":
        glob["Restaurant"].log = lambda self, *rest: os._exit(9)
    elif mode == "die-before-cursor":
        original_change = glob["Restaurant"].change_meta

        def dying_change(self, **fields):
            if "cursors" in fields:
                os._exit(9)
            return original_change(self, **fields)

        glob["Restaurant"].change_meta = dying_change
    elif mode == "die-before-thread":
        original_change = glob["Restaurant"].change_meta

        def dying_thread(self, drop=(), **fields):
            if "thread" in fields:
                os._exit(9)
            return original_change(self, drop, **fields)

        glob["Restaurant"].change_meta = dying_thread
    elif mode == "read16":
        reads = []

        def short_read(fd):
            data = os.read(fd, 16)
            if not reads:
                (case / "paused").touch()
                _wait_for_path(case / "proceed", timeout=60)
            reads.append(data)
            return data

        glob["read_chunk"] = short_read
    elif mode == "hold":
        original_append = glob["Restaurant"].append

        def holding_append(self, table, row):
            original_append(self, table, row)
            if table == "log.tsv" and not getattr(self, "held_once", False):
                self.held_once = True
                (case / "holding").touch()
                _wait_for_path(case / "proceed", timeout=60)

        glob["Restaurant"].append = holding_append
    elif mode == "append-row":
        directory, *fields = args
        restaurant = glob["Restaurant"](directory)
        restaurant.append("log.tsv", dict(zip(("at", "kind", "id", "state", "note"), fields)))
        sys.exit(0)
    elif mode == "die-inside-log-append":
        real_write = os.write

        def partial(fd, data):
            if b"\tfrom " in data:
                real_write(fd, data[:data.index(b"\tfrom ") + len(b"\tfro")])
                os._exit(9)
            return real_write(fd, data)

        os.write = partial
    else:
        raise SystemExit(f"unknown mode {mode}")
    sys.exit(mod["main"](args))


class BrigadeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Path(self.temporary.name) / "store"
        self.project = Path(self.temporary.name) / "Bridge Kit"
        self.project.mkdir()
        self.at = self.store / "bridge-kit" / "perf"

    def tearDown(self):
        self.temporary.cleanup()

    def brigade(self, *args, ok=True, stdin=None, owner=True):
        return self.run_at(self.at, *args, ok=ok, stdin=stdin, owner=owner)

    def run_at(self, directory, *args, ok=True, stdin=None, owner=True):
        if owner and "--owner" not in args:
            args = (*_owner_words(directory), *args)
        result = subprocess.run([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(directory), *args],
                                capture_output=True, text=True, input=stdin)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return (result.stdout if ok else result.stderr).strip()

    def table_row(self, directory, table, ident):
        lines = [line for line in (directory / table).read_text().splitlines() if line]
        keys = lines[0].split("\t")
        for line in lines[1:]:
            row = dict(zip(keys, line.split("\t")))
            if row["id"] == ident:
                return row
        self.fail(f"no {ident} in {directory / table}")

    def blocked_note(self, directory, ident):
        lines = [line for line in (directory / "log.tsv").read_text().splitlines() if line]
        keys = lines[0].split("\t")
        note = None
        for line in lines[1:]:
            row = dict(zip(keys, line.split("\t")))
            if row["kind"] == "ticket" and row["id"] == ident and row["state"] == "blocked":
                note = json.loads(row["note"])
        self.assertIsNotNone(note, ident)
        return note

    def bash_unblocked(self, directory, line):
        command = line.split("run ", 1)[1]
        invocation = " ".join([shlex.quote(sys.executable), shlex.quote(str(SCRIPT)),
                               "--store", shlex.quote(str(self.store)), "--at", shlex.quote(str(directory)),
                               command])
        return subprocess.run(["bash", "-c", invocation], capture_output=True, text=True, env=self.land_env())

    def open(self):
        return self.brigade("open", "--project-root", str(self.project), "--name", "Perf")

    def test_open_creates_the_store_once_and_keeps_edits(self):
        self.assertEqual(self.open(), f"opened {self.at}")
        (self.at / "menu.md").write_text("# Menu: Perf\n\nKeep startup under 400 ms.\n")
        self.assertEqual(self.open(), f"exists {self.at}")
        self.assertEqual((self.at / "menu.md").read_text(), "# Menu: Perf\n\nKeep startup under 400 ms.\n")
        self.assertIn("only through the repository's landing queue", (self.at / "house-rules.md").read_text())
        self.assertIn("Never use them in replies", (self.at / "house-rules.md").read_text())

    def test_fire_groups_waiting_tickets_into_one_dish(self):
        self.open()
        self.brigade("set", "--intake", "github")
        self.assertEqual(self.brigade("ticket", "add", "--summary", "Startup is slow on cold boot", "--source", "github", "--ref", "#12"), "T1")
        self.assertEqual(self.brigade("ticket", "add", "--summary", "Splash screen hangs"), "T2")
        self.assertEqual(self.brigade("fire", "--tickets", "T1,T2", "--station", "perf-issue", "--summary", "Cut cold start time"), "D1")
        self.assertEqual(self.brigade("ticket", "list"),
                         "T1 assigned [github] Startup is slow on cold boot #12\nT2 assigned [user] Splash screen hangs")
        self.assertEqual(self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "again", ok=False),
                         "brigade: T1 is assigned, not waiting")

    def test_pass_refuses_a_verifier_from_the_author_family(self):
        self.open()
        self.brigade("ticket", "add", "--summary", "s")
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Fix s")
        error = self.brigade("pass", "record", "D1", "--pr", "u/1", "--sha", "abc", "--verdict", "pass",
                             "--author", CLAUDE, "--verifier", "cursor/claude-sonnet-5-5", ok=False)
        self.assertIn("same model family", error)
        self.assertEqual(self.brigade("pass", "record", "D1", "--pr", "u/1", "--sha", "abc", "--verdict", "pass",
                                      "--author", CLAUDE, "--verifier", "cursor/claude-sonnet-5-5", "--same-family"), "D1 passed")
        self.assertIn("same model family", (self.at / "pass.tsv").read_text())

    def test_queueing_or_merging_needs_a_pass_at_the_current_sha(self):
        self.open()
        self.brigade("ticket", "add", "--summary", "s")
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Fix s")
        self.brigade("pass", "record", "D1", "--pr", "u/1", "--sha", "abc", "--verdict", "pass", "--author", CLAUDE, "--verifier", CODEX)
        self.assertEqual(self.brigade("dish", "D1", "--state", "queued", "--sha", "def", ok=False),
                         "brigade: only reviewed work lands: D1 has no review verdict for def")
        self.assertEqual(self.brigade("dish", "D1", "--state", "queued"), "D1 queued")
        self.assertEqual(self.brigade("status"), "thread not recorded\nreporting: milestones, no landing contract, waiting to land: 1")
        self.assertEqual(self.brigade("dish", "D1", "--state", "merged"), "D1 merged")
        self.assertEqual(self.brigade("ticket", "list", "--state", "done"), "T1 done [user] s")

    def test_send_back_blocks_landing(self):
        self.open()
        self.brigade("ticket", "add", "--summary", "s")
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Fix s")
        self.brigade("pass", "record", "D1", "--pr", "u/1", "--sha", "abc", "--verdict", "send-back",
                     "--author", CLAUDE, "--verifier", CODEX, "--note", "test asserts the bug")
        self.assertEqual(self.brigade("pass", "check", "D1", "--sha", "abc", ok=False),
                         "brigade: D1 at abc: send-back (test asserts the bug)")
        self.assertEqual(self.brigade("dish", "D1", "--state", "queued", ok=False),
                         "brigade: only reviewed work lands: D1 at abc: send-back (test asserts the bug)")

    def test_report_lists_only_what_changed_since_the_last_report(self):
        self.open()
        self.brigade("ticket", "add", "--summary", "Startup is slow")
        self.brigade("ticket", "add", "--summary", "Splash hangs")
        self.brigade("fire", "--tickets", "T1", "--station", "perf-issue", "--summary", "Cut cold start time")
        self.brigade("86", "add", "--question", "Drop Windows 7 support?", "--options", "yes, no", "--default", "no")
        first = self.brigade("close")
        self.assertIn("## In progress\n\n- D1 (T1): Cut cold start time\n", first)
        self.assertIn("## New tickets, not started\n\n- T2: Splash hangs\n", first)
        self.assertIn("- Q1: Drop Windows 7 support? Options: yes, no. Default if no answer: no.", first)
        self.brigade("pass", "record", "D1", "--pr", "https://github.com/o/r/pull/7", "--sha", "abc", "--verdict", "pass",
                     "--author", CLAUDE, "--verifier", CODEX)
        self.brigade("dish", "D1", "--state", "merged")
        self.brigade("86", "answer", "Q1", "--answer", "no")
        second = self.brigade("close")
        self.assertIn("## Merged\n\n- D1 (T1): Cut cold start time https://github.com/o/r/pull/7", second)
        self.assertNotIn("In progress", second)
        self.assertNotIn("Passed review", second)
        self.assertNotIn("Decisions for you", second)
        self.assertIn("Nothing new.", self.brigade("close"))
        self.assertEqual(len(list((self.at / "closeouts").glob("*.md"))), 3)

    def test_a_dish_that_moves_through_every_state_in_one_report_appears_once(self):
        self.open()
        self.brigade("ticket", "add", "--summary", "s")
        self.brigade("ticket", "add", "--summary", "UI", "--source", "user")
        self.brigade("ticket", "set", "T2", "--state", "dropped")
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Fix s")
        self.brigade("dish", "D1", "--state", "in-review", "--sha", "abc")
        self.brigade("pass", "record", "D1", "--sha", "abc", "--verdict", "pass", "--author", "grok/grok-4.7", "--verifier", CODEX)
        self.brigade("dish", "D1", "--state", "merged")
        self.assertEqual(self.brigade("close"), "\n".join([
            "# Perf report", "",
            f"Since opening. merged: 1.", "",
            "## Merged", "", "- D1 (T1): Fix s", "",
            "## Dropped", "", "- T2: UI",
        ]))

    def test_close_to_file_prints_the_written_path_and_advances_the_report(self):
        self.open()
        self.brigade("ticket", "add", "--summary", "s")
        self.brigade("ticket", "add", "--summary", "UI", "--source", "user")
        self.brigade("ticket", "set", "T2", "--state", "dropped")
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Fix s")
        self.brigade("dish", "D1", "--state", "in-review", "--sha", "abc")
        self.brigade("pass", "record", "D1", "--sha", "abc", "--verdict", "pass", "--author", "grok/grok-4.7", "--verifier", CODEX)
        self.brigade("dish", "D1", "--state", "merged")
        written = self.brigade("close", "--to-file")
        files = list((self.at / "closeouts").glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertEqual(written, str(files[0].resolve()))
        self.assertIn("## Merged\n\n- D1 (T1): Fix s", files[0].read_text())
        self.assertIn("Nothing new.", self.brigade("close"))

    def test_close_to_file_with_dry_run_writes_no_closeout(self):
        self.open()
        self.brigade("close", "--to-file", "--dry-run", ok=False)
        self.assertEqual(list((self.at / "closeouts").glob("*.md")), [])

    def test_status_help_names_the_thread_line_then_counts(self):
        env = os.environ | {"COLUMNS": "200"}
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status              the thread line first, then counts, then reports to and owner when present", result.stdout)
        self.assertNotIn("one line of counts", result.stdout)

    def test_status_and_walk_speak_plain_engineering_prose(self):
        self.open()
        self.brigade("set", "--thread", "thread-1", "--schedule", "report=s-1")
        self.brigade("ticket", "add", "--summary", "s")
        self.brigade("86", "add", "--question", "Ship it?", "--options", "yes, no", "--default", "no")
        self.assertEqual(self.brigade("status"),
                         "thread thread-1\nreporting: milestones, no landing contract, waiting tickets: 1, decisions for you: 1\nowner thread-1@1")
        self.assertEqual(self.brigade("walk"), "\n".join([
            f"{_shown_root(self.project)}: no landing contract",
            "  Perf (reports milestones): waiting tickets: 1, decisions for you: 1",
            "    thread thread-1",
            "    Q1: Ship it?",
        ]))
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["schedules"], {"report": "s-1"})
        for word in ("86", "plate", "heard", "chef", "fire", "pass "):
            self.assertNotIn(word, self.brigade("status").lower())

    def test_status_without_a_contract_ignores_a_stored_landing_field(self):
        self.open()
        self.assertNotIn("landing", json.loads((self.at / "restaurant.json").read_text()))
        self.assertEqual(self.brigade("status"), "thread not recorded\nreporting: milestones, no landing contract")
        meta = json.loads((self.at / "restaurant.json").read_text())
        meta["landing"] = "merge"
        (self.at / "restaurant.json").write_text(json.dumps(meta, indent=2) + "\n")
        self.assertEqual(self.brigade("status"), "thread not recorded\nreporting: milestones, no landing contract")
        walked = self.brigade("walk")
        self.assertEqual(walked.splitlines()[0], f"{_shown_root(self.project)}: no landing contract")
        self.assertNotIn("lands by", walked)

    def test_set_landing_exits_2(self):
        self.open()
        before = (self.at / "restaurant.json").read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.at),
                                 "set", "--landing", "merge"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual((self.at / "restaurant.json").read_bytes(), before)

    def test_walk_prints_a_home_directory_as_a_tilde(self):
        from unittest import mock
        project = Path.home() / "brigade-no-such-contract-d57"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            directory = self.store / "brigade-no-such-contract-d57" / "docs"
            self.assertEqual(self.brigade("open", "--project-root", str(project), "--name", "Docs"), f"opened {directory}")
            self.assertEqual(self.brigade("walk"), "\n".join([
                "~/brigade-no-such-contract-d57: no landing contract",
                "  Docs (reports milestones): nothing on record",
                "    thread not recorded",
            ]))

    def test_status_and_walk_follow_a_landing_mode_change(self):
        from unittest import mock
        self.git_project()
        remote = Path(self.temporary.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.project, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.project, check=True)
        self.land("init", "--trunk", "main", "--mode", "human")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.open()
            self.land("mode", "merge")
            self.assertEqual(self.brigade("status"), "thread not recorded\nreporting: milestones, lands by merge")
            header = self.brigade("walk").splitlines()[0]
            self.assertEqual(header, f"{_shown_root(self.project)}: {self.land('status')}")
            self.assertTrue(header.startswith(f"{_shown_root(self.project)}: merge mode onto "))

    def test_walk_groups_repositories_and_repo_selects_one(self):
        from unittest import mock
        other = Path(self.temporary.name) / "other-app"
        other.mkdir()
        self.init_landing("--cap", "4")
        run = lambda *command: subprocess.run(command, cwd=other, capture_output=True, text=True, check=True)
        run("git", "init", "-q", "-b", "main")
        (other / "a.txt").write_text("a\n")
        run("git", "add", "-A")
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.land_repo(other, "init", "--trunk", "lane", "--mode", "local", "--base", "main", "--cap", "4")
        docs = self.store / "bridge-kit" / "docs"
        engine = self.store / "bridge-kit" / "engine"
        core = self.store / "other-app" / "core"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.assertEqual(self.brigade("walk"), f"no restaurants under {self.store}")
            self.run_at(docs, "open", "--project-root", str(self.project), "--name", "Docs")
            self.run_at(engine, "open", "--project-root", str(self.project), "--name", "Engine")
            self.run_at(core, "open", "--project-root", str(other), "--name", "Core")
            self.run_at(docs, "set", "--thread", "thread-docs")
            self.run_at(engine, "set", "--thread", "thread-engine")
            self.run_at(core, "set", "--thread", "thread-core")
            self.run_at(docs, "ticket", "add", "--summary", "Write the guide")
            self.assertEqual(self.run_at(docs, "fire", "--tickets", "T1", "--station", "bug-fix",
                                         "--summary", "Write the guide", "--paths", "a.txt"),
                             "D1 (lease L1 held by docs/D1)")
            self.run_at(engine, "ticket", "add", "--summary", "Blocked edit")
            refused = self.run_at(engine, "fire", "--tickets", "T1", "--station", "bug-fix",
                                  "--summary", "Blocked edit", "--paths", "a.txt", ok=False)
            self.assertIn("nothing fired: paths overlap L1 held by docs/D1", refused)
            self.assertEqual(self.land("lease", "claim", "--holder", "engine/D12", "--paths", "src"), "L2")
            self.assertEqual(self.land("lease", "claim", "--holder", "engine/D13", "--paths", "lib"), "L3")
            self.assertEqual(self.run_at(docs, "86", "add", "--question", "Ship the guide?",
                                         "--options", "yes, no", "--default", "no"), "Q1")
            app = _shown_root(self.project)
            other_root = _shown_root(other)
            expected = "\n".join([
                f"{app}: local mode onto refs/landing/lane. leases held: 3, changes in flight: 3 of 4.",
                "  Docs (reports milestones): in progress: 1, decisions for you: 1",
                "    thread thread-docs, leases L1 (D1)",
                "    Q1: Ship the guide?",
                "  Engine (reports milestones): waiting tickets: 1 (1 blocked)",
                "    thread thread-engine, leases L2 (D12), L3 (D13)",
                f"{other_root}: local mode onto refs/landing/lane. changes in flight: 0 of 4.",
                "  Core (reports milestones): nothing on record",
                "    thread thread-core",
            ])
            self.assertEqual(self.brigade("walk"), expected)
            self.assertEqual(self.brigade("walk", "--repo", str(self.project)), "\n".join(expected.splitlines()[:6]))
            self.assertEqual(self.brigade("walk", "--repo", str(other)), "\n".join(expected.splitlines()[6:]))
            missing = Path(self.temporary.name) / "empty-repo"
            missing.mkdir()
            self.assertEqual(self.brigade("walk", "--repo", str(missing)), f"no restaurants for {missing.resolve()}")

    def fire_one(self, station="perf-issue", timebox="60"):
        (self.at / "menu.md").write_text("# Menu: Perf\n\n## Purpose\n\nMake startup fast.\n\n## Budget\n\nsmall\n")
        self.brigade("ticket", "add", "--summary", "Startup is slow")
        return self.brigade("fire", "--tickets", "T1", "--station", station, "--summary", "Cut startup", "--branch", "perf/d1",
                            "--thread", "thread-9", "--timebox", timebox)

    def test_a_state_is_logged_only_when_it_changes(self):
        self.open()
        self.fire_one()
        self.brigade("dish", "D1", "--state", "in-progress", "--task", "t-1")
        self.brigade("pass", "record", "D1", "--sha", "abc", "--verdict", "pass", "--author", "grok/grok-4.7", "--verifier", CODEX)
        self.brigade("dish", "D1", "--state", "merged")
        self.brigade("ticket", "set", "T1", "--state", "done")
        log = (self.at / "log.tsv").read_text()
        self.assertEqual(log.count("\tdish\tD1\tin-progress\t"), 1)
        self.assertEqual(log.count("\tticket\tT1\tdone\t"), 1)

    def test_brief_assembles_every_field_and_adds_the_exclusive_rule_for_measuring_stations(self):
        self.open()
        self.fire_one()
        self.brigade("set", "--thread", "thread-coord")
        text = self.brigade("brief", "D1", "--goal", "Cold start under 400 ms.", "--acceptance", "Median cold start below 400 ms",
                            "--verify", "npm run perf", "--paths", "src/boot.ts", "--lease", "L4", "--base", "origin/main")
        self.assertTrue(text.startswith("Use the poteto-mode skill and its `perf-issue` playbook."))
        report = f"{self.at}/reports/D1.md"
        for part in ("PURPOSE: Make startup fast.", "TICKETS: T1: Startup is slow", "branch `perf/d1`, started from `origin/main`",
                     "leased to you as L4: src/boot.ts", "- Median cold start below 400 ms", "slot --exclusive --",
                     "TIMEBOX: 60 minutes. The timebox orders the work and never waives a playbook step (How, Architect, investigation, or the implementation delegate). At the limit, write the report with what remains instead of skipping steps.",
                     f"Write it to {report}", "1. Write in plain engineering prose.",
                     f'call t3_thread_send to thread thread-coord with mode "auto" and the one-line message "D1 done: report at {report}".'):
            self.assertIn(part, text)
        self.assertEqual((self.at / "briefs/D1.md").read_text().strip(), text)

    def test_brief_carries_the_workers_mode_seat_rule_and_brigade_gate(self):
        self.open()
        self.fire_one()
        self.brigade("set", "--thread", "thread-coord")
        args = ("brief", "D1", "--goal", "Cold start under 400 ms.", "--acceptance", "Median cold start below 400 ms",
                "--verify", "npm run perf", "--paths", "src/boot.ts", "--lease", "L4", "--base", "origin/main")
        lines = self.brigade(*args).splitlines()
        self.assertTrue(lines[2].startswith("Mode: "))
        self.assertTrue(lines[3].startswith("Mode source: "))
        self.assertTrue(lines[4].startswith("Seat rule. Copy the Mode value above"))
        self.assertEqual(lines[5], "Gate: brigade")
        self.brigade("dish", "D1", "--state", "in-progress", "--task", "t-1")
        for sha in ("abc", "def"):
            self.brigade("pass", "record", "D1", "--sha", sha, "--verdict", "send-back", "--author", "grok/grok-4.7", "--verifier", CODEX)
        lines = self.brigade(*args).splitlines()
        self.assertEqual(lines[2:4], ["Mode: full", "Mode source: escalated: second send-back"])

    def test_brief_refuses_when_no_coordinator_thread_is_recorded(self):
        self.open()
        self.fire_one(station="bug-fix")
        error = self.brigade("brief", "D1", "--goal", "g", "--acceptance", "a", "--verify", "v",
                             "--paths", "a", "--lease", "L1", "--base", "origin/main", ok=False)
        self.assertIn("brigade.py set --thread", error)
        self.assertFalse((self.at / "briefs" / "D1.md").exists())

    def test_brief_refuses_a_menu_without_a_purpose_or_a_missing_acceptance(self):
        self.open()
        self.brigade("ticket", "add", "--summary", "s")
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Fix s")
        self.brigade("set", "--thread", "thread-coord")
        args = ["brief", "D1", "--goal", "g", "--verify", "v", "--paths", "a", "--lease", "L1", "--base", "origin/main"]
        self.assertIn("menu.md has no purpose yet", self.brigade(*args, "--acceptance", "a", ok=False))
        (self.at / "menu.md").write_text("## Purpose\n\nFix bugs.\n")
        self.assertIn("at least one --acceptance", self.brigade(*args, ok=False))
        self.assertNotIn("slot --exclusive", self.brigade(*args, "--acceptance", "a"))

    def test_watch_reports_running_finished_and_overdue_work(self):
        self.open()
        self.assertEqual(self.brigade("watch"), "no work in progress")
        self.fire_one(timebox="30")
        self.assertEqual(self.brigade("watch"), "D1: running 0m of 30m (thread thread-9)")
        log = self.at / "log.tsv"
        log.write_text(log.read_text().replace(f"{__import__('datetime').date.today().year}-", "2020-"))
        self.assertIn("D1: over its 30m timebox", self.brigade("watch"))
        (self.at / "reports").mkdir()
        (self.at / "reports/D1.md").write_text("done")
        self.assertEqual(self.brigade("watch"), "D1: report written, no report-back (thread thread-9)")
        self.assertEqual(self.brigade("dish", "D1", "--reported"), "D1 in-progress")
        self.assertEqual(self.brigade("watch"), "D1: report written 0m ago; review it even if the worker's run is still open (thread thread-9)")
        self.brigade("dish", "D1", "--state", "sent-back")
        self.brigade("dish", "D1", "--state", "in-progress")
        self.assertEqual(self.brigade("watch"), "D1: in progress with no worker thread; launch a fresh worker")
        self.brigade("dish", "D1", "--thread", "thread-10")
        (self.at / "reports/D1.md").write_text("second attempt")
        self.assertEqual(self.brigade("watch"), "D1: report written, no report-back (thread thread-10)")

    def test_a_send_back_clears_the_worker_and_refuses_the_old_thread(self):
        self.open()
        self.fire_one()
        self.brigade("dish", "D1", "--state", "sent-back")
        self.assertEqual(self.brigade("dish", "D1", "--thread", "thread-8"), "D1 sent-back")
        self.assertEqual(self.brigade("dish", "D1", "--thread", "thread-9", ok=False),
                         "brigade: thread thread-9 is an earlier attempt of D1; a send-back launches a fresh worker")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["thread"], "thread-8")
        refused = self.brigade("dish", "D1", "--state", "in-progress", "--thread", "thread-9", ok=False)
        self.assertEqual(refused, "brigade: thread thread-9 is an earlier attempt of D1; a send-back launches a fresh worker")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["state"], "sent-back")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["thread"], "thread-8")
        self.assertEqual(self.brigade("dish", "D1", "--state", "in-progress", "--thread", "thread-10"), "D1 in-progress")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["thread"], "thread-10")
        self.brigade("dish", "D1", "--state", "sent-back")
        self.assertEqual(self.brigade("dish", "D1", "--state", "in-progress"), "D1 in-progress")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["thread"], "")
        self.assertEqual(self.brigade("watch"), "D1: in progress with no worker thread; launch a fresh worker")
        self.assertEqual(self.brigade("dish", "D1", "--thread", "thread-9", ok=False),
                         "brigade: thread thread-9 is an earlier attempt of D1; a send-back launches a fresh worker")
        self.assertEqual(self.brigade("dish", "D1", "--thread", "thread-10", ok=False),
                         "brigade: thread thread-10 is an earlier attempt of D1; a send-back launches a fresh worker")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["thread"], "")
        self.assertEqual(self.brigade("dish", "D1", "--thread", "thread-11"), "D1 in-progress")
        self.assertEqual(self.brigade("dish", "D1", "--thread", "thread-11"), "D1 in-progress")

    def test_replacing_an_in_progress_worker_starts_a_new_attempt(self):
        self.open()
        self.fire_one(timebox="60")
        log = self.at / "log.tsv"
        log.write_text(log.read_text().replace(f"{__import__('datetime').date.today().year}-", "2020-"))
        report = self.at / "reports" / "D1.md"
        report.parent.mkdir()
        report.write_text("partial from the overdue worker")
        self.assertEqual(self.brigade("watch"), "D1: report written, no report-back (thread thread-9)")
        self.assertEqual(self.brigade("dish", "D1", "--state", "in-progress", "--thread", "fresh-worker"), "D1 in-progress")
        self.assertEqual(self.brigade("watch"), "D1: running 0m of 60m (thread fresh-worker)")
        report.write_text("partial from the fresh worker")
        self.assertEqual(self.brigade("watch"), "D1: report written, no report-back (thread fresh-worker)")
        self.brigade("dish", "D1", "--timebox", "90", "--task", "t-2")
        self.assertEqual(self.brigade("watch"), "D1: report written, no report-back (thread fresh-worker)")
        self.brigade("dish", "D1", "--state", "in-progress", "--thread", "fresh-worker")
        self.assertEqual(self.brigade("watch"), "D1: report written, no report-back (thread fresh-worker)")
        self.brigade("dish", "D1", "--reported")
        self.assertEqual(self.brigade("watch"), "D1: report written 0m ago; review it even if the worker's run is still open (thread fresh-worker)")
        self.brigade("dish", "D1", "--thread", "worker-3")
        self.assertEqual(self.brigade("watch"), "D1: running 0m of 90m (thread worker-3)")
        self.assertEqual(self.brigade("dish", "D1", "--thread", "thread-9", ok=False),
                         "brigade: thread thread-9 is an earlier attempt of D1; a send-back launches a fresh worker")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["thread"], "worker-3")
        report.write_text("partial from worker-3")
        self.assertEqual(self.brigade("watch"), "D1: report written, no report-back (thread worker-3)")

    def test_an_idle_replacement_with_an_earlier_report_reaches_the_nudge(self):
        self.open()
        self.fire_one(timebox="60")
        log = self.at / "log.tsv"
        log.write_text(log.read_text().replace(f"{__import__('datetime').date.today().year}-", "2020-"))
        report = self.at / "reports" / "D1.md"
        report.parent.mkdir()
        report.write_text("report from the replaced worker")
        self.brigade("dish", "D1", "--state", "in-progress", "--thread", "fresh-worker")
        line = self.brigade("watch")
        text = (ROOT / "t3/added/brigade/SKILL.md").read_text()
        section = text.split("## Liveness check", 1)[1].split("\n## ", 1)[0]
        step = next(row for row in section.splitlines() if row.startswith("5. "))
        trigger = step.split("`", 2)[1].split("Nm")[0].replace("D<n>", "D1")
        self.assertEqual((report.exists(), line), (True, "D1: running 0m of 60m (thread fresh-worker)"))
        self.assertTrue(line.startswith(trigger), (trigger, line))
        self.assertIn("no report for the current attempt", step)
        self.assertNotIn("no report file exists", step)

    def test_fire_claims_the_lease_and_a_refused_claim_fires_nothing(self):
        import os
        from unittest import mock
        run = lambda *a: subprocess.run(a, cwd=self.project, capture_output=True, text=True, check=True)
        run("git", "init", "-q", "-b", "main")
        (self.project / "a.txt").write_text("a\n")
        run("git", "add", "-A")
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        land = ROOT / "t3/added/landing/scripts/land.py"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.temporary.name) / "state")}):
            subprocess.run([sys.executable, str(land), "--repo", str(self.project), "init", "--trunk", "lane", "--mode", "local", "--base", "main"],
                           check=True, capture_output=True)
            self.open()
            (self.at / "menu.md").write_text("## Purpose\n\nFast.\n")
            self.brigade("ticket", "add", "--summary", "one")
            self.brigade("ticket", "add", "--summary", "two")
            self.assertEqual(self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s", "--paths", "src"),
                             "D1 (lease L1 held by perf/D1)")
            self.assertIn("nothing fired: paths overlap L1 held by perf/D1",
                          self.brigade("fire", "--tickets", "T2", "--station", "bug-fix", "--summary", "s", "--paths", "src/x.py", ok=False))
            self.assertEqual(self.brigade("ticket", "list", "--state", "waiting"), "T2 waiting [user] two blocked: lease")
            self.brigade("set", "--thread", "thread-coord")
            text = self.brigade("brief", "D1", "--goal", "g", "--acceptance", "a", "--verify", "v", "--base", "refs/landing/lane")
            self.assertIn("leased to you as L1: src,changes/perf%2Fd1.md.", text)
            self.assertIn("branch `perf/d1`", text)

    def test_fire_claims_outside_the_store_lock_and_rechecks_before_it_writes(self):
        case = Path(self.temporary.name)
        calls = case / "land-calls"
        (case / "land.py").write_text(
            "import sys, time\nfrom pathlib import Path\n"
            f"case = Path({str(case)!r})\n"
            "with open(case / 'land-calls', 'a') as f:\n    f.write(' '.join(sys.argv[3:]) + '\\n')\n"
            "if sys.argv[4] == 'claim':\n"
            "    (case / 'claiming').touch()\n"
            "    end = time.monotonic() + 30\n"
            "    while not (case / 'claim-proceed').exists() and time.monotonic() < end:\n        time.sleep(0.01)\n"
            "    print('L7')\n"
            "else:\n    print('L7 released')\n")
        self.open()
        (self.at / "menu.md").write_text("## Purpose\n\nFast.\n")
        self.brigade("ticket", "add", "--summary", "one")
        firing = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--race-child", "fake-land", str(case),
                                   "--store", str(self.store), "--at", str(self.at),
                                   "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s", "--paths", "src"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        try:
            _wait_for_path(case / "claiming")
            other = subprocess.run([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.at),
                                    "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "other"],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(other.stdout.strip(), "D1", other.stderr)
            (case / "claim-proceed").touch()
            _, err = firing.communicate(timeout=10)
        finally:
            firing.kill()
        self.assertEqual(firing.returncode, 1)
        self.assertEqual(err.strip(), "brigade: nothing fired: T1 is assigned, not waiting")
        self.assertEqual(calls.read_text().splitlines(),
                         ["lease claim --holder perf/D1 --paths src,changes/perf%2Fd1.md", "lease release L7"])
        self.assertEqual(self.brigade("status"), "thread not recorded\nreporting: milestones, no landing contract, in progress: 1")

    def test_tabs_and_newlines_in_input_cannot_break_a_table(self):
        self.open()
        self.brigade("ticket", "add", "--summary", "line one\nline\ttwo")
        self.assertEqual(self.brigade("ticket", "list"), "T1 waiting [user] line one line two")

    def test_commands_outside_a_restaurant_fail_with_the_fix(self):
        self.assertIn("run brigade.py open", self.brigade("status", ok=False))

    def test_set_schedule_drops_a_recorded_name_and_a_missing_one(self):
        self.open()
        self.brigade("set", "--schedule", "liveness=sched-4", "--schedule", "drain=sched-5")
        self.brigade("set", "--schedule", "liveness=")
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["schedules"], {"drain": "sched-5"})
        self.brigade("set", "--schedule", "evening=")
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["schedules"], {"drain": "sched-5"})
        self.brigade("set", "--schedule", "morning=sched-1")
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["schedules"],
                         {"drain": "sched-5", "morning": "sched-1"})
        error = self.brigade("set", "--schedule", "drain", ok=False)
        self.assertIn("NAME=ID", error)
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["schedules"],
                         {"drain": "sched-5", "morning": "sched-1"})

    def test_open_records_reporting_and_defaults_to_milestones(self):
        self.open()
        meta = json.loads((self.at / "restaurant.json").read_text())
        self.assertEqual(meta["reporting"], "milestones")
        self.assertNotIn("landing", meta)
        self.assertEqual(self.brigade("status"), "thread not recorded\nreporting: milestones, no landing contract")
        self.assertIn("reports milestones", self.brigade("walk"))
        self.assertEqual(self.brigade("open", "--project-root", str(self.project), "--name", "Perf",
                                      "--reporting", "every-turn"), f"exists {self.at}")
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["reporting"], "milestones")
        other = self.brigade("open", "--project-root", str(self.project), "--name", "Quiet",
                             "--reporting", "digest")
        quiet = self.store / "bridge-kit" / "quiet"
        self.assertEqual(other, "\n".join([
            f"opened {quiet}",
            f"sibling Perf ({self.at}), thread not recorded",
            "  purpose: not written yet",
            "  off the menu: not written yet",
        ]))
        self.assertEqual(json.loads((quiet / "restaurant.json").read_text())["reporting"], "digest")
        self.assertIn("reports digest", self.brigade("walk"))

    def test_set_reporting_changes_the_level_and_rejects_an_unknown_one(self):
        self.open()
        self.brigade("set", "--reporting", "every-turn")
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["reporting"], "every-turn")
        self.assertEqual(self.brigade("status"), "thread not recorded\nreporting: every-turn, no landing contract")
        self.brigade("set", "--reporting", "digest")
        before = (self.at / "restaurant.json").read_text()
        error = self.brigade("set", "--reporting", "hourly", ok=False)
        for level in ("every-turn", "milestones", "digest"):
            self.assertIn(level, error)
        self.assertEqual((self.at / "restaurant.json").read_text(), before)
        opened = self.brigade("open", "--project-root", str(self.project), "--name", "Loud",
                              "--reporting", "hourly", ok=False)
        for level in ("every-turn", "milestones", "digest"):
            self.assertIn(level, opened)
        self.assertFalse((self.store / "bridge-kit" / "loud").exists())

    def literal_brief_fields(self):
        return {
            "goal": "Run `$B status` and `$B close` before $(touch sentinel).",
            "acceptance": ["Median cold start below 400 ms"],
            "verify": "npm run perf",
            "paths": "src/boot.ts",
            "lease": "L4",
            "base": "origin/main",
            "context": ["notes/startup.md"],
        }

    def brief_from_flags(self, fields):
        return self.brigade("brief", "D1", "--goal", fields["goal"], "--acceptance", fields["acceptance"][0],
                            "--verify", fields["verify"], "--paths", fields["paths"], "--lease", fields["lease"],
                            "--base", fields["base"], "--context", fields["context"][0])

    def test_brief_fields_file_matches_flags_and_keeps_shell_text_literal(self):
        self.open()
        self.fire_one()
        self.brigade("set", "--thread", "thread-coord")
        fields = self.literal_brief_fields()
        flagged = self.brief_from_flags(fields)
        path = self.at / "brief-fields.json"
        path.write_text(json.dumps(fields), encoding="utf-8")
        filed = self.brigade("brief", "D1", "--fields", str(path))
        self.assertEqual(filed, flagged)
        goal = fields["goal"]
        self.assertIn(goal, filed)
        for part in ("`", "$B status", "$B close", "$(touch sentinel)"):
            self.assertIn(part, filed)
        self.assertIsNone(json.loads((self.at / "restaurant.json").read_text())["lastReportAt"])
        self.assertEqual(list((self.at / "closeouts").glob("*")), [])

    def test_brief_fields_stdin_matches_the_file(self):
        self.open()
        self.fire_one()
        self.brigade("set", "--thread", "thread-coord")
        fields = self.literal_brief_fields()
        raw = json.dumps(fields)
        path = self.at / "brief-fields.json"
        path.write_text(raw, encoding="utf-8")
        filed = self.brigade("brief", "D1", "--fields", str(path))
        piped = self.brigade("brief", "D1", "--fields", "-", stdin=raw)
        self.assertEqual(piped, filed)
        self.assertIn("$(touch sentinel)", piped)

    def test_a_brief_waiting_on_stdin_leaves_the_store_to_other_commands(self):
        self.open()
        self.fire_one()
        self.brigade("set", "--thread", "thread-coord")
        waiting = subprocess.Popen([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.at),
                                    *_owner_words(self.at),
                                    "brief", "D1", "--fields", "-"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.3)
            self.assertIsNone(waiting.poll())
            others = [subprocess.Popen([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.at),
                                        *_owner_words(self.at), *args],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                      for args in (("status",), ("watch",), ("ticket", "add", "--summary", "second"))]
            outputs = [other.communicate(timeout=10) for other in others]
            self.assertEqual([other.returncode for other in others], [0, 0, 0], outputs)
            self.assertEqual(outputs[2][0].strip(), "T2")
            out, err = waiting.communicate(json.dumps(self.literal_brief_fields()), timeout=10)
        finally:
            waiting.kill()
        self.assertEqual(waiting.returncode, 0, err)
        self.assertIn("TICKETS: T1: Startup is slow", out)

    def test_a_shell_reads_brief_fields_without_running_them(self):
        self.open()
        self.fire_one()
        self.brigade("set", "--thread", "thread-coord")
        fields = self.literal_brief_fields()
        flagged = self.brief_from_flags(fields)
        work = Path(self.temporary.name) / "shell"
        work.mkdir()
        script = (
            f"FIELDS='{work / 'fields.json'}'\n"
            f"B='{sys.executable} {SCRIPT} --store {self.store} --at {self.at} {' '.join(_owner_words(self.at))}'\n"
            "cat > \"$FIELDS\" <<'JSON'\n"
            + json.dumps(fields) + "\n"
            "JSON\n"
            "$B brief D1 --fields \"$FIELDS\"\n"
        )
        result = subprocess.run(["bash", "-c", script], cwd=work, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), flagged)
        self.assertFalse((work / "sentinel").exists())
        self.assertIsNone(json.loads((self.at / "restaurant.json").read_text())["lastReportAt"])

    def test_brief_fields_rejects_a_mix_and_bad_json(self):
        self.open()
        self.fire_one()
        self.brigade("set", "--thread", "thread-coord")
        brief = self.at / "briefs" / "D1.md"
        fields = self.at / "brief-fields.json"
        error = self.brigade("brief", "D1", "--fields", str(fields), "--goal", "g", ok=False)
        self.assertIn("use either --fields or the field flags, not both", error)
        self.assertFalse(brief.exists())
        missing = self.at / "no-such-fields.json"
        error = self.brigade("brief", "D1", "--fields", str(missing), ok=False)
        self.assertIn("brief fields:", error)
        self.assertIn("no-such-fields.json", error)
        self.assertFalse(brief.exists())
        fields.write_text("[]\n", encoding="utf-8")
        error = self.brigade("brief", "D1", "--fields", str(fields), ok=False)
        self.assertIn("JSON must be an object", error)
        self.assertFalse(brief.exists())
        fields.write_text(json.dumps({"goal": "g", "acceptance": ["a"], "verify": "v", "base": "main", "nope": "x"}),
                          encoding="utf-8")
        error = self.brigade("brief", "D1", "--fields", str(fields), ok=False)
        self.assertIn("unknown key nope", error)
        self.assertFalse(brief.exists())
        fields.write_text(json.dumps({"goal": "g", "acceptance": "a", "verify": "v", "base": "main"}), encoding="utf-8")
        error = self.brigade("brief", "D1", "--fields", str(fields), ok=False)
        self.assertIn("acceptance must be a list of strings", error)
        self.assertFalse(brief.exists())

    def test_a_restaurant_file_without_reporting_reads_as_milestones(self):
        self.open()
        meta = json.loads((self.at / "restaurant.json").read_text())
        del meta["reporting"]
        (self.at / "restaurant.json").write_text(json.dumps(meta, indent=2) + "\n")
        self.assertNotIn("reporting", json.loads((self.at / "restaurant.json").read_text()))
        self.assertEqual(self.brigade("status"), "thread not recorded\nreporting: milestones, no landing contract")
        self.assertIn("reports milestones", self.brigade("walk"))
        self.assertNotIn("reporting", json.loads((self.at / "restaurant.json").read_text()))

    def log_rows(self):
        lines = (self.at / "log.tsv").read_text().splitlines()
        header = lines[0].split("\t")
        return [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]

    def test_watch_flags_a_reported_run_still_open_past_ten_minutes(self):
        self.open()
        self.fire_one()
        report = self.at / "reports" / "D1.md"
        report.parent.mkdir()
        report.write_text("done")
        self.brigade("dish", "D1", "--reported")
        self.assertEqual(self.brigade("watch"),
                         "D1: report written 0m ago; review it even if the worker's run is still open (thread thread-9)")
        start = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(timespec="microseconds")
        log = self.at / "log.tsv"
        lines = log.read_text().splitlines()
        rewritten = [lines[0]]
        for line in lines[1:]:
            fields = line.split("\t")
            if fields[1:4] == ["dish", "D1", "in-progress"]:
                fields[0] = start
                line = "\t".join(fields)
            rewritten.append(line)
        log.write_text("\n".join(rewritten) + "\n")
        moment = datetime.now(timezone.utc) - timedelta(minutes=12, seconds=30)
        os.utime(report, (moment.timestamp(), moment.timestamp()))
        self.assertEqual(self.brigade("watch"), "D1: reported 12m ago, not in review; read the thread (thread thread-9)")
        moment = datetime.now(timezone.utc) - timedelta(minutes=10, seconds=10)
        os.utime(report, (moment.timestamp(), moment.timestamp()))
        self.assertEqual(self.brigade("watch"),
                         "D1: report written 10m ago; review it even if the worker's run is still open (thread thread-9)")

    def test_hang_records_one_open_run_per_attempt(self):
        self.open()
        self.fire_one()
        error = self.brigade("hang", "D1", "--provider", "grok/grok-4.7", "--minutes", "20", ok=False)
        self.assertIn("no report-back on this attempt", error)
        self.assertEqual([row for row in self.log_rows() if row["kind"] == "hang"], [])
        report = self.at / "reports" / "D1.md"
        report.parent.mkdir()
        report.write_text("done")
        self.brigade("dish", "D1", "--reported")
        self.assertEqual(self.brigade("hang", "D1", "--provider", "grok/grok-4.7", "--minutes", "20"),
                         "D1: grok/grok-4.7 open 20m")
        hangs = [row for row in self.log_rows() if row["kind"] == "hang"]
        self.assertEqual([(row["kind"], row["id"], row["state"], row["note"]) for row in hangs],
                         [("hang", "D1", "open", "grok/grok-4.7 20m")])
        self.assertEqual(self.brigade("hang", "D1", "--provider", "grok/grok-4.7", "--minutes", "5"),
                         "D1: hang already recorded")
        self.assertEqual(len([row for row in self.log_rows() if row["kind"] == "hang"]), 1)
        closed = self.brigade("close", "--dry-run")
        self.assertIn("Cut startup", closed)
        self.assertNotIn("grok/grok-4.7 20m", closed)
        self.brigade("dish", "D1", "--thread", "worker-3")
        error = self.brigade("hang", "D1", "--provider", "grok/grok-4.7", "--minutes", "20", ok=False)
        self.assertIn("no report-back", error)
        report.write_text("done again")
        self.brigade("dish", "D1", "--reported")
        self.brigade("hang", "D1", "--provider", "grok/grok-4.7", "--minutes", "20")
        self.assertEqual(len([row for row in self.log_rows() if row["kind"] == "hang"]), 2)

    def test_hang_records_a_run_left_open_by_an_earlier_attempt(self):
        self.open()
        self.fire_one()
        report = self.at / "reports" / "D1.md"
        report.parent.mkdir()
        report.write_text("done")
        self.brigade("dish", "D1", "--reported")
        self.brigade("dish", "D1", "--thread", "worker-3")
        error = self.brigade("hang", "D1", "--provider", "grok", "--minutes", "13", ok=False)
        self.assertEqual(error, "brigade: no report-back on this attempt; "
                                "for a run left open by an earlier attempt, pass --attempt 1")
        self.assertEqual(self.brigade("hang", "D1", "--attempt", "1", "--provider", "grok", "--minutes", "13"),
                         "D1: grok open 13m (attempt 1)")
        self.assertEqual(self.brigade("hang", "D1", "--attempt", "1", "--provider", "grok", "--minutes", "14"),
                         "D1: hang already recorded")
        self.assertEqual(self.brigade("hang", "D1", "--attempt", "3", "--provider", "grok", "--minutes", "1", ok=False),
                         "brigade: D1 has attempts 1 to 2")
        report.write_text("done again")
        self.brigade("dish", "D1", "--reported")
        self.assertEqual(self.brigade("hang", "D1", "--provider", "grok", "--minutes", "2"), "D1: grok open 2m")
        self.assertEqual(self.brigade("hang", "D1", "--attempt", "2", "--provider", "grok", "--minutes", "2"),
                         "D1: hang already recorded")
        self.assertEqual([row["note"] for row in self.log_rows() if row["kind"] == "hang"],
                         ["grok 13m (attempt 1)", "grok 2m"])

    def test_hang_without_attempt_counts_toward_the_attempt_it_was_recorded_in(self):
        self.open()
        self.fire_one()
        report = self.at / "reports" / "D1.md"
        report.parent.mkdir()
        report.write_text("done")
        self.brigade("dish", "D1", "--reported")
        self.brigade("hang", "D1", "--provider", "grok", "--minutes", "20")
        self.brigade("dish", "D1", "--thread", "worker-3")
        self.assertEqual(self.brigade("hang", "D1", "--attempt", "1", "--provider", "grok", "--minutes", "20"),
                         "D1: hang already recorded")

    def test_dropping_an_item_returns_its_tickets_to_waiting(self):
        from unittest import mock
        self.init_landing()
        ref = "https://github.com/o/r/issues/1"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.open()
            self.brigade("set", "--intake", "github")
            self.assertEqual(self.brigade("ticket", "add", "--summary", "bug", "--source", "github", "--ref", ref), "T1")
            self.assertEqual(self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "fix",
                                          "--paths", "a.py"), "D1 (lease L1 held by perf/D1)")
            self.assertEqual(self.brigade("dish", "D1", "--state", "dropped"), "D1 dropped; T1 waiting again")
            self.assertEqual(self.brigade("ticket", "list"), f"T1 waiting [github] bug {ref}")
            self.assertEqual(self.table_row(self.at, "rail.tsv", "T1")["dish"], "")
            self.assertEqual([(row["kind"], row["id"], row["state"], row["note"]) for row in self.log_rows()][-2:],
                             [("dish", "D1", "dropped", "fix (released L1)"),
                              ("ticket", "T1", "waiting", "bug (back from dropped D1)")])
            self.assertIn("waiting tickets: 1", self.brigade("status"))
            self.assertEqual(self.brigade("ticket", "add", "--summary", "bug again", "--source", "github", "--ref", ref,
                                          ok=False), f"brigade: {ref} is already T1 (waiting); nothing added")
            self.assertEqual(self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "fix",
                                          "--paths", "a.py"), "D2 (lease L2 held by perf/D2)")
            self.assertEqual(self.brigade("dish", "D2", "--state", "dropped"), "D2 dropped; T1 waiting again")
            self.brigade("ticket", "set", "T1", "--state", "dropped")
            self.brigade("set", "--intake", "")
            self.assertEqual(self.land("lease", "list"), "no leases held")

    def test_dropping_an_item_leaves_its_other_tickets_alone(self):
        self.open()
        for summary in ("one", "two"):
            self.brigade("ticket", "add", "--summary", summary)
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "a")
        self.brigade("fire", "--tickets", "T2", "--station", "bug-fix", "--summary", "b")
        self.brigade("ticket", "set", "T2", "--state", "done")
        self.assertEqual(self.brigade("dish", "D2", "--state", "dropped"), "D2 dropped")
        self.assertEqual(self.brigade("ticket", "list"), "T1 assigned [user] one\nT2 done [user] two")

    def test_blocked_counts_follow_the_block_that_holds_now(self):
        from unittest import mock
        self.init_landing()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.open()
            self.brigade("ticket", "add", "--summary", "Fix the gate")
            self.land("lease", "claim", "--holder", "engine/D1", "--paths", "src")
            self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Fix the gate",
                         "--paths", "src", ok=False)
            self.assertEqual(self.brigade("ticket", "list"), "T1 waiting [user] Fix the gate blocked: lease")
            self.assertIn("Perf (reports milestones): waiting tickets: 1 (1 blocked)", self.brigade("walk"))
            self.land("lease", "release", "L1")
            self.assertTrue(self.brigade("watch").startswith("T1: unblocked; run fire"))
            self.assertEqual(self.brigade("ticket", "list"), "T1 waiting [user] Fix the gate")
            self.assertIn("Perf (reports milestones): waiting tickets: 1\n", self.brigade("walk") + "\n")
            self.land("lease", "claim", "--holder", "engine/D2", "--paths", "src")
            self.assertEqual(self.brigade("ticket", "list"), "T1 waiting [user] Fix the gate blocked: lease")

    def test_fire_and_watch_name_the_worker_cap_first_when_both_caps_hold(self):
        from unittest import mock
        self.init_landing("--cap", "1")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.brigade("open", "--project-root", str(self.project), "--name", "Perf", "--workers", "1")
            self.brigade("ticket", "add", "--summary", "one")
            self.brigade("ticket", "add", "--summary", "two")
            self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "one", "--paths", "src")
            refused = self.brigade("fire", "--tickets", "T2", "--station", "bug-fix", "--summary", "two",
                                   "--paths", "docs", ok=False)
            self.assertEqual(refused, "brigade: nothing fired: 1 of 1 workers running")
            self.assertEqual(self.brigade("watch").splitlines()[-1], "T2: waiting for a worker (1 of 1 running)")
            self.assertEqual(self.brigade("ticket", "list", "--state", "waiting"), "T2 waiting [user] two blocked: workers")
            self.brigade("set", "--workers", "2")
            self.assertEqual(self.brigade("watch").splitlines()[-1],
                             "T2: waiting for room in the repository (1 of 1 changes in flight)")
            self.assertEqual(self.brigade("ticket", "list", "--state", "waiting"),
                             "T2 waiting [user] two blocked: repository")

    def renamed(self, hook=None):
        """Fire D1 on perf/d1, then run dish D1 --branch perf/d1-r2 in this process, calling hook at its lease list."""
        self.open()
        self.brigade("set", "--thread", "coordinator")
        self.brigade("ticket", "add", "--summary", "one")
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s",
                     "--branch", "perf/d1", "--paths", "src")
        glob = runpy.run_path(str(SCRIPT))["run"].__globals__
        original = glob["land_result"]

        def hooked(project_root, *args):
            if hook and args[:2] == ("lease", "list"):
                hook()
            return original(project_root, *args)

        glob["land_result"] = hooked
        argv = ["--store", str(self.store), "--at", str(self.at), "--owner", "coordinator@1",
                "dish", "D1", "--branch", "perf/d1-r2"]
        try:
            return glob["run"](argv)
        except glob["BrigadeError"] as error:
            return f"brigade: {error}"

    def lease_lines(self):
        return [line.split(" until ")[0] + ": " + line.rsplit(": ", 1)[1]
                for line in self.land("lease", "list").splitlines()]

    def test_a_new_branch_prints_the_commands_that_lease_its_fragment(self):
        from unittest import mock
        self.init_landing("--cap", "3")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            wide = "src,changes/perf%2Fd1.md,changes/perf%2Fd1-r2.md"
            self.assertEqual(self.renamed(), "\n".join([
                "D1 in-progress",
                "D1: L1 does not cover changes/perf%2Fd1-r2.md; cover it with these commands:",
                f"  $L lease claim --holder perf/D1 --paths {wide} --owner perf/@1",
                f"  $B dish D1 --lease <new lease> --paths {wide}",
                "  $L lease release L1 --owner perf/@1",
            ]))
            row = self.table_row(self.at, "dishes.tsv", "D1")
            self.assertEqual((row["branch"], row["lease"], row["paths"]), ("perf/d1-r2", "L1", "src,changes/perf%2Fd1.md"))
            self.assertEqual(self.lease_lines(), ["L1 active perf/D1: changes/perf%2Fd1.md, src"])
            self.assertEqual(self.land("lease", "claim", "--holder", "perf/D1", "--paths", wide, "--owner", "perf/@1"), "L2")
            self.assertEqual(self.brigade("dish", "D1", "--lease", "L2", "--paths", wide), "D1 in-progress")
            self.land("lease", "release", "L1", "--owner", "perf/@1")
            self.assertEqual(self.lease_lines(), ["L2 active perf/D1: changes/perf%2Fd1-r2.md, changes/perf%2Fd1.md, src"])
            self.assertEqual(self.brigade("dish", "D1", "--branch", "perf/d1-r2"), "D1 in-progress")

    def test_a_branch_rename_during_a_drop_leaves_no_lease(self):
        from unittest import mock
        self.init_landing("--cap", "3")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            dropped = []
            result = self.renamed(lambda: dropped or dropped.append(self.brigade("dish", "D1", "--state", "dropped")))
            self.assertEqual(dropped, ["D1 dropped; T1 waiting again"])
            self.assertEqual(result, "D1 in-progress")
            self.assertEqual(self.land("lease", "list"), "no leases held")
            refused = self.brigade("dish", "D1", "--lease", "L2", "--paths", "src", ok=False)
            self.assertEqual(refused, "brigade: D1 is dropped; it holds no lease")
            self.assertEqual(self.land("lease", "claim", "--holder", "engine/D1", "--paths", "src"), "L2")
            self.assertEqual(self.brigade("watch"), "no work in progress")

    def test_a_branch_rename_by_a_replaced_owner_claims_nothing(self):
        from unittest import mock
        self.init_landing("--cap", "3")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            taken = []
            result = self.renamed(lambda: taken or taken.append(
                self.brigade("set", "--thread", "replacement", "--replace")))
            self.assertEqual(len(taken), 1)
            self.assertIn("$L lease claim --holder perf/D1", result)
            self.assertEqual(self.lease_lines(), ["L1 active perf/D1: changes/perf%2Fd1.md, src"])
            stale = self.run_at(self.at, "--owner", "coordinator@1", "dish", "D1", "--branch", "perf/d1-r3", ok=False)
            self.assertEqual(stale, "brigade: owner coordinator@1 is stale; this store is owned by replacement@2")
            self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["branch"], "perf/d1-r2")
            self.assertEqual(self.brigade("dish", "D1", "--state", "dropped"), "D1 dropped; T1 waiting again")
            self.assertEqual(self.land("lease", "list"), "no leases held")

    def test_a_branch_rename_after_submit_says_the_lease_cannot_change(self):
        self.init_landing("--cap", "3")
        from unittest import mock
        with mock.patch.dict(os.environ, self.land_env()):
            self.open()
            self.brigade("ticket", "add", "--summary", "one")
            self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "one",
                         "--branch", "perf/d1", "--paths", "a.txt")
            sha = self.worker_commit("perf/d1", {"a.txt": "changed\n"})
            self.passed(sha)
            self.submit(sha)
            self.brigade("dish", "D1", "--state", "queued")
            self.assertEqual(self.brigade("dish", "D1", "--branch", "perf/d1-r2"), "\n".join([
                "D1 queued",
                "D1: L1 is submitted, so it cannot take changes/perf%2Fd1-r2.md; the submitted commit lands as it is. "
                "Run dish D1 --branch perf/d1-r2 again if its entry bounces",
            ]))
            self.assertEqual(self.lease_lines(), ["L1 submitted perf/D1: a.txt, changes/perf%2Fd1.md"])

    def test_open_prints_the_sibling_on_the_same_project_root(self):
        self.open()
        menu = (self.at / "menu.md").read_text()
        menu = menu.replace(
            "What this restaurant exists to achieve, in one or two sentences.",
            "Keep startup under 400 ms.")
        menu = menu.replace(
            "Work this restaurant does not take, even when asked.",
            "Docs and release notes\nVendor upgrades")
        (self.at / "menu.md").write_text(menu)
        self.brigade("set", "--thread", "thread-perf")
        docs_dir = self.store / "bridge-kit" / "docs"
        self.assertEqual(
            self.brigade("open", "--project-root", str(self.project), "--name", "Docs"),
            "\n".join([
                f"opened {docs_dir}",
                f"sibling Perf ({self.at}), thread thread-perf",
                "  purpose: Keep startup under 400 ms.",
                "  off the menu: Docs and release notes; Vendor upgrades",
            ]))
        self.assertEqual(
            self.brigade("open", "--project-root", str(self.project), "--name", "Perf"),
            "\n".join([
                f"exists {self.at}",
                "thread thread-perf already recorded",
                f"sibling Docs ({docs_dir}), thread not recorded",
                "  purpose: not written yet",
                "  off the menu: not written yet",
            ]))

    def test_open_refuses_a_directory_that_holds_another_project_root(self):
        first = (Path(self.temporary.name) / "left" / "app").resolve()
        second = (Path(self.temporary.name) / "right" / "app").resolve()
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        directory = self.store / "app" / "docs"
        self.assertEqual(
            self.brigade("open", "--project-root", str(first), "--name", "Docs"),
            f"opened {directory}")
        before = (directory / "restaurant.json").read_bytes()
        error = self.brigade("open", "--project-root", str(second), "--name", "Docs", ok=False)
        self.assertEqual(
            error,
            f"brigade: {directory} already holds a coordinator for {first}; pick another --name")
        self.assertEqual((directory / "restaurant.json").read_bytes(), before)

    def test_two_processes_opening_one_name_on_different_roots_leave_one_store(self):
        roots = []
        for label in ("left", "right"):
            path = (Path(self.temporary.name) / label / "app").resolve()
            path.mkdir(parents=True)
            roots.append(path)
        directory = self.store / "app" / "docs"
        procs = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "--store", str(self.store),
                 "open", "--project-root", str(root), "--name", "Docs"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for root in roots
        ]
        finished = []
        for proc in procs:
            out, err = proc.communicate(timeout=15)
            finished.append((proc.returncode, out, err))
        self.assertEqual(sorted(code for code, _, _ in finished), [0, 1])
        self.assertEqual(len(list((directory.parent).glob("*/restaurant.json"))), 1)
        meta = json.loads((directory / "restaurant.json").read_text())
        winner = next(item for item in finished if item[0] == 0)
        loser = next(item for item in finished if item[0] == 1)
        self.assertIn(meta["projectRoot"], [str(root) for root in roots])
        self.assertTrue(winner[1].startswith(f"opened {directory}"))
        self.assertIn(f"already holds a coordinator for {meta['projectRoot']}", loser[2])
        self.assertIn("pick another --name", loser[2])

    def test_set_thread_refuses_to_replace_a_recorded_thread(self):
        self.open()
        self.brigade("set", "--thread", "thread-1")
        self.brigade("set", "--thread", "thread-1")
        before = (self.at / "restaurant.json").read_bytes()
        error = self.brigade("set", "--thread", "thread-2", ok=False)
        self.assertEqual(error, "brigade: thread thread-1 already recorded")
        self.assertEqual((self.at / "restaurant.json").read_bytes(), before)
        self.brigade("set", "--thread", "thread-2", "--replace")
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["thread"], "thread-2")

    def _spawn_race(self, mode, case, args):
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--race-child", mode, str(case), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def _finish_race(self, proc):
        try:
            out, err = proc.communicate(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail(f"timed out\nstdout:\n{out}\nstderr:\n{err}")
        return proc.returncode, out.strip(), err.strip()

    def test_a_losing_open_keeps_the_owners_ticket_and_menu(self):
        case = Path(self.temporary.name) / "race"
        case.mkdir()
        project = Path(self.temporary.name) / "app"
        project.mkdir()
        directory = self.store / "app" / "docs"
        args = ["--store", str(self.store), "open", "--project-root", str(project), "--name", "Docs"]
        owner = loser = None
        try:
            owner = self._spawn_race("owner", case, args)
            _wait_for_path(case / "metadata-ready")
            loser = self._spawn_race("loser", case, args)
            _wait_for_path(case / "loser-saw-meta")
            self.assertIsNone(owner.poll())
            self.assertIsNone(loser.poll())
            if not (directory / "rail.tsv").exists():
                (case / "loser-after-meta").touch()
                _wait_for_path(case / "loser-at-scaffold")
                (case / "owner-proceed").touch()
                owner_code, owner_out, owner_err = self._finish_race(owner)
                owner = None
            else:
                owner_code = owner_out = owner_err = None
            added = subprocess.run(
                [sys.executable, str(SCRIPT), "--at", str(directory), "ticket", "add", "--summary", "Keep this ticket"],
                capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            self.assertEqual((added.returncode, added.stdout), (0, "T1\n"), added.stderr)
            menu = directory / "menu.md"
            menu.write_text(menu.read_text() + "Keep the menu sentence.\n")
            if (case / "loser-at-scaffold").exists():
                (case / "loser-scaffold-proceed").touch()
            else:
                (case / "loser-after-meta").touch()
            loser_code, loser_out, loser_err = self._finish_race(loser)
            loser = None
            if owner is not None:
                (case / "owner-proceed").touch()
                owner_code, owner_out, owner_err = self._finish_race(owner)
                owner = None
        finally:
            for proc in (owner, loser):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
        self.assertEqual((owner_code, owner_err), (0, ""), owner_out)
        self.assertEqual(owner_out, f"opened {directory}")
        self.assertEqual((loser_code, loser_err), (0, ""), loser_out)
        self.assertEqual(loser_out, f"exists {directory}")
        self.assertIn("Keep this ticket", (directory / "rail.tsv").read_text())
        self.assertIn("Keep the menu sentence.", (directory / "menu.md").read_text())

    def test_open_rejects_a_restaurant_json_that_is_not_valid_json(self):
        self.at.mkdir(parents=True)
        (self.at / "restaurant.json").write_text('{"restaurant":')
        started = time.monotonic()
        error = self.brigade("open", "--project-root", str(self.project), "--name", "Perf", ok=False)
        elapsed = time.monotonic() - started
        self.assertEqual(error, f"brigade: {self.at / 'restaurant.json'} is not valid JSON")
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 3)
        self.assertEqual((self.at / "restaurant.json").read_text(), '{"restaurant":')
        self.assertFalse((self.at / "menu.md").exists())
        self.assertFalse((self.at / "rail.tsv").exists())

    def test_open_accepts_restaurant_json_that_becomes_valid_within_one_second(self):
        self.at.mkdir(parents=True)
        path = self.at / "restaurant.json"
        path.write_text('{"restaurant":')
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--store", str(self.store), "open",
             "--project-root", str(self.project), "--name", "Perf"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        try:
            time.sleep(0.2)
            self.assertIsNone(proc.poll())
            path.write_text(json.dumps({
                "restaurant": "Perf",
                "projectRoot": str(self.project.resolve()),
                "landing": "merge",
                "thread": None,
                "schedules": {},
            }))
            out, err = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail(f"timed out\nstdout:\n{out}\nstderr:\n{err}")
        self.assertEqual((proc.returncode, err.strip()), (0, ""), out)
        self.assertTrue(out.startswith(f"exists {self.at}"))


    def git_project(self):
        if (self.project / ".git").exists():
            return
        run = lambda *command: subprocess.run(command, cwd=self.project, capture_output=True, text=True, check=True)
        run("git", "init", "-q", "-b", "main")
        (self.project / "a.txt").write_text("a\n")
        run("git", "add", "-A")
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    def land_env(self):
        return dict(os.environ, XDG_STATE_HOME=str(Path(self.temporary.name) / "state"))

    def land_repo(self, repo, *args, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / "t3/added/landing/scripts/land.py"),
                                 "--repo", str(repo), *args],
                                capture_output=True, text=True, env=self.land_env())
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return (result.stdout if ok else result.stderr).strip()

    def land(self, *args, ok=True):
        return self.land_repo(self.project, *args, ok=ok)

    def init_landing(self, *args):
        self.git_project()
        return self.land("init", "--trunk", "lane", "--mode", "local", "--base", "main", *args)

    def test_a_refused_fire_stays_blocked_until_the_lease_is_free(self):
        from unittest import mock
        self.init_landing()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.open()
            self.brigade("ticket", "add", "--summary", "Fix the gate")
            self.land("lease", "claim", "--holder", "engine/D1", "--paths", "src")
            refused = self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Fix the gate",
                                   "--paths", "src", "--timebox", "60", ok=False)
            self.assertIn("nothing fired: paths overlap L1 held by engine/D1", refused)
            self.assertEqual(self.brigade("ticket", "list"), "T1 waiting [user] Fix the gate blocked: lease")
            self.assertEqual(self.brigade("watch"), "T1: waiting on L1 (engine/D1)")
            self.land("lease", "release", "L1")
            self.land("lease", "claim", "--holder", "engine/D1", "--paths", "src")
            self.assertEqual(self.brigade("watch"), "T1: waiting on L2 (engine/D1)")
            self.land("lease", "release", "L2")
            line = "T1: unblocked; run fire --tickets=T1 --station=bug-fix '--summary=Fix the gate' --paths=src --timebox=60"
            self.assertEqual(self.brigade("watch"), line)
            started = self.brigade(*shlex.split(line.split("run ", 1)[1]))
            self.assertEqual(started, "D1 (lease L3 held by perf/D1)")
            self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["paths"], "src,changes/perf%2Fd1.md")
            self.assertEqual(self.brigade("ticket", "list"), "T1 assigned [user] Fix the gate")

    def test_an_unblocked_command_round_trips_through_bash(self):
        from unittest import mock
        marker = Path(self.temporary.name) / "shell-marker"
        summary = f"Fix O'Reilly; touch {marker}; # $(touch {marker}) & spaces"
        self.init_landing()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.open()
            self.brigade("ticket", "add", "--summary", "quoted")
            self.land("lease", "claim", "--holder", "engine/D1", "--paths", "src/app.py")
            refused = self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", summary,
                                   "--branch", "docs/gate", "--paths", "src/app.py", "--timebox", "45", ok=False)
            self.assertIn("nothing fired: paths overlap L1 held by engine/D1", refused)
            self.land("lease", "release", "L1")
            line = self.brigade("watch")
            ran = self.bash_unblocked(self.at, line)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertFalse(marker.exists(), ran.stdout + ran.stderr)
        row = self.table_row(self.at, "dishes.tsv", "D1")
        self.assertEqual(row["summary"], summary)
        self.assertEqual(row["paths"], "src/app.py,changes/docs%2Fgate.md")
        self.assertEqual(row["timebox"], "45")
        self.assertEqual(row["branch"], "docs/gate")

    def test_an_option_leading_summary_round_trips_through_bash(self):
        self.brigade("open", "--project-root", str(self.project), "--name", "Perf",
                     "--workers", "1")
        self.brigade("ticket", "add", "--summary", "seed")
        self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "seed")
        blocker = "D1"
        cases = (("-Werror", "T2", "D2"), ("--help", "T3", "D3"))
        for summary, ticket, dish in cases:
            self.brigade("ticket", "add", f"--summary={summary}")
            refused = self.brigade("fire", "--tickets", ticket, "--station", "bug-fix",
                                   f"--summary={summary}", "--timebox", "45", ok=False)
            self.assertIn("nothing fired: 1 of 1 workers running", refused)
            self.brigade("dish", blocker, "--state", "sent-back")
            line = next(part for part in self.brigade("watch").splitlines() if part.startswith(f"{ticket}: unblocked"))
            ran = self.bash_unblocked(self.at, line)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            row = self.table_row(self.at, "dishes.tsv", dish)
            self.assertEqual(row["summary"], summary)
            self.assertEqual(row["timebox"], "45")
            self.assertEqual(row["station"], "bug-fix")
            blocker = dish

    def test_a_later_fire_does_not_keep_the_refused_attempts_fragment(self):
        from unittest import mock
        self.init_landing()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            directory = self.store / "bridge-kit" / "review-more"
            opened = self.brigade("open", "--project-root", str(self.project), "--name", "Review more")
            self.assertEqual(opened, f"opened {directory}")
            self.run_at(directory, "ticket", "add", "--summary", "Blocked on src")
            self.run_at(directory, "ticket", "add", "--summary", "Takes the first id")
            self.land("lease", "claim", "--holder", "engine/D9", "--paths", "src")
            refused = self.run_at(directory, "fire", "--tickets", "T1", "--station", "bug-fix",
                                  "--summary", "Blocked on src", "--paths", "src", "--timebox", "30", ok=False)
            self.assertIn("nothing fired: paths overlap L1 held by engine/D9", refused)
            note = self.blocked_note(directory, "T1")
            self.assertEqual(note["paths"], "src")
            self.assertNotIn("branch", note)
            started = self.run_at(directory, "fire", "--tickets", "T2", "--station", "bug-fix",
                                  "--summary", "Takes the first id", "--paths", "other")
            self.assertEqual(started, "D1 (lease L2 held by review-more/D1)")
            self.assertEqual(self.table_row(directory, "dishes.tsv", "D1")["paths"], "other,changes/review-more%2Fd1.md")
            self.land("lease", "release", "L1")
            line = self.run_at(directory, "watch")
            self.assertNotIn("waiting on", line)
            self.assertIn("--paths=src --timebox=30", line)
            self.assertNotIn("changes/review-more%2Fd1.md", line)
            ran = self.bash_unblocked(directory, line)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        row = self.table_row(directory, "dishes.tsv", "D2")
        self.assertEqual(row["paths"], "src,changes/review-more%2Fd2.md")
        self.assertEqual(row["branch"], "")
        self.assertEqual(row["timebox"], "30")
        self.assertEqual(row["summary"], "Blocked on src")

    def test_a_refused_fire_keeps_an_explicit_branch(self):
        from unittest import mock
        self.init_landing()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            directory = self.store / "bridge-kit" / "review"
            opened = self.brigade("open", "--project-root", str(self.project), "--name", "Review")
            self.assertEqual(opened, f"opened {directory}")
            self.run_at(directory, "ticket", "add", "--summary", "Explicit branch")
            self.land("lease", "claim", "--holder", "engine/D1", "--paths", "src")
            refused = self.run_at(directory, "fire", "--tickets", "T1", "--station", "bug-fix",
                                  "--summary", "Explicit branch", "--branch", "docs/a%b/c",
                                  "--paths", "src,changes/docs%2Fa%25b%2Fc.md", ok=False)
            self.assertIn("nothing fired: paths overlap L1 held by engine/D1", refused)
            note = self.blocked_note(directory, "T1")
            self.assertEqual(note["paths"], "src,changes/docs%2Fa%25b%2Fc.md")
            self.assertEqual(note.get("branch"), "docs/a%b/c")
            self.land("lease", "release", "L1")
            line = self.run_at(directory, "watch")
            self.assertIn("--branch", line)
            self.assertIn("docs/a%b/c", line)
            ran = self.bash_unblocked(directory, line)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        row = self.table_row(directory, "dishes.tsv", "D1")
        self.assertEqual(row["branch"], "docs/a%b/c")
        self.assertEqual(row["paths"], "src,changes/docs%2Fa%25b%2Fc.md")
        self.assertEqual(row["summary"], "Explicit branch")
        self.assertNotIn("changes/review%2Fd1.md", row["paths"])

    def test_a_worker_refusal_prints_the_running_count(self):
        self.brigade("open", "--project-root", str(self.project), "--name", "Perf", "--workers", "3")
        for number, summary in enumerate(("one", "two", "three"), start=1):
            self.brigade("ticket", "add", "--summary", summary)
            self.brigade("fire", "--tickets", f"T{number}", "--station", "bug-fix", "--summary", summary)
        self.brigade("ticket", "add", "--summary", "four")
        self.brigade("set", "--workers", "1")
        refused = self.brigade("fire", "--tickets", "T4", "--station", "bug-fix", "--summary", "four", ok=False)
        self.assertIn("nothing fired: 3 of 1 workers running", refused)
        self.assertIn("T4: waiting for a worker (3 of 1 running)", self.brigade("watch"))

    def test_a_failed_lease_check_keeps_the_landing_diagnostic(self):
        case = Path(self.temporary.name)
        (case / "land.py").write_text(
            "import sys\n"
            "action = sys.argv[4]\n"
            "if action == 'claim':\n"
            "    print('paths overlap L1 held by engine/D1 on src', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "if action == 'check':\n"
            "    print('queue database is locked', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n")
        self.open()
        self.brigade("ticket", "add", "--summary", "one")
        refused = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--race-child", "fake-land", str(case),
                                  "--store", str(self.store), "--at", str(self.at),
                                  "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s", "--paths", "src"],
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        watched = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--race-child", "fake-land", str(case),
                                  "--store", str(self.store), "--at", str(self.at), "watch"],
                                 capture_output=True, text=True)
        self.assertEqual(watched.returncode, 0, watched.stderr)
        self.assertEqual(watched.stdout.strip(), "T1: waiting on the landing queue (queue database is locked)")

    def test_a_repository_cap_refusal_prints_the_repository_line(self):
        from unittest import mock
        self.init_landing("--cap", "1")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.open()
            self.brigade("ticket", "add", "--summary", "Room")
            self.land("lease", "claim", "--holder", "engine/D1", "--paths", "other.txt")
            refused = self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "Room",
                                   "--paths", "src", ok=False)
            self.assertIn("nothing fired: repository at its cap: 1 of 1 changes in flight", refused)
            self.assertIn("blocked: repository", self.brigade("ticket", "list"))
            self.assertEqual(self.brigade("watch"), "T1: waiting for room in the repository (1 of 1 changes in flight)")

    def test_the_worker_cap_defaults_to_two_and_a_later_fire_clears_the_block(self):
        self.open()
        self.assertNotIn("workers", json.loads((self.at / "restaurant.json").read_text()))
        for summary in ("one", "two", "three"):
            self.brigade("ticket", "add", "--summary", summary)
        self.assertEqual(self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "a"), "D1")
        self.assertEqual(self.brigade("fire", "--tickets", "T2", "--station", "bug-fix", "--summary", "b"), "D2")
        refused = self.brigade("fire", "--tickets", "T3", "--station", "bug-fix", "--summary", "c", ok=False)
        self.assertIn("nothing fired: 2 of 2 workers running", refused)
        self.assertEqual(self.brigade("ticket", "list", "--state", "waiting"),
                         "T3 waiting [user] three blocked: workers")
        self.assertIn("T3: waiting for a worker (2 of 2 running)", self.brigade("watch"))
        self.brigade("set", "--workers", "3")
        self.assertEqual(self.brigade("fire", "--tickets", "T3", "--station", "bug-fix", "--summary", "c"), "D3")
        self.assertNotIn("blocked:", self.brigade("ticket", "list"))

    def test_workers_one_refuses_fire_and_a_dish_that_is_not_already_counted(self):
        opened = self.brigade("open", "--project-root", str(self.project), "--name", "Perf",
                              "--workers", "1")
        self.assertEqual(opened, f"opened {self.at}")
        self.assertEqual(json.loads((self.at / "restaurant.json").read_text())["workers"], 1)
        for summary in ("one", "two", "three"):
            self.brigade("ticket", "add", "--summary", summary)
        self.assertEqual(self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "a"), "D1")
        refused = self.brigade("fire", "--tickets", "T2", "--station", "bug-fix", "--summary", "b", ok=False)
        self.assertIn("nothing fired: 1 of 1 workers running", refused)
        self.brigade("dish", "D1", "--state", "sent-back")
        self.assertEqual(self.brigade("fire", "--tickets", "T2", "--station", "bug-fix", "--summary", "b"), "D2")
        self.brigade("dish", "D2", "--state", "blocked")
        self.assertEqual(self.brigade("fire", "--tickets", "T3", "--station", "bug-fix", "--summary", "c"), "D3")
        sent = self.brigade("dish", "D1", "--state", "in-progress", ok=False)
        review = self.brigade("dish", "D2", "--state", "in-review", ok=False)
        self.assertIn("1 of 1 workers running", sent)
        self.assertIn("1 of 1 workers running", review)
        self.assertEqual(self.brigade("dish", "D3", "--state", "in-review"), "D3 in-review")
        self.assertEqual(self.brigade("dish", "D3", "--state", "in-progress", "--thread", "worker-2"), "D3 in-progress")
        before = (self.at / "restaurant.json").read_text()
        error = self.brigade("set", "--workers", "0", ok=False)
        self.assertIn("workers must be 1 or more", error)
        self.assertEqual((self.at / "restaurant.json").read_text(), before)
        missing = self.brigade("open", "--project-root", str(self.project), "--name", "Nope",
                               "--workers", "0", ok=False)
        self.assertIn("workers must be 1 or more", missing)
        self.assertFalse((self.store / "bridge-kit" / "nope").exists())

    def test_open_warns_when_workers_meet_the_repository_cap_and_a_sibling_exists(self):
        from unittest import mock
        self.init_landing("--cap", "2")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            first = self.brigade("open", "--project-root", str(self.project), "--name", "Perf",
                                 "--workers", "4")
            self.assertNotIn("warning:", first)
            second = self.brigade("open", "--project-root", str(self.project), "--name", "Docs",
                                  "--workers", "2")
            self.assertIn("warning: workers 2 is at or above the repository cap of 2 while a sibling exists", second)
            third = self.brigade("open", "--project-root", str(self.project), "--name", "Quiet",
                                 "--workers", "1")
            self.assertNotIn("warning:", third)

    def test_fire_leases_the_branch_changelog_fragment_and_dish_records_a_new_lease(self):
        from unittest import mock
        self.init_landing()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]}):
            self.open()
            (self.at / "menu.md").write_text("## Purpose\n\nFast.\n")
            self.brigade("set", "--thread", "thread-coord")
            self.brigade("ticket", "add", "--summary", "one")
            self.brigade("ticket", "add", "--summary", "two")
            self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s",
                         "--branch", "docs/a", "--paths", "src/a.py")
            text = self.brigade("brief", "D1", "--goal", "g", "--acceptance", "a", "--verify", "v", "--base", "origin/main")
            self.assertIn("leased to you as L1: src/a.py,changes/docs%2Fa.md.", text)
            self.brigade("fire", "--tickets", "T2", "--station", "bug-fix", "--summary", "s",
                         "--branch", "docs/a%b", "--paths", "src/b.py")
            text = self.brigade("brief", "D2", "--goal", "g", "--acceptance", "a", "--verify", "v", "--base", "origin/main")
            self.assertIn("leased to you as L2: src/b.py,changes/docs%2Fa%25b.md.", text)
            self.assertEqual(text.count("changes/docs%2Fa%25b.md"), 1)
            self.brigade("dish", "D1", "--lease", "L9", "--paths", "src/a.py,changes/docs%2Fa.md")
            text = self.brigade("brief", "D1", "--goal", "g", "--acceptance", "a", "--verify", "v", "--base", "origin/main")
            self.assertIn("leased to you as L9: src/a.py,changes/docs%2Fa.md.", text)

    def test_fire_warns_when_releasing_a_conflicting_lease_fails(self):
        case = Path(self.temporary.name)
        (case / "land.py").write_text(
            "import sys, time\nfrom pathlib import Path\n"
            f"case = Path({str(case)!r})\n"
            "with open(case / 'land-calls', 'a') as handle:\n    handle.write(' '.join(sys.argv[3:]) + '\\n')\n"
            "if sys.argv[4] == 'claim':\n"
            "    (case / 'claiming').touch()\n"
            "    end = time.monotonic() + 30\n"
            "    while not (case / 'claim-proceed').exists() and time.monotonic() < end:\n        time.sleep(0.01)\n"
            "    print('L7')\n"
            "else:\n"
            "    print('lease L7 is held elsewhere', file=sys.stderr)\n"
            "    sys.exit(1)\n")
        self.open()
        self.brigade("ticket", "add", "--summary", "one")
        firing = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--race-child", "fake-land", str(case),
                                   "--store", str(self.store), "--at", str(self.at),
                                   "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s", "--paths", "src"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        try:
            _wait_for_path(case / "claiming")
            other = subprocess.run([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.at),
                                    "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "other"],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(other.stdout.strip(), "D1", other.stderr)
            (case / "claim-proceed").touch()
            _, err = firing.communicate(timeout=10)
        finally:
            firing.kill()
        self.assertEqual(firing.returncode, 1)
        self.assertIn("nothing fired: T1 is assigned, not waiting", err)
        self.assertIn("L7", err)
        self.assertIn("lease L7 is held elsewhere", err)

    def test_watch_rechecks_a_lease_outside_the_store_lock(self):
        case = Path(self.temporary.name)
        (case / "land.py").write_text(
            "import sys, time\nfrom pathlib import Path\n"
            f"case = Path({str(case)!r})\n"
            "action = sys.argv[4]\n"
            "if action == 'claim':\n"
            "    print('paths overlap L1 held by engine/D1 on src', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "if action == 'check':\n"
            "    (case / 'checking').touch()\n"
            "    end = time.monotonic() + 30\n"
            "    while not (case / 'check-proceed').exists() and time.monotonic() < end:\n        time.sleep(0.01)\n"
            "    print('L1 held by engine/D1 on src')\n"
            "    sys.exit(1)\n"
            "print('ok')\n")
        self.open()
        self.brigade("ticket", "add", "--summary", "one")
        refused = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--race-child", "fake-land", str(case),
                                  "--store", str(self.store), "--at", str(self.at),
                                  "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s", "--paths", "src"],
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("blocked: lease", self.brigade("ticket", "list"))
        watching = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--race-child", "fake-land", str(case),
                                     "--store", str(self.store), "--at", str(self.at), "watch"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        try:
            _wait_for_path(case / "checking")
            added = subprocess.run([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.at),
                                    "ticket", "add", "--summary", "two"],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(added.stdout.strip(), "T2", added.stderr)
            (case / "check-proceed").touch()
            out, err = watching.communicate(timeout=10)
        finally:
            watching.kill()
        self.assertEqual(watching.returncode, 0, err)
        self.assertEqual(out.strip(), "T1: waiting on L1 (engine/D1)")

    def landing_env(self):
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.land_env()["XDG_STATE_HOME"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def started(self, *init, paths="README.md"):
        """A local landing contract, the Perf coordinator, and D1 holding L1."""
        self.init_landing(*init)
        self.landing_env()
        self.open()
        (self.at / "menu.md").write_text("## Purpose\n\nFast.\n")
        self.brigade("ticket", "add", "--summary", "one")
        self.assertEqual(self.brigade("fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s", "--paths", paths),
                         "D1 (lease L1 held by perf/D1)")

    def landing_db(self):
        import sqlite3
        database = next((Path(self.temporary.name) / "state").glob("pstack-t3/landing/*/land.db"))
        db = sqlite3.connect(database)
        db.row_factory = sqlite3.Row
        return db

    def lease_row(self, number):
        from contextlib import closing
        with closing(self.landing_db()) as db:
            return dict(db.execute("SELECT * FROM lease WHERE id = ?", (number,)).fetchone())

    def seed_entry(self, sha, state, note):
        """A queue entry for perf/D1 at another commit whose first 12 characters match, copied from E1."""
        from contextlib import closing
        with closing(self.landing_db()) as db, db:
            db.execute("INSERT INTO entry (at, holder, branch, sha, base, fingerprint, lease, reviewer, state, note) "
                       "SELECT at, holder, branch, ?, base, fingerprint, lease, reviewer, ?, ? FROM entry WHERE id = 1",
                       (sha, state, note))

    @staticmethod
    def same_prefix(sha):
        return sha[:12] + ("1" if sha[12] == "0" else "0") + sha[13:]

    def worker_commit(self, branch, files):
        path = Path(self.temporary.name) / branch.replace("/", "-")
        git = lambda *command, cwd=self.project: subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                                                check=True).stdout.strip()
        if not path.exists():
            git("git", "worktree", "add", "-q", "-b", branch, str(path), "main")
        for name, text in files.items():
            if text is None:
                (path / name).unlink()
            else:
                (path / name).write_text(text)
        git("git", "add", "-A", cwd=path)
        git("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", branch, cwd=path)
        return git("git", "rev-parse", "HEAD", cwd=path)

    def passed(self, sha):
        self.brigade("dish", "D1", "--state", "in-review", "--sha", sha)
        self.brigade("pass", "record", "D1", "--sha", sha, "--verdict", "pass", "--author", CODEX, "--verifier", CLAUDE)

    def submit(self, sha):
        return self.land("submit", "--holder", "perf/D1", "--branch", "perf/d1", "--sha", sha, "--lease", "L1",
                         "--reviewer", CLAUDE)

    def test_watch_renews_a_live_lease_and_records_activity(self):
        self.started()
        self.land("lease", "renew", "L1", "--ttl-hours", "0.01")
        before = json.loads((self.at / "restaurant.json").read_text())["lastActivityAt"]
        self.assertLess(self.lease_row(1)["expires"], (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.assertEqual(self.brigade("watch"), "D1: running 0m of 60m (no worker recorded)")
        self.assertGreater(self.lease_row(1)["expires"], (datetime.now(timezone.utc) + timedelta(hours=5.9)).isoformat())
        self.assertGreater(json.loads((self.at / "restaurant.json").read_text())["lastActivityAt"], before)

    def test_an_expired_lease_stays_expired_until_an_explicit_renew(self):
        self.started()
        self.land("lease", "renew", "L1", "--ttl-hours", "0")
        expired = self.lease_row(1)["expires"]
        self.assertEqual(self.brigade("watch").splitlines(),
                         ["D1: running 0m of 60m (no worker recorded)",
                          "D1: lease L1 expired; stop its worker, then run lease renew L1"])
        self.assertEqual(self.lease_row(1)["expires"], expired)
        self.assertEqual(self.land("lease", "renew", "L1"), "L1 renewed")
        self.assertEqual(self.brigade("watch"), "D1: running 0m of 60m (no worker recorded)")

    def test_re_admitting_an_expired_lease_names_the_holder_that_claimed_its_paths(self):
        self.started()
        self.land("lease", "renew", "L1", "--ttl-hours", "0")
        self.assertEqual(self.land("lease", "claim", "--holder", "engine/D1", "--paths", "README.md"), "L2")
        self.assertIn("D1: lease L1 expired; stop its worker, then run lease renew L1", self.brigade("watch"))
        self.assertEqual(self.land("lease", "renew", "L1", ok=False),
                         "land: L1 expired and L2 held by engine/D1 now covers README.md; claim again after it is released")

    def test_a_released_or_unreadable_lease_gets_its_own_line(self):
        self.started()
        self.brigade("dish", "D1", "--state", "in-review", "--sha", "abc1234")
        self.land("lease", "release", "L1")
        self.assertEqual(self.brigade("watch"), "D1: lease L1 was released; claim again before submitting")
        before = (self.at / "restaurant.json").read_text()
        (Path(self.temporary.name) / "state").rename(Path(self.temporary.name) / "moved")
        self.assertEqual(self.brigade("watch"),
                         f"D1: could not renew L1: {self.project} has no landing contract; run land.py init in it")
        self.assertEqual((self.at / "restaurant.json").read_text(), before)

    def test_review_passed_parked_and_sent_back_items_each_get_a_watch_line_and_renew(self):
        self.started()
        verdict = ("pass", "record", "D1", "--sha", "abc1234", "--author", CODEX, "--verifier", CLAUDE, "--verdict")
        steps = [(("dish", "D1", "--state", "in-review", "--sha", "abc1234"), "D1: in review"),
                 ((*verdict, "send-back"), "D1: sent back"),
                 ((*verdict, "blocked"), "D1: parked"),
                 ((*verdict, "pass"), "D1: passed, not submitted")]
        for command, line in steps:
            self.brigade(*command)
            self.land("lease", "renew", "L1", "--ttl-hours", "0.01")
            self.assertEqual(self.brigade("watch"), line)
            self.assertGreater(self.lease_row(1)["expires"],
                               (datetime.now(timezone.utc) + timedelta(hours=5.9)).isoformat(), line)

    def test_a_passed_item_with_a_submitted_entry_says_mark_it_queued(self):
        self.started()
        sha = self.worker_commit("perf/d1", {"README.md": "fast\n"})
        self.passed(sha)
        self.assertEqual(self.submit(sha), "E1")
        self.assertEqual(self.brigade("watch"), "D1: E1 already submitted; mark it queued")

    def test_a_passed_item_whose_entry_landed_is_not_told_to_claim_again(self):
        self.started()
        sha = self.worker_commit("perf/d1", {"README.md": "fast\n"})
        self.passed(sha)
        self.submit(sha)
        self.land("land")
        self.assertEqual(self.brigade("watch"), "D1: E1 already submitted; mark it queued")

    def test_dropping_a_queued_item_whose_entry_bounced_releases_its_lease(self):
        self.started("--check", "test ! -e BROKEN", paths="README.md,BROKEN")
        self.brigade("dish", "D1", "--thread", "w1")
        sha = self.worker_commit("perf/d1", {"BROKEN": "x\n"})
        self.passed(sha)
        self.submit(sha)
        self.brigade("dish", "D1", "--state", "queued")
        self.land("land")
        self.assertIn("D1 holds L1", self.brigade("dish", "D1", "--state", "dropped", ok=False))
        self.assertEqual(self.brigade("dish", "D1", "--state", "dropped", "--stopped", "idle"), "D1 dropped; T1 waiting again")
        self.assertEqual(self.land("lease", "list"), "no leases held")

    def test_an_entry_another_run_landed_says_mark_it_merged(self):
        self.started()
        sha = self.worker_commit("perf/d1", {"README.md": "fast\n"})
        self.passed(sha)
        self.submit(sha)
        self.brigade("dish", "D1", "--state", "queued")
        self.assertEqual(self.brigade("watch"), "D1: E1 queued")
        self.land("land")
        landed = subprocess.run(["git", "rev-parse", "refs/landing/lane"], cwd=self.project, capture_output=True,
                                text=True, check=True).stdout.strip()
        self.assertEqual(self.brigade("watch"), f"D1: landed as E1 ({landed[:12]}); mark it merged")

    def test_an_old_bounced_entry_is_ignored_for_the_current_sha(self):
        self.started("--check", "test ! -e BROKEN", paths="README.md,BROKEN")
        old = self.worker_commit("perf/d1", {"README.md": "fast\n", "BROKEN": "x\n"})
        self.passed(old)
        self.submit(old)
        self.brigade("dish", "D1", "--state", "queued")
        self.land("land")
        self.assertRegex(self.brigade("watch"), r"^D1: E1 bounced: checks failed")
        self.brigade("dish", "D1", "--thread", "worker-1")
        self.brigade("dish", "D1", "--state", "in-progress")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["thread"], "")
        self.assertIn("D1: in progress with no worker thread; launch a fresh worker", self.brigade("watch"))
        self.assertEqual(self.brigade("dish", "D1", "--thread", "worker-1", ok=False),
                         "brigade: thread worker-1 is an earlier attempt of D1; a send-back launches a fresh worker")
        self.brigade("dish", "D1", "--thread", "worker-2")
        new = self.worker_commit("perf/d1", {"BROKEN": None})
        self.passed(new)
        self.assertEqual(self.submit(new), "E2")
        self.brigade("dish", "D1", "--state", "queued")
        self.assertEqual(self.brigade("watch"), "D1: E2 queued")

    def test_an_old_bounce_at_a_sha_sharing_the_items_prefix_is_ignored(self):
        self.started()
        sha = self.worker_commit("perf/d1", {"README.md": "reviewed\n"})
        self.passed(sha)
        self.submit(sha)
        from contextlib import closing
        with closing(self.landing_db()) as db, db:
            db.execute("UPDATE entry SET sha = ?, state = 'bounced', note = 'old different SHA' WHERE id = 1",
                       (self.same_prefix(sha),))
            db.execute("UPDATE lease SET state = 'active' WHERE id = 1")
        self.assertEqual(self.brigade("watch"), "D1: passed, not submitted")

    def test_a_later_bounce_sharing_the_prefix_does_not_hide_the_queued_entry(self):
        self.started()
        sha = self.worker_commit("perf/d1", {"README.md": "reviewed\n"})
        self.passed(sha)
        self.assertEqual(self.submit(sha), "E1")
        self.brigade("dish", "D1", "--state", "queued")
        self.seed_entry(self.same_prefix(sha), "bounced", "other SHA")
        self.assertEqual(self.land("status", "--holder", "perf/D1").splitlines()[1],
                         f"E2 bounced (perf/D1, {sha[:12]}): other SHA")
        self.assertEqual(self.brigade("watch"), "D1: E1 queued")

    def test_a_drop_waits_for_the_worker_and_then_releases_the_lease(self):
        self.started()
        self.brigade("dish", "D1", "--thread", "w1")
        self.assertEqual(self.brigade("dish", "D1", "--state", "dropped", ok=False),
                         "brigade: D1 holds L1 and its worker may still be running; "
                         "wait for its run with t3_thread_wait, then pass --stopped <run id>")
        self.assertEqual(self.table_row(self.at, "dishes.tsv", "D1")["state"], "in-progress")
        self.assertEqual(self.land("lease", "list").split(" until ")[0], "L1 active perf/D1")
        self.assertIn("L1 held by perf/D1",
                      self.land("lease", "claim", "--holder", "engine/D1", "--paths", "README.md", ok=False))
        self.worker_commit("perf/d1", {"README.md": "late\n"})
        self.assertEqual(self.brigade("dish", "D1", "--state", "dropped", "--stopped", "r1"), "D1 dropped; T1 waiting again")
        dropped = [line for line in (self.at / "log.tsv").read_text().splitlines() if "\tD1\tdropped\t" in line]
        self.assertEqual(len(dropped), 1)
        self.assertIn("stopped r1", dropped[0])
        self.assertEqual(self.land("lease", "claim", "--holder", "engine/D1", "--paths", "README.md"), "L2")

    def test_a_late_worker_submit_after_the_drop_is_refused(self):
        self.started()
        self.brigade("dish", "D1", "--thread", "w1")
        self.worker_commit("perf/d1", {"README.md": "first\n"})
        case = Path(self.temporary.name)
        worktree = case / "perf-d1"
        script = (f"while [ ! -e {shlex.quote(str(case / 'go'))} ]; do sleep 0.01; done; "
                  f"echo late > README.md && git add -A && git -c user.name=t -c user.email=t@t commit -qm late && "
                  f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 't3/added/landing/scripts/land.py'))} "
                  f"--repo {shlex.quote(str(self.project))} submit --holder perf/D1 --branch perf/d1 "
                  f"--sha $(git rev-parse HEAD) --lease L1 --reviewer {CLAUDE}")
        worker = subprocess.Popen(["bash", "-c", script], cwd=worktree, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        try:
            self.assertEqual(self.brigade("dish", "D1", "--state", "dropped", "--stopped", "r1"), "D1 dropped; T1 waiting again")
            engine = self.store / "bridge-kit" / "engine"
            self.run_at(engine, "open", "--project-root", str(self.project), "--name", "Engine")
            (engine / "menu.md").write_text("## Purpose\n\nEngine.\n")
            self.run_at(engine, "ticket", "add", "--summary", "engine work")
            self.assertEqual(self.run_at(engine, "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "e",
                                         "--paths", "README.md"), "D1 (lease L2 held by engine/D1)")
            (case / "go").touch()
            _, err = worker.communicate(timeout=30)
        finally:
            worker.kill()
        self.assertEqual(worker.returncode, 1)
        self.assertEqual(err.strip(), "land: L1 is not an active lease held by perf/D1; claim one before submitting")
        self.assertEqual(self.land("status", "--holder", "perf/D1"), "no entries held by perf/D1")
        self.assertEqual([line.split(" until ")[0] for line in self.land("lease", "list").splitlines()],
                         ["L2 active engine/D1"])
        self.assertEqual(self.table_row(engine, "dishes.tsv", "D1")["lease"], "L2")

    def test_a_replaced_coordinators_paused_commands_change_nothing(self):
        self.init_landing()
        self.landing_env()
        docs = self.store / "bridge-kit" / "docs"
        self.run_at(docs, "open", "--project-root", str(self.project), "--name", "Docs")
        (docs / "menu.md").write_text("## Purpose\n\nDocs.\n")
        self.run_at(docs, "set", "--thread", "t1")
        old = ("--owner", "t1@1")
        self.assertIn("owner t1@1", self.run_at(docs, *old, "status"))
        self.run_at(docs, *old, "ticket", "add", "--summary", "one")
        self.assertEqual(self.run_at(docs, *old, "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "s",
                                     "--paths", "README.md"), "D1 (lease L1 held by docs/D1)")
        self.run_at(docs, *old, "dish", "D1", "--thread", "w1")
        self.land("lease", "renew", "L1", "--ttl-hours", "0", "--owner", "docs/@1")
        stale_brigade = "brigade: owner t1@1 is stale; this store is owned by t2@2"
        stale_land = "land: owner docs/@1 is stale; docs/ is at generation 2"
        land_script = str(ROOT / "t3/added/landing/scripts/land.py")
        commands = [
            ("pause", ["--store", str(self.store), "--at", str(docs), *old, "ticket", "add", "--summary", "late"], stale_brigade),
            ("pause", ["--store", str(self.store), "--at", str(docs), *old, "dish", "D1", "--state", "dropped", "--stopped", "r1"],
             stale_brigade),
            ("land-pause", ["--repo", str(self.project), "lease", "release", "L1", "--owner", "docs/@1"], stale_land),
            ("land-pause", ["--repo", str(self.project), "lease", "renew", "L1", "--owner", "docs/@1"], stale_land),
        ]
        running = []
        for number, (mode, args, message) in enumerate(commands):
            case = Path(self.temporary.name) / f"stale{number}"
            case.mkdir()
            running.append((case, self._spawn_race(mode, case, args), message))
        for case, _, _ in running:
            _wait_for_path(case / "paused", timeout=20)
        self.run_at(docs, "set", "--thread", "t2", "--replace")
        self.assertIn("owner t2@2", self.run_at(docs, "--owner", "t2@2", "status"))
        tables = ("rail.tsv", "log.tsv", "dishes.tsv", "restaurant.json")
        after_replace = {name: (docs / name).read_bytes() for name in tables}
        row = self.lease_row(1)
        for case, proc, message in running:
            (case / "proceed").touch()
            code, _, err = self._finish_race(proc)
            self.assertEqual((code, err), (1, message))
        self.assertEqual(self.run_at(docs, *old, "set", "--thread", "t2", "--workers", "9", ok=False), stale_brigade)
        self.assertEqual(self.run_at(docs, *old, "set", "--thread", "t1", ok=False), "brigade: thread t2 already recorded")
        self.assertEqual({name: (docs / name).read_bytes() for name in tables}, after_replace)
        self.assertEqual(self.lease_row(1), row)
        new = ("--owner", "t2@2")
        self.assertEqual(self.run_at(docs, *new, "ticket", "add", "--summary", "late"), "T2")
        self.assertEqual(self.land("lease", "renew", "L1", "--owner", "docs/@2"), "L1 renewed")
        self.assertEqual(self.land("lease", "release", "L1", "--owner", "docs/@2"), "L1 released")
        self.assertEqual(self.run_at(docs, *new, "dish", "D1", "--state", "dropped", "--stopped", "r1"), "D1 dropped; T1 waiting again")
        self.assertEqual(self.land("owner", "--prefix", "docs/", "--generation", "2"), "docs/ at generation 2")
        self.assertEqual(self.land("owner", "--prefix", "docs/", "--generation", "1", ok=False),
                         "land: docs/ is at generation 2; a floor never lowers")

    def test_a_store_without_a_generation_works_until_set_thread_records_one(self):
        self.open()
        meta = json.loads((self.at / "restaurant.json").read_text())
        meta["thread"] = "t1"
        (self.at / "restaurant.json").write_text(json.dumps(meta))
        self.assertEqual(self.brigade("ticket", "add", "--summary", "legacy"), "T1")
        self.brigade("set", "--thread", "t1")
        self.assertEqual(self.brigade("ticket", "add", "--summary", "unowned", ok=False, owner=False),
                         "brigade: this store is owned by t1@1; pass --owner <thread>@<generation> from status")
        self.assertEqual(self.brigade("--owner", "t1@1", "ticket", "add", "--summary", "owned"), "T2")


class StoresTest(unittest.TestCase):
    """Two or three coordinators on one project root, as `app/<name>` under the store."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Path(self.temporary.name) / "store"
        self.project = (Path(self.temporary.name) / "app").resolve()
        self.project.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def dir(self, name):
        return self.store / "app" / name

    def run_brigade(self, name, *args):
        if "--owner" not in args:
            args = (*_owner_words(self.dir(name)), *args)
        return subprocess.run([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.dir(name)), *args],
                              capture_output=True, text=True)

    def brigade(self, name, *args, ok=True):
        result = self.run_brigade(name, *args)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return (result.stdout if ok else result.stderr).strip()

    def open(self, name, *extra):
        return self.brigade(name, "open", "--project-root", str(self.project), "--name", name, *extra)

    def child(self, mode, name, *args):
        return subprocess.run([sys.executable, str(Path(__file__).resolve()), "--race-child", mode, self.temporary.name,
                               "--store", str(self.store), "--at", str(self.dir(name)), *args],
                              capture_output=True, text=True, timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def paused(self, label, name, *args):
        """A command that has parsed its arguments and waits before it takes the store lock."""
        case = Path(self.temporary.name) / label
        case.mkdir()
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--race-child", "pause", str(case),
                                 "--store", str(self.store), "--at", str(self.dir(name)), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        _wait_for_path(case / "paused", timeout=20)
        return case, proc

    def resume(self, case, proc):
        (case / "proceed").touch()
        _, err = proc.communicate(timeout=30)
        return proc.returncode, err.strip()

    def files(self, name):
        directory = self.dir(name)
        return {str(path.relative_to(directory)): path.read_bytes() for path in sorted(directory.rglob("*"))
                if path.is_file() and path.name != "restaurant.lock"}

    def rows(self, name, table):
        lines = (self.dir(name) / table).read_text().splitlines()
        return [line.split("\t") for line in lines[1:]]

    def inbox(self, name):
        return sorted(path.name for path in (self.dir(name) / "inbox").glob("*")) if (self.dir(name) / "inbox").exists() else []

    def handoff(self, source="core", target="engine", ref="https://github.com/o/r/issues/7"):
        """core owns github, files T1, and moves it to engine. Returns engine's inbox file."""
        self.open(source, "--intake", "github")
        self.open(target)
        self.brigade(source, "ticket", "add", "--summary", "Fix the cache", "--source", "github", "--ref", ref)
        self.brigade(source, "ticket", "move", "T1", "--to", target)
        return self.dir(target) / "inbox" / f"app~{source}~T1.json"


class HandoffTest(StoresTest):
    def test_a_sibling_cannot_claim_a_source_another_owns(self):
        self.open("docs", "--intake", "github")
        self.assertEqual(json.loads((self.dir("docs") / "restaurant.json").read_text())["intake"], ["github"])
        self.open("engine")
        self.assertEqual(self.brigade("engine", "set", "--intake", "github", ok=False),
                         "brigade: docs already owns intake from github; move tickets to it instead")
        self.assertEqual(self.brigade("engine", "open", "--project-root", str(self.project), "--name", "release",
                                      "--intake", "github", ok=False),
                         "brigade: docs already owns intake from github; move tickets to it instead")
        self.assertFalse(self.dir("release").exists())
        self.assertEqual(self.brigade("engine", "open", "--project-root", str(self.project), "--name", "engine",
                                      "--intake", "github"),
                         "\n".join([f"exists {self.dir('engine')}",
                                    "intake stays empty; change it with set --intake",
                                    f"sibling docs ({self.dir('docs')}), thread not recorded",
                                    "  purpose: not written yet",
                                    "  off the menu: not written yet"]))
        self.assertEqual(json.loads((self.dir("engine") / "restaurant.json").read_text())["intake"], [])

    def test_two_owners_of_one_source_both_refuse_to_file(self):
        self.open("core")
        self.open("docs")
        for name in ("core", "docs"):
            path = self.dir(name) / "restaurant.json"
            meta = json.loads(path.read_text())
            meta["intake"] = ["github"]
            path.write_text(json.dumps(meta))
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "x", "--source", "github", ok=False),
                         "brigade: docs owns intake from github; ask it to file this and move it here")
        self.assertEqual(self.brigade("docs", "ticket", "add", "--summary", "x", "--source", "github", ok=False),
                         "brigade: core owns intake from github; ask it to file this and move it here")
        self.assertEqual(self.rows("core", "rail.tsv") + self.rows("docs", "rail.tsv"), [])
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "from the user"), "T1")

    def test_one_ref_is_filed_once_while_live(self):
        ref = "https://github.com/o/r/issues/7"
        self.open("core", "--intake", "github")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "a", "--source", "github", "--ref", ref), "T1")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "b", "--source", "github", "--ref", f"  {ref} ", ok=False),
                         f"brigade: {ref} is already T1 (waiting); nothing added")
        self.brigade("core", "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "fix")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "b", "--ref", ref, ok=False),
                         f"brigade: {ref} is already T1 (assigned); nothing added")
        self.brigade("core", "ticket", "set", "T1", "--state", "done")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "reopened", "--source", "github", "--ref", ref), "T2")

    def test_dropping_a_source_with_a_waiting_ticket_is_refused(self):
        self.open("core", "--intake", "github,feed")
        self.brigade("core", "ticket", "add", "--summary", "a", "--source", "github")
        self.assertEqual(self.brigade("core", "set", "--intake", "feed", ok=False),
                         "brigade: T1 from github is waiting; finish, drop, or move it before dropping github from intake")
        self.brigade("core", "ticket", "set", "T1", "--state", "dropped")
        self.brigade("core", "set", "--intake", "feed")
        self.assertEqual(json.loads((self.dir("core") / "restaurant.json").read_text())["intake"], ["feed"])

    def test_move_hands_a_ticket_to_a_sibling_and_take_files_it(self):
        self.open("core", "--intake", "github")
        self.open("engine")
        self.brigade("engine", "set", "--thread", "thread-engine")
        self.brigade("core", "ticket", "add", "--summary", "Fix the cache", "--source", "github", "--ref", "R7")
        self.assertEqual(self.brigade("core", "ticket", "move", "T1", "--to", "engine"),
                         "T1 moved to engine; tell thread thread-engine")
        self.assertEqual(self.brigade("core", "ticket", "list"), "T1 moved [github] Fix the cache R7")
        self.assertEqual(self.brigade("core", "fire", "--tickets", "T1", "--station", "bug-fix", "--summary", "x", ok=False),
                         "brigade: T1 is moved, not waiting")
        self.assertEqual(self.brigade("core", "ticket", "set", "T1", "--state", "waiting", ok=False),
                         "brigade: T1 moved to engine; it is that coordinator's ticket now")
        self.assertEqual(json.loads((self.dir("engine") / "inbox" / "app~core~T1.json").read_text()),
                         {"handoff": "app/core/T1", "summary": "Fix the cache", "source": "github", "ref": "R7"})
        self.assertEqual(self.brigade("core", "watch"), "T1: moved to engine, waiting for ticket take")
        self.assertEqual(self.brigade("engine", "watch"), "handed to you: 1; run ticket take")
        self.assertEqual(self.brigade("engine", "status"),
                         "thread thread-engine\nreporting: milestones, no landing contract, handed to you: 1\nowner thread-engine@1")
        self.assertEqual(self.brigade("engine", "ticket", "take"), "T1 from app/core/T1: Fix the cache")
        self.assertEqual(self.brigade("engine", "ticket", "list"), "T1 waiting [github (from app/core/T1)] Fix the cache R7")
        self.assertEqual(self.inbox("engine"), [])
        self.assertEqual(self.brigade("core", "watch"), "no work in progress")
        self.assertEqual(self.brigade("engine", "watch"), "no work in progress")
        self.assertIn("## Handed to another coordinator\n\n- T1: Fix the cache (to engine)",
                      self.brigade("core", "close", "--dry-run"))
        self.assertEqual(self.brigade("engine", "ticket", "take"), "nothing handed to you")
        (self.dir("engine") / "inbox" / "app~core~T1.json").write_text("{}")
        self.assertEqual(self.brigade("core", "watch"), "no work in progress")

    def test_a_moved_ref_stays_live_through_each_move_until_the_end_of_the_chain_finishes(self):
        ref = "R7"
        self.open("core", "--intake", "github")
        self.open("docs")
        self.open("engine")
        self.brigade("core", "ticket", "add", "--summary", "s", "--source", "github", "--ref", ref)
        self.brigade("core", "ticket", "move", "T1", "--to", "docs")
        refused = f"brigade: {ref} is already T1 (moved); nothing added"
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "s", "--source", "github", "--ref", ref, ok=False), refused)
        self.brigade("docs", "ticket", "take")
        self.brigade("docs", "ticket", "move", "T1", "--to", "engine")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "s", "--source", "github", "--ref", ref, ok=False), refused)
        self.brigade("engine", "ticket", "take")
        self.assertEqual(self.brigade("engine", "ticket", "list"), f"T1 waiting [github (from app/docs/T1)] s {ref}")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "s", "--source", "github", "--ref", ref, ok=False), refused)
        self.brigade("engine", "ticket", "set", "T1", "--state", "done")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "s", "--source", "github", "--ref", ref), "T2")

    def test_a_new_owner_refuses_a_ref_the_old_owner_still_holds_live(self):
        self.handoff(source="core", target="docs", ref="R7")
        self.open("engine")
        self.brigade("core", "set", "--intake", "")
        self.brigade("engine", "set", "--intake", "github")
        self.assertEqual(self.brigade("engine", "ticket", "add", "--summary", "s", "--source", "github", "--ref", "R7", ok=False),
                         "brigade: R7 is already T1 in core (moved); nothing added")
        self.assertEqual(self.brigade("engine", "ticket", "add", "--summary", "s", "--source", "github", "--ref", "R8"), "T1")

    def test_a_replaced_owners_paused_move_does_not_republish_the_handoff(self):
        self.open("core", "--intake", "github")
        self.open("engine")
        self.brigade("core", "set", "--thread", "t1")
        self.brigade("core", "ticket", "add", "--summary", "Fix the cache", "--source", "github", "--ref", "R7")
        self.brigade("core", "ticket", "move", "T1", "--to", "engine")
        # The handoff was never delivered, so a rerun of the move would publish it again.
        (self.dir("engine") / "inbox" / "app~core~T1.json").unlink()
        stale = self.paused("stale", "core", "--owner", "t1@1", "ticket", "move", "T1", "--to", "engine")
        missing = self.paused("missing", "core", "ticket", "move", "T1", "--to", "engine")
        self.brigade("core", "--owner", "t1@1", "set", "--thread", "t2", "--replace")
        before = {"core": self.files("core"), "engine": self.files("engine")}
        self.assertEqual(self.resume(*stale), (1, "brigade: owner t1@1 is stale; this store is owned by t2@2"))
        self.assertEqual(self.resume(*missing),
                         (1, "brigade: this store is owned by t2@2; pass --owner <thread>@<generation> from status"))
        self.assertEqual({"core": self.files("core"), "engine": self.files("engine")}, before)
        self.assertEqual(self.inbox("engine"), [])
        self.brigade("core", "--owner", "t2@2", "ticket", "move", "T1", "--to", "engine")
        self.assertEqual(self.inbox("engine"), ["app~core~T1.json"])

    def test_a_replaced_owners_paused_take_does_not_delete_the_inbox_file(self):
        inbox = self.handoff()
        self.brigade("engine", "set", "--thread", "e1")
        handed = inbox.read_bytes()
        self.assertEqual(self.brigade("engine", "--owner", "e1@1", "ticket", "take"), "T1 from app/core/T1: Fix the cache")
        # A take that filed the ticket and died before deleting the file leaves exactly this.
        inbox.write_bytes(handed)
        stale = self.paused("stale", "engine", "--owner", "e1@1", "ticket", "take")
        missing = self.paused("missing", "engine", "ticket", "take")
        self.brigade("engine", "--owner", "e1@1", "set", "--thread", "e2", "--replace")
        before = self.files("engine")
        self.assertEqual(self.resume(*stale), (1, "brigade: owner e1@1 is stale; this store is owned by e2@2"))
        self.assertEqual(self.resume(*missing),
                         (1, "brigade: this store is owned by e2@2; pass --owner <thread>@<generation> from status"))
        self.assertEqual(self.files("engine"), before)
        self.assertEqual(self.inbox("engine"), ["app~core~T1.json"])
        self.assertEqual(self.brigade("engine", "--owner", "e2@2", "ticket", "take"), "T1 from app/core/T1: Fix the cache")
        self.assertEqual(self.inbox("engine"), [])

    def test_a_move_interrupted_after_the_rewrite_publishes_once_on_rerun(self):
        self.open("core", "--intake", "github")
        self.open("engine")
        self.brigade("core", "ticket", "add", "--summary", "Fix the cache", "--source", "github", "--ref", "R7")
        died = self.child("die-before-publish", "core", "ticket", "move", "T1", "--to", "engine")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual(self.brigade("core", "ticket", "list"), "T1 moved [github] Fix the cache R7")
        self.assertEqual(self.inbox("engine"), [])
        self.assertEqual(self.brigade("core", "watch"),
                         "T1: moved to engine, not delivered; run ticket move T1 --to engine again")
        self.brigade("core", "ticket", "move", "T1", "--to", "engine")
        self.brigade("core", "ticket", "move", "T1", "--to", "engine")
        self.assertEqual(self.inbox("engine"), ["app~core~T1.json"])
        self.assertEqual([row[1:4] for row in self.rows("core", "log.tsv") if row[3] == "moved"], [["ticket", "T1", "moved"]])
        self.brigade("engine", "ticket", "take")
        self.brigade("core", "ticket", "move", "T1", "--to", "engine")
        self.assertEqual(self.inbox("engine"), [])

    def test_watch_says_undelivered_when_the_inbox_file_is_deleted_before_take(self):
        self.handoff().unlink()
        self.assertEqual(self.brigade("core", "watch"),
                         "T1: moved to engine, not delivered; run ticket move T1 --to engine again")

    def test_take_twice_with_the_file_copied_back_files_one_ticket(self):
        inbox = self.handoff()
        saved = inbox.read_bytes()
        self.brigade("engine", "ticket", "take")
        inbox.write_bytes(saved)
        self.assertEqual(self.brigade("engine", "ticket", "take"), "T1 from app/core/T1: Fix the cache")
        self.assertEqual(len(self.rows("engine", "rail.tsv")), 1)
        self.assertEqual([row for row in self.rows("engine", "log.tsv") if row[4] == "from app/core/T1"],
                         [[self.rows("engine", "log.tsv")[0][0], "ticket", "T1", "waiting", "from app/core/T1"]])
        self.assertEqual(self.inbox("engine"), [])

    def assert_one_handed_ticket(self, name="engine"):
        self.assertEqual([row[2:] for row in self.rows(name, "rail.tsv")],
                         [["waiting", "github (from app/core/T1)", "https://github.com/o/r/issues/7", "", "Fix the cache"]])
        log = (self.dir(name) / "log.tsv").read_text()
        self.assertTrue(log.endswith("\n"))
        self.assertTrue(all(len(line.split("\t")) == 5 for line in log.splitlines()))
        self.assertEqual([row[1:] for row in self.rows(name, "log.tsv") if "from app/core/T1" in row[4]],
                         [["ticket", "T1", "waiting", "from app/core/T1"]])
        self.assertEqual(self.inbox(name), [])
        self.assertEqual(sorted(path.name for path in self.dir(name).glob("*.tmp")), [])

    def test_a_take_killed_before_its_log_row_finishes_on_the_next_take(self):
        self.handoff()
        died = self.child("die-before-log", "engine", "ticket", "take")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual(len(self.rows("engine", "rail.tsv")), 1)
        self.assertEqual(self.rows("engine", "log.tsv"), [])
        self.brigade("engine", "ticket", "take")
        self.assert_one_handed_ticket()

    def test_a_take_killed_inside_the_log_append_recovers_the_tail(self):
        self.handoff()
        died = self.child("die-inside-log-append", "engine", "ticket", "take")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertRegex((self.dir("engine") / "log.tsv").read_text(), r"\n[^\n]+\tticket\tT1\twaiting\tfro$")
        self.brigade("engine", "ticket", "take")
        self.assert_one_handed_ticket()

    def test_a_failure_inside_the_temp_file_write_leaves_the_same_end_state(self):
        self.handoff()
        module = runpy.run_path(str(SCRIPT))
        glob = module["run"].__globals__

        class FailingTempfile:
            @staticmethod
            def NamedTemporaryFile(*args, **kwargs):
                handle = tempfile.NamedTemporaryFile(*args, **kwargs)
                write = handle.write

                def fail(text):
                    write(text[:10])
                    handle.flush()
                    raise OSError(28, "No space left on device")

                handle.write = fail
                return handle

        glob["tempfile"] = FailingTempfile
        with self.assertRaises(OSError):
            glob["run"](["--at", str(self.dir("engine")), "ticket", "take"])
        glob["tempfile"] = tempfile
        self.assertEqual(self.rows("engine", "rail.tsv"), [])
        self.assertEqual(self.inbox("engine"), ["app~core~T1.json"])
        self.brigade("engine", "ticket", "take")
        self.assert_one_handed_ticket()

    def test_a_joined_line_stops_the_read_with_the_malformed_message(self):
        self.open("core")
        self.brigade("core", "ticket", "add", "--summary", "a")
        log = self.dir("core") / "log.tsv"
        stamp = "2026-10-06T00:00:00.000000+00:00"
        log.write_text(log.read_text() + f"{stamp}\tticket\tT6\twaiting\tfro{stamp}\tticket\tT7\twaiting\tnote\n")
        self.assertEqual(self.brigade("core", "ticket", "list", ok=False), "brigade: log.tsv line 3 is malformed; fix or remove it")
        log.write_text(log.read_text().replace(f"fro{stamp}\tticket\tT7\twaiting\tnote", "fro"))
        self.assertEqual(self.brigade("core", "ticket", "list"), "T1 waiting [user] a")
        log.write_text(log.read_text() + f"{stamp[:19]}\tticket")
        self.assertEqual(self.brigade("core", "ticket", "list"), "T1 waiting [user] a")
        log.write_text(log.read_text() + f"{stamp}\tticket\tT8\twaiting\tnote\n")
        self.assertEqual(self.brigade("core", "ticket", "list", ok=False), "brigade: log.tsv line 4 is malformed; fix or remove it")
        log.write_text(log.read_text().replace(f"{stamp[:19]}\tticket{stamp}", "2026-10-06"))
        self.assertEqual(self.brigade("core", "ticket", "list", ok=False), "brigade: log.tsv line 4 is malformed; fix or remove it")

    def test_a_table_that_is_not_utf8_names_the_line(self):
        self.open("core")
        self.brigade("core", "ticket", "add", "--summary", "a")
        rail = self.dir("core") / "rail.tsv"
        rail.write_bytes(rail.read_bytes() + b"\xff\n")
        self.assertEqual(self.brigade("core", "ticket", "list", ok=False),
                         "brigade: rail.tsv line 3 is malformed; fix or remove it")

    def test_a_short_append_restores_the_last_complete_row(self):
        from unittest import mock
        self.open("core")
        self.brigade("core", "ticket", "add", "--summary", "a")
        log = self.dir("core") / "log.tsv"
        complete = log.read_text()
        log.write_text(complete + "2026-10-06T00:00:00.000000+00:00\tticket\tT9\twai")
        glob = runpy.run_path(str(SCRIPT))["run"].__globals__
        restaurant = glob["Restaurant"](self.dir("core"))
        row = {"at": "2026-10-06T00:00:01.000000+00:00", "kind": "ticket", "id": "T2", "state": "waiting", "note": "b"}
        real_write = os.write
        with mock.patch.object(os, "write", lambda fd, data: real_write(fd, data[:7])):
            with self.assertRaises(glob["BrigadeError"]) as raised:
                restaurant.append("log.tsv", row)
        self.assertEqual(str(raised.exception), "wrote 7 of 53 bytes to log.tsv; nothing appended")
        self.assertEqual(log.read_text(), complete)
        restaurant.append("log.tsv", row)
        self.assertEqual(log.read_text(), complete + "2026-10-06T00:00:01.000000+00:00\tticket\tT2\twaiting\tb\n")

    def test_two_processes_appending_200_rows_each_leave_400_rows(self):
        self.open("core")
        procs = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--race-child", "append",
                                   self.temporary.name, str(self.dir("core")), label],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                 for label in ("a", "b")]
        (Path(self.temporary.name) / "go").touch()
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
        rows = self.rows("core", "log.tsv")
        self.assertEqual(len(rows), 400)
        self.assertTrue(all(len(row) == 5 for row in rows))
        self.assertEqual(sorted(row[4] for row in rows),
                         sorted(f"{label} row {number}" for label in "ab" for number in range(200)))
        self.assertEqual(self.brigade("core", "ticket", "list"), "")


LAND_SCRIPT = ROOT / "t3/added/landing/scripts/land.py"


def _land_has_rulings():
    """Change 8's share, lease reserve, and contest commands, which the admin's rulings run."""
    for words in (("share",), ("lease", "reserve"), ("contest",)):
        result = subprocess.run([sys.executable, str(LAND_SCRIPT), *words, "--help"], capture_output=True, text=True)
        if result.returncode != 0:
            return False
    return True


class AdminTest(StoresTest):
    """The executive admin at `app/.admin`, beside the coordinators on the same root."""

    STAMP = "2026-10-06T00:00:00Z"

    def admin(self, *args, ok=True):
        return self.brigade(".admin", *args, ok=ok)

    def open_admin(self, *extra, ok=True):
        return self.brigade(".admin", "open", "--admin", "--project-root", str(self.project), *extra, ok=ok)

    def meta(self, name):
        return json.loads((self.dir(name) / "restaurant.json").read_text())

    def relays(self):
        """The admin's relay rows as (store@offset, the copied row)."""
        return [(row[2], json.loads(row[4])) for row in self.rows(".admin", "log.tsv") if row[1] == "relay"]

    def ruling_events(self):
        """Ruling log rows as (id, state, note)."""
        return [tuple(row[2:5]) for row in self.rows(".admin", "log.tsv") if row[1] == "ruling"]

    def cursor(self, name):
        return self.meta(".admin").get("cursors", {}).get(f"app/{name}", 0)

    def size(self, name, table="log.tsv"):
        return len((self.dir(name) / table).read_bytes())

    def admin_child(self, mode, label, *args):
        """A race child running brigade.py on the admin store, started and not yet finished."""
        case = Path(self.temporary.name) / label
        case.mkdir()
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--race-child", mode, str(case),
                                 "--store", str(self.store), "--at", str(self.dir(".admin")), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return case, proc

    def append_row(self, name, *fields):
        """Restaurant.append of one log.tsv row in its own process, started and not yet finished."""
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--race-child", "append-row", self.temporary.name,
                                 str(self.dir(name)), *fields],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def finish(self, proc):
        out, err = proc.communicate(timeout=30)
        return proc.returncode, out.strip(), err.strip()

    def with_thread(self, thread="t1"):
        """docs, engine, and the admin, with the admin's thread recorded at generation 1."""
        self.open("docs")
        self.open("engine")
        self.open_admin()
        self.admin("set", "--thread", thread)

    # The store.

    def test_two_admin_opens_at_once_leave_one_store(self):
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), "--store", str(self.store), "open", "--admin",
                                   "--project-root", str(self.project)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        words = sorted(proc.communicate(timeout=30)[0].split()[0] for proc in procs)
        self.assertEqual(words, ["exists", "opened"])
        self.assertEqual(sorted(path.name for path in (self.store / "app").iterdir()), [".admin"])
        meta = self.meta(".admin")
        self.assertEqual((meta["role"], meta["projectRoot"]), ("admin", str(self.project)))
        self.assertTrue((self.dir(".admin") / "rulings.tsv").is_file())
        self.assertIn("## Priorities", (self.dir(".admin") / "menu.md").read_text())

    def test_open_admin_refuses_a_second_root_with_the_same_directory_name(self):
        other = Path(self.temporary.name) / "other" / "app"
        other.mkdir(parents=True)
        self.open_admin()
        before = (self.dir(".admin") / "restaurant.json").read_bytes()
        self.assertEqual(self.brigade(".admin", "open", "--admin", "--project-root", str(other), ok=False),
                         f"brigade: {self.dir('.admin')} already holds a coordinator for {self.project}; pick another --name")
        self.assertEqual((self.dir(".admin") / "restaurant.json").read_bytes(), before)
        self.assertEqual(self.open_admin("--name", "boss", ok=False), "brigade: open --admin takes no --name")
        self.assertEqual(self.brigade("docs", "open", "--project-root", str(self.project), ok=False),
                         "brigade: open needs --name, or --admin")

    def test_open_admin_prints_every_coordinator_and_walk_puts_it_first(self):
        self.open("docs")
        (self.dir("docs") / "menu.md").write_text("## Purpose\n\nKeep the docs right.\n\n## Off the menu\n\nEngine work\n")
        self.assertEqual(self.open_admin("--reporting", "digest"), "\n".join([
            f"opened {self.dir('.admin')}",
            f"sibling docs ({self.dir('docs')}), thread not recorded",
            "  purpose: Keep the docs right.",
            "  off the menu: Engine work",
        ]))
        self.open("engine")
        self.assertIn(f"sibling executive admin ({self.dir('.admin')}), thread not recorded",
                      self.brigade("engine", "open", "--project-root", str(self.project), "--name", "engine"))
        walked = self.brigade("docs", "walk", "--repo", str(self.project)).splitlines()
        self.assertEqual(walked[1:], [
            "  executive admin (reports digest): nothing on record",
            "    thread not recorded",
            "  docs (reports milestones): nothing on record",
            "    thread not recorded",
            "  engine (reports milestones): nothing on record",
            "    thread not recorded",
        ])

    def test_the_admin_store_refuses_work_commands(self):
        self.open_admin()
        refusal = "brigade: the executive admin routes work and never runs it"
        for args in (("fire", "--tickets", "T1", "--station", "feature", "--summary", "s"),
                     ("brief", "D1", "--goal", "g", "--verify", "v", "--base", "main", "--acceptance", "a"),
                     ("dish", "D1", "--state", "merged"),
                     ("dish", "D1", "--state", "dropped", "--stopped", "r1"),
                     ("pass", "check", "D1", "--sha", "abc"),
                     ("watch",)):
            self.assertEqual(self.admin(*args, ok=False), refusal, args)
        self.assertEqual(self.admin("ticket", "add", "--summary", "Add a FAQ"), "T1")
        self.assertEqual(self.admin("86", "add", "--question", "Which purpose?", "--options", "docs, engine",
                                    "--default", "docs"), "Q1")
        self.assertEqual(self.admin("status"),
                         "thread not recorded\nreporting: milestones, no landing contract, waiting tickets: 1, decisions for you: 1")

    def test_a_ref_moved_through_the_admin_frees_at_every_step_once_done(self):
        ref = "https://github.com/o/r/issues/7"
        self.open("core", "--intake", "github")
        for name in ("docs", "engine"):
            self.open(name)
        self.open_admin()

        def refused(ident):
            self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "again", "--source", "github", "--ref", ref,
                                          ok=False), f"brigade: {ref} is already {ident} (moved); nothing added")

        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "Fix", "--source", "github", "--ref", ref), "T1")
        self.brigade("core", "ticket", "move", "T1", "--to", ".admin")
        refused("T1")
        self.assertEqual(self.admin("inbox", "take"), "T1 from app/core/T1: Fix")
        refused("T1")
        self.assertEqual(self.admin("ticket", "move", "T1", "--to", "docs"), "T1 moved to docs; no thread recorded for docs")
        refused("T1")
        self.assertEqual(self.brigade("docs", "inbox", "take"), "T1 from app/.admin/T1: Fix")
        refused("T1")
        self.brigade("docs", "ticket", "set", "T1", "--state", "done")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "Fix", "--source", "github", "--ref", ref), "T2")
        self.brigade("core", "ticket", "move", "T2", "--to", ".admin")
        self.assertEqual(self.admin("inbox", "take"), "T2 from app/core/T2: Fix")
        self.admin("ticket", "move", "T2", "--to", "docs")
        self.assertEqual(self.brigade("docs", "inbox", "take"), "T2 from app/.admin/T2: Fix")
        self.brigade("docs", "ticket", "move", "T2", "--to", ".admin")
        refused("T2")
        self.assertEqual(self.admin("inbox", "take"), "T3 from app/docs/T2: Fix")
        refused("T2")
        self.admin("ticket", "move", "T3", "--to", "engine")
        refused("T2")
        self.assertEqual(self.brigade("engine", "ticket", "take"), "T1 from app/.admin/T3: Fix")
        refused("T2")
        self.brigade("engine", "ticket", "set", "T1", "--state", "done")
        self.assertEqual(self.brigade("core", "ticket", "add", "--summary", "Fix", "--source", "github", "--ref", ref), "T3")

    def test_rule_overrule_leaves_two_rows_and_close_lists_both(self):
        self.open("docs")
        self.open_admin()
        self.assertEqual(self.brigade("docs", "rule", "list", ok=False), "brigade: rule works only in the executive admin's store")
        self.assertEqual(self.admin("rule", "add", "--kind", "contested-paths", "--parties", "docs,engine",
                                    "--question", "Who claims README.md next?", "--rule", "age",
                                    "--decision", "docs claims README.md next"), "R1")
        self.assertEqual(self.admin("rule", "overrule", "R1", "--decision", "engine goes first"), "R2")
        rows = self.rows(".admin", "rulings.tsv")
        self.assertEqual([(row[0], row[2], row[3], row[5], row[6], row[7], row[8]) for row in rows], [
            ("R1", "contested-paths", "docs, engine", "age", "docs claims README.md next", "", "overruled"),
            ("R2", "contested-paths", "docs, engine", "user", "engine goes first", "R1", "in-force"),
        ])
        self.assertEqual(self.admin("rule", "list"), "\n".join([
            "R1 overruled contested-paths (docs, engine): Who claims README.md next? Decided by age: docs claims README.md next.",
            "R2 in-force contested-paths (docs, engine): Who claims README.md next? Decided by user: engine goes first. Supersedes R1.",
        ]))
        self.assertEqual(self.admin("rule", "list", "--state", "in-force").splitlines()[0][:12], "R2 in-force ")
        report = self.admin("close")
        self.assertIn("\n".join([
            "## Rulings", "",
            "- R1 overruled contested-paths (docs, engine): Who claims README.md next? Decided by age: docs claims README.md next.",
            "- R2 in-force contested-paths (docs, engine): Who claims README.md next? Decided by user: engine goes first. Supersedes R1.",
        ]), report)
        self.assertNotIn("## Rulings", self.admin("close"))
        self.assertEqual(self.admin("rule", "add", "--kind", "ownership", "--parties", "docs,engine", "--question", "Who owns T4?",
                                    "--rule", "priority", "--decision", "docs", "--supersedes", "R2"), "R3")
        self.assertEqual(self.admin("rule", "set", "R1", "--state", "done", ok=False),
                         "brigade: R1 is overruled; only an in-force ruling ends")
        self.assertEqual(self.admin("rule", "set", "R3", "--state", "expired"), "R3 expired")
        self.assertEqual(self.admin("rule", "set", "R3", "--state", "expired"), "R3 expired")
        self.assertEqual([row[8] for row in self.rows(".admin", "rulings.tsv")], ["overruled", "superseded", "expired"])
        self.assertEqual(self.admin("rule", "list", "--state", "in-force"), "no rulings")
        self.assertIn("invalid choice: 'guess'", self.admin("rule", "add", "--kind", "ownership", "--parties", "docs",
                                                          "--question", "q", "--rule", "guess", "--decision", "d", ok=False))

    def _ruling_section(self, *lines):
        return "\n".join(["## Rulings", "", *lines])

    def test_a_ruling_killed_before_its_log_row_is_named_by_the_next_close(self):
        self.open_admin()
        died = self.child("die-before-log", ".admin", "rule", "add", "--kind", "ownership", "--parties", "docs",
                          "--question", "Who owns this?", "--rule", "priority", "--decision", "docs")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual(self.admin("rule", "list"),
                         "R1 in-force ownership (docs): Who owns this? Decided by priority: docs.")
        section = self._ruling_section(
            "- R1 in-force ownership (docs): Who owns this? Decided by priority: docs.")
        self.assertIn(section, self.admin("close", "--dry-run"))
        self.assertEqual(self.ruling_events(), [])
        self.assertIn(section, self.admin("close"))
        self.assertEqual(self.ruling_events(), [("R1", "in-force", "docs")])
        self.assertNotIn("## Rulings", self.admin("close"))

    def test_an_overrule_killed_before_its_log_rows_is_named_by_the_next_close(self):
        self.open_admin()
        self.admin("rule", "add", "--kind", "contested-paths", "--parties", "docs,engine",
                   "--question", "Who claims README.md next?", "--rule", "age",
                   "--decision", "docs claims README.md next")
        died = self.child("die-before-log", ".admin", "rule", "overrule", "R1", "--decision", "engine goes first")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual(self.admin("rule", "list"), "\n".join([
            "R1 overruled contested-paths (docs, engine): Who claims README.md next? Decided by age: docs claims README.md next.",
            "R2 in-force contested-paths (docs, engine): Who claims README.md next? Decided by user: engine goes first. Supersedes R1.",
        ]))
        section = self._ruling_section(
            "- R1 overruled contested-paths (docs, engine): Who claims README.md next? Decided by age: docs claims README.md next.",
            "- R2 in-force contested-paths (docs, engine): Who claims README.md next? Decided by user: engine goes first. Supersedes R1.",
        )
        self.assertIn(section, self.admin("close", "--dry-run"))
        self.assertEqual(self.ruling_events(), [("R1", "in-force", "docs claims README.md next")])
        self.assertIn(section, self.admin("close"))
        self.assertEqual(self.ruling_events(), [
            ("R1", "in-force", "docs claims README.md next"),
            ("R2", "in-force", "engine goes first"),
            ("R1", "overruled", "replaced by R2"),
        ])
        self.assertNotIn("## Rulings", self.admin("close"))

    def test_an_ended_ruling_killed_before_its_log_row_is_named_by_the_next_close(self):
        self.open_admin()
        self.admin("rule", "add", "--kind", "ownership", "--parties", "docs",
                   "--question", "Who owns this?", "--rule", "priority", "--decision", "docs")
        died = self.child("die-before-log", ".admin", "rule", "set", "R1", "--state", "done")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual(self.admin("rule", "list"),
                         "R1 done ownership (docs): Who owns this? Decided by priority: docs.")
        section = self._ruling_section(
            "- R1 done ownership (docs): Who owns this? Decided by priority: docs.")
        self.assertIn(section, self.admin("close", "--dry-run"))
        self.assertEqual(self.ruling_events(), [("R1", "in-force", "docs")])
        self.assertIn(section, self.admin("close"))
        self.assertEqual(self.ruling_events(), [
            ("R1", "in-force", "docs"),
            ("R1", "done", "docs (done)"),
        ])
        self.assertNotIn("## Rulings", self.admin("close"))

    def test_the_next_rule_add_logs_a_killed_ruling_once(self):
        self.open_admin()
        died = self.child("die-before-log", ".admin", "rule", "add", "--kind", "ownership", "--parties", "docs",
                          "--question", "Who owns this?", "--rule", "priority", "--decision", "docs")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual(self.admin("rule", "add", "--kind", "ownership", "--parties", "engine",
                                    "--question", "Who owns T4?", "--rule", "priority", "--decision", "engine"), "R2")
        self.assertEqual(self.ruling_events(), [
            ("R1", "in-force", "docs"),
            ("R2", "in-force", "engine"),
        ])

    # Requests.

    def test_a_request_waits_in_the_inbox_until_done(self):
        self.open("docs")
        self.open_admin()
        self.brigade("docs", "set", "--thread", "th-docs")
        self.assertEqual(self.brigade("docs", "request", "--to", "docs", "x", ok=False),
                         "brigade: request works only in the executive admin's store")
        self.assertEqual(self.admin("request", "--to", "engine", "x", ok=False),
                         f"brigade: engine is not a sibling coordinator on {self.project}")
        line = "from-user docs: add a FAQ"
        self.assertEqual(self.admin("request", "--to", "docs", line), "A1 for docs; tell thread th-docs")
        self.assertEqual(self.inbox("docs"), ["A1.line"])
        # A failed send leaves the file. The coordinator's next wake prints it again.
        for _ in range(2):
            self.assertEqual(self.brigade("docs", "inbox", "take"), f"A1: {line}")
        self.assertIn("requests from the user: 1", self.brigade("docs", "status"))
        self.assertEqual(self.brigade("docs", "inbox", "done", "A1"), "A1 done")
        self.assertEqual(self.brigade("docs", "inbox", "done", "A1"), "A1 done")
        self.assertEqual(self.brigade("docs", "inbox", "done", "A2", ok=False), "brigade: no request A2 in the inbox")
        self.assertEqual(self.inbox("docs"), [])
        self.assertEqual(self.brigade("docs", "inbox", "take"), "nothing handed to you")
        self.assertNotIn("requests from the user", self.brigade("docs", "status"))
        self.assertEqual([row[1:4] for row in self.rows("docs", "log.tsv") if row[1] == "inbox-done"], [["inbox-done", "A1", "done"]])
        self.assertEqual([row[1:] for row in self.rows(".admin", "log.tsv") if row[1] == "request"],
                         [["request", "A1", "sent", f"to docs: {line}"]])

    def test_a_request_killed_before_its_file_is_republished_once(self):
        self.open("docs")
        self.open_admin()
        died = self.child("die-before-publish", ".admin", "request", "--to", "docs", "from-user docs: add a FAQ")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual(self.inbox("docs"), [])
        self.assertEqual(self.admin("request", "--republish"), "A1 republished for docs")
        self.assertEqual(self.admin("request", "--republish"), "nothing to republish")
        self.assertEqual(self.inbox("docs"), ["A1.line"])
        self.brigade("docs", "inbox", "done", "A1")
        self.assertEqual(self.admin("request", "--republish"), "nothing to republish")
        self.assertEqual(self.inbox("docs"), [])
        self.admin("sync")
        self.assertEqual(self.admin("request", "--republish"), "nothing to republish")
        self.assertEqual(self.inbox("docs"), [])

    def test_republish_before_sync_skips_a_request_the_coordinator_finished(self):
        self.open("core")
        self.open_admin()
        self.brigade("core", "set", "--thread", "core-thread")
        self.admin("set", "--thread", "admin-thread")
        self.assertEqual(self.admin("request", "--to", "core", "reports-to core admin-thread"),
                         "A1 for core; tell thread core-thread")
        self.assertEqual(self.brigade("core", "inbox", "take"), "A1: reports-to core admin-thread")
        self.assertEqual(self.brigade("core", "inbox", "done", "A1"), "A1 done")
        self.assertEqual(self.admin("request", "--republish"), "nothing to republish")
        self.assertEqual(self.inbox("core"), [])
        self.assertNotIn("requests from the user", self.brigade("core", "status"))

    def test_a_request_finished_while_republish_runs_stays_finished(self):
        self.open("core")
        self.open_admin()
        self.brigade("core", "set", "--thread", "core-thread")
        self.admin("request", "--to", "core", "reports-to core admin-thread")
        case, proc = self.admin_child("pause-after-finished", "republish", "request", "--republish")
        _wait_for_path(case / "paused", timeout=20)
        self.assertEqual(self.brigade("core", "inbox", "done", "A1"), "A1 done")
        self.assertEqual(self.inbox("core"), [])
        (case / "proceed").touch()
        code, out, err = self.finish(proc)
        self.assertEqual(code, 0, err)
        self.assertNotIn("requests from the user", self.brigade("core", "status"))
        self.assertEqual(self.brigade("core", "inbox", "take"), "nothing handed to you")
        self.assertEqual(self.inbox("core"), [])
        self.assertEqual(self.admin("request", "--republish"), "nothing to republish")
        self.assertEqual(self.inbox("core"), [])

    def test_a_replayed_from_user_request_files_no_second_ticket(self):
        self.open("docs")
        self.assertEqual(self.brigade("docs", "ticket", "add", "--summary", "Add a FAQ", "--request", "A1"), "T1")
        refusal = "brigade: request A1 is already T1; nothing added"
        self.assertEqual(self.brigade("docs", "ticket", "add", "--summary", "Add a FAQ", "--request", "A1", ok=False), refusal)
        self.brigade("docs", "ticket", "set", "T1", "--state", "done")
        self.assertEqual(self.brigade("docs", "ticket", "add", "--summary", "Add a FAQ", "--request", "A1", ok=False), refusal)
        self.assertEqual(self.brigade("docs", "ticket", "list"), "T1 done [user (request A1)] Add a FAQ")
        self.assertEqual(self.brigade("docs", "ticket", "add", "--summary", "Other", "--request", "A2"), "T2")

    # Sync.

    def test_sync_relays_each_row_once_and_a_kill_before_the_cursor_copies_nothing_twice(self):
        self.open("docs")
        self.open_admin()
        start = self.size("docs")
        self.brigade("docs", "ticket", "add", "--summary", "one")
        sync = self.admin("sync")
        self.assertRegex(sync, r"^docs \S+ ticket T1 waiting: one$")
        self.assertEqual([(ident, row["kind"], row["id"], row["state"], row["note"]) for ident, row in self.relays()],
                         [(f"app/docs@{start}", "ticket", "T1", "waiting", "one")])
        self.assertEqual(self.cursor("docs"), self.size("docs"))
        self.assertEqual(self.admin("sync"), "nothing new")
        self.brigade("docs", "ticket", "add", "--summary", "two")
        before = self.cursor("docs")
        died = self.child("die-before-cursor", ".admin", "sync")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual(self.cursor("docs"), before)
        self.assertEqual(len(self.relays()), 2)
        self.assertEqual(self.admin("sync"), "nothing new")
        self.assertEqual([row["note"] for _, row in self.relays()], ["one", "two"])
        self.assertEqual(self.cursor("docs"), self.size("docs"))

    def test_a_row_appended_while_sync_runs_is_relayed_once_even_with_an_older_timestamp(self):
        self.open("docs")
        self.open_admin()
        self.brigade("docs", "ticket", "add", "--summary", "one")
        case, proc = self.admin_child("pause", "sync", "sync")
        _wait_for_path(case / "paused", timeout=20)
        older = "2020-01-01T00:00:00Z"
        appended = self.append_row("docs", older, "ticket", "T9", "waiting", "late")
        self.assertEqual(self.finish(appended)[0], 0)
        (case / "proceed").touch()
        code, out, err = self.finish(proc)
        self.assertEqual(code, 0, err)
        self.assertEqual([row["note"] for _, row in self.relays()], ["one"])
        self.assertRegex(self.admin("sync"), rf"^docs {older} ticket T9 waiting: late$")
        self.assertEqual(self.admin("sync"), "nothing new")
        self.assertEqual([row["note"] for _, row in self.relays()], ["one", "late"])

    def test_an_older_sync_finishing_late_never_moves_the_cursor_back(self):
        self.open("docs")
        self.open_admin()
        self.brigade("docs", "ticket", "add", "--summary", "one")
        case, outer = self.admin_child("pause", "outer", "sync")
        _wait_for_path(case / "paused", timeout=20)
        self.brigade("docs", "ticket", "add", "--summary", "two")
        self.admin("sync")
        consumed = self.size("docs")
        self.assertEqual(self.cursor("docs"), consumed)
        (case / "proceed").touch()
        code, out, err = self.finish(outer)
        self.assertEqual((code, out), (0, "nothing new"), err)
        self.assertEqual(self.cursor("docs"), consumed)
        self.assertEqual([row["note"] for _, row in self.relays()], ["one", "two"])

    def test_sync_leaves_an_unfinished_tail_and_relays_the_repaired_row_once(self):
        self.open("engine")
        self.open("core")
        self.open_admin()
        for number in range(1, 7):
            self.brigade("engine", "ticket", "add", "--summary", f"engine {number}")
        for number in range(1, 6):
            self.brigade("core", "ticket", "add", "--summary", f"core {number}")
        self.brigade("engine", "ticket", "move", "T6", "--to", "core")
        self.admin("sync")
        died = self.child("die-inside-log-append", "core", "ticket", "take")
        self.assertEqual(died.returncode, 9, died.stderr)
        tail = self.cursor("core")
        self.assertRegex((self.dir("core") / "log.tsv").read_bytes()[tail:].decode(), r"^[^\n]+\tticket\tT6\twaiting\tfro$")
        relayed = len(self.relays())
        self.assertEqual(self.admin("sync"), "nothing new")
        self.assertEqual((len(self.relays()), self.cursor("core")), (relayed, tail))
        self.brigade("core", "ticket", "take")
        self.admin("sync")
        new = self.relays()[relayed:]
        self.assertEqual([(ident, row["kind"], row["id"], row["state"], row["note"]) for ident, row in new],
                         [(f"app/core@{tail}", "ticket", "T6", "waiting", "from app/engine/T6")])

    def test_a_joined_line_stops_sync_at_that_line(self):
        self.open("docs")
        self.open_admin()
        self.admin("sync")
        log = self.dir("docs") / "log.tsv"
        good = f"{self.STAMP}\tticket\tT1\twaiting\tgood\n"
        joined = f"{self.STAMP}\tticket\tT6\twaiting\tfro{self.STAMP}\tticket\tT7\twaiting\tnote\n"
        at = self.size("docs") + len(good)
        log.write_text(log.read_text() + good + joined + f"{self.STAMP}\tticket\tT8\twaiting\tafter\n")
        self.assertEqual(self.admin("sync", ok=False), f"brigade: app/docs/log.tsv at byte {at} is malformed; nothing past it relayed")
        self.assertEqual(self.cursor("docs"), at)
        self.assertEqual([row["note"] for _, row in self.relays()], ["good"])
        self.assertEqual(self.admin("sync", ok=False), f"brigade: app/docs/log.tsv at byte {at} is malformed; nothing past it relayed")
        self.assertEqual([row["note"] for _, row in self.relays()], ["good"])

    def test_a_reader_racing_a_tail_repair_relays_only_the_repaired_row(self):
        self.open("core")
        self.open_admin()
        self.admin("sync")
        log = self.dir("core") / "log.tsv"
        tail = self.size("core")
        self.assertEqual(self.cursor("core"), tail)
        log.write_bytes(log.read_bytes() + f"{self.STAMP}\tticket\tT6\twaiting\tfro".encode())
        case, sync = self.admin_child("read16", "reader", "sync")
        _wait_for_path(case / "paused", timeout=20)
        append = self.append_row("core", "2026-10-06T00:00:01Z", "ticket", "T7", "waiting", "from app/engine/T7")
        time.sleep(1)
        self.assertIsNone(append.poll(), "the append waits for the reader's lock")
        (case / "proceed").touch()
        code, out, err = self.finish(sync)
        self.assertEqual((code, out), (0, "nothing new"), err)
        self.assertEqual(self.cursor("core"), tail)
        self.assertEqual(self.finish(append)[0], 0)
        self.assertEqual(self.relays(), [])
        self.admin("sync")
        self.assertEqual(self.relays(), [(f"app/core@{tail}", {"at": "2026-10-06T00:00:01Z", "kind": "ticket", "id": "T7",
                                                                 "state": "waiting", "note": "from app/engine/T7"})])
        self.assertEqual(log.read_bytes()[tail:], b"2026-10-06T00:00:01Z\tticket\tT7\twaiting\tfrom app/engine/T7\n")

    def test_snapshot_under_another_stores_lock_raises_and_returns_nothing(self):
        self.open("docs")
        self.open_admin()
        glob = runpy.run_path(str(SCRIPT))["run"].__globals__
        admin = glob["Restaurant"](self.dir(".admin"))
        docs = glob["Restaurant"](self.dir("docs"))
        log = docs.dir / "log.tsv"
        start = log.stat().st_size
        tail = b"2026-10-06T00:00:00Z\tticket\tT6\twaiting\tfro"
        log.write_bytes(log.read_bytes() + tail)
        fabricated = b"2026-10-06T00:00:00Z\tticket\tT6\twaiting\tfrom app/engine/T7\n"
        reads = [0]

        def chunk(fd):
            data = os.read(fd, 16)
            reads[0] += len(data)
            if reads[0] == 42 and data:
                proc = subprocess.run([sys.executable, "-B", "-c",
                                       "import runpy,sys; g=runpy.run_path(sys.argv[1]); r=g['Restaurant'](sys.argv[2]); "
                                       "r.append('log.tsv',{'at':'2026-10-06T00:00:01Z','kind':'ticket','id':'T7',"
                                       "'state':'waiting','note':'from app/engine/T7'})",
                                       str(SCRIPT), str(docs.dir)],
                                      capture_output=True, text=True, timeout=3)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            return data

        original_read = glob["read_chunk"]
        glob["read_chunk"] = chunk
        copied = b""
        with admin.locked():
            try:
                copied = docs.snapshot("log.tsv", start)
            except glob["BrigadeError"] as error:
                self.assertEqual(str(error),
                                 f"cannot read {docs.dir} while holding {admin.dir.resolve()}'s lock; "
                                 "read other stores before taking a lock")
            else:
                self.fail(f"snapshot returned {copied!r}")
        self.assertEqual(copied, b"")
        self.assertNotEqual(log.read_bytes()[start:], fabricated)
        glob["read_chunk"] = original_read
        with docs.locked():
            self.assertEqual(docs.snapshot("log.tsv", start), tail)

    # Recovery.

    def test_two_recoveries_racing_leave_one_claim(self):
        self.with_thread()
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.dir(".admin")),
                                   "set", "--thread", f"recovering:{name}", "--replace", "--expect", "t1"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("a", "b")]
        results = [(proc.communicate(timeout=30), proc.returncode) for proc in procs]
        self.assertEqual(sorted(code for _, code in results), [0, 1])
        meta = self.meta(".admin")
        self.assertIn(meta["thread"], ("recovering:a", "recovering:b"))
        self.assertEqual((meta["generation"], meta["previousThread"]), (2, "t1"))
        loser = next(err for (_, err), code in results if code == 1)
        self.assertEqual(loser.strip(), f"brigade: thread is {meta['thread']}, not t1; nothing replaced")

    def test_recovery_needs_a_stopped_run_and_retirement_raises_the_generation(self):
        self.with_thread()
        self.admin("set", "--schedule", "intake=s-1")
        self.assertEqual(self.admin("set", "--thread", "t9", "--replace", ok=False),
                         "brigade: the executive admin's thread changes only with --replace --expect <old>")
        claim = json.loads(self.admin("set", "--thread", "recovering:t9", "--replace", "--expect", "t1"))
        self.assertEqual((claim["thread"], claim["generation"], claim["previousThread"], claim["schedules"]),
                         ("recovering:t9", 2, "t1", {"intake": "s-1"}))
        before = (self.dir(".admin") / "restaurant.json").read_bytes()
        self.assertEqual(self.admin("set", "--thread", "t2", "--replace", "--expect", "recovering:t9", ok=False),
                         "brigade: the old run has not been confirmed stopped; wait for it with t3_thread_wait, then pass --stopped <run id>")
        self.assertEqual((self.dir(".admin") / "restaurant.json").read_bytes(), before)
        self.admin("set", "--thread", "t2", "--replace", "--expect", "recovering:t9", "--stopped", "r1")
        self.assertEqual(self.admin("status").splitlines()[0], "thread t2")
        self.assertEqual(self.admin("status").splitlines()[-1], "owner t2@3")
        self.assertEqual(self.meta(".admin")["previousThread"], "t1")
        self.assertEqual([row[1:] for row in self.rows(".admin", "log.tsv") if row[1] == "thread"], [
            ["thread", "t1@1", "recorded", "first thread"],
            ["thread", "recovering:t9@2", "recorded", "replaced t1"],
            ["thread", "t2@3", "recorded", "replaced recovering:t9; stopped r1"],
        ])
        self.admin("set", "--thread", "", "--replace", "--expect", "t2")
        self.assertEqual([row[2:] for row in self.rows(".admin", "log.tsv") if row[1] == "thread" and row[2] == "@4"],
                         [["@4", "recorded", "replaced t2"]])
        self.assertEqual(self.admin("--owner", "t2@3", "status").splitlines()[0], "thread not recorded")
        self.assertEqual(self.admin("--owner", "t2@3", "ticket", "add", "--summary", "late", ok=False),
                         "brigade: owner t2@3 is stale; this store is owned by @4")
        self.admin("set", "--thread", "recovering:t5", "--replace", "--expect", "")
        self.admin("set", "--thread", "t6", "--replace", "--expect", "recovering:t5", "--stopped", "gone")
        meta = self.meta(".admin")
        self.assertEqual((meta["thread"], meta["generation"], meta["previousThread"]), ("t6", 6, "t2"))

    def test_a_replacement_killed_before_the_metadata_write_retries_with_one_row(self):
        self.with_thread()
        self.admin("set", "--thread", "recovering:t9", "--replace", "--expect", "t1")
        died = self.child("die-before-thread", ".admin", "set", "--thread", "t2", "--replace",
                          "--expect", "recovering:t9", "--stopped", "r1")
        self.assertEqual(died.returncode, 9, died.stderr)
        meta = self.meta(".admin")
        self.assertEqual((meta["thread"], meta["generation"]), ("recovering:t9", 2))
        self.assertEqual([row[1:] for row in self.rows(".admin", "log.tsv") if row[2] == "t2@3"], [
            ["thread", "t2@3", "recorded", "replaced recovering:t9; stopped r1"],
        ])
        self.admin("set", "--thread", "t2", "--replace", "--expect", "recovering:t9", "--stopped", "r1")
        meta = self.meta(".admin")
        self.assertEqual((meta["thread"], meta["generation"], meta["previousThread"]), ("t2", 3, "t1"))
        self.assertEqual([row[2] for row in self.rows(".admin", "log.tsv") if row[1] == "thread" and row[2] == "t2@3"],
                         ["t2@3"])

    def test_a_replacement_killed_inside_the_log_leaves_the_old_thread(self):
        self.with_thread()
        self.admin("set", "--thread", "recovering:t9", "--replace", "--expect", "t1")
        before = (self.dir(".admin") / "restaurant.json").read_bytes()
        died = self.child("die-before-log", ".admin", "set", "--thread", "t2", "--replace",
                          "--expect", "recovering:t9", "--stopped", "r1")
        self.assertEqual(died.returncode, 9, died.stderr)
        self.assertEqual((self.dir(".admin") / "restaurant.json").read_bytes(), before)
        self.assertNotIn("stopped r1", (self.dir(".admin") / "log.tsv").read_text())
        self.admin("set", "--thread", "t2", "--replace", "--expect", "recovering:t9", "--stopped", "r1")
        meta = self.meta(".admin")
        self.assertEqual((meta["thread"], meta["generation"], meta["previousThread"]), ("t2", 3, "t1"))
        self.assertEqual([row[2:] for row in self.rows(".admin", "log.tsv") if "stopped r1" in row[4]],
                         [["t2@3", "recorded", "replaced recovering:t9; stopped r1"]])

    def test_the_claim_raises_the_admin_floor_in_the_landing_store(self):
        env = dict(os.environ, XDG_STATE_HOME=str(Path(self.temporary.name) / "state"))
        git = lambda *command: subprocess.run(command, cwd=self.project, capture_output=True, text=True, check=True)
        git("git", "init", "-q", "-b", "main")
        (self.project / "a.txt").write_text("a\n")
        git("git", "add", "-A")
        git("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        land = lambda *args: subprocess.run([sys.executable, str(LAND_SCRIPT), "--repo", str(self.project), *args],
                                            capture_output=True, text=True, env=env)
        self.assertEqual(land("init", "--trunk", "lane", "--mode", "local", "--base", "main").returncode, 0)
        self.open_admin()
        for args in (("set", "--thread", "t1"), ("set", "--thread", "recovering:t9", "--replace", "--expect", "t1")):
            result = subprocess.run([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.dir(".admin")), *args],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(land("owner", "--prefix", ".admin/", "--generation", "1").stderr.strip(),
                         "land: .admin/ is at generation 2; a floor never lowers")

    def test_expect_works_only_in_the_admin_store(self):
        self.open("docs")
        self.brigade("docs", "set", "--thread", "t1")
        self.assertEqual(self.brigade("docs", "set", "--thread", "t2", "--replace", "--expect", "t1", ok=False),
                         "brigade: --expect works only in the executive admin's store")
        self.assertEqual(self.meta("docs")["thread"], "t1")

    def test_a_log_write_that_read_restaurant_json_before_a_replacement_keeps_the_new_thread(self):
        self.with_thread()
        glob = runpy.run_path(str(SCRIPT))["run"].__globals__
        restaurant = glob["Restaurant"](self.dir(".admin"), "t1@1")
        self.assertEqual(restaurant.meta["thread"], "t1")
        self.admin("set", "--thread", "recovering:t9", "--replace", "--expect", "t1")
        after_claim = self.files(".admin")
        with self.assertRaises(glob["BrigadeError"]) as raised:
            restaurant.log("ticket", "T1", "waiting", "one")
        self.assertEqual(str(raised.exception), "owner t1@1 is stale; this store is owned by recovering:t9@2")
        self.assertEqual(self.files(".admin"), after_claim)
        unfenced = glob["Restaurant"](self.dir(".admin"))
        unfenced.unfenced = True
        unfenced.log("ticket", "T1", "waiting", "one")
        meta = self.meta(".admin")
        self.assertEqual((meta["thread"], meta["generation"], meta["previousThread"]), ("recovering:t9", 2, "t1"))

    def test_reports_to_is_recorded_cleared_and_shown_by_status(self):
        self.open("docs")
        self.brigade("docs", "set", "--thread", "th-docs")
        self.brigade("docs", "set", "--reports-to", "th-admin")
        self.assertEqual(self.meta("docs")["reportsTo"], "th-admin")
        self.assertEqual(self.brigade("docs", "status"),
                         "thread th-docs\nreporting: milestones, no landing contract\nreports to th-admin\nowner th-docs@1")
        self.brigade("docs", "set", "--reports-to", "")
        self.assertNotIn("reportsTo", self.meta("docs"))
        self.assertEqual(self.brigade("docs", "status"), "thread th-docs\nreporting: milestones, no landing contract\nowner th-docs@1")

    # The owner fence.

    def test_claim_first_then_a_paused_rule_add_is_refused(self):
        self.with_thread()
        case, proc = self.admin_child("pause", "rule", "--owner", "t1@1", "rule", "add", "--kind", "ownership",
                                      "--parties", "docs,engine", "--question", "Who owns T1?", "--rule", "priority",
                                      "--decision", "docs")
        _wait_for_path(case / "paused", timeout=20)
        self.admin("set", "--thread", "recovering:t9", "--replace", "--expect", "t1")
        after_claim = self.files(".admin")
        (case / "proceed").touch()
        code, _, err = self.finish(proc)
        self.assertEqual((code, err), (1, "brigade: owner t1@1 is stale; this store is owned by recovering:t9@2"))
        self.assertEqual(self.files(".admin"), after_claim)
        self.assertEqual(self.rows(".admin", "rulings.tsv"), [])

    def test_after_a_terminal_wait_the_old_runs_paused_commands_change_nothing(self):
        self.with_thread()
        self.brigade("docs", "ticket", "add", "--summary", "one")
        self.admin("ticket", "add", "--summary", "route me")
        old = ("--owner", "t1@1")
        paused = [self.admin_child("pause", f"stale{number}", *old, *args) for number, args in enumerate((
            ("rule", "add", "--kind", "ownership", "--parties", "docs,engine", "--question", "q", "--rule", "priority", "--decision", "d"),
            ("request", "--to", "docs", "from-user docs: late"),
            ("sync",),
            ("ticket", "move", "T1", "--to", "docs"),
        ))]
        for case, _ in paused:
            _wait_for_path(case / "paused", timeout=20)
        self.admin("set", "--thread", "recovering:t9", "--replace", "--expect", "t1")
        self.admin("set", "--thread", "t2", "--replace", "--expect", "recovering:t9", "--stopped", "r1")
        self.assertEqual(self.admin("status").splitlines()[-1], "owner t2@3")
        self.assertIn("docs", self.admin("sync"))
        admin_after, docs_after = self.files(".admin"), self.files("docs")
        for case, proc in paused:
            (case / "proceed").touch()
            code, _, err = self.finish(proc)
            self.assertEqual((code, err), (1, "brigade: owner t1@1 is stale; this store is owned by t2@3"))
        self.assertEqual(self.files(".admin"), admin_after)
        self.assertEqual(self.files("docs"), docs_after)
        self.assertEqual(self.inbox("docs"), [])

    def test_a_write_that_took_the_lock_before_the_claim_finishes_first(self):
        self.with_thread()
        case, proc = self.admin_child("hold", "hold", "--owner", "t1@1", "rule", "add", "--kind", "ownership",
                                      "--parties", "docs,engine", "--question", "Who owns T1?", "--rule", "priority",
                                      "--decision", "docs")
        _wait_for_path(case / "holding", timeout=20)
        claim = subprocess.Popen([sys.executable, str(SCRIPT), "--store", str(self.store), "--at", str(self.dir(".admin")),
                                  "set", "--thread", "recovering:t9", "--replace", "--expect", "t1"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: claim.poll() is None and claim.kill())
        time.sleep(0.5)
        self.assertIsNone(claim.poll(), "the claim waits for the lock")
        (case / "proceed").touch()
        self.assertEqual(self.finish(proc)[:2], (0, "R1"))
        self.assertEqual(self.finish(claim)[0], 0)
        kinds = [(row[1], row[2]) for row in self.rows(".admin", "log.tsv")]
        self.assertLess(kinds.index(("ruling", "R1")), kinds.index(("thread", "recovering:t9@2")))
        self.assertEqual(self.meta(".admin")["thread"], "recovering:t9")

    @unittest.skipUnless(_land_has_rulings(), "land.py has no share, lease reserve, or contest yet; change 8 adds them, "
                                              "and this test runs once it lands")
    def test_after_a_recovery_the_old_runs_paused_land_rulings_change_nothing(self):
        state = Path(self.temporary.name) / "state"
        env = dict(os.environ, XDG_STATE_HOME=str(state))
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)})
        patcher.start()
        self.addCleanup(patcher.stop)

        def land(*args, ok=True):
            result = subprocess.run([sys.executable, str(LAND_SCRIPT), "--repo", str(self.project), *args],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
            return (result.stdout if ok else result.stderr).strip()

        git = lambda *command: subprocess.run(command, cwd=self.project, capture_output=True, text=True, check=True)
        git("git", "init", "-q", "-b", "main")
        (self.project / "README.md").write_text("a\n")
        git("git", "add", "-A")
        git("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        land("init", "--trunk", "lane", "--mode", "local", "--base", "main", "--cap", "3")
        self.with_thread()
        contest = land("contest", "--holders", "docs/D1,engine/D1", "--owner", ".admin/@1")
        old = ("--owner", ".admin/@1")
        commands = [("share", "--for", "docs/", "1", *old),
                    ("lease", "reserve", "--for", "docs/", "--paths", "README.md", "--ruling", "R1", *old),
                    ("contest", "--settle", contest, "--first", "docs/D1", *old)]
        running = []
        for number, args in enumerate(commands):
            case = Path(self.temporary.name) / f"land{number}"
            case.mkdir()
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--race-child", "land-pause", str(case),
                                     "--repo", str(self.project), *args],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            self.addCleanup(lambda proc=proc: proc.poll() is None and proc.kill())
            running.append((case, proc))
        for case, _ in running:
            _wait_for_path(case / "paused", timeout=20)
        self.admin("set", "--thread", "recovering:t9", "--replace", "--expect", "t1")
        self.admin("set", "--thread", "t2", "--replace", "--expect", "recovering:t9", "--stopped", "r1")
        before = (land("lease", "list"), land("status"))
        for case, proc in running:
            (case / "proceed").touch()
            code, _, err = self.finish(proc)
            self.assertEqual((code, err), (1, "land: owner .admin/@1 is stale; .admin/ is at generation 3"))
        self.assertEqual((land("lease", "list"), land("status")), before)


class LivenessIdleDocTest(unittest.TestCase):
    def test_step_5_nudges_an_idle_worker_with_its_timebox_open(self):
        text = (ROOT / "t3/added/brigade/SKILL.md").read_text()
        section = text.split("## Liveness check", 1)[1].split("\n## ", 1)[0]
        step = next(line for line in section.splitlines() if line.startswith("5. "))
        self.assertIn("activeRunId", step)
        self.assertIn("nudge-<dish>-<that run id>", step)
        self.assertIn("goes to step 4", step)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--race-child":
        try:
            _race_child(sys.argv[2], sys.argv[3], sys.argv[4:])
        except TimeoutError as error:
            print(f"race child timed out waiting for {error}", file=sys.stderr)
            sys.exit(2)
    else:
        unittest.main()
