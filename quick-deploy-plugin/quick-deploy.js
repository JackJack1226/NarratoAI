#!/usr/bin/env node
const { spawnSync } = require('child_process');
const { deploy } = require('./deploy-core');

function printUsage() {
  console.log('Usage:');
  console.log('  node quick-deploy.js https://github.com/user/repo');
  console.log('  node quick-deploy.js https://github.com/user/repo --json');
  console.log('');
  console.log('Options via env:');
  console.log('  QUICK_DEPLOY_FRESH=1      remove an existing checkout before cloning');
  console.log('  QUICK_DEPLOY_TIMEOUT=90000 milliseconds to wait for a localhost URL');
  console.log('');
  console.log('  --json                    output machine-readable JSON for AI agents');
}

function parseTimeout(value) {
  if (value === undefined || value === '') return 90_000;
  const timeout = Number(value);
  if (!Number.isFinite(timeout) || timeout < 5_000) {
    throw new Error('QUICK_DEPLOY_TIMEOUT must be a number of milliseconds >= 5000.');
  }
  return timeout;
}

function stopChild(child) {
  if (!child || child.killed) return;

  if (process.platform === 'win32' && child.pid) {
    const result = spawnSync('taskkill', ['/pid', String(child.pid), '/t', '/f'], {
      stdio: 'ignore',
      windowsHide: true,
    });
    if (result.status === 0) return;
  }

  try {
    child.kill('SIGTERM');
  } catch {}
}

function waitForAppToExit(child) {
  return new Promise((resolve) => {
    if (!child || child.killed) {
      resolve();
      return;
    }
    child.once('close', (code) => resolve(code));
  });
}

async function main() {
  const args = process.argv.slice(2);
  const jsonMode = args.includes('--json');
  const repoUrl = args.find((a) => !a.startsWith('--'));

  if (!repoUrl || repoUrl === '-h' || repoUrl === '--help') {
    printUsage();
    process.exit(repoUrl ? 0 : 2);
  }

  let activeChild = null;
  let shuttingDown = false;

  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    try {
      stopChild(activeChild);
    } catch {}
  };

  process.on('SIGINT', () => {
    shutdown();
    if (jsonMode) {
      console.log('{"type":"quick_deploy","success":false,"error":"Interrupted by user"}');
    }
    process.exit(130);
  });

  process.on('SIGTERM', () => {
    shutdown();
    if (jsonMode) {
      console.log('{"type":"quick_deploy","success":false,"error":"Terminated"}');
    }
    process.exit(143);
  });

  try {
    const timeoutMs = parseTimeout(process.env.QUICK_DEPLOY_TIMEOUT);
    const result = await deploy(repoUrl, {
      fresh: process.env.QUICK_DEPLOY_FRESH === '1',
      timeoutMs,
      json: jsonMode,
      onChild(child) {
        activeChild = child;
      },
    });

    activeChild = result.child;

    if (jsonMode) {
      console.log(JSON.stringify({
        type: 'quick_deploy',
        success: true,
        url: result.url,
        pid: result.pid,
        repoDir: result.repoDir,
        packageManager: result.packageManager,
        scriptName: result.scriptName,
        monorepoSubPath: result.monorepoSubPath || null,
      }));
      // In json mode, keep the child alive and wait for exit
      const code = await waitForAppToExit(activeChild);
      if (!shuttingDown) {
        console.log(JSON.stringify({ type: 'quick_deploy_exit', code }));
      }
      process.exit(code === 0 || code === null ? 0 : 1);
    } else {
      console.log('');
      console.log(`✅ Live URL: ${result.url}`);
      console.log(`↳ Repo: ${result.repoDir}`);
      if (result.monorepoSubPath) {
        console.log(`↳ Sub-path: ${result.monorepoSubPath}`);
      }
      console.log(`↳ PID: ${result.pid}`);
      console.log('');
      console.log('Quick Deploy is keeping the app in the foreground. Press Ctrl+C to stop it.');

      const code = await waitForAppToExit(activeChild);
      if (!shuttingDown) {
        console.log(`\n[quick-deploy] App process exited with code ${code ?? 'unknown'}.`);
      }
      process.exit(code === 0 || code === null ? 0 : 1);
    }
  } catch (error) {
    if (jsonMode) {
      console.log(JSON.stringify({
        type: 'quick_deploy',
        success: false,
        error: error.message,
      }));
    } else {
      console.error('');
      console.error(`❌ Quick Deploy failed: ${error.message}`);
      console.error('');
      console.error('Checklist:');
      console.error('  - git is installed and available in PATH');
      console.error('  - Node.js and npm/pnpm/yarn are installed');
      console.error('  - the repo is a Node app with dev/start/preview/serve script');
      console.error('  - set QUICK_DEPLOY_FRESH=1 if the workspace checkout is stale');
    }
    process.exit(1);
  }
}

main();
