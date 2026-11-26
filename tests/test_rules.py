from conftest import codes


def test_matrix_upload_same_name_collides(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            strategy:
              matrix:
                os: [ubuntu-latest, windows-latest]
            runs-on: ${{ matrix.os }}
            steps:
              - uses: actions/upload-artifact@v4
                with:
                  name: dist
                  path: dist/
    """})
    assert codes(f) == ["AF101"]
    assert f[0].severity == "error" and "2 uploads named 'dist'" in f[0].message


def test_matrix_value_in_name_is_fine(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            strategy:
              matrix:
                os: [ubuntu-latest, windows-latest]
            runs-on: ${{ matrix.os }}
            steps:
              - uses: actions/upload-artifact@v4
                with:
                  name: dist-${{ matrix.os }}
                  path: dist/
    """})
    assert codes(f) == []


def test_runner_os_resolves_from_runs_on(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            strategy:
              matrix:
                os: [ubuntu-22.04, ubuntu-24.04, macos-15]
            runs-on: ${{ matrix.os }}
            steps:
              - uses: actions/upload-artifact@v4
                with:
                  name: dist-${{ runner.os }}
                  path: dist/
    """})
    assert codes(f) == ["AF101"]
    assert "'dist-Linux'" in f[0].message


def test_run_constant_unknown_still_collides(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            strategy:
              matrix:
                py: ["3.12", "3.13"]
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with:
                  name: build-${{ github.sha }}
                  path: dist/
              - uses: actions/upload-artifact@v4
                with:
                  name: out-${{ steps.meta.outputs.tag }}
                  path: out/
    """})
    assert codes(f) == ["AF101"]
    assert "github.sha" in f[0].message


def test_step_if_false_on_leg_is_skipped(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            strategy:
              matrix:
                include:
                  - {arch: x64, type: debug}
                  - {arch: x64, type: release}
            runs-on: ubuntu-latest
            steps:
              - if: matrix.type == 'release'
                uses: actions/upload-artifact@v4
                with:
                  name: pkg-${{ matrix.arch }}
                  path: pkg/
    """})
    assert codes(f) == []


def test_same_job_exclusive_steps_are_info(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - if: steps.x.outcome == 'success'
                uses: actions/upload-artifact@v4
                with: {name: result, path: ok.json}
              - if: steps.x.outcome != 'success'
                uses: actions/upload-artifact@v4
                with: {name: result, path: err.json}
    """})
    assert codes(f, "info") == ["AF101"] and codes(f, "error") == []


def test_overwrite_in_later_job_is_fine_but_parallel_is_a_race(repo):
    ok = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: site, path: site/}
          b:
            needs: a
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: site, path: site/, overwrite: true}
    """})
    assert codes(ok) == []
    race = repo({"ci.yml": """
        on: push
        jobs:
          a:
            strategy: {matrix: {n: [1, 2]}}
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: site, path: site/, overwrite: true}
    """})
    assert codes(race) == ["AF102"]


def test_dynamic_matrix_constant_name(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            strategy:
              matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: report, path: r/}
              - uses: actions/upload-artifact@v4
                with: {name: "report-${{ matrix.target }}", path: r/}
    """})
    assert codes(f) == ["AF103"]


def test_invalid_name_after_matrix_expansion(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            strategy: {matrix: {node: ["lts/*", "22"]}}
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: "logs-${{ matrix.node }}", path: logs/}
    """})
    assert codes(f) == ["AF104"]
    assert "lts/*" in f[0].message


def test_hidden_path(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: cov, path: .coverage, if-no-files-found: error}
              - uses: actions/upload-artifact@v4
                with:
                  name: cov2
                  path: |
                    htmlcov/
                    .coverage
              - uses: actions/upload-artifact@v4
                with: {name: cov3, path: .coverage, include-hidden-files: true}
    """})
    sev = sorted((x.code, x.severity, x.line) for x in f)
    assert [s[:2] for s in sev] == [("AF105", "error"), ("AF105", "warn")]


def test_archive_false_renames(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v7
                with: {name: sums, path: out/SHA256SUMS, archive: false}
              - uses: actions/upload-artifact@v7
                with: {path: "out/*.tar", archive: false}
          b:
            needs: a
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v8
                with: {name: sums}
              - uses: actions/download-artifact@v8
                with: {name: SHA256SUMS}
    """})
    assert codes(f) == ["AF106", "AF106"]
    msgs = " | ".join(x.message for x in f)
    assert "named 'SHA256SUMS'" in msgs and "exactly one file" in msgs


def test_archive_on_old_version_is_ignored_input(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: sums, path: SHA256SUMS, archive: false}
          b:
            needs: a
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: sums, skip-decompress: true}
    """})
    assert codes(f) == ["AF302", "AF302"]


