---
name: quick-deploy
description: Clone a GitHub repo and run it locally, returning a live localhost URL. Handles plain Node apps, pnpm/npm/yarn monorepos (auto-builds workspace:* libs first), static HTML, and bare Node files. Use when the user says quick deploy, deploy this repo, run this GitHub project, spin up this repo, or wants to see a GitHub project running locally.
---

# Quick Deploy

Turn a GitHub repository into a running local app and report its `localhost` URL. Built to be called by an agent: pass `--json` and parse the structured result.

## Command

```bash
node .claude/skills/quick-deploy/quick-deploy.js https://github.com/user/repo
node .claude/skills/quick-deploy/quick-deploy.js https://github.com/user/repo --json   # agent mode
```

## How it detects what to run

In order:

1. **Root Node app** — `package.json` with a `dev`/`start`/`preview`/`serve` (or `develop`/`docs:dev`/`storybook`/`start:dev`/…) script → install + run.
2. **Monorepo** — root has no runnable script: scans `apps/`, `packages/`, `examples/`, `app/`, `demo/`, `playground/` for a sub-project that does.
   - Detects the **workspace root** (`pnpm-workspace.yaml`, or `workspaces` field) and the package manager (honors the `packageManager` field, then lockfiles).
   - Installs at the **workspace root**, not the sub-dir.
   - If the app depends on siblings via `workspace:*` **and** the root has a `build` script, runs that build first (so `dist/` outputs exist) — separate timeout, default 600s.
   - Starts the app's `dev` script in its sub-dir.
3. **Static HTML** — `index.html` (root / `docs` / `public` / `dist`) → `npx serve`.
4. **Bare Node file** — `server.js`/`app.js`/`index.js`/`main.js` → `node <file>`.

URL is parsed from the child's stdout/stderr, **with ANSI color codes stripped first** (Vite et al. colorize the port, which otherwise defeats parsing).

## JSON output (agent mode)

| Outcome | JSON |
|---------|------|
| Success | `{"type":"quick_deploy","success":true,"url":"http://localhost:5173/","pid":12345,"packageManager":"pnpm","scriptName":"dev","monorepoSubPath":"playground/data"}` |
| Failure | `{"type":"quick_deploy","success":false,"error":"message"}` |
| Exit | `{"type":"quick_deploy_exit","code":0}` |

In JSON mode the process stays alive (app in foreground) and emits `quick_deploy_exit` when the app stops.

## Env knobs

| Var | Default | Meaning |
|-----|---------|---------|
| `QUICK_DEPLOY_FRESH=1` | off | remove existing checkout before cloning |
| `QUICK_DEPLOY_TIMEOUT` | 90000 | ms to wait for a localhost URL after the app starts |
| `QUICK_DEPLOY_BUILD_TIMEOUT_MS` | 600000 | ms for the workspace build phase |

## Verified

- `remix-run/react-router` (pnpm workspace + `workspace:*` + `catalog:`, needs build) → `playground/data` dev → HTTP 200. End-to-end tested.

## Boundaries

- Returns a **local** URL only; does not publish to any cloud. (For EXE packaging / distribution, that's the separate `DeployEngine` skill.)
- GitHub HTTPS repos only.
- Never reads/writes API keys; never uploads repo code anywhere.
- Requires `git`, `node`, and the repo's package manager (e.g. `pnpm` for pnpm workspaces) on PATH.

## Recovery

```bash
QUICK_DEPLOY_FRESH=1 node .claude/skills/quick-deploy/quick-deploy.js https://github.com/user/repo
```

If start fails: confirm git/node/package-manager are installed, and the repo has a runnable script or `index.html`. Stop a running app with `Ctrl+C`.
