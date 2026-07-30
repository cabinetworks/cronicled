"""Detecting markup in a performer description, and what to put in its place.

Every fixture here is invented. The names, the biographies and the ids belong
to nobody.

Two things every test in this file is built around:

* **the measurement.** Three descriptions in 651 carry a fault. A detector
  that fires on ordinary prose does not merely add noise, it immediately
  outnumbers what it correctly finds — so for every rule that says "propose",
  there is a test here saying "and NOT for this", using prose that contains
  the very character the naive rule keys on.
* **whole values.** A cleaned description is asserted in full, never by
  checking that a tag is absent. `assertNotIn("<p>", cleaned)` is satisfied by
  a cleanup that deleted the sentence around it, which is the one failure a
  reviewer cannot see.
"""
import os
import shutil
import tempfile
import threading
import unittest

from cronicled.descriptions import (
    FAULT_ENTITY, FAULT_MARKUP, PRODUCER_NAME, REFUSE_DOUBLE_ENCODED,
    REFUSE_NOTHING_LEFT, REFUSE_UNSAFE_REFERENCE, SUBJECT_TYPE,
    DescriptionProducer, assess,
)
from cronicled.jobs import JobRunner
from cronicled.schedule import resolve
from cronicled.store import Store


class _Ctx:
    """What a producer is handed as `ctx`.

    `message` holds ONLY the last line, because that is all the real
    collaborator keeps: `JobRunner._log` assigns `state.message`, so every
    line but the final one is overwritten by the time a job ends. A double
    that accumulated them would let a test assert a property production does
    not have.

    `messages` is a recording of the stream for the per-line assertions that
    are genuinely about the stream, and no conclusion about what a finished
    job REPORTS may be drawn from it.
    """

    def __init__(self):
        self.message = ""
        self.messages = []
        self._lock = threading.Lock()

    def log(self, message):
        with self._lock:
            self.message = message
            self.messages.append(message)


