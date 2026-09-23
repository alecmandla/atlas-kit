# Release checklist

Run this top to bottom before every tagged release and before the repository is first made
public. Every item is a gate: a failing item stops the release until it passes. Items
marked **maintainer** are done by hand by the person cutting the release; everything else
can run from a shell at the repository root.

## 1. Manifest and layout

- [ ] `.claude-plugin/plugin.json` has `name`, `version`, `description`, `author`, and
      `license`, and parses as strict JSON:
      `python3 -c 'import json,sys; json.load(open(".claude-plugin/plugin.json"))'`.
- [ ] `.claude-plugin/marketplace.json` parses as strict JSON, its `name` is `atlas-kit`,
      its single plugin entry is named `atlas-kit` with `"source": "./"`, and the entry's
      `version` equals the one in `plugin.json`.
- [ ] `version` was bumped in **both** files since the last release. Unchanged versions do
      not trigger updates because installed plugins are cached.
- [ ] `CHANGELOG.md` has an entry for this version, dated, in Keep a Changelog format.
- [ ] No symlinks anywhere in the tree:
      `find . -path ./.git -prune -o -type l -print` prints nothing. Marketplace installs
      copy the repository into a cache; symlinks that point outside it are silently
      dropped.
- [ ] `skills/atlas-kickoff/SKILL.md` has valid frontmatter (`name`, `description`) and is
      under 500 lines; heavy material stays in `references/`.
- [ ] `engine/` is present at the paths in `docs/LAYOUT-CONTRACT.md`
      (`_shared/atlas_config.py`, `atlas.config.example.json`, one directory per skill)
      and imports with no third-party dependency:
      `python3 -c 'import sys; sys.path.insert(0, "engine/_shared"); import atlas_config'`.
- [ ] No plugin-root `CLAUDE.md` is shipped; installed plugins do not load it.

## 2. Validate and cost

- [ ] `claude plugin validate .` passes. Run it again with `--strict` and clear every
      warning or record why it stays.
- [ ] `claude --plugin-dir .` starts a session in which `/atlas-kickoff` appears and
      `/atlas-kickoff --write-only` runs Phase 1 without error against a scratch repo and
      a scratch vault.
- [ ] `claude plugin details atlas-kit` (with the plugin loaded) shows a projected context
      cost you are willing to charge every session; the kickoff's description is the only
      permanent cost.

## 3. Scrub gate (repository-specific)

The gate script is in `docs/SCRUB-RULES.md` section 7. It reads its banned patterns from
`docs/scrub-patterns.local.txt`, which is git-ignored and never committed; each maintainer
keeps their own copy built from the categories in section 1 of that file. On a fresh clone
the file is absent and the gate fails closed.

- [ ] `docs/scrub-patterns.local.txt` exists locally and covers every category in
      SCRUB-RULES section 1 (maintainer identity, employer, coworkers, clients, vendors,
      real Monday, Slack, and GitHub identifiers, real meeting titles, source-system IDs,
      paths, task IDs, run history).
- [ ] The gate prints `gate clean` and exits 0 over **all four shipped trees**, not only
      `exemplars/`. The published script scopes its `git grep` calls to `exemplars/`;
      widen every `-- exemplars/` to
      `-- exemplars/ engine/ skills/ vault-scaffold/` when you run it for a release.
- [ ] The packaging files carry no identity either. With `PAT` built from the local
      pattern file as in section 4 below, both of these print nothing:

      ```sh
      git grep -niE "$PAT" -- .claude-plugin README.md LICENSE CHANGELOG.md docs
      git grep -niE '/Use[r]s/|@[a-z0-9-]+\.(com|org|net)' -- .claude-plugin README.md LICENSE CHANGELOG.md docs
      ```

      The only place the maintainer appears is the GitHub handle in the manifest author
      fields, the marketplace owner, the LICENSE holder, and the README install command.
- [ ] `git status --porcelain` shows no untracked `*.local.*`, `state.json`, `.env`, or
      `atlas.config.json`; `.gitignore` already excludes them, so anything listed is a
      naming mistake.

## 4. Whole-history check (repository-specific)

A clean working tree is not enough: once the repository is public, every commit in every
branch is readable. Check the full history, not just `HEAD`.

- [ ] Build one alternation from the local pattern file and count matches across every
      patch in every ref, ignoring the `Author:` header lines (those legitimately carry the
      committer identity and are governed by the commit-email rule, not the scrub rule):

      ```sh
      PAT=$(grep -v '^#' docs/scrub-patterns.local.txt | grep -v '^$' | paste -sd'|' -)
      git log -p --all | grep -v '^Author:' | grep -ciE "$PAT"
      ```

      The count must be `0`. Any other number means an earlier commit leaked something;
      find it with `git log -p --all -S'<string>'`, rewrite history to remove it, and rerun.
      The pattern file is local and git-ignored; the command above never prints its
      contents, only the count.
- [ ] Every commit's author and committer email is the GitHub noreply address, not a real
      one: `git log --all --format='%ae%n%ce' | sort -u` shows one line.
- [ ] No branch or tag other than the ones you intend to publish:
      `git branch -a` and `git tag` list only `main` and released version tags. Delete
      scratch branches before the flip; they are part of the public history too.

## 5. Tag and publish

- [ ] `claude plugin tag . --dry-run` reports the tag `atlas-kit--v<version>` and confirms
      `plugin.json` and the marketplace entry agree. Then run it without `--dry-run`.
- [ ] Push `main` and the tag.
- [ ] From a machine or account that has never seen the repository, run the two install
      commands from the README and then `/atlas-kickoff --write-only` in a scratch repo.
      It must reach the interview with no missing-file errors.
- [ ] **maintainer:** flip the GitHub repository from private to public. This is the last
      item, done by hand in the repository settings, only after every box above is checked.
      There is no undo for what a crawler reads in the minutes after the flip.
