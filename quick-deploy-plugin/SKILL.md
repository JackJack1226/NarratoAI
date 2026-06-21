---
name: quick-deploy
description: Run a GitHub Node.js repository locally and return a live localhost URL. Use when the user says quick deploy, deploy this repo, GitHub to live URL, run this GitHub project, or wants a plugin-first deployment workflow.
---

# Quick Deploy Skill

Turn a GitHub repository into a local live URL from the agent workflow.

## Primary command

```bash
node quick-deploy.js https://github.com/user/repo
```

## What it does

1. Validates the GitHub HTTPS repository URL.
2. Clones the repo into `workspace/`.
3. Detects the app type:
   - **Node.js web app** → finds `dev`/`start`/`preview`/`serve` scripts
   - **Monorepo** → scans `apps/*`, `packages/*`, `examples/*` for runnable sub-projects
   - **Static HTML** → serves via `npx serve`
   - **Simple Node file** → runs `node server.js`/`app.js`/`index.js`
4. Installs dependencies if needed.
5. Runs the app and watches for a local URL.
6. Prints the final `Live URL`.
7. Keeps the app running in the foreground until the user presses `Ctrl+C`.

## Primary command

```bash
node quick-deploy.js https://github.com/user/repo
```

## JSON mode (AI agents)

```bash
node quick-deploy.js https://github.com/user/repo --json
```

**Output:**

| Outcome | JSON |
|---------|------|
| Success | `{"type":"quick_deploy","success":true,"url":"http://localhost:5173","pid":12345}` |
| Failure | `{"type":"quick_deploy","success":false,"error":"message"}` |
| Interrupt | `{"type":"quick_deploy","success":false,"error":"Interrupted by user"}` |

## Boundaries

- MVP supports Node.js repositories only.
- MVP returns a local URL such as `http://localhost:5173`.
- It does not publish to a public cloud URL yet.
- It does not read or write API keys.
- It does not upload repository code anywhere.

## Recovery guidance

If deployment fails:

- Check that `git`, `node`, and the selected package manager are installed.
- Confirm the target repo has `package.json` and one of `dev`, `start`, `preview` scripts.
- Re-run with a clean checkout:

```bash
QUICK_DEPLOY_FRESH=1 node quick-deploy.js https://github.com/user/repo
```

If the app starts successfully, stop it with `Ctrl+C` when finished.
