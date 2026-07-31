"""Arming the run's own guard, at the one point every way of running this
suite goes through.

`nonetwork.install()` is called here rather than from a test module because a
check that only runs when its own module is collected is a check the run does
not have: discovery imports this package first, and so does
`python -m unittest tests.test_whichever`.
"""

from . import nonetwork

nonetwork.install()
