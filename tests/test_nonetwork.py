"""The run's own guard: no test resolves a name that is not loopback.

Everything here goes through `socket` itself rather than through the guard's
internals, because the thing worth pinning is that the HOOK fires -- a check
wired to something the suite never calls would pass every one of these while
checking nothing.

Two addresses appear throughout, both reserved for documentation and neither
belonging to anybody: 198.51.100.9 and 203.0.113.4. Neither is ever resolved
by these tests; they are literals, and the guard decides a literal without
asking anyone.
"""

import ipaddress
import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from . import nonetwork
from .nonetwork import NonLoopbackResolution

# A routable literal. Not loopback, spelled as an address so that refusing it
# needs no resolver at all.
OFF_MACHINE = "198.51.100.9"
ALSO_OFF_MACHINE = "203.0.113.4"

# What was hooked WHEN THIS MODULE WAS IMPORTED, which is before any test in
# it has run.
#
# Read here rather than inside the test that asserts on it, and the difference
# is the whole point. `Arming.test_arming_it_again_changes_nothing` calls
# `install()`, and it sorts ahead of the test that checks the hooks are in
# place -- so a run in which `tests/__init__` never armed the guard at all was
# armed by that test instead, and the check passed. Measured: with the arming
# line replaced by `pass`, the whole suite stayed green. Captured at import,
# no test can arm the guard early enough to hide it.
_HOOKED_AT_IMPORT = {
    "getaddrinfo": socket.getaddrinfo,
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
    "run": unittest.TestCase.run,
    "start": threading.Thread.start,
}


def _answer(address):
    """One `getaddrinfo` row for `address`, shaped the way the real one is."""
    ip = ipaddress.ip_address(address)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (address, 0))


def _resolver(*addresses):
    """A stand-in resolver answering with exactly `addresses`.

    Only as capable as the real one in the way that matters here: it answers
    with rows. Called with no addresses it answers with an empty list, which
    the real resolver never does -- a resolver with no answer raises. That
    case is here because `all()` of nothing is true, and the guard must not
    read an empty answer as a verdict.
    """
    def resolve(host, port, *args, **kwargs):
        return [_answer(address) for address in addresses]
    return resolve


class _Base(unittest.TestCase):
    def setUp(self):
        # Nothing a test here provokes may leak into the run's own verdict.
        self.addCleanup(nonetwork.take)

    def refused(self, call):
        """Run `call`, require the guard to refuse it, return what it
        recorded."""
        with self.assertRaises(NonLoopbackResolution):
            call()
        return nonetwork.take()

    def allowed(self, call):
        """Run `call`, require the guard to have recorded nothing, return
        whatever it answered.

        Two claims in one, and that is only safe where the call is one every
        platform answers the same way. Where it is not, use `passed_through`.
        """
        answer = call()
        self.assertEqual(nonetwork.take(), [])
        return answer

    def passed_through(self, call):
        """Run `call` and require only that the GUARD had no opinion on it.

        Separate from `allowed` because "the guard did not screen this" and
        "the call succeeded" are different claims and only the first is the
        guard's business. An empty host answers with the wildcard address on
        one platform and refuses with EAI_NONAME on another; `allowed` asserts
        the call returns, so using it here pinned whichever platform the test
        was written on and broke on the other.

        The resolver's own refusal is tolerated. A `NonLoopbackResolution` is
        NOT -- it is outside `OSError`, so it escapes this handler and errors
        the test, and a guard that started screening these still fails here.
        """
        try:
            call()
        except OSError:
            pass
        self.assertEqual(nonetwork.take(), [])


