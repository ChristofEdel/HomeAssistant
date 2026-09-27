# AGENTS.md

## Scope

This workspace contains a Home Assistant custom integration. Its purpose is to push data from HomeAssistant for a InfluxDB database for futher analysis with Grafana.

## Environment

This integration runs inside an existing Home Assistant Python virtual environment located outside this workspace.
- Do not create a new virtual environment.
- Do not install, upgrade, or remove Python packages unless explicitly instructed.
- Use the Python interpreter already configured in VS Code.
- Assume Home Assistant and its development dependencies are provided by the existing environment.

## Home Assistant conventions

- Follow current Home Assistant integration development conventions.
- Prefer Home Assistant APIs and helpers over custom implementations.
- Use async APIs where expected by Home Assistant.
- Do not perform blocking I/O in the event loop.
- Use type annotations for new or modified Python code where practical.
- Do not make changes that would unnecessarily break existing installations.

## Code changes

- Make the smallest change necessary to complete the requested task.
- Preserve the existing architecture and coding style unless there is a clear reason to change it.
- Do not refactor unrelated code.
- do not reformat untouched lines
- preserve comments, or update as needed; do not remove them
- only break python lines if they exceed 100 characters in width
- Do not rename files, classes, entities, config keys, services, or constants unless required by the task.
- Do not change `manifest.json` dependencies, requirements, version information, or domains unless explicitly required.
- Do not add third-party dependencies without explicit approval.
- When fixing a bug, identify the underlying cause rather than hiding the symptom.

## Local Boundaries

* Only inspect, create, edit, or delete files inside this workspace.
* Do not read, search, inspect, or modify files outside this workspace unless explicitly instructed.
* Do not access parent directories, including the local Home Assistant configuration directory.
* Do not inspect other locally installed custom integrations.
* Do not access local secrets, credentials, `.storage`, databases, backups, tokens, or unrelated configuration files.
* Do not modify Home Assistant core files, `.gitignore`, VS Code settings, virtual-environment files, or system configuration unless explicitly instructed.
* If information outside the workspace appears necessary, explain what is needed and why before accessing it.
* You may consult publicly available Home Assistant source code, official documentation, and public integration source repositories for reference.

## Validation

After making Python changes:
- Check for syntax errors.
- Check imports and type-related issues where possible.

## Working style

Before making substantial changes, inspect the relevant files and understand the existing implementation.

For multi-file changes, briefly identify which files need modification before editing them.

Avoid speculative changes.

If requirements are ambiguous, prefer preserving current behaviour.

Do not expand the task beyond what was requested.

At completion, summarise:

* what changed
* which files changed
* any behaviour that changed
* any validation performed
* whether Home Assistant needs to be restarted or the integration reloaded
