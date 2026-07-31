import importlib
import pkgutil
import unittest

import cronicled
from cronicled.web import inboxes


class InboxMapTotality(unittest.TestCase):
    def test_every_declared_subject_type_has_an_inbox(self):
        """The rule this pins is that a new subject type cannot be added
        without being given an inbox.

        Discovered by walking the package and importing every module,
        rather than from a list written in this test: a hand-copied list
        agrees with `INBOXES` by construction and would go stale in the
        same commit that breaks the rule it is meant to catch. This mirrors
        `tests/test_main.py`'s own
        `test_every_subject_type_this_package_declares_has_a_heading`,
        which discovers the same six values the same way.

        A NAMED subset of modules was considered and rejected here: an
        earlier draft of this test scanned only
        `(scan, tags, performer_tags, tag_hygiene, descriptions)` for
        anything ending in `SUBJECT_TYPE` or `SUBJECT`. That list omits
        `tag_descriptions`, whose `SUBJECT_TYPE` is `"tag"` -- one of the
        six live values -- so the omission is not cosmetic: that draft
        would never have noticed "tag" losing its inbox. Walking the whole
        package removes the chance of leaving a module out.
        """
        declared = {}
        for module in pkgutil.iter_modules(cronicled.__path__):
            subject = getattr(
                importlib.import_module("cronicled." + module.name),
                "SUBJECT_TYPE", None)
            if isinstance(subject, str):
                declared[module.name] = subject
        # A discovery that found nothing would make the rest of this
        # vacuous -- six is the number of subject types the live database
        # holds.
        self.assertGreaterEqual(len(declared), 6, declared)

        unmapped = set(declared.values()) - set(inboxes.ALL_SUBJECT_TYPES)
        self.assertEqual(unmapped, set(),
                          "subject types with no inbox: %s" % unmapped)


class InboxFor(unittest.TestCase):
    def test_each_mapped_subject_type_resolves_to_its_own_inbox(self):
        for name, types in inboxes.INBOXES.items():
            for subject_type in types:
                self.assertEqual(inboxes.inbox_for(subject_type), name)

    def test_an_unmapped_subject_type_raises_key_error(self):
        with self.assertRaises(KeyError):
            inboxes.inbox_for("invented-unmapped-type")


class CheckTotal(unittest.TestCase):
    def test_every_mapped_subject_type_passes(self):
        inboxes.check_total(inboxes.ALL_SUBJECT_TYPES)  # must not raise

    def test_an_empty_iterable_passes(self):
        inboxes.check_total(())  # nothing to check, nothing unmapped

    def test_an_unmapped_subject_type_raises_naming_it(self):
        with self.assertRaisesRegex(ValueError, "invented-unmapped-type"):
            inboxes.check_total(["invented-unmapped-type"])

    def test_every_unmapped_subject_type_is_named_not_just_one(self):
        # Two invented, unmapped types: the message must name both, not
        # just whichever one happened to be seen first.
        with self.assertRaises(ValueError) as ctx:
            inboxes.check_total(["invented-zeta-type", "invented-alpha-type"])
        message = str(ctx.exception)
        self.assertIn("invented-zeta-type", message)
        self.assertIn("invented-alpha-type", message)

    def test_a_mapped_subject_type_alongside_an_unmapped_one_names_only_the_gap(self):
        with self.assertRaises(ValueError) as ctx:
            inboxes.check_total(["scene", "invented-unmapped-type"])
        message = str(ctx.exception)
        self.assertIn("invented-unmapped-type", message)
        self.assertNotIn("scene", message)


if __name__ == "__main__":
    unittest.main()