def test_download_missing_and_unordered(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: dist, path: dist/}
          test:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: dist}
              - uses: actions/download-artifact@v4
                with: {name: dits}
          deploy:
            needs: [test]
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: dist}
    """})
    assert codes(f) == ["AF201", "AF202", "AF202"]
    missing = [x for x in f if x.code == "AF201"][0]
    assert missing.severity == "error" and "uploaded in this run: dist" in missing.detail[0]


def test_transitive_needs_count(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: dist, path: dist/}
          test:
            needs: build
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          deploy:
            needs: test
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: dist}
    """})
    assert codes(f) == []


def test_same_job_download_before_upload(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: dist}
              - uses: actions/upload-artifact@v4
                with: {name: dist, path: dist/}
    """})
    assert codes(f) == ["AF202"]
    assert "later in the same job" in f[0].message


def test_patterns(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          test:
            strategy: {matrix: {py: ["3.12", "3.13"]}}
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: "coverage-${{ matrix.py }}", path: cov.xml}
          lint:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: coverage-lint, path: cov.xml}
          report:
            needs: test
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v5
                with: {pattern: "coverage-*", path: cov}
              - uses: actions/download-artifact@v5
                with: {pattern: "covrage-*", merge-multiple: true}
              - uses: actions/download-artifact@v5
                with: {pattern: "coverage-3.12"}
    """})
    got = sorted((x.code, x.severity) for x in f)
    assert got == [("AF203", "warn"), ("AF204", "warn"), ("AF205", "info"), ("AF205", "info")]
    partial = [x for x in f if x.code == "AF204"][0]
    assert "'lint'" in partial.message
    nested = [x for x in f if x.code == "AF205" and "3 artifacts" in x.message]
    assert nested and "cov/<artifact-name>/" in nested[0].message


def test_v4_single_pattern_match_is_nested(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: only-one, path: x}
          b:
            needs: a
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {pattern: "only-*", path: out}
    """})
    assert codes(f) == ["AF205"]
    assert "v5+ extracts it flat" in f[0].message


def test_run_id_without_token_and_conflicting_inputs(repo):
    f = repo({"ci.yml": """
        on:
          workflow_run:
            workflows: [CI]
            types: [completed]
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with:
                  name: coverage
                  run-id: ${{ github.event.workflow_run.id }}
              - uses: actions/download-artifact@v4
                with:
                  name: coverage
                  github-token: ${{ github.token }}
                  run-id: ${{ github.event.workflow_run.id }}
              - uses: actions/download-artifact@v4
                with: {name: x, artifact-ids: "1,2"}
    """})
    assert codes(f) == ["AF206", "AF207"]


def test_name_and_pattern(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: a, path: x}
              - uses: actions/download-artifact@v4
                with: {name: a, pattern: "a*"}
    """})
    assert codes(f) == ["AF207"] and f[0].severity == "warn"


def test_deprecated_versions_and_sha_comment(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v3
                with: {name: a, path: x}
              - uses: actions/download-artifact@fa0a91b85d4f404e444e00e005971372dc801d16 # v3.0.2
                with: {name: a}
              - uses: actions/upload-artifact@v4.1.0
                with: {name: b, path: x, overwrite: true}
    """})
    assert codes(f) == ["AF301", "AF301", "AF302"]


def test_retention(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: a, path: x, retention-days: 400}
    """})
    assert codes(f) == ["AF303"]


def test_pages(repo):
    ok = repo({"pages.yml": """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-pages-artifact@v4
                with: {path: _site}
          deploy:
            needs: build
            runs-on: ubuntu-latest
            steps:
              - uses: actions/deploy-pages@v4
    """})
    assert codes(ok) == []
    bad = repo({"pages.yml": """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-pages-artifact@v4
                with: {path: _site}
          deploy:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/deploy-pages@v4
    """})
    assert codes(bad) == ["AF202"]


def test_merge_action(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            strategy: {matrix: {os: [a, b]}}
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact@v4
                with: {name: "part-${{ matrix.os }}", path: x}
          merge:
            needs: build
            runs-on: ubuntu-latest
            steps:
              - uses: actions/upload-artifact/merge@v4
                with: {name: all, pattern: "part-*"}
          use:
            needs: merge
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: all}
    """})
    assert codes(f) == []


def test_known_and_possible_uploaders(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: hynek/build-and-inspect-python-package@v2
              - uses: some/builder@v1
                with: {artifact-name: wheels}
          publish:
            needs: build
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: Packages}
              - uses: actions/download-artifact@v4
                with: {name: wheels}
    """})
    assert [(x.code, x.severity) for x in f] == [("AF201", "warn")]
    assert "some/builder@v1" in " ".join(f[0].detail)
