# Frontmatter Actions

Versioned Hugo builds and GitHub Pages deployments for Frontmatter. Build logic
lives here; each blog keeps its own source, build settings, and a small caller
workflow. Builds run in the blog's GitHub Actions account. No Frontmatter service,
personal access token, or deployment secret is required by the shared workflow.

## Status

Initial implementation for review. There is no supported release yet. Use a
reviewed commit SHA when evaluating; never reference a moving branch in production.
Initial scope is GitHub.com and Linux x86_64 GitHub-hosted runners. GitHub Enterprise
Server is not supported because the workflow uses GitHub.com's `job.workflow_*`
identity properties to fetch its own implementation at the caller's pinned commit.

## Install in a blog

1. Create `.frontmatter/build.json` using [the example](examples/build.json).
   Set the exact Hugo release, standard/extended edition, and public base URL.
   The example starts with Hugo 0.168.0; older versions remain covered by regression tests.
   Include the repository prefix for project sites, or your existing custom domain.
2. Copy [the caller workflow](examples/frontmatter.yml) into
   `.github/workflows/frontmatter.yml`. Replace `PINNED_RELEASE_COMMIT` with a
   reviewed 40-character commit from this repository. Set the publication branch
   consistently in the triggers and workflow input.
3. Set **Settings → Pages → Source** to **GitHub Actions**. Preserve the existing
   custom domain and DNS. Allow the publication branch in the `github-pages`
   environment. Organization Actions policies must allow the pinned public workflow
   and its dependencies.
4. Open a setup PR. Review/remove a superseded deployment workflow as part of the
   migration so two workflows do not deploy the same site.
5. PRs validate and upload artifacts. A push to the publication branch deploys.
   A manual run builds only unless its **deploy** input is explicitly enabled;
   even then, only the configured publication branch can deploy.

The caller grants the reusable workflow a maximum permission set. The build job
reduces this to `contents: read`; only deployment receives `pages: write` and
`id-token: write`. PR events cannot enter deployment, including when a caller
incorrectly passes `deploy: true`. Forked PRs remain subject to GitHub's approval
and permission policies. Never run untrusted PR source with `pull_request_target`.

Public blogs work with GitHub Free. Private-repository Pages requires an eligible
GitHub plan. Recursive checkout supports public theme submodules. Access to private
submodules would need a separately designed credential flow; no secrets are
inherited or forwarded by this workflow.

## Build configuration

The [JSON schema](schema/build.schema.json) is the native app/workflow contract.
The runner rejects duplicate keys, unknown fields, unsupported schema versions,
unpinned versions, invalid URLs/time zones, and paths outside the checkout.

| Setting | Behavior |
| --- | --- |
| `schemaVersion` | Required; currently `1`. |
| `hugo.version` | Required; stable exact release such as `0.168.0`. |
| `hugo.edition` | Required; `standard` or `extended`. |
| `baseURL` | Required; absolute HTTP(S) site URL, including any project prefix. Overrides the site's configured base URL for this build. |
| `siteDirectory` | Defaults to `.`; may point to a nested Hugo site inside the repository. |
| `timeZone` | Defaults to `UTC`; an IANA name such as `America/New_York`. |
| `build.minify` | Defaults to `true`. |
| `build.garbageCollect` | Defaults to `true`. |

The site retains its ordinary Hugo configuration, theme, content, and shortcodes.
Hugo's production environment is selected. Drafts and future posts follow the
site's Hugo settings; the workflow does not enable draft/future publishing.

Version 1 does not run arbitrary shell commands or install Node, Go, Dart Sass, or
package dependencies. Sites requiring those tools need explicit support before
adopting this workflow. The Bear Blog fixture and LandingPage do not require a
root package installation. Older Hugo releases using different asset naming
conventions may also need an installer extension.

Each build downloads the exact official Hugo archive and verifies its SHA-256
against that release's published checksum manifest. It extracts only the regular
`hugo` binary and checks the executable's reported version/edition. Checksums
protect download integrity; they are not independent publisher signatures.
Generated output and reports are kept outside the checkout; preexisting output
files are never deleted. Output must contain HTML and cannot contain symlinks.

## Reports and publication status

The `frontmatter-build-report` artifact includes `report.json` and the complete
`report.log` when available. The report follows [schema version 1](schema/report.schema.json):

- Success/failure, failure phase, requested and actual Hugo version/edition.
- Repository, actual checked-out commit SHA, requested PR head SHA, ref, and exact
  implementation commit SHA. A PR's actual build commit can be GitHub's synthetic
  merge commit; it must not be confused with the PR head.
- Timestamps, up to 100 warning lines, and an actionable failure message.

Reports are uploaded after configuration/install/build failures when the report
script ran. Checkout failures, job cancellation, or runner failure may prevent a
report from existing. Frontmatter must use GitHub's run/job conclusions as the
authority, and must not interpret a missing report as success.

A successful build is not a successful deployment. The separate deployment job
uses that run's uploaded site artifact, targets `github-pages`, and returns
`page-url` only after GitHub's deployment succeeds. Frontmatter should associate
deployment state with the exact source revision; it must not show another run's
deployment as the current post's result. Reports/logs can contain private content;
they stay in the caller's repository artifacts (14-day retention).

## Upgrades and rollback

Hugo upgrades change only the blog's configuration and run through PR validation.
Workflow upgrades change the pinned implementation SHA in a separate PR. A new
release here does not silently change existing sites. All action dependencies are
pinned to commits; Dependabot proposes reviewed updates.

Reverting an upgrade restores the previous settings/implementation for the next
build. The previous Pages deployment remains available when a build fails. A
successful build cannot prove visual compatibility; inspect representative pages
before accepting theme/Hugo changes.

## Validate

```sh
git submodule update --init --recursive
python3 -m unittest discover -s tests -v
actionlint
```

CI installs a pinned, checksum-verified actionlint. Its narrow exception covers
the new documented `job.workflow_repository` and `job.workflow_sha` properties
until actionlint updates its context types. CI also runs the real Bear Blog fixture
on Hugo 0.147.2 and 0.168.0, including standard/extended editions; checks shortcodes,
draft exclusion, assets, and project URLs; and calls the reusable workflow itself.

For a manual Linux build, keep output/tools/reports outside the source checkout:

```sh
python3 scripts/frontmatter.py build \
  --workspace "$PWD" --config tests/fixtures/reusable.json \
  --output /tmp/frontmatter-check/public \
  --tools /tmp/frontmatter-check/tools \
  --report /tmp/frontmatter-check/report.json
```

Repository and host-specific agent instructions still govern local validation.

## GitHub App integration

The Frontmatter GitHub App needs Contents/PR write for configuration PRs,
Workflows write for setup and implementation upgrades, and Actions read to inspect
private-repository runs/artifacts. Manual rerun/cancel controls would require
Actions write. Those app permissions are separate from the short-lived
`GITHUB_TOKEN` permissions declared by the deployment workflow. Initial Pages
settings remain an explicit repository-owner setup step; no Administration
permission is requested by the app for this implementation.

Native app UI, automatic setup PR generation, Pages administration, and release
automation are follow-up work. This repository defines and exercises the contract
those features will consume.
