# artifactflow

Check how artifacts move between jobs in your GitHub Actions workflows, before a run finds out for you.

`actions/upload-artifact` and `actions/download-artifact` fail in ways that are easy to write and hard to see in review:

- **Two uploads with the same name.** Since v4, artifacts are immutable, so the second upload of `dist` in a 3-leg matrix fails with `(409) Conflict: an artifact with this name already exists`.
- **A download that races its upload.** The job that downloads `dist` doesn't `needs:` the job that uploads it. Sometimes it works, sometimes you get `Artifact 'dist' not found`.
- **A `pattern:` that matches nothing.** That isn't an error: the step logs `Total of 0 artifact(s) downloaded` and succeeds.
- **Hidden files.** Since v4.4.0, `.coverage`, `.next/` and anything else starting with `.` is skipped unless you set `include-hidden-files: true`. If every path is hidden, nothing is uploaded and the job still passes (`if-no-files-found` defaults to `warn`).
- **`archive: false` (v7+).** The `name:` input is ignored and the artifact is named after the file. A later `download-artifact` with `name: checksums` won't find `SHA256SUMS`.
- **`run-id:` without `github-token:`.** download-artifact only uses `run-id` and `repository` when a token is given. Without one, it quietly looks in the current run instead of the one you pointed at.
- **Layout changes.** When 2+ artifacts match, each one lands in `<path>/<artifact-name>/` unless `merge-multiple: true`. On v5+, a single match is extracted flat, so your paths change the day a second artifact matches.

artifactflow reads `.github/workflows/*.yml` and works out the artifact names each job produces and consumes, per matrix leg. It evaluates `${{ matrix.* }}`, `runner.os`, `format()`, `||`, env and `if:` conditions statically, and it follows local reusable workflows and composite actions. It then checks the hand-offs against what the actions actually do. All of it is offline: no token, no API calls.

## Install

```bash
pip install git+https://github.com/jatingargiitk/artifactflow
```

You need Python 3.9 or later. The only dependency is PyYAML. Or, from a clone: `pip install -e .`, or `python -m artifactflow`.

## Usage

```bash
artifactflow check [DIR]          # default command; DIR defaults to .
artifactflow check -w .github/workflows/release.yml
artifactflow check --json         # machine-readable
artifactflow check --fail-on warn # exit 1 on warnings too (default: errors only)
artifactflow check --min-severity warn --ignore AF205
artifactflow graph [DIR]          # who uploads / downloads each artifact
artifactflow rules                # list rule codes
```

Exit codes: `0` means clean at the `--fail-on` level, `1` means there are findings at or above it, and `2` means no `.github/workflows` was found.

## Example

[`examples/demo`](examples/demo/.github/workflows) has three small workflows with the mistakes above:

<!-- run: artifactflow check examples/demo -->
```console
$ artifactflow check examples/demo
.github/workflows/build.yml:13  error  AF101 [build]  3 uploads named 'dist' in one run: every one after the first fails with (409) Conflict: an artifact with this name already exists
    .github/workflows/build.yml:13 build (ubuntu-latest)
    .github/workflows/build.yml:13 build (windows-latest)
    .github/workflows/build.yml:13 build (macos-latest)
    fix: put the matrix values in the name, e.g. name: dist-${{ matrix.<key> }}
.github/workflows/build.yml:26  warn   AF105 [test]  hidden path '.coverage' without include-hidden-files: true; every path is hidden, so nothing is uploaded (if-no-files-found: warn)
    upload-artifact >= v4.4.0 skips files and folders starting with '.'
.github/workflows/build.yml:35  info   AF205 [report]  2 artifacts match, so each is extracted to cov/<artifact-name>/ (merge-multiple is false)
    e.g. cov/coverage-3.12/, cov/coverage-3.13/
    set merge-multiple: true to get one flat directory
.github/workflows/build.yml:45  error  AF202 [publish]  'dist' is uploaded by job 'build', which job 'publish' does not need: the download can run before the upload exists
    add needs: [build]
.github/workflows/comment.yml:11  error  AF206 [comment]  run-id set without github-token: download-artifact ignores it and looks in this run
    add github-token: ${{ github.token }} (and actions: read)
.github/workflows/release.yml:33  warn   AF203 [publish]  pattern 'wheels-*' matches no artifact uploaded in this run: the step succeeds with "Total of 0 artifact(s) downloaded"
    uploaded in this run: SHA256SUMS, wheel-aarch64, wheel-x86_64
.github/workflows/release.yml:38  error  AF106 [publish]  downloads 'checksums', but the upload at .github/workflows/release.yml:23 has archive: false, so the artifact is named 'SHA256SUMS'
    with archive: false the name input is ignored; download name: SHA256SUMS

3 workflows, 8 upload steps, 5 download steps (per matrix leg): 4 errors, 2 warnings, 1 info
```

