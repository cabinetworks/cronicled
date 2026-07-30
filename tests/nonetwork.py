"""Enforcement, during the run, of one property of this suite:

    no test resolves a name that is not loopback.

NOT "no test opens a socket". That property is false by design and always
will be: the web tests serve real HTTP on a local port, and an HTTP server
cannot be exercised without binding one. Measured over a full run, a rule
banning sockets would have banned 54 loopback resolutions and 54 loopback
connects that are all legitimate, and caught twelve resolutions of names
outside the machine, which are the ones worth catching -- they consult the
resolver, so on a host whose resolver answers wildcards the outcome of a test
would depend on the network it ran on.

The rule is therefore decided on what an address IS, never on how it was
spelled. `::1` and any address in 127/8 are loopback, and so is whatever
`localhost` answers with here; a rule comparing against the string
"127.0.0.1" would refuse all three of those spellings but one.

An address literal is decided without asking anybody. A NAME cannot be:
nothing about the text of a name says which address it stands for, so the
name is resolved and then judged on every address it answered with. That is
the residual, and it is deliberate -- a name still reaches the resolver once,
because the alternative is to decide names by their spelling, which is the
one thing this rule must not do. What it removes is the part that mattered:
the ANSWER can no longer change whether a test passes.

Armed from `tests/__init__.py`, so it covers a whole discovery run and a
single module run alike, and it covers background threads -- which is where
all twelve of the original resolutions happened, inside a worker the code
under test had started.
"""

import contextlib
import ipaddress
import socket
import sys
import threading
import unittest


class NonLoopbackResolution(BaseException):
    """Raised where something under test asks for an address off this machine.

    A `BaseException` rather than an `Exception` on purpose. The code under
    test relays whatever a request raised across a worker thread and wraps
    anything it does not recognise, and every ordinary `except Exception:` on
    a transport path would swallow this one. Being outside that hierarchy is
    not a guarantee that it always escapes -- a handler catching
    `BaseException` still catches it, and one does -- which is why the RECORD
    below, not this raise, is what fails the run.
    """


# Captured at import, before anything is replaced, so the guard's own calls go
# to the real implementations rather than back through itself.
_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_run = unittest.TestCase.run
_real_start = threading.Thread.start

_lock = threading.Lock()

# Violations recorded but not yet reported against a test. Taken -- not read --
# at the end of every test, so one violation fails one test rather than every
# test that follows it.
_violations = []

# The test currently running, for attribution. A worker the code under test
# started can outlive the test that started it, so the test running when the
# resolution happens is often not the one that caused it -- which is how a
# report like this sends somebody to read the wrong test. Each thread is
# stamped with its origin at `start`, and the stamp propagates, so what gets
# reported is the test whose start-up set the work going.
_running_test = None

# Names a test has declared it means to resolve. A list rather than a set so
# that nesting, and two threads inside the same declaration, behave.
_declared = []


def take():
    """Remove and return every violation recorded since the last take.

    The run takes them at the end of each test. A test ABOUT this guard takes
    its own, so that deliberately provoking a violation does not also fail the
    test that provoked it on purpose.
    """
    with _lock:
        taken = list(_violations)
        del _violations[:]
    return taken


@contextlib.contextmanager
def resolving(host):
    """Declare that resolving `host` is the point of the test inside.

    For a test that exercises what happens when a name does not resolve: the
    resolution is neither refused nor recorded, and -- because the declaration
    is checked before anything is resolved -- the name is not looked up by the
    guard either.
    """
    with _lock:
        _declared.append(host)
    try:
        yield
    finally:
        with _lock:
            _declared.remove(host)


def _named(host):
    """`host` as text, or None where the call names no host at all.

    `getaddrinfo(None, port)` and `getaddrinfo("", port)` are how a server
    asks for the wildcard address. Neither resolves a name, so neither is this
    rule's business.

    ONE bound here, not two. This was first written as
    `if host is None or host == ""`, which reads as more careful and is not:
    `None` is already what the fall-through answers for `None`, so that
    clause was a second mechanism for a case already covered -- and two
    independent mechanisms for one case means NEITHER is observable.
    Measured: deleting `host is None` from it changed nothing any test could
    see. `_screen` is where a `None` is acted on, and deleting it THERE is
    caught.
    """
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    if host == "":
        return None
    return host


