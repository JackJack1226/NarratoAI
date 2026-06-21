# Quick Deploy Plugin

GitHub repo in. Live URL out.

This is a standalone MVP for the Quick-Deploy plugin idea. It intentionally avoids the old web UI, WebSocket server, and bridge dependency so the first product loop stays simple and shippable.

## Install

No runtime dependencies are required beyond Node.js and git.

```bash
cd E:\MyClaudeProject\quick-deploy-plugin
npm install
```

`npm install` is currently optional because the MVP uses only Node built-ins.

## Usage

**Human-readable mode:**

```bash
node quick-deploy.js https://github.com/user/repo
```

**Machine-readable JSON (for AI agents):**

```bash
node quick-deploy.js https://github.com/user/repo --json
```

## What it handles

| Repo type | Detection path |
|-----------|----------------|
| Node.js web app | `dev`/`start`/`preview`/`serve` scripts from `package.json` |
| Monorepo | Scans `apps/*`, `packages/*`, `examples/*` for runnable sub-projects |
| Static HTML site | `index.html` in root or common subdirectories → served via `npx serve` |
| Simple Node file | Falls back to `node server.js` or `node app.js` |

## Runtime behavior

Quick Deploy runs the app in the foreground after it detects a URL.

Expected success output:

```text
✅ Live URL: http://localhost:5173
Quick Deploy is keeping the app in the foreground. Press Ctrl+C to stop it.
```

Press `Ctrl+C` to stop the app process.

## JSON mode (for AI agents)

```bash
node quick-deploy.js https://github.com/user/repo --json
```

Outputs a single JSON line on success:

```json
{"type":"quick_deploy","success":true,"url":"http://localhost:5173","pid":12345}
```

On failure:

```json
{"type":"quick_deploy","success":false,"error":"No runnable app found."}
```

On interrupt:

```json
{"type":"quick_deploy","success":false,"error":"Interrupted by user"}
```

## Environment variables

```bash
QUICK_DEPLOY_FRESH=1       # remove stale workspace checkout before cloning
QUICK_DEPLOY_TIMEOUT=90000 # milliseconds to wait for a localhost URL; must be >= 5000
```

## Requirements

- Node.js
- git
- npm, pnpm, or yarn depending on the target repo
- Target repo should be a Node project or a static HTML site

## Design boundaries

- Local-only MVP: this does not create a public cloud deployment URL yet.
- No API keys are hardcoded or required.
- Repository code stays local under `workspace/`.
- Shell commands are executed with argument arrays, not by string-concatenating untrusted input into a shell.

## Next steps

- Add optional Cloudflare Tunnel / ngrok for public URLs.
- Package as Claude Code / OpenClaw Skill.
- Extract a reusable core for VS Code extension integration.
- Add better framework detection and per-framework hints.