`graph` shows the same thing as a map:

<!-- run: artifactflow graph examples/demo -w examples/demo/.github/workflows/build.yml -->
```console
$ artifactflow graph examples/demo -w examples/demo/.github/workflows/build.yml
.github/workflows/build.yml  (Build)
  dist
    up    build (macos-latest)
    up    build (ubuntu-latest)
    up    build (windows-latest)
    down  publish  NOT ORDERED
  coverage-3.12
    up    test (3.12)
    down  report  ok
  coverage-3.13
    up    test (3.13)
    down  report  ok
```

## In CI

```yaml
  artifactflow:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
      - run: pip install git+https://github.com/jatingargiitk/artifactflow
      - run: artifactflow check . --fail-on warn
```

This repository's own [CI](.github/workflows/ci.yml) builds a wheel in one job, hands it to another through `upload-artifact`/`download-artifact`, and runs `artifactflow check . --fail-on warn` on itself. It also re-runs every example block in this README and diffs the output ([`scripts/check_readme.py`](scripts/check_readme.py)).

## On real workflows

I ran artifactflow over the workflow directories of a few large public repositories on 2026-11-26 (IST). These blocks were captured by hand that day and aren't re-run in CI. I haven't reported any of them upstream.

**apache/arrow** at `dd456e0`, `r_extra.yml`:

```console
$ artifactflow check -w .github/workflows/r_extra.yml
.github/workflows/r_extra.yml:197  error  AF104 [docker]  artifact name 'test-output-{{ "" }}' contains a double quote ("): upload fails with "The artifact name is not valid"
    leg: docker ([...], r, ubuntu-latest, Rocker R-ver)

1 workflow, 7 upload steps, 0 download steps (per matrix leg): 1 error, 0 warnings, 0 info
```

The step's name is `test-output-{{ "${{ matrix.config.r_image }}" }}`, and the matrix has no `config` key. The scheduled run [45752877074](https://github.com/apache/arrow/actions/runs/45752877074) shows exactly that name failing in the "Save the test output" step. It's hidden by `continue-on-error: true`, so the run has no artifacts at all:

```
##[error]The artifact name is not valid: test-output-{{ "" }}. Contains the following character:  Double quote "
```

**pydantic/pydantic** at `6449f80`, `ci.yml`:

```console
$ artifactflow check -w .github/workflows/ci.yml --min-severity warn
.github/workflows/ci.yml:428  warn   AF204 [core-test-builds-arch]  pattern 'pydantic_core_pypi_files_linux_*' matches artifacts from 'core-build-pgo', which job 'core-test-builds-arch' does not need: it gets whatever exists when it runs
    add needs: [core-build-pgo]
.github/workflows/ci.yml:509  warn   AF204 [core-test-builds-os]  pattern 'pydantic_core_pypi_files_*' matches artifacts from 'core-build-pyemscripten', 'core-build-sdist', which job 'core-test-builds-os' does not need: it gets whatever exists when it runs
    add needs: [core-build-pyemscripten, core-build-sdist]

1 workflow, 74 upload steps, 14 download steps (per matrix leg): 0 errors, 2 warnings, 0 info
```

`core-test-builds-arch` needs only `core-build`. Its pattern also matches the PGO builds' `pydantic_core_pypi_files_linux_<interpreter>` artifacts, and with `merge-multiple: true` those land in the same directory. Which wheels the job tests depends on which builds have finished when it starts.

**pytorch/pytorch** at `95f301b`, `generated-linux-binary-manywheel-nightly.yml`:

