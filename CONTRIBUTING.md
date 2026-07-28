# Contributing

## A new test has to be proven to fail before it is trusted

A test that has never been watched failing does not hold. Several tests in
this repository have been found to pass for a reason other than the one they
name — one passed before the code it tested even existed. Passing on its
first run is not evidence against any of that; the only test of a test is
watching it fail, for the reason it claims to guard, and then restoring the
code so it passes again.

**Before trusting a new test, or an existing test extended to cover new
behaviour:**

1. Break the *specific* behaviour the test names — comment out the guard,
   invert the condition, delete the line the test's own name is about.
2. Run the test. Confirm it fails, and that the failure is the one you meant
   to see — not an unrelated error from a fixture that no longer builds, and
   not a bare `SyntaxError` from the mutation itself standing in for a real
   result.
3. Restore the code exactly. Commit only once the test has gone red for the
   reason it exists, and green again afterward.

Do this for the whole test, not just its first assertion: a test with
several assertions can still pass against a mutation that only defeats the
second or third of them, if the first was already enough to turn it red for
an unrelated reason.

This costs one throwaway edit and one extra test run per new test. It is not
a note to add to a pull request description — it is the only way to tell a
real guard rail apart from one that merely reads like it exists.