class _Performers:
    """A media server holding exactly the performers it was given, and
    refusing everything else.

    `__getattr__` refuses rather than returning a mock: this producer reads
    one field off one query and writes nothing at all, and these tests are the
    first place a write introduced here would show up.
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = 0

    def performers_with_descriptions(self):
        self.calls += 1
        return list(self.rows)

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the description pass called %r on the media server; it reads "
                "one field and proposes, it never writes" % (name,))
        return refuse


def _row(performer_id="7", name="Wren Alderly", details=None):
    return {"id": performer_id, "name": name, "details": details}


class MarkupIsDetected(unittest.TestCase):
    def test_a_paragraph_tag_is_a_fault_and_the_text_inside_it_is_kept(self):
        cleanup = assess("<p>Retired after a long run in regional theatre.</p>")

        self.assertEqual(cleanup.faults, (FAULT_MARKUP,))
        # The WHOLE field. `assertNotIn("<p>", ...)` would also pass for a
        # cleanup that returned the empty string.
        self.assertEqual(cleanup.cleaned,
                         "Retired after a long run in regional theatre.")

    def test_a_tag_in_the_middle_of_a_sentence_keeps_both_sides_of_it(self):
        # The surrounding text is the whole assertion: a cleanup that took the
        # tag and the words either side of it would satisfy any check phrased
        # as "the tag is gone".
        cleanup = assess("Played the <b>lead</b> for eleven seasons.")

        self.assertEqual(cleanup.cleaned,
                         "Played the lead for eleven seasons.")

    def test_paragraph_breaks_survive_the_cleanup(self):
        # `cronicled.text.strip_html` collapses every run of whitespace,
        # newlines included, and would answer "One. Two." here — one line
        # where the description had two paragraphs. That is why this module
        # does not call it.
        cleanup = assess("<p>Born inland.</p>\n\n<p>Moved to the coast.</p>")

        self.assertEqual(cleanup.cleaned, "Born inland.\n\nMoved to the coast.")

    def test_a_tag_removed_between_two_words_leaves_exactly_one_space(self):
        cleanup = assess("a <em>quiet</em> career")

        self.assertEqual(cleanup.cleaned, "a quiet career")

    def test_two_adjacent_tags_leave_one_space_between_the_sentences(self):
        cleanup = assess("<p>First.</p><p>Second.</p>")

        self.assertEqual(cleanup.cleaned, "First. Second.")

    def test_a_self_closing_tag_with_attributes_is_markup(self):
        cleanup = assess('Stage work.<br />Screen work.')

        self.assertEqual(cleanup.faults, (FAULT_MARKUP,))
        self.assertEqual(cleanup.cleaned, "Stage work. Screen work.")

    def test_an_anchor_with_attributes_is_markup_and_its_text_is_kept(self):
        cleanup = assess('See <a href="http://example.invalid/x">the notes</a>.')

        self.assertEqual(cleanup.cleaned, "See the notes .")


class OrdinaryProseIsLeftAlone(unittest.TestCase):
    """The direction that matters most, given the measurement.

    Each fixture here contains exactly the character a naive rule keys on and
    nothing else that could be mistaken for a fault, so it cannot pass by
    tripping some other guard.
    """

    def test_an_angle_bracket_pair_in_prose_is_not_markup(self):
        # `<[^>]+>` — the pattern `cronicled.text.strip_html` uses — matches
        # `< b and c >` here and would delete four words from a description
        # that has nothing wrong with it.
        self.assertIsNone(
            assess("Taught that a < b and c > d before anyone wrote it down."))

    def test_an_ampersand_followed_by_a_word_is_not_an_entity(self):
        # `html.unescape` decodes HTML5 named references with no terminating
        # semicolon: it turns "me&not you" into "me¬ you". Prose is full
        # of `&` followed by a word.
        self.assertIsNone(
            assess("Spent a decade in R&D, then not one more day."))

    def test_an_ampersand_word_and_semicolon_that_names_no_entity(self):
        # The closest a sentence gets to the entity shape by accident. It is
        # refused by the entity table being closed, not by the semicolon
        # being absent.
        self.assertIsNone(assess("Ran the Q&A; nobody else would."))

    def test_an_entity_name_that_is_only_the_start_of_a_word_is_not_an_entity(self):
        # HARM: `html.unescape` does not require the terminating semicolon, so
        # it reads "&copyright" as the copyright sign followed by "right" and
        # rewrites this sentence to "Filed under (c)right law." -- prose
        # silently corrupted by the cleanup that was supposed to be certain.
        # Distinct from the case above: there the name is absent from the
        # table, here it is present and the semicolon is what refuses.
        self.assertIsNone(assess("Filed under &copyright law."))

    def test_a_word_in_angle_brackets_that_is_not_a_tag_name(self):
        self.assertIsNone(assess("Filed under <biography> at the archive."))

    def test_no_description_at_all_is_not_a_fault(self):
        self.assertIsNone(assess(None))
        self.assertIsNone(assess(""))

    def test_ordinary_prose_with_neither_character(self):
        self.assertIsNone(assess("Two decades on stage, then teaching."))


class EntitiesAreDecoded(unittest.TestCase):
    def test_a_named_entity_becomes_the_character_it_names(self):
        cleanup = assess("Worked with Marsh &amp; Holloway for years.")

        self.assertEqual(cleanup.faults, (FAULT_ENTITY,))
        self.assertEqual(cleanup.cleaned,
                         "Worked with Marsh & Holloway for years.")

    def test_a_decimal_reference_becomes_the_character_it_names(self):
        cleanup = assess("It&#39;s a long story.")

        self.assertEqual(cleanup.cleaned, "It's a long story.")

    def test_a_hexadecimal_reference_becomes_the_character_it_names(self):
        cleanup = assess("It&#x27;s a long story.")

        self.assertEqual(cleanup.cleaned, "It's a long story.")

    def test_both_faults_are_named_and_both_are_fixed(self):
        cleanup = assess("<p>Marsh &amp; Holloway, 1994.</p>")

        self.assertEqual(cleanup.faults, (FAULT_MARKUP, FAULT_ENTITY))
        self.assertEqual(cleanup.cleaned, "Marsh & Holloway, 1994.")

    def test_a_decoded_angle_bracket_is_not_then_read_as_markup(self):
        # `&lt;` decodes to a bare `<` with a space after it, which is not a
        # tag by this module's rules — so it is cleaned rather than refused.
        # The discriminating half of the double-encoding refusal below: that
        # refusal has to fire on an entity-encoded TAG, not on every `&lt;`.
        cleanup = assess("Told me &quot;a &lt; b&quot; and left.")

        self.assertEqual(cleanup.cleaned, 'Told me "a < b" and left.')


class WhatItWillNotClean(unittest.TestCase):
    """A fault found and no replacement offered — the third answer, and the
    reason `assess` does not simply return `None` for these."""

    def test_an_entity_encoded_tag_produces_no_proposal(self):
        # THE ORDERING TEST, and the whole of the ticket's open question.
        #
        # Two cleanups, two orders, two different answers for this one input:
        #
        #   decode first, then strip -> "Played the lead."   (four visible
        #       characters deleted from text that was displaying correctly)
        #   strip first, then decode -> "Played the <b>lead</b>."  (a value
        #       this same function reports as markup on the next run, so the
        #       proposal returns every night and approving it twice reaches
        #       the deletion above by a longer road)
        #
        # Neither is an answer this project's own rules allow, so the decision
        # is to offer none — and the rule is stated as a property rather than
        # as an ordering: a proposal is offered only when the cleaned value is
        # a fixed point. Both of the strings above are asserted absent, so
        # this fails whichever order a change picks.
        cleanup = assess("Played the &lt;b&gt;lead&lt;/b&gt;.")

        self.assertEqual(cleanup.faults, (FAULT_ENTITY,))
        self.assertIsNone(cleanup.cleaned)
        self.assertEqual(cleanup.reason, REFUSE_DOUBLE_ENCODED)
        self.assertNotEqual(cleanup.cleaned, "Played the lead.")
        self.assertNotEqual(cleanup.cleaned, "Played the <b>lead</b>.")
        self.assertEqual(cleanup.original, "Played the &lt;b&gt;lead&lt;/b&gt;.")

    def test_a_doubly_escaped_entity_produces_no_proposal(self):
        # The same rule reaching a case nothing in the code names: decoding
        # "&amp;amp;" once leaves "&amp;", which is still an escape. Settled
        # by the fixed-point check, not by a branch about ampersands.
        cleanup = assess("Signed &amp;amp; sealed.")

        self.assertIsNone(cleanup.cleaned)
        self.assertEqual(cleanup.reason, REFUSE_DOUBLE_ENCODED)

    def test_a_reference_naming_no_printable_character_produces_no_proposal(self):
        cleanup = assess("A note&#0;and another.")

        self.assertEqual(cleanup.faults, (FAULT_ENTITY,))
        self.assertIsNone(cleanup.cleaned)
        self.assertEqual(cleanup.reason, REFUSE_UNSAFE_REFERENCE)

    def test_a_reference_outside_unicode_produces_no_proposal(self):
        cleanup = assess("A note&#9999999;and another.")

        self.assertIsNone(cleanup.cleaned)
        self.assertEqual(cleanup.reason, REFUSE_UNSAFE_REFERENCE)

    def test_a_description_that_is_only_markup_produces_no_proposal(self):
        # Proposing "" here would ask a reviewer to approve emptying the
        # field, which reads on the page as an ordinary tidy-up.
        cleanup = assess("<p></p>\n<p>  </p>")

        self.assertEqual(cleanup.faults, (FAULT_MARKUP,))
        self.assertIsNone(cleanup.cleaned)
        self.assertEqual(cleanup.reason, REFUSE_NOTHING_LEFT)

    def test_one_undecidable_entity_withholds_the_whole_description(self):
        # Not "clean the parts it can and leave the rest". A partly-cleaned
        # description is the plausible-looking proposal this module exists to
        # avoid making: the reviewer sees a fix and cannot see what was
        # skipped.
        cleanup = assess("<p>Marsh &amp; Holloway&#0; of Ashgate.</p>")

        self.assertIsNone(cleanup.cleaned)
        self.assertEqual(cleanup.reason, REFUSE_UNSAFE_REFERENCE)


class TheProducer(unittest.TestCase):
    def _run(self, rows, **kwargs):
        stash = _Performers(rows)
        ctx = _Ctx()
        producer = DescriptionProducer(stash, **kwargs)
        return list(producer.produce(ctx)), ctx, stash

    def test_yields_one_whole_proposal_for_a_faulty_description(self):
        # The WHOLE dict, not a sample of its keys. A field-by-field check
        # cannot see a key that was added — which in this project let an
        # unlisted `rating100` into an update payload with a green suite.
        proposals, _, _ = self._run(
            [_row(performer_id=41, name="Wren Alderly",
                  details="<p>Marsh &amp; Holloway, 1994.</p>")])

        self.assertEqual(proposals, [{
            "folder": "library",
            "subject_type": SUBJECT_TYPE,
            "subject_id": "41",
            "summary": "Wren Alderly: description contains markup and entity",
            "confidence": None,
            "payload": {
                "name": "Wren Alderly",
                "field": "details",
                "faults": ["markup", "entity"],
                "original": "<p>Marsh &amp; Holloway, 1994.</p>",
                "cleaned": "Marsh & Holloway, 1994.",
            },
        }])

    def test_a_clean_library_proposes_nothing(self):
        proposals, ctx, _ = self._run([
            _row(performer_id="1", details=None),
            _row(performer_id="2", details=""),
            _row(performer_id="3", details="Two decades on stage."),
            _row(performer_id="4", details="Ran the Q&A; nobody else would."),
        ])

        self.assertEqual(proposals, [])
        self.assertEqual(
            ctx.message,
            "finished: 0 proposed, 0 could not be cleaned confidently, "
            "4 descriptions looked at")

    def test_a_description_it_cannot_clean_is_counted_and_not_proposed(self):
        # Mutating the producer to fall back on a best guess -- yielding a
        # proposal built from `cleanup.original`, or from a partly-cleaned
        # value -- fails on the empty list here, and the count in the closing
        # line fails too.
        proposals, ctx, _ = self._run(
            [_row(performer_id="9", details="Played the &lt;b&gt;lead&lt;/b&gt;.")])

        self.assertEqual(proposals, [])
        self.assertEqual(
            ctx.message,
            "finished: 0 proposed, 1 could not be cleaned confidently, "
            "1 descriptions looked at")

    def test_the_closing_line_tells_a_refusal_from_a_clean_library(self):
        # The two above report "0 proposed" and mean opposite things. This is
        # the assertion that they are not the same message -- without it, both
        # tests pass against a closing line that says only how many were
        # proposed.
        _, clean_ctx, _ = self._run([_row(details="Nothing wrong here.")])
        _, refused_ctx, _ = self._run(
            [_row(details="Played the &lt;b&gt;lead&lt;/b&gt;.")])

        self.assertNotEqual(clean_ctx.message, refused_ctx.message)

    def test_every_performer_is_examined_in_one_read(self):
        proposals, _, stash = self._run([
            _row(performer_id="1", name="Wren Alderly", details="fine"),
            _row(performer_id="2", name="Cassia Mould", details="<p>Also.</p>"),
            _row(performer_id="3", name="Ilsa Devon", details="fine too"),
        ])

        self.assertEqual([p["subject_id"] for p in proposals], ["2"])
        self.assertEqual(stash.calls, 1)

    def test_the_folder_it_was_built_with_is_the_folder_it_proposes_into(self):
        proposals, _, _ = self._run([_row(details="<p>Also.</p>")],
                                    folder="tidying")

        self.assertEqual(proposals[0]["folder"], "tidying")

    def test_the_declared_cadence_is_what_a_schedule_reads_off_it(self):
        producer = DescriptionProducer(_Performers([]), every=3600)

        entries = resolve([producer])

        self.assertEqual(entries[PRODUCER_NAME].every, 3600)
        self.assertTrue(entries[PRODUCER_NAME].enabled)

    def test_a_producer_with_no_cadence_is_refused_when_the_schedule_resolves(self):
        # At start-up, with a stack trace an operator reads -- not at 3am as a
        # producer that quietly never ran. `every` defaults to `None` rather
        # than to an interval precisely so forgetting it cannot look like a
        # decision.
        producer = DescriptionProducer(_Performers([]))

        with self.assertRaisesRegex(ValueError, "cadence"):
            resolve([producer])

    def test_it_runs_in_the_cost_class_that_is_not_rationed(self):
        # `scraping` is limited to one job because it drives a headless
        # browser inside the media server. Nothing here does, and queueing
        # this behind a twenty-minute scrape would be a limit protecting
        # nothing.
        self.assertEqual(DescriptionProducer.cost, "local")
        self.assertEqual(DescriptionProducer.name, PRODUCER_NAME)


class ThroughTheRealRunnerAndStore(unittest.TestCase):
    """The producer wired to the objects that actually record what it yields.

    Everything above hands `produce` a recording context and reads the list it
    returns. That cannot see the two properties this class exists for: what a
    finished job REPORTS (the runner keeps one message, so a stream recording
    proves nothing about it), and what a second run does to a store that
    already holds the first run's proposals.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.store = Store(os.path.join(self._dir, "cronicled.sqlite3"))
        self.addCleanup(self.store.close)
        self.runner = JobRunner(self.store)

    def _run(self, rows):
        producer = DescriptionProducer(_Performers(rows), every=60)
        self.runner.reregister(producer)
        job = self.runner.start(producer.name, trigger="manual")
        self.assertTrue(self.runner.wait(job.id, 10))
        return self.runner.job(job.id)

    def test_a_proposal_reaches_the_store_with_both_texts_intact(self):
        job = self._run([_row(performer_id="7", name="Wren Alderly",
                              details="<p>Marsh &amp; Holloway.</p>")])

        self.assertEqual(job.state, "done", job.traceback)
        self.assertEqual(job.recorded, 1)
        items = self.store.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["subject_type"], SUBJECT_TYPE)
        self.assertEqual(items[0]["subject_id"], "7")
        self.assertEqual(items[0]["payload"], {
            "name": "Wren Alderly",
            "field": "details",
            "faults": ["markup", "entity"],
            "original": "<p>Marsh &amp; Holloway.</p>",
            "cleaned": "Marsh & Holloway.",
        })

    def test_a_second_run_over_the_same_library_adds_no_second_row(self):
        # The property the whole design rests on (see `Store.record`): a
        # producer that finds the same thing again UPDATES a row rather than
        # making a second, or the inbox turns into noise on its second night.
        rows = [_row(performer_id="7", details="<p>Marsh &amp; Holloway.</p>")]
        self._run(rows)
        self._run(rows)

        self.assertEqual(len(self.store.items()), 1)

    def test_a_subject_the_reviewer_muted_is_not_proposed_again(self):
        # No `select` here, deliberately -- the store is the one place a
        # reviewer's past decision is enforced, and it still is. Asserted
        # through the real store rather than by reading the producer.
        rows = [_row(performer_id="7", details="<p>Marsh &amp; Holloway.</p>")]
        self.store.mute(SUBJECT_TYPE, "7", reason="not this one")

        job = self._run(rows)

        self.assertEqual(job.skipped, 1)
        self.assertEqual(job.recorded, 0)
        self.assertEqual(self.store.items(), [])

    def test_the_message_a_finished_job_keeps_is_the_closing_count(self):
        # The runner keeps ONE message: every line but the last is overwritten
        # by the time a job ends. So the counts have to be on the LAST line or
        # they do not survive the run at all.
        job = self._run([
            _row(performer_id="1", details="<p>Marsh &amp; Holloway.</p>"),
            _row(performer_id="2", details="Played the &lt;b&gt;lead&lt;/b&gt;."),
            _row(performer_id="3", details="Nothing wrong here."),
        ])

        self.assertEqual(
            job.message,
            "finished: 1 proposed, 1 could not be cleaned confidently, "
            "3 descriptions looked at")


if __name__ == "__main__":
    unittest.main()
