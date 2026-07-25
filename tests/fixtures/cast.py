"""Synthetic test vocabulary. No real performer, title, or path appears here.

Each entry preserves a specific matching property — see the comments. When adding
a fixture, state which property it exercises, or the test it feeds is decoration.
"""

PERFORMERS = {
    # Two-word handle: exercises spaceless slug matching ('velvetcrane').
    "two_word": "Velvet Crane",
    # Shares a first token with `two_word`: exercises "wrong performer, same
    # prefix" rejection.
    "prefix_clash": "Velvet Marsh",
}

TITLES = {
    # Fully contained in `containing`: exercises the containment auto-pick (#17).
    "contained": "Copper Kettle",
    "containing": "Velvet Crane - Copper Kettle (Extended Cut)",
    # A single generic token shared with many titles: must NOT auto-apply on its
    # own (#16). 'session' is deliberately as generic as the real case was.
    "generic_token": "My Session",
    # No token overlap with any of the above: the true-negative case.
    "unrelated": "Harbour Lights",
}

# Stand-in for a platform's word substitutions. Invented words, real shape:
# {canonical: [censored_form, ...]}, including one ambiguous variant claimed by two
# canonicals, which `decensor` must refuse to rewrite.
CENSORSHIP = {
    "kestrel": ["k3strel", "starling"],
    "peregrine": ["starling"],          # 'starling' is ambiguous on purpose
    "brass lantern": ["br@ss lantern"],  # multi-word phrase, punctuation-substituted
}


def scene(sid, basename, folder="Velvet Crane", root="/library"):
    """A Stash-shaped scene dict with one file. Paths are synthetic."""
    return {"id": sid, "title": basename,
            "files": [{"basename": basename,
                       "path": "%s/%s/%s" % (root, folder, basename)}]}