```console
$ artifactflow check -w .github/workflows/generated-linux-binary-manywheel-nightly.yml --min-severity warn
.github/workflows/generated-linux-binary-manywheel-nightly.yml:2186  error  AF202 [libtorch-extract]  'manywheel-py3_11-cpu' is uploaded by job 'manywheel-cpu-build', which job 'libtorch-extract' does not need: the download can run before the upload exists
    add needs: [manywheel-cpu-build]

1 workflow, 62 upload steps, 124 download steps (per matrix leg): 1 error, 0 warnings, 0 info
```

`libtorch-extract` has `needs: [manywheel-build]`. Its cpu leg downloads the wheel from `manywheel-cpu-build`, which is a separate job. Nothing orders the two jobs, so it only works as long as `manywheel-cpu-build` finishes first.

## Rules

<!-- run: artifactflow rules -->
```console
$ artifactflow rules
AF101  two uploads in one run produce the same artifact name
AF102  same name with overwrite: true from jobs that run in parallel
AF103  matrix comes from an expression and the name does not vary per leg
AF104  artifact name contains a character the service rejects
AF105  path selects hidden files that upload-artifact leaves out
AF106  archive: false ignores name: the artifact is named after the file
AF201  download by name that nothing in this run uploads
AF202  download runs in a job that does not need the uploading job
AF203  pattern matches no artifact uploaded in this run
AF204  pattern matches uploads from jobs this job does not need
AF205  several artifacts land in per-artifact subdirectories
AF206  run-id / repository without github-token is ignored
AF207  conflicting download inputs (name + pattern / artifact-ids)
AF301  artifact action v1-v3: fails on github.com since 2025-01-30
AF302  input needs a newer version of the action than the one pinned
AF303  retention-days outside 1-90
```

Severities follow what happens at run time. An **error** means the step fails (or downloads from the wrong place). A **warn** means it succeeds but probably not the way you meant. An **info** is something to know about, like where the files land.

How a few of the rules work:

- **Names** are worked out per matrix leg. Values that are the same for the whole run (`github.sha`, `inputs.*`, `vars.*`, `needs.*`) still count, so `build-${{ github.sha }}` in a 3-leg matrix is a collision. A name built from a step output or `hashFiles()` can't be known ahead of time, so it's never reported as a collision.
- **`if:` conditions** are evaluated when they can be. `if: matrix.type == 'release'` removes the debug legs, and `failure() && runner.os == 'macOS'` removes the Linux legs. When a condition can't be decided, a collision is a warning that notes the condition. Two steps in one job with different conditions (the "success path / failure path" pattern) are only info.
- **Reusable workflows** called with `uses: ./.github/workflows/x.yml` are inlined into the caller's run, with `with:` values as `inputs.*`, and are not checked on their own. Remote reusable workflows can't be read, so a download that only they could satisfy is a warning, not an error. The same goes for third-party actions with an `artifact*` input.
- **Composite actions** under `./` are read, so an upload hidden inside `.github/actions/x/action.yml` counts.
- **Versions** come from the tag or from the `# v4.3.0` comment after a pinned SHA. `@v4` means the newest v4. Inputs older pins don't know (`archive` on v4, `skip-decompress` before v8, `overwrite` before v4.2.0) are AF302, because the runner ignores them with an "Unexpected input" warning.

Where the behaviour comes from:

- The upload-artifact, download-artifact and @actions/glob sources.
- The 409 Conflict message, name validation, single-file `archive: false` naming, and hidden-file skipping at the search root and below.
- download-artifact's `artifacts.length === 1` flat extraction, added in v5.0.0.
- The [v3 deprecation notice](https://github.blog/changelog/2024-04-16-deprecation-notice-v3-of-the-artifact-actions/).

## What it can't see

- **Values only known at run time:** step outputs, `$GITHUB_ENV`, `fromJSON(needs.*.outputs.*)` matrices, and remote reusable workflows and third-party actions. When a name is partly unknown, it matches loosely: it's never a collision, and it can satisfy a download.
- **Other runs.** A download with `github-token` and a real `run-id`, or with `artifact-ids`, is left alone.
- **The file system.** It doesn't know whether `dist/` will contain hidden files at run time, only whether the path you wrote selects them.
- **GHES.** It assumes github.com, where v4+ is current. On GHES, v3 is still the supported line, so ignore AF301 there.

## License

MIT
