# Starting a new domain repo

**Do not fork an existing domain repo.** A fork drags along the other
domain's parsers, README, and citation, creates a GitHub fork relationship
that says something untrue, and — worst — makes every future workflow
improvement a manual sync across N copies.

Instead, scaffold from the engine. The engine owns the boilerplate, so a
generated repo is always current with the engine version that produced it:

```bash
pip install "snapshotter @ git+https://github.com/<owner>/snapshotter.git@v<version>"
snapshotter init ../wss-arxiv --owner <owner> --title "arXiv Listing History"
```

That writes a complete, immediately valid repo:

```
.github/workflows/   capture-daily, health, derive, validate
registry/            one example entry, shipped `paused`
parsers/             one example parser
examples/            queries.sql + load_observations.py
docs-ready README, LICENSE (MIT) + LICENSE-DATA (CC-BY-4.0), CITATION.cff
.gitattributes       CSV conventions — lands before any CSV is ever committed
raw/ manifest/ derived/ health/ state/
```

`snapshotter validate` passes on the fresh scaffold, and nothing captures
until you flip a source to `active`.

## Then

1. `git init && git add -A && git commit` — get `.gitattributes` in before
   the first CSV, or line endings are baked in wrong forever.
2. Write `registry/<source_id>.yml` (copy the example, then delete it).
3. Write a parser in `parsers/` if the payload shape is new.
4. `snapshotter doctor <source_id>` — **read the raw response** before
   trusting any field name.
5. Flip `status: active`, commit, push under the same owner as the engine,
   set the `SNAPSHOTTER_CONTACT` secret, and dispatch `capture-daily` once by
   hand.

## What the template does for you

- **Parsers are auto-discovered.** The derive workflow globs `parsers/*.py`,
  so adding a schema needs no workflow edit — the same rule that already
  holds for adding a source.
- **The engine pin is stamped at generation time.** Workflows install
  `snapshotter@v<the version that scaffolded the repo>`, so a repo's
  behaviour never changes underneath it. Upgrading is a deliberate edit of
  `ENGINE_SPEC` and `requirements.txt`.
- **The owner is resolved at runtime** as `${{ github.repository_owner }}`,
  so a repo that gets transferred or forked keeps working.

## If you want a "Use this template" button

Generate one and publish it, rather than hand-maintaining it:

```bash
snapshotter init ../wss-template --owner <owner> --title "Template"
gh repo create <owner>/wss-template --public --source ../wss-template --push
gh repo edit <owner>/wss-template --template
```

Regenerate and force-push it whenever the engine's templates change — it
stays a build artifact, never a second source of truth.