class WhatCountsAsLoopback(_Base):
    """The rule is about what an address IS, not how it was written."""

    def test_the_dotted_quad_is_allowed(self):
        self.allowed(lambda: socket.getaddrinfo("127.0.0.1", None))

    def test_the_rest_of_the_loopback_block_is_allowed_too(self):
        # The whole of 127/8 is loopback. A rule comparing against the one
        # spelling everybody writes would refuse this.
        self.allowed(lambda: socket.getaddrinfo("127.0.0.2", None))

    def test_the_v6_loopback_is_allowed(self):
        # The same interface, in the other protocol's spelling. A string
        # comparison against a dotted quad cannot possibly accept this.
        self.allowed(lambda: socket.getaddrinfo("::1", None))

    def test_a_name_is_allowed_when_everything_it_answers_with_is_loopback(self):
        # A NAME, not a literal, and one this machine answers for itself.
        # This is the spelling the acceptance criterion is about: nothing in
        # the text of it looks like 127.0.0.1.
        self.allowed(lambda: socket.getaddrinfo("localhost", None))

    def test_an_address_off_this_machine_is_refused(self):
        self.assertEqual([host for host, _, _ in
                          self.refused(lambda: socket.getaddrinfo(
                              OFF_MACHINE, None))],
                         [OFF_MACHINE])


class DecidingAName(_Base):
    """A name cannot be decided from its spelling, so it is decided from
    every address it answers with."""

    def test_a_name_answering_off_this_machine_is_refused(self):
        with patch.object(nonetwork, "_real_getaddrinfo",
                          _resolver(OFF_MACHINE)):
            recorded = self.refused(
                lambda: socket.getaddrinfo("an-invented-name", None))
        self.assertEqual([host for host, _, _ in recorded],
                         ["an-invented-name"])

    def test_one_routable_answer_among_loopback_ones_is_still_refused(self):
        # `any` in place of `all` here would let a name through on the
        # strength of its most convenient answer while a client picked
        # whichever the resolver put first.
        with patch.object(nonetwork, "_real_getaddrinfo",
                          _resolver("127.0.0.1", OFF_MACHINE, "::1")):
            self.refused(lambda: socket.getaddrinfo("an-invented-name", None))

    def test_a_name_that_answers_with_nothing_is_refused(self):
        # `all()` of no answers is true. A name with no evidence behind it
        # must not inherit the verdict for loopback from an empty sequence.
        with patch.object(nonetwork, "_real_getaddrinfo", _resolver()):
            self.refused(lambda: socket.getaddrinfo("an-invented-name", None))

    def test_a_name_that_does_not_resolve_is_refused(self):
        # The shape the original twelve had: a reserved name that answers
        # nowhere today. It is a violation because of what it ASKED for, not
        # because of what came back -- on a resolver that answers wildcards
        # the same call would come back with a routable address.
        def raising(host, port, *args, **kwargs):
            raise socket.gaierror(socket.EAI_NONAME, "no answer")

        with patch.object(nonetwork, "_real_getaddrinfo", raising):
            self.refused(lambda: socket.getaddrinfo("an-invented-name", None))

    def test_a_name_answering_only_loopback_is_allowed(self):
        with patch.object(nonetwork, "_real_getaddrinfo",
                          _resolver("127.0.0.1", "::1")):
            self.allowed(lambda: socket.getaddrinfo("an-invented-name", None))


class NoHostAtAll(_Base):
    """A wildcard bind names nobody, so there is nothing here to judge.

    Both tests below assert that the GUARD had no opinion, and nothing about
    what the resolver went on to do. That is the rule -- "no host to judge" --
    and it is the only part that is the same everywhere: an empty host answers
    with the wildcard address on one platform and refuses with EAI_NONAME on
    another, while the guard's own decision is identical on both because it
    never gets as far as asking.

    Not a skip, because there is nothing here that cannot be unified. The
    property holds on every platform; it was the assertion that did not.
    """

    def test_a_missing_host_is_not_screened(self):
        # With AI_PASSIVE this asks for 0.0.0.0, which is NOT loopback -- so a
        # guard that let it reach the address rules would refuse every server
        # in the suite that binds every interface.
        self.passed_through(lambda: socket.getaddrinfo(None, 0,
                                                       flags=socket.AI_PASSIVE))

    def test_an_empty_host_is_not_screened(self):
        self.passed_through(lambda: socket.getaddrinfo("", 0,
                                                       flags=socket.AI_PASSIVE))

    def test_a_host_given_as_bytes_is_refused_under_its_own_name(self):
        # `getaddrinfo` takes bytes as well as text. Left as bytes, the host
        # reported to whoever reads the failure -- and the host a declaration
        # is matched against -- would be a different value from the one the
        # test wrote.
        recorded = self.refused(
            lambda: socket.getaddrinfo(OFF_MACHINE.encode(), None))
        self.assertEqual([host for host, _, _ in recorded], [OFF_MACHINE])


