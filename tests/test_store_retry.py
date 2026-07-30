"""What a retried store fault looks like by the time a person reads it.

Its own module because the property under test is a COMPOSITION of three:
`cronicled.stash` decides a failure is worth repeating and repeats it,
`cronicled.search` turns the client into the `search` callable a scan takes,
and `cronicled.scan` decides what a store's failure means for the file. None
of those three files is an honest home for an assertion about all of them —
`tests/test_stash.py` pins the client's own behaviour (the bound, the
classification, the delay), and this file pins only what survives the trip
upstream.

The thing that must survive is one distinction: a store that FAILED is not a
store that answered with NOTHING. An error is evidence about the round trip,
not about the file, and only a confirmed-empty catalogue earns a mute — the
verdict that stops a file ever being examined again. A retry that ends in
failure therefore has to arrive as an error, with the store named and the
bound visible, and never as an empty answer.

Every fixture is invented: the store names, the creator, the titles, the
scraper named inside the upstream fault text. Nothing here opens a socket —
each store is a real `cronicled.stash.Stash` over a scripted fake transport —
and nothing here sleeps: the retry delay is injected and recorded.
"""
import unittest

from cronicled.scan import Source, examine_sources
from cronicled.search import catalog_search
from cronicled.stash import RETRY_ATTEMPTS, Stash

FOLDER = "library"
CREATOR = "Kestrel Hollow"
PATH = "/library/Kestrel Hollow/Morning Ritual.mp4"

# The shape of the fault this ticket exists for: the media server reporting
# that its configured scraper handed back a single object where the schema
# promises a list. The scraper name is invented; the Go type name is the part
# a reader of THIS project can do nothing with, which is why the assertions
# below are about the text that surrounds it.
SCRAPER_FAULT = (
    "error while name scraping with scraper Bramblewick: could not unmarshal "
    "json from script output: json: cannot unmarshal object into Go value of "
    "type []models.ScrapedScene")


def candidate(title, slug):
    """One store row, as `Stash.scrape_scenes_by_query` answers it."""
    return {"title": title, "url": "https://example.invalid/clip/" + slug,
            "urls": ["https://example.invalid/clip/" + slug]}


def scene(scene_id, path):
    return {"id": str(scene_id), "title": None, "date": None,
            "files": [{"basename": path.rsplit("/", 1)[-1], "path": path}],
            "studio": None, "performers": [], "tags": []}


class _Adapter:
    """The two attributes `catalog_search` reads off a `SiteAdapter`. The
    censorship map is empty on purpose: one variant per name, so a call count
    below is a count of ATTEMPTS and not of spelling variants."""

    def __init__(self, scraper_id):
        self.scraper_id = scraper_id
        self.censorship = {}


def _transport(script):
    """Plays `script` in order — a dict is returned as the parsed payload —
    and refuses to run past its end, so a test that provoked more calls than
    it describes says so instead of quietly repeating an answer."""
    calls = []

    def send(body, timeout):
        calls.append((body, timeout))
        if len(calls) > len(script):
            raise AssertionError(
                "the store was asked %d times against a script of %d"
                % (len(calls), len(script)))
        return script[len(calls) - 1]

    send.calls = calls
    return send


def _failure(message=SCRAPER_FAULT):
    """The payload the media server answers with when the scrape failed
    server-side: no `data`, an `errors` array."""
    return {"errors": [{"message": message}]}


def _answer(rows):
    return {"data": {"scrapeSingleScene": list(rows)}}


# A candidate no test here wants: it scores 0.107 against "Morning Ritual.mp4",
# so it cannot win at any threshold these tests use.
DECOY = candidate("Harbour Lights", "harbour-lights")


def _fails_every_attempt():
    """A script for a store that fails every attempt the bound allows —
    padded, well past any bound, with an answer no assertion here accepts.

    The padding is the point. A script that simply RAN OUT would raise on the
    request worker thread, which `gql` reclassifies as transient, so a retry
    loop mutated to unlimited would spin on it and HANG the suite; a hang is
    not a failing test. Ending in a decoy answer means such a loop terminates
    with a proposal instead, which every assertion in this file contradicts."""
    return [_failure()] * 5 + [_answer([DECOY])]


class _Store:
    """One configured store: a real client over a scripted transport, wrapped
    in the production `search` callable, wrapped in a scan `Source`."""

    def __init__(self, name, script):
        self.sleeps = []
        self.transport = _transport(script)
        stash = Stash("http://media-server.invalid:9999", "invented-key",
                      transport=self.transport, sleep=self.sleeps.append)
        self.source = Source(
            name=name, search=catalog_search(stash, _Adapter("scraper-" + name)),
            owner_of=None, catalog_resolvable=True, censorship={})

    @property
    def attempts(self):
        return len(self.transport.calls)


def _examine(stores, threshold=0.5):
    return examine_sources(scene(1, PATH), sources=[s.source for s in stores],
                           folder=FOLDER, threshold=threshold)


WINNER = candidate("Morning Ritual", "morning-ritual")
UNRELATED = candidate("Lantern Song", "lantern-song")


