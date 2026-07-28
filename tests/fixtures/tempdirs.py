"""Cleanup for tests whose fixtures hand back a throwaway directory (a git
repo, a fake PATH entry, a copied script tree) instead of a file object with
its own cleanup. Left alone, a full run of this project's suite leaves dozens
of these behind -- the guard, hook and adapter tests build a fresh repository
or configuration directory per test method and never remove it.

`mkdtemp()` is a drop-in replacement for `tempfile.mkdtemp()` that also
registers the directory here for automatic removal; `TempDirCleanup` is a
`unittest.TestCase` mixin whose `tearDown` sweeps whatever this process has
created since the last sweep.

The tracking is a single MODULE-level list, not one per test case, and that
is deliberate rather than an oversight: several of the fixtures this exists
for are plain functions shared across test FILES (`tests.test_guard._repo`
is imported directly by `tests.test_commit_msg_hook`), so a directory can be
created from a helper that has no `self` to call `addCleanup` on, and no way
to know which test file's mixin ought to claim it. A module list works
because `unittest` runs one test at a time: whatever is pending when a test's
`tearDown` fires is, in practice, everything that test created, however many
layers of helper function it went through to get there.

`tearDown`, not `addCleanup`, for the same reason: nothing here is ever
handed a running test's `self` to register a callback on. `tearDown` still
gives the property that actually matters -- it runs whether the test passed,
failed an assertion, or raised partway through building its own fixture, so
a leak cannot hide behind "that test happened to fail." The one case it does
NOT cover -- `setUp` itself raising, which skips `tearDown` too -- does not
apply to any of the classes this mixin is used on: none of them create a
directory before their test body runs.
"""
import shutil
import tempfile

_pending = []


def mkdtemp():
    """Same as `tempfile.mkdtemp()`, plus registering the result for
    automatic removal by whichever test's `tearDown` runs next."""
    d = tempfile.mkdtemp()
    _pending.append(d)
    return d


class TempDirCleanup:
    """Mix in ahead of `unittest.TestCase` (so this `tearDown` runs and then
    chains to the real one, not the other way around) for any test class
    whose fixtures create directories through this module's `mkdtemp`."""

    def tearDown(self):
        while _pending:
            shutil.rmtree(_pending.pop(), ignore_errors=True)
        super().tearDown()