class Connecting(_Base):
    """`connect` resolves its own host, in C, without going back through
    `socket.getaddrinfo` -- so it needs its own hook."""

    def _listener(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        return server.getsockname()

    def _client(self, timeout=None):
        client = socket.socket()
        self.addCleanup(client.close)
        if timeout is not None:
            client.settimeout(timeout)
        return client

    def _bounded_client(self):
        """A client for the addresses that must be REFUSED.

        The timeout is never reached while the guard is armed -- it refuses
        before a packet leaves. It bounds the case where the guard is not
        armed, so that deleting the hook fails in seconds instead of sitting
        on a reserved address until the kernel gives up. Set only here: on
        the loopback tests below it would be a deadline for work that is
        supposed to succeed, and a loaded run can miss one of those.
        """
        return self._client(timeout=5)

    def test_connecting_to_loopback_is_allowed(self):
        address = self._listener()
        client = self._client()
        self.allowed(lambda: client.connect(address))

    def test_connecting_off_this_machine_is_refused(self):
        client = self._bounded_client()
        self.assertEqual([host for host, _, _ in
                          self.refused(lambda: client.connect(
                              (ALSO_OFF_MACHINE, 9)))],
                         [ALSO_OFF_MACHINE])

    def test_connect_ex_to_loopback_is_allowed(self):
        address = self._listener()
        client = self._client()
        self.assertEqual(self.allowed(lambda: client.connect_ex(address)), 0)

    def test_connect_ex_off_this_machine_is_refused(self):
        # `connect_ex` reports failure by returning an errno rather than
        # raising, so a caller that ignores the number would sail past a
        # refusal that was only a return value.
        client = self._bounded_client()
        self.assertEqual([host for host, _, _ in
                          self.refused(lambda: client.connect_ex(
                              (ALSO_OFF_MACHINE, 9)))],
                         [ALSO_OFF_MACHINE])

    def test_an_address_with_no_host_is_left_to_the_socket_layer(self):
        # Indexed without first checking there is anything to index, the
        # guard's own IndexError stands where the socket layer's complaint
        # about a malformed address belongs.
        client = self._client()
        with self.assertRaises(Exception) as ctx:
            client.connect(())
        self.assertNotIsInstance(ctx.exception, IndexError)
        self.assertEqual(nonetwork.take(), [])

    def test_a_filesystem_socket_is_none_of_this_rules_business(self):
        # Its address is a path, not a host and port. Screened as though it
        # were one, the first character of the path gets treated as a name to
        # resolve and every such socket in the suite becomes a violation.
        directory = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, directory)
        path = os.path.join(directory, "socket")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        with self.assertRaises(FileNotFoundError):
            client.connect(path)
        self.assertEqual(nonetwork.take(), [])


