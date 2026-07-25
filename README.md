# cronicled

An always-on companion for a [Stash](https://github.com/stashapp/stash) media
library. It watches the library on a schedule and files what it finds into an
inbox of proposed changes — metadata matches, tag merges, performer fixes — that
you confirm or dismiss. Nothing is written to your library without your say-so.

Zero runtime dependencies: Python standard library only.

## Status

Early. The foundation layer is in place; the service itself is being built.

## Site adapters

Matching against a clip store is done through a *site adapter*, configured in
`config/adapters.json`, which is not tracked by this repo. See
`config/adapters.example.json` for the shape. No store is built in.

## Leak guard

`scripts/check_leaks.sh` fails the build if a forbidden string (configured out
of band — see the script header — never committed to this repo) shows up in
tracked file contents or filenames, in untracked-but-not-ignored files, in
tracked file contents anywhere in history, or in a commit message anywhere in
history. CI runs it on every push and pull request.

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

## License

MIT
