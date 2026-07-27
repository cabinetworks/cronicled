"""The Jinja environment, configured once.

Autoescaping is the entire reason this project carries a runtime dependency.
Jinja's own default is off, so leaving it unset would take the cost of the
dependency and none of its benefit -- and would look correct while doing it.
"""

from jinja2 import Environment, PackageLoader, select_autoescape

_ENV = None


def environment():
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=PackageLoader("cronicled.web", "templates"),
            autoescape=select_autoescape(default_for_string=True,
                                         default=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _ENV


def render(name, **context):
    return environment().get_template(name).render(**context)