def _is_loopback(host):
    """Whether `host` stands for this machine's loopback interface.

    An address literal is decided from the literal. A name is resolved and
    judged on EVERY address it answered with: one routable answer among
    several is a name that reaches off this machine, whatever else it also
    answers with. A name that answers with nothing -- including one that does
    not resolve at all -- is not loopback either; `all()` of no answers is
    true, and a default that happens to say "loopback" for the case with no
    evidence is exactly the shape of failing open.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        answers = _real_getaddrinfo(host, None)
    except OSError:
        return False
    return bool(answers) and all(
        ipaddress.ip_address(sockaddr[0]).is_loopback
        for *_, sockaddr in answers)


def _origin():
    """The test this thread's work belongs to.

    The stamp `_start` left on a worker, if there is one, in preference to
    whatever is running now: a worker started by one test and resolving during
    the next belongs to the test that started it.
    """
    stamped = getattr(threading.current_thread(), "_nonetwork_origin", None)
    return stamped if stamped is not None else _running_test


def _screen(host):
    """Let the call through, or record it and raise.

    Declarations are checked BEFORE the address is decided, so a test that
    means to resolve a name that will not resolve does not have the guard
    resolve it first.
    """
    host = _named(host)
    if host is None:
        return
    with _lock:
        if host in _declared:
            return
    if _is_loopback(host):
        return
    with _lock:
        _violations.append((host, _origin(), threading.current_thread().name))
    raise NonLoopbackResolution(
        "%r is not loopback: a test may not resolve a name off this machine. "
        "Give the code under test its transport seam, or -- if resolving is "
        "the point of the test -- say so with tests.nonetwork.resolving()."
        % (host,))


def _screen_address(address):
    """Screen the host half of whatever `connect` was handed.

    A tuple is an IP socket's `(host, port, ...)`; anything else is a
    filesystem socket's path or an address family this rule has no opinion
    about, and neither resolves a name.
    """
    if isinstance(address, tuple) and address:
        _screen(address[0])


def _getaddrinfo(host, *args, **kwargs):
    _screen(host)
    return _real_getaddrinfo(host, *args, **kwargs)


def _connect(sock, address):
    # `connect` resolves a name itself, in C, without going back through
    # `socket.getaddrinfo` -- so the hook above cannot see it and this one has
    # to exist.
    _screen_address(address)
    return _real_connect(sock, address)


def _connect_ex(sock, address):
    _screen_address(address)
    return _real_connect_ex(sock, address)


def _start(thread):
    thread._nonetwork_origin = _origin()
    return _real_start(thread)


def _describe(violations):
    return ("resolved %d address(es) that are not loopback: %s"
            % (len(violations),
               ", ".join("%s (from %s, in thread %s)" % triple
                         for triple in violations)))


def _run(test, result=None):
    """Every test's own run, plus the verdict on what it resolved.

    The verdict is delivered as a failure of the test that was running,
    because the raise inside `_screen` cannot be relied on to reach anybody:
    all twelve of the resolutions this was built for happened on a worker
    thread whose exception the code under test catches and rewraps. Which
    test it is reported AGAINST is therefore whichever one was running when
    the resolution landed; which test it CAME from is in the message.
    """
    global _running_test
    previous = _running_test
    _running_test = test.id()
    try:
        result = _real_run(test, result)
    finally:
        _running_test = previous
    violations = take()
    if violations:
        try:
            raise NonLoopbackResolution(_describe(violations))
        except NonLoopbackResolution:
            result.addFailure(test, sys.exc_info())
    return result


def install():
    """Arm the guard.

    Safe to call more than once: every original above was captured at import,
    so a second call rebinds the same four names to the same four wrappers
    rather than wrapping the wrappers. Capturing them HERE instead would make
    a second call point `_real_getaddrinfo` at the guard, and every resolution
    would recurse until the stack ran out.
    """
    socket.getaddrinfo = _getaddrinfo
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    unittest.TestCase.run = _run
    threading.Thread.start = _start
