# NoneBot Plugin Load Check Action

A GitHub Action for checking that a NoneBot plugin can be installed and loaded in an isolated `uv` environment. It installs the target package, initializes NoneBot with a generated fake driver, loads the plugin module, validates plugin metadata, and verifies that `metadata.config` derives from Pydantic `BaseModel`.

Use the reusable workflow to test a JSON list of Python versions with native GitHub Actions matrix jobs. The composite action remains available for a single Python version inside an existing job.

## Matrix Usage

The reusable workflow accepts `python-versions` as a JSON array. Every value becomes an independent matrix job; one value produces one job, and multiple values run independently with `fail-fast: false`.

```yaml
name: load-test

on:
  pull_request:
  push:

jobs:
  load-test:
    uses: BalconyJH/noneload/.github/workflows/load-check.yml@v1
    with:
      python-versions: '["3.12", "3.13"]'
      module-name: nonebot_plugin_example
      package: ".[all]"
      check-optional-dependencies: true
```

The reusable workflow checks out the caller repository and invokes the released composite action once for each matrix value. Matrix jobs do not expose one aggregate result-file path; inspect the individual job summary and logs instead.

To pass sensitive `.env.prod` content to the reusable workflow, use its `PLUGIN_CONFIG` secret. It takes precedence over the non-secret `plugin-config` input.

```yaml
jobs:
  load-test:
    uses: BalconyJH/noneload/.github/workflows/load-check.yml@v1
    with:
      python-versions: '["3.12", "3.13"]'
      module-name: nonebot_plugin_example
    secrets:
      PLUGIN_CONFIG: ${{ secrets.PLUGIN_CONFIG }}
```

## Single-Version Usage

Check out the repository before using the composite action. `setup-uv` reads version and cache dependency files from the plugin project.

```yaml
name: load-test

on:
  pull_request:
  push:

jobs:
  load-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.0
      - uses: BalconyJH/noneload@v1
        with:
          python-version: "3.12"
          module-name: nonebot_plugin_example
```

## Optional Dependencies

Set `check-optional-dependencies: true` to read every extra from `[project.optional-dependencies]` in the repository's `pyproject.toml` and merge all groups into the local package installation. `uv pip install` resolves the resulting requirements for the selected Python and platform. Installed optional NoneBot plugins are included in the subsequent `require()` check when `load-dependencies` is enabled.

The default is `false`, which preserves the historical behavior: only `[project].dependencies` are installed and checked. This option requires the default `uv pip install` flow and cannot be combined with `install-command`. The action records the direct optional requirements declared in `pyproject.toml` as `optional_dependencies` in the result file.

## Behavior

The composite action uses `astral-sh/setup-uv` to install `uv`, select the requested Python version, and restore or save the uv cache. It starts the check program with the selected Python:

```bash
uv run --no-project --managed-python --python 3.12 python "$GITHUB_ACTION_PATH/src/check_load.py"
```

The check program runs in `working-directory` and performs the following operations:

```bash
uv python install 3.12
uv venv --managed-python --seed --python 3.12 "$RUNNER_TEMP/nonebot-plugin-load-check/.venv"
uv pip install --python "$RUNNER_TEMP/nonebot-plugin-load-check/.venv/bin/python" -e .
```

When `install-command` is set, it replaces the default package installation command. The created virtual environment is first added to `PATH` and exposed through `VIRTUAL_ENV`, so the command may use either `uv pip install --python "$VIRTUAL_ENV/bin/python" ...` or `python -m pip ...`.

## Composite Action Inputs

The reusable workflow forwards every input below, except that it uses `python-versions` instead of the single `python-version` input.

| Input | Default | Description |
| --- | --- | --- |
| `module-name` | Auto-detected | Plugin module import path, such as `nonebot_plugin_foo`. Set it explicitly when possible. |
| `project-link` | `[project].name` | Distribution name from `pyproject.toml`, such as `nonebot-plugin-foo`. |
| `package` | `.` | Local target passed to `uv pip install`. Projects commonly use `.`, `.[all]`, or `.[test]`. |
| `working-directory` | `.` | Repository subdirectory containing the plugin project. |
| `python-version` | `3.12` | Python version installed by uv and used for the test virtual environment. |
| `uv-version` | Empty | Version passed to `astral-sh/setup-uv`. When empty, setup-uv resolves it from files in `working-directory` or installs the latest version. |
| `uv-cache` | `true` | Enables the setup-uv cache. Accepts `true`, `false`, or `auto`. |
| `editable` | `true` | Installs local package specifications in editable mode when `install-command` is unset. |
| `install-extra-args` | Empty | Extra arguments used for the default package installation. |
| `install-command` | Empty | Custom package installation command. Overrides the default package installation logic. |
| `plugin-config` | Empty | Inline `.env.prod` content used while loading the plugin. The action does not add this content to the result JSON or step summary. |
| `plugin-config-file` | Empty | Path to a file whose contents are appended to `.env.prod`, relative to `working-directory` unless absolute. Its contents are not added to the result JSON or step summary. |
| `registry-url` | NoneBot registry results | URL of the NoneBot `plugins.json` registry used to map package dependencies to plugin modules. |
| `load-dependencies` | `true` | Requires detected, installed NoneBot plugin dependencies after loading the target plugin. |
| `check-optional-dependencies` | `false` | Installs every optional dependency group declared in `pyproject.toml`. Requires a local package spec and cannot be used with `install-command`. |
| `timeout` | `600` | Timeout in seconds for Python installation, package installation, and plugin loading commands. |
| `fail-on-error` | `true` | Fails the action when the plugin cannot be loaded. |
| `summary` | `true` | Writes a GitHub Step Summary. |

## Reusable Workflow Inputs

| Input | Default | Description |
| --- | --- | --- |
| `python-versions` | `["3.12"]` | JSON array of Python versions. Each value is a matrix job. |
| `PLUGIN_CONFIG` secret | Empty | Secret `.env.prod` content. Takes precedence over `plugin-config`. |

All other reusable workflow inputs have the same name and behavior as the composite action inputs.

## Outputs

The following outputs are available from a single composite action invocation:

| Output | Description |
| --- | --- |
| `load` | Whether the plugin loaded successfully. |
| `run` | Whether the isolated runner was reached. This is `false` when installation fails. |
| `version` | Installed plugin distribution version, when detected. |
| `metadata` | Plugin metadata as compact JSON, when detected. |
| `result-file` | Absolute path to the complete result JSON file. |
| `test-env` | Resolved test environment, for example `python==3.12.13 nonebot2==2.5.0 pydantic==2.13.4`. |

## Result File

The `result-file` output points to a JSON file containing the load result, captured runner output, detected metadata, package version, test environment, resolved module name, project name, direct optional requirements, and dependent plugin modules.

```json
{
  "metadata": {
    "name": "Example",
    "desc": "Example plugin",
    "usage": "...",
    "type": "application",
    "homepage": "https://github.com/nonebot/example",
    "supported_adapters": null
  },
  "output": "...",
  "load": true,
  "run": true,
  "version": "0.1.0",
  "config_present": true,
  "test_env": "python==3.12.13 nonebot2==2.5.0 pydantic==2.13.4",
  "module_name": "nonebot_plugin_example",
  "project_link": "nonebot-plugin-example",
  "optional_dependencies": ["httpx>=0.28"],
  "dependencies": []
}
```

## License

This project is licensed under the [MIT License](LICENSE).