class AStoreThatFailsOnceThenAnswers(unittest.TestCase):
    def test_the_file_gets_that_stores_real_answer(self):
        """The defect, end to end. Treating the first failure as final loses a
        file the store could identify, and loses it as a refusal — which is
        the same output as a store that genuinely had nothing."""
        store = _Store("alpha", [_failure(), _answer([WINNER])])

        outcome = _examine([store])

        self.assertIsNotNone(outcome.proposal, outcome.reason)
        self.assertEqual(outcome.proposal["payload"]["candidate"], WINNER)
        self.assertEqual(outcome.proposal["payload"]["store"], "alpha")
        self.assertEqual(store.attempts, 2)

    def test_nothing_about_the_retry_reaches_the_recorded_reason(self):
        """A recovered failure is not a finding. The bound is reported when it
        is EXHAUSTED — a store that answered on its second attempt answered,
        and a reason mentioning an error would send a reader hunting a store
        problem that resolved itself."""
        store = _Store("alpha", [_failure(), _answer([WINNER])])

        outcome = _examine([store])

        self.assertNotIn("store errors", outcome.reason)
        self.assertNotIn("gave up", outcome.reason)


class AStoreThatFailsEveryAttempt(unittest.TestCase):
    """The bound, seen from where a person reads it."""

    def _outcome_and_store(self):
        store = _Store("nightgale reels", _fails_every_attempt())
        return _examine([store]), store

    def test_it_is_recorded_as_an_error_and_never_as_a_mute(self):
        """HARM: a mute is the claim that the file is unidentifiable, and it
        stops the file ever being examined again. Reaching that claim off a
        network failure spends the strongest verdict here makes on no evidence
        about the file at all."""
        outcome, _ = self._outcome_and_store()

        self.assertIsNone(outcome.mute_reason)
        self.assertIsNone(outcome.proposal)
        self.assertIsNotNone(outcome.error)

    def test_the_recorded_text_names_the_store_and_the_bound(self):
        """Asserted WHOLE, because a test checking that the upstream sentence
        is merely PRESENT would pass on the behaviour that shipped before this
        ticket — that sentence was the entire message. What is new is
        everything around it: which store failed, and that it was asked twice
        and gave up. Both halves are readable without the Go type name at the
        end, which is the part naming a type in another project's source."""
        outcome, _ = self._outcome_and_store()

        self.assertEqual(
            outcome.error,
            "nightgale reels: StashError: " + SCRAPER_FAULT
            + " — gave up after 2 attempts")
        self.assertEqual(outcome.reason, outcome.error)

    def test_the_store_was_actually_asked_more_than_once(self):
        """The counterpart to the message: text claiming two attempts is a
        false record if only one request was made."""
        _, store = self._outcome_and_store()

        self.assertEqual(store.attempts, 2)

    def test_the_delay_between_attempts_was_never_paid_in_this_suite(self):
        """Not a property of the product — a property of the seam that makes
        this file cheap. A retry whose delay were not injectable would cost
        the suite `RETRY_DELAY` per failing store, on every run."""
        _, store = self._outcome_and_store()

        self.assertEqual(len(store.sleeps), 1)


class OneStoreFailingDoesNotDisturbAnother(unittest.TestCase):
    def test_a_second_store_still_answers_and_still_proposes(self):
        """HARM: a store's failure is isolated to that store for that file.
        A retry that broke the isolation — by raising out of the loop over
        sources, or by consuming the other store's turn — would lose a file
        that one healthy store could identify with certainty."""
        failing = _Store("alpha", _fails_every_attempt())
        healthy = _Store("beta", [_answer([WINNER])])

        outcome = _examine([failing, healthy])

        self.assertIsNotNone(outcome.proposal, outcome.reason)
        self.assertEqual(outcome.proposal["payload"]["candidate"], WINNER)
        self.assertEqual(outcome.proposal["payload"]["store"], "beta")

    def test_the_healthy_store_is_asked_exactly_once(self):
        """HARM: a retry keyed on anything other than the failing call — a
        flag on the client, a counter shared across stores — would repeat the
        healthy store's query too, doubling the cost of every scan against a
        library where one store is down."""
        failing = _Store("alpha", _fails_every_attempt())
        healthy = _Store("beta", [_answer([WINNER])])

        _examine([failing, healthy])

        self.assertEqual(healthy.attempts, 1)
        self.assertEqual(healthy.sleeps, [])
        self.assertEqual(failing.attempts, 2)

    def test_the_failing_store_is_still_reported_alongside_the_proposal(self):
        """HARM: a proposal off a healthy store must not bury the fact that
        another store was failing. Silently dropping it is how a store stays
        broken for weeks while the scan looks like it is working."""
        failing = _Store("alpha", _fails_every_attempt())
        healthy = _Store("beta", [_answer([WINNER])])

        outcome = _examine([failing, healthy])

        self.assertEqual(
            outcome.reason,
            "chosen with score 1.000 "
            "(store errors: alpha: StashError: " + SCRAPER_FAULT
            + " — gave up after 2 attempts)")

    def test_a_store_that_recovered_and_one_that_never_failed_both_answer(self):
        """The mixed case the acceptance names: one store retried, the other
        did not, and the file is judged on BOTH answers. Asserted through the
        cross-store finding, which is the only output that can distinguish
        "beta was never asked" from "beta was asked and agreed"."""
        retried = _Store("alpha", [_failure(), _answer([WINNER])])
        first_time = _Store("beta", [_answer([UNRELATED])])

        outcome = _examine([retried, first_time], threshold=0.05)

        self.assertIsNotNone(outcome.proposal, outcome.reason)
        self.assertEqual(outcome.proposal["payload"]["store"], "alpha")
        # The whole shape, with the score stated rather than read back out of
        # the answer: an expected value derived from the output under test
        # cannot fail.
        self.assertEqual(
            outcome.proposal["payload"]["competing_store"],
            [{"store": "beta", "candidate": UNRELATED, "score": 0.092}])
        self.assertEqual(retried.attempts, 2)
        self.assertEqual(first_time.attempts, 1)


if __name__ == "__main__":
    unittest.main()
