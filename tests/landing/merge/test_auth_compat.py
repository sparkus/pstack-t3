from ..support import setUpModule, LandingTest as LandingTestBase, land, os, sys


class AuthCompatibilityTest(LandingTestBase):
    def use_auth_status(self, active_supported, logged_in):
        real = os.environ["LAND_GH"]
        wrapper = self.base / "gh-auth-status"
        wrapper.write_text(f'''#!{sys.executable}
import os, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    if "--active" in args and not {active_supported!r}:
        print("unknown flag: --active", file=sys.stderr)
        sys.exit(1)
    if not {logged_in!r} and ("--active" in args or not {active_supported!r}):
        print("You are not logged into any GitHub hosts.", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
os.execv({real!r}, [{real!r}, *args])
''')
        wrapper.chmod(0o755)
        os.environ["LAND_GH"] = str(wrapper)

    def assert_init_accepts_auth(self, mode, active_supported):
        with self.fake_gh():
            self.use_auth_status(active_supported, logged_in=True)
            self.assertIn("created", self.init(mode=mode))
        with land.Store.for_repo(self.work) as store:
            self.assertEqual(store.contract["mode"], mode)

    def test_human_init_accepts_logged_in_gh_without_active(self):
        self.assert_init_accepts_auth("human", active_supported=False)

    def test_merge_init_accepts_logged_in_gh_without_active(self):
        self.assert_init_accepts_auth("merge", active_supported=False)

    def test_human_init_accepts_logged_in_gh_with_active(self):
        self.assert_init_accepts_auth("human", active_supported=True)

    def test_merge_init_accepts_logged_in_gh_with_active(self):
        self.assert_init_accepts_auth("merge", active_supported=True)

    def test_init_refuses_logged_out_gh_with_and_without_active(self):
        for active_supported in (False, True):
            for mode in ("human", "merge"):
                with self.subTest(active_supported=active_supported, mode=mode), self.fake_gh():
                    self.use_auth_status(active_supported, logged_in=False)
                    err = self.land("init", "--trunk", "main", "--mode", mode,
                                    "--check", "true", ok=False)
                    self.assertIn("run gh auth login", err)
                    self.assertEqual(list((self.base / "state").glob("pstack-t3/landing/*/land.db")), [])