class DeclaringAResolution(_Base):
    """A test whose subject IS a resolution says so, rather than being broken
    by the rule."""

    def test_a_declared_host_is_neither_refused_nor_recorded(self):
        with patch.object(nonetwork, "_real_getaddrinfo",
                          _resolver(OFF_MACHINE)):
            with nonetwork.resolving("an-invented-name"):
                self.allowed(
                    lambda: socket.getaddrinfo("an-invented-name", None))

    def test_a_declared_host_is_not_resolved_by_the_guard_itself(self):
        # Declared BEFORE the address is decided, so a test about a name that
        # answers nowhere does not have the guard go and ask about it.
        asked = []

        def counting(host, port, *args, **kwargs):
            asked.append(host)
            return [_answer("127.0.0.1")]

        with patch.object(nonetwork, "_real_getaddrinfo", counting):
            with nonetwork.resolving("an-invented-name"):
                socket.getaddrinfo("an-invented-name", None)
        self.assertEqual(asked, ["an-invented-name"])

    def test_the_declaration_stops_at_the_end_of_the_block(self):
        with patch.object(nonetwork, "_real_getaddrinfo",
                          _resolver(OFF_MACHINE)):
            with nonetwork.resolving("an-invented-name"):
                socket.getaddrinfo("an-invented-name", None)
            self.refused(lambda: socket.getaddrinfo("an-invented-name", None))

    def test_a_declaration_covers_only_the_host_it_names(self):
        with patch.object(nonetwork, "_real_getaddrinfo",
                          _resolver(OFF_MACHINE)):
            with nonetwork.resolving("an-invented-name"):
                self.refused(
                    lambda: socket.getaddrinfo("another-invented-name", None))


def _inner_case(body):
    """A `TestCase` whose one test runs `body`.

    Built inside a function, not declared at module level, because the loader
    collects every `TestCase` subclass it can see in a module -- a test that
    exists to fail would be discovered and would fail the run.
    """
    class Inner(unittest.TestCase):
        def runTest(self):
            body()

    return Inner()


def _swallowing_a_refusal():
    """Reach off this machine and swallow whatever came back, the way the
    code under test swallows it: behind a handler that catches everything and
    rewraps it."""
    try:
        socket.getaddrinfo(OFF_MACHINE, None)
    except BaseException:
        pass


def _swallowing_a_refusal_on_a_thread():
    thread = threading.Thread(target=_swallowing_a_refusal)
    thread.start()
    thread.join()


