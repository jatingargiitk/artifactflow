from conftest import codes


def test_reusable_workflow_inlined_into_caller(repo):
    f = repo({
        "ci.yml": """
            on: push
            jobs:
              build:
                strategy: {matrix: {os: [linux, mac]}}
                uses: ./.github/workflows/build.yml
                with:
                  os: ${{ matrix.os }}
              publish:
                needs: build
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/download-artifact@v4
                    with: {name: dist-linux}
                  - uses: actions/download-artifact@v4
                    with: {name: dist-windows}
        """,
        "build.yml": """
            on:
              workflow_call:
                inputs:
                  os: {type: string, required: true}
            jobs:
              make:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/upload-artifact@v4
                    with:
                      name: dist-${{ inputs.os }}
                      path: dist/
        """,
    })
    # build.yml is only workflow_call and has a caller: it is not checked on its own
    assert codes(f) == ["AF201"]
    assert "dist-windows" in f[0].message
    assert "dist-linux, dist-mac" in f[0].detail[0]


def test_reusable_without_local_caller_is_lenient(repo):
    f = repo({"lib.yml": """
        on: workflow_call
        jobs:
          use:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: from-caller}
    """})
    assert [(x.code, x.severity) for x in f] == [("AF201", "info")]


def test_remote_reusable_downgrades_missing(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            uses: org/shared/.github/workflows/build.yml@v1
          publish:
            needs: build
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: dist}
    """})
    assert [(x.code, x.severity) for x in f] == [("AF201", "warn")]


def test_composite_action_steps_count(repo):
    f = repo({"ci.yml": """
        on: push
        jobs:
          build:
            strategy: {matrix: {os: [a, b]}}
            runs-on: ubuntu-latest
            steps:
              - uses: ./.github/actions/pack
                with:
                  suffix: ${{ matrix.os }}
          test:
            needs: build
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: {name: pkg-a}
              - uses: ./.github/actions/logs
    """}, actions={
        ".github/actions/pack": """
            name: pack
            inputs:
              suffix: {required: true}
            runs:
              using: composite
              steps:
                - uses: actions/upload-artifact@v4
                  with:
                    name: pkg-${{ inputs.suffix }}
                    path: pkg/
        """,
        ".github/actions/logs": """
            name: logs
            runs:
              using: composite
              steps:
                - uses: actions/upload-artifact@v4
                  with: {name: logs, path: .logs/}
        """,
    })
    assert codes(f) == ["AF105"]
    assert f[0].path == ".github/actions/logs/action.yml"


def test_yaml_error_reported(repo):
    f = repo({"bad.yml": "on: push\njobs: [\n"})
    assert codes(f) == ["AF000"]
