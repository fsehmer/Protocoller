# Repository Guidelines

## Project Structure & Module Organization

Protocoller is an initial Python project. `pyproject.toml` declares the package name `protocoller`, version `0.1.0`, Python `>=3.12`, and no dependencies. No application modules, tests, or assets exist yet.

Place new application modules under `src/protocoller/` and tests under `tests/`. Keep project metadata and dependency declarations in `pyproject.toml`. `.venv/` contains the local Python environment; `.idea/` contains PyCharm settings. Do not treat either directory as application source.

## Build, Test, and Development Commands

- `python3 --version`: confirm that the interpreter is Python 3.12 or newer.
- `python3 -m venv .venv`: create a local environment if one is missing.
- `source .venv/bin/activate`: activate the environment on macOS or Linux.
- `python -m unittest discover -s tests -v`: run tests once the `tests/` directory exists.

No build backend, application entry point, or automated development scripts are configured. Document new run and build commands when adding those capabilities. Declare runtime dependencies in `pyproject.toml`.

## Coding Style & Naming Conventions

Use four-space indentation and follow PEP 8. Use `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_CASE` for constants. Add type annotations to public functions and keep modules focused on a single responsibility.

No repository-wide formatter or linter is configured. If introducing one, commit its configuration and document its invocation.

## Testing Guidelines

Use standard-library `unittest` initially, with files named `test_*.py` and test methods named `test_*`. Cover new behavior, boundary conditions, and regression cases. No coverage threshold is currently configured.

## Commit & Pull Request Guidelines

This directory has no Git metadata, so no historical commit convention can be established. When version control is initialized, use concise imperative subjects, such as `Add transcript parser`.

Pull requests should explain the change, reference relevant issues, and list validation commands and results. Include screenshots only for visual changes. Keep secrets, generated files, virtual environments, and personal IDE state out of commits.
