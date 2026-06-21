# Marketplace Copy

## Title

Quick Deploy — GitHub Repo to Local Live URL

## Short description

Paste a GitHub repo URL and get a running localhost URL from your agent workflow.

## Long description

Quick Deploy is a plugin-first deployment helper for developers and AI agents. It clones a GitHub project into an isolated workspace, detects the project type (Node.js web app, monorepo sub-project, static HTML, or simple script), installs dependencies, runs the app, watches terminal output, and returns the local live URL.

It is intentionally local-first for the MVP: no cloud account, no API key, no dashboard, no web UI required.

## Target users

- Developers testing open-source repos quickly.
- AI agents that need to run demo apps before editing them.
- Builders who want deployment-style feedback directly in Claude Code / OpenClaw.
- Creators packaging small GitHub demos into repeatable workflows.

## Demo command

```bash
node quick-deploy.js https://github.com/user/repo --json
```

Expected output:

```json
{"type":"quick_deploy","success":true,"url":"http://localhost:5173","pid":12345}
```

## MVP limits

- Node.js web apps, monorepos, static HTML sites, and simple Node scripts.
- Local URL only (no public cloud deployment yet).
- Requires git, Node.js, and npm/pnpm/yarn to be installed locally.

## Roadmap

1. Public URL mode via Cloudflare Tunnel or ngrok.
2. VS Code command palette extension.
3. Error auto-repair loop using LLM feedback.
4. Framework-aware deploy hints.
5. Integration with the original Quick-Deploy bridge once cross-directory writes are no longer blocked.
