# Leak guard

`scripts/check_leaks` fails the build if a forbidden string (configured out
of band — see the script header — never committed to this repo) shows up in
tracked file contents or filenames, in untracked-but-not-ignored files, in
tracked file contents anywhere in history, or in a commit message anywhere in
history. CI runs it (`./scripts/check_leaks`) on every push and pull request.

Separately from the patterns, it refuses tracked files whose *type* could
carry library data at all — the extension lists in `scripts/data-extensions.txt`
and `scripts/media-extensions.txt`, which deny by default. A short list of
specific configuration files is named back in by exact path, mirroring the
negations in `.gitignore`: `pyproject.toml`, `uv.lock`, `mkdocs.yml` and the
workflow files. `*.yml` and `*.toml` themselves stay denied, because either is
a perfectly good place for library data to hide.

## The local commit-msg hook

To also block a bad commit message locally, *before* it is ever written,
enable the accompanying `commit-msg` hook. This repo's `core.hooksPath` is
owned by a different, unrelated mechanism, so the hook is not installed via
that — copy or symlink it into `.git/hooks/commit-msg` instead:

```sh
cp scripts/hooks/commit-msg .git/hooks/commit-msg
chmod +x .git/hooks/commit-msg
```

or, to track upstream changes to the hook automatically:

```sh
ln -sf ../../scripts/hooks/commit-msg .git/hooks/commit-msg
```

## Why the site is published from CI

This site is the first thing the project pushes to the open internet, so it is
built and deployed by the same workflow that runs the guard: both site jobs
take `needs: guard`, and nothing is published unless the guard passed first.

Letting the platform build the site from a branch would be the easier setup and
is deliberately not used. It builds outside this workflow, and would publish
pages without `scripts/check_leaks` ever running against the commit that
produced them.

The deploy needs no credential of its own — it authenticates with the
workflow's own token — so there is no secret to add and none to leak. It runs
only on a push to the default branch, which is also why a pull request gets no
preview: there is one site, and a pull request must not be able to overwrite
it.

The guard is a backstop, not a filter. It knows only the patterns it is
configured with, so it catches a known string that slipped through — it cannot
judge prose it has never been told about.