class TheVerdictOnEachTest(_Base):
    """What makes a violation a failed run rather than a swallowed exception.

    The twelve resolutions this guard was built for all happened inside a
    worker thread whose exception the code under test catches and rewraps, so
    raising at the point of resolution reaches nobody. The record is what
    fails the run.
    """

    def _run_inner(self, body):
        case = _inner_case(body)
        result = unittest.TestResult()
        case.run(result)
        return case, result

    def test_a_test_that_resolved_off_this_machine_fails(self):
        _, result = self._run_inner(_swallowing_a_refusal)

        self.assertEqual(result.testsRun, 1)
        # An error rather than a failure would mean the raise escaped the
        # inner test, which is the case this guard cannot rely on.
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn(OFF_MACHINE, result.failures[0][1])
        self.assertFalse(result.wasSuccessful())

    def test_a_test_that_resolved_nothing_still_passes(self):
        _, result = self._run_inner(lambda: None)

        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])
        self.assertTrue(result.wasSuccessful())

    def test_a_test_that_resolved_only_loopback_still_passes(self):
        _, result = self._run_inner(
            lambda: socket.getaddrinfo("127.0.0.1", None))

        self.assertEqual(result.failures, [])
        self.assertTrue(result.wasSuccessful())

    def test_a_resolution_on_a_worker_thread_fails_the_test_too(self):
        _, result = self._run_inner(_swallowing_a_refusal_on_a_thread)

        self.assertEqual(len(result.failures), 1)
        self.assertIn(OFF_MACHINE, result.failures[0][1])

    def test_the_failure_names_the_test_the_resolution_came_from(self):
        # A worker outlives the test that started it, so the test a failure is
        # attached to is often not the one that caused it. What is reported
        # has to say which test set the work going, or it sends whoever reads
        # it to the wrong file.
        case, result = self._run_inner(_swallowing_a_refusal_on_a_thread)

        self.assertIn(case.id(), result.failures[0][1])

    def test_a_worker_that_outlives_its_test_is_attributed_to_it(self):
        # The window the stamp exists for, and the ONLY one that can tell the
        # stamp from falling back to whatever is running now. The test above
        # joins its worker before it ends, so the test that started the work
        # is still the test that is running and both answers agree -- it
        # cannot fail if the stamp is dropped. All twelve of the resolutions
        # this guard was built for landed here instead: after the test that
        # configured the address had finished.
        release, finished = threading.Event(), threading.Event()
        worker = []

        def wait_then_resolve():
            release.wait(10)
            _swallowing_a_refusal()
            finished.set()

        def body():
            thread = threading.Thread(target=wait_then_resolve)
            worker.append(thread)
            thread.start()

        case, result = self._run_inner(body)
        # Nothing had happened yet, so the inner test itself is clean: this is
        # a violation with no test of its own left to attach it to.
        self.assertEqual(result.failures, [])

        release.set()
        worker[0].join(10)
        self.assertTrue(finished.is_set())

        self.assertNotEqual(case.id(), self.id())
        self.assertEqual([origin for _, origin, _ in nonetwork.take()],
                         [case.id()])

    def test_the_test_being_run_is_recorded_and_put_back(self):
        # Two rules at once, and both are about attribution rather than about
        # catching anything: what a violation on this thread is blamed on has
        # to be the test that is running, and an inner run has to hand that
        # back when it finishes. Left set to the inner test, everything after
        # it in the outer test is filed under a test that already ended.
        case, _ = self._run_inner(lambda: None)
        try:
            socket.getaddrinfo(OFF_MACHINE, None)
        except NonLoopbackResolution:
            pass

        self.assertNotEqual(case.id(), self.id())
        self.assertEqual([origin for _, origin, _ in nonetwork.take()],
                         [self.id()])

    def test_a_reported_violation_is_not_reported_a_second_time(self):
        # Read rather than taken, one violation would fail the test that
        # caused it and then every test after it, for the rest of the run.
        self._run_inner(_swallowing_a_refusal)

        self.assertEqual(nonetwork.take(), [])

    def test_a_violation_before_any_test_is_reported_against_the_first(self):
        # Nothing recorded may be dropped on the floor: a resolution during
        # collection, or in a `setUpModule`, belongs to the run even though no
        # test was running when it happened.
        try:
            socket.getaddrinfo(OFF_MACHINE, None)
        except NonLoopbackResolution:
            pass

        _, result = self._run_inner(lambda: None)

        self.assertEqual(len(result.failures), 1)
        self.assertIn(OFF_MACHINE, result.failures[0][1])


class Arming(_Base):
    def test_the_run_was_already_armed_before_this_module_ran(self):
        # The whole guard is worth nothing if `tests/__init__` did not arm it,
        # and everything else here would pass just as happily against an
        # unarmed `socket` if the assertions were only about return values.
        #
        # Against `_HOOKED_AT_IMPORT`, never against `socket` as it is now:
        # the test below arms the guard itself and sorts ahead of this one, so
        # reading `socket` here would be reading its own side effect.
        self.assertEqual(_HOOKED_AT_IMPORT, {
            "getaddrinfo": nonetwork._getaddrinfo,
            "connect": nonetwork._connect,
            "connect_ex": nonetwork._connect_ex,
            "run": nonetwork._run,
            "start": nonetwork._start,
        })

    def test_arming_it_again_changes_nothing(self):
        nonetwork.install()

        self.assertIs(socket.getaddrinfo, nonetwork._getaddrinfo)
        # And the originals still point at the real implementations: captured
        # inside `install`, a second call would leave the guard calling itself
        # and the first loopback resolution after it would recurse forever.
        self.allowed(lambda: socket.getaddrinfo("127.0.0.1", None))
