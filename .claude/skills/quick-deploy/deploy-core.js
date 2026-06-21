const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const DEFAULT_URL_TIMEOUT_MS = 90_000;
const DEFAULT_BUILD_TIMEOUT_MS = 600_000;
const WORKSPACE_DIR = path.resolve(__dirname, 'workspace');

// Monitor subdirectories where apps commonly live in monorepos
const MONOREPO_SUBDIRS = ['apps', 'packages', 'examples', 'app', 'demo', 'playground'];

// Script candidate groups — first group wins
const SCRIPT_GROUPS = [
  ['dev', 'start', 'preview', 'serve', 'web', 'app'],
  ['develop', 'docs:dev', 'storybook', 'start:dev', 'start:app', 'dev:app'],
];

let quiet = false;

function log(message) {
  if (!quiet) process.stdout.write(`${message}\n`);
}

function setQuiet(q) {
  quiet = q;
}

function validateGitHubUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error('Expected a valid GitHub HTTPS URL, for example: https://github.com/user/repo');
  }

  const parts = url.pathname.split('/').filter(Boolean);
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || parts.length < 2) {
    throw new Error('Only GitHub HTTPS repository URLs are supported in this MVP. Example: https://github.com/user/repo');
  }

  const owner = parts[0];
  const repo = parts[1].replace(/\.git$/, '');
  if (!/^[A-Za-z0-9_.-]+$/.test(owner) || !/^[A-Za-z0-9_.-]+$/.test(repo)) {
    throw new Error('GitHub owner/repo contains unsupported characters.');
  }

  return `https://github.com/${owner}/${repo}.git`;
}

function getRepoDirName(repoUrl) {
  const url = new URL(repoUrl);
  const [owner, repoWithGit] = url.pathname.split('/').filter(Boolean);
  return `${owner}__${repoWithGit.replace(/\.git$/, '')}`;
}

function ensureWorkspace() {
  fs.mkdirSync(WORKSPACE_DIR, { recursive: true });
  return WORKSPACE_DIR;
}

function commandName(name) {
  return process.platform === 'win32' ? `${name}.cmd` : name;
}

function runCommand(command, args, options = {}) {
  const { cwd, label, env = {}, onChild, shell = false, timeoutMs } = options;
  return new Promise((resolve, reject) => {
    log(`\n$ ${[command, ...args].join(' ')}`);

    const child = spawn(command, args, {
      cwd,
      env: { ...process.env, FORCE_COLOR: '0', ...env },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      shell,
    });

    onChild?.(child);

    let timer = null;
    let timedOut = false;
    if (timeoutMs) {
      timer = setTimeout(() => {
        timedOut = true;
        try { child.kill(); } catch {}
      }, timeoutMs);
    }

    child.stdout.on('data', (chunk) => process.stdout.write(chunk));
    child.stderr.on('data', (chunk) => process.stderr.write(chunk));

    child.on('error', (error) => {
      if (timer) clearTimeout(timer);
      reject(new Error(`${label || command} failed to start: ${error.message}`));
    });

    child.on('close', (code) => {
      if (timer) clearTimeout(timer);
      if (timedOut) {
        reject(new Error(`${label || command} timed out after ${Math.round(timeoutMs / 1000)}s`));
        return;
      }
      if (code === 0) {
        resolve();
        return;
      }
      reject(new Error(`${label || command} exited with code ${code ?? 'unknown'}`));
    });
  });
}

async function cloneRepository(repoUrl, repoDir, options = {}) {
  const fresh = Boolean(options.fresh);

  if (fs.existsSync(repoDir) && fresh) {
    log(`[quick-deploy] Removing existing checkout: ${repoDir}`);
    fs.rmSync(repoDir, { recursive: true, force: true });
  }

  if (fs.existsSync(path.join(repoDir, 'package.json'))) {
    log(`[quick-deploy] Reusing existing checkout: ${repoDir}`);
    return;
  }

  if (fs.existsSync(repoDir)) {
    const entries = fs.readdirSync(repoDir);
    if (entries.length > 0) {
      throw new Error(`Workspace directory exists but is not a Node repo: ${repoDir}. Set QUICK_DEPLOY_FRESH=1 to replace it.`);
    }
  } else {
    fs.mkdirSync(repoDir, { recursive: true });
  }

  await runCommand('git', ['clone', '--depth=1', repoUrl, repoDir], {
    label: 'git clone',
    onChild: options.onChild,
  });
}

function readPackageJson(repoDir) {
  const packagePath = path.join(repoDir, 'package.json');
  if (!fs.existsSync(packagePath)) return null;

  try {
    return JSON.parse(fs.readFileSync(packagePath, 'utf-8'));
  } catch (error) {
    throw new Error(`Invalid package.json at ${packagePath}: ${error.message}`);
  }
}

function detectPackageManager(repoDir) {
  // 1. Honor an explicit packageManager field (corepack convention)
  const pkg = readPackageJson(repoDir);
  if (pkg && typeof pkg.packageManager === 'string') {
    const name = pkg.packageManager.split('@')[0].trim();
    if (name === 'pnpm' || name === 'yarn' || name === 'npm') return name;
  }
  // 2. Fall back to lockfiles / workspace markers
  if (fs.existsSync(path.join(repoDir, 'pnpm-lock.yaml'))) return 'pnpm';
  if (fs.existsSync(path.join(repoDir, 'pnpm-workspace.yaml'))) return 'pnpm';
  if (fs.existsSync(path.join(repoDir, 'yarn.lock'))) return 'yarn';
  if (fs.existsSync(path.join(repoDir, 'package-lock.json'))) return 'npm';
  return 'npm';
}

// Detect whether repoDir is a monorepo workspace root. Returns the package
// manager that owns the workspace, or null when it is a plain repo.
function detectWorkspaceRoot(repoDir, rootPkg) {
  if (fs.existsSync(path.join(repoDir, 'pnpm-workspace.yaml'))) return 'pnpm';
  if (rootPkg && rootPkg.workspaces) {
    return fs.existsSync(path.join(repoDir, 'yarn.lock')) ? 'yarn' : 'npm';
  }
  return null;
}

// A workspace app needs its sibling libraries built first when it depends on
// them via the `workspace:*` protocol AND the root exposes a build script.
function appNeedsWorkspaceBuild(appPkg, rootPkg) {
  if (!rootPkg || !rootPkg.scripts || typeof rootPkg.scripts.build !== 'string') return false;
  const deps = { ...appPkg.dependencies, ...appPkg.devDependencies };
  return Object.values(deps).some(
    (v) => typeof v === 'string' && v.startsWith('workspace:')
  );
}

function packageManagerInstallArgs(packageManager) {
  if (packageManager === 'pnpm') return ['install'];
  if (packageManager === 'yarn') return ['install'];
  return ['install'];
}

async function installDependencies(repoDir, packageManager, options = {}) {
  await runCommand(commandName(packageManager), packageManagerInstallArgs(packageManager), {
    cwd: repoDir,
    label: `${packageManager} install`,
    onChild: options.onChild,
    shell: process.platform === 'win32',
  });
}

// Build sibling workspace libraries from the repo root so that `workspace:*`
// apps can resolve their built `dist/` outputs before they start.
async function runWorkspaceBuild(repoDir, packageManager, options = {}) {
  const timeoutMs = options.timeoutMs
    || Number(process.env.QUICK_DEPLOY_BUILD_TIMEOUT_MS)
    || DEFAULT_BUILD_TIMEOUT_MS;
  log(`[quick-deploy] Building workspace packages (timeout ${Math.round(timeoutMs / 1000)}s)...`);
  await runCommand(commandName(packageManager), ['run', 'build'], {
    cwd: repoDir,
    label: `${packageManager} run build`,
    onChild: options.onChild,
    shell: process.platform === 'win32',
    timeoutMs,
  });
}

function selectStartScript(pkg) {
  const scripts = pkg.scripts || {};

  for (const group of SCRIPT_GROUPS) {
    for (const candidate of group) {
      if (typeof scripts[candidate] === 'string' && scripts[candidate].trim()) {
        return candidate;
      }
    }
  }

  throw new Error('No runnable script found. Expected one of: dev, start, preview, serve.');
}

function detectFramework(pkg) {
  const deps = { ...pkg.dependencies, ...pkg.devDependencies };
  if (deps['next']) return 'next';
  if (deps['vite']) return 'vite';
  if (deps['nuxt'] || deps['nuxt3']) return 'nuxt';
  if (deps['@remix-run/dev']) return 'remix';
  if (deps['@sveltejs/kit'] || deps['svelte']) return 'svelte';
  if (deps['astro']) return 'astro';
  if (deps['vue'] || deps['@vue/cli-service']) return 'vue';
  if (deps['react-scripts'] || deps['react-dev-utils']) return 'react';
  if (deps['express']) return 'express';
  return null;
}

function hasRunnableScript(pkg) {
  const scripts = pkg.scripts || {};
  for (const group of SCRIPT_GROUPS) {
    for (const candidate of group) {
      if (typeof scripts[candidate] === 'string' && scripts[candidate].trim()) return true;
    }
  }
  return false;
}

function scanMonorepoForAppDir(repoDir) {
  for (const subdir of MONOREPO_SUBDIRS) {
    const fullPath = path.join(repoDir, subdir);
    if (!fs.existsSync(fullPath)) continue;
    if (!fs.statSync(fullPath).isDirectory()) continue;

    const entries = fs.readdirSync(fullPath);
    for (const entry of entries) {
      const entryPath = path.join(fullPath, entry);
      if (!fs.statSync(entryPath).isDirectory()) continue;
      const pkg = readPackageJson(entryPath);
      if (pkg && hasRunnableScript(pkg)) {
        return { dir: entryPath, pkg, subPath: path.join(subdir, entry) };
      }
    }
  }
  return null;
}

function scanForHtmlApp(repoDir) {
  // Check common locations for index.html files that can be served
  const candidates = [
    path.join(repoDir, 'index.html'),
    path.join(repoDir, 'docs', 'index.html'),
    path.join(repoDir, 'public', 'index.html'),
    path.join(repoDir, 'dist', 'index.html'),
  ];

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

function scanForNodeFile(repoDir) {
  const candidates = ['server.js', 'app.js', 'index.js', 'main.js'];
  for (const file of candidates) {
    const fullPath = path.join(repoDir, file);
    if (fs.existsSync(fullPath)) return file;
  }
  return null;
}

function normalizeLocalUrl(url) {
  return url
    .replace('http://0.0.0.0:', 'http://localhost:')
    .replace('https://0.0.0.0:', 'https://localhost:')
    .replace(/[\s'"`>),.;\]]+$/, '');
}

// Strip ANSI escape sequences. Tools like Vite colorize their "ready" banner
// and may wrap the port number in bold codes (http://localhost:\x1b[1m5173\x1b[22m/),
// which would otherwise defeat the :\d+ URL match.
const ANSI_PATTERN = /\x1B\[[0-9;]*[A-Za-z]/g;
function stripAnsi(text) {
  return text.replace(ANSI_PATTERN, '');
}

function detectLocalUrl(text) {
  const patterns = [
    /(?:Local|Network|ready|server|listening|running|available)[^\n]*?(https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+(?:\/[^\s'"`<]*)?)/i,
    /(https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+(?:\/[^\s'"`<]*)?)/i,
  ];

  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match) {
      return normalizeLocalUrl(match[1]);
    }
  }

  return null;
}

function startApp(repoDir, packageManager, scriptName, options = {}) {
  const timeoutMs = options.timeoutMs || DEFAULT_URL_TIMEOUT_MS;

  return new Promise((resolve, reject) => {
    const command = commandName(packageManager);
    const args = ['run', scriptName];
    let settled = false;

    log(`\n$ ${[command, ...args].join(' ')}`);
    log('[quick-deploy] Waiting for a localhost URL...');

    const child = spawn(command, args, {
      cwd: repoDir,
      env: { ...process.env, FORCE_COLOR: '0', BROWSER: 'none' },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      shell: process.platform === 'win32',
    });

    options.onChild?.(child);

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const fail = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { child.kill(); } catch {}
      reject(error);
    };

    const handleOutput = (chunk, writer) => {
      const text = chunk.toString();
      writer.write(text);
      const url = detectLocalUrl(stripAnsi(text));
      if (url) {
        finish({ url, pid: child.pid, child });
      }
    };

    const timer = setTimeout(() => {
      fail(new Error(`Timed out after ${Math.round(timeoutMs / 1000)}s waiting for a localhost URL.`));
    }, timeoutMs);

    child.stdout.on('data', (chunk) => handleOutput(chunk, process.stdout));
    child.stderr.on('data', (chunk) => handleOutput(chunk, process.stderr));

    child.on('error', (error) => fail(new Error(`Failed to start app: ${error.message}`)));
    child.on('close', (code) => {
      if (!settled) {
        fail(new Error(`App process exited before a URL was detected. Exit code: ${code ?? 'unknown'}`));
      }
    });
  });
}

async function deploy(repoUrlInput, options = {}) {
  const repoUrl = validateGitHubUrl(repoUrlInput);
  const workspace = ensureWorkspace();
  const repoDir = path.join(workspace, getRepoDirName(repoUrl));
  quiet = Boolean(options.json);
  let activeChild = null;

  const onChild = (child) => {
    activeChild = child;
    options.onChild?.(child);
  };

  try {
    log(`[quick-deploy] Repo: ${repoUrl}`);
    log(`[quick-deploy] Workspace: ${repoDir}`);

    await cloneRepository(repoUrl, repoDir, {
      fresh: options.fresh,
      onChild,
    });

    // Try to detect the application entry point
    let pkg = readPackageJson(repoDir);
    const rootPkg = pkg;
    let workingDir = repoDir;
    let monorepoSubPath = null;
    let workspaceManager = null; // set when app lives in a monorepo workspace

    if (pkg && hasRunnableScript(pkg)) {
      // Standard Node.js app with runnable scripts in root
      log('[quick-deploy] Found Node.js app in repo root');
    } else if (pkg && !hasRunnableScript(pkg)) {
      // Has package.json but no runnable scripts — try monorepo subdirectory
      const found = scanMonorepoForAppDir(repoDir);
      if (found) {
        workingDir = found.dir;
        pkg = found.pkg;
        monorepoSubPath = found.subPath;
        workspaceManager = detectWorkspaceRoot(repoDir, rootPkg);
        log(`[quick-deploy] Detected monorepo: using sub-path ${found.subPath}`);
      } else {
        // No monorepo app found — check for HTML fallback
        const htmlFile = scanForHtmlApp(repoDir);
        if (htmlFile) {
          const serveDir = path.dirname(htmlFile);
          const relServeDir = path.relative(repoDir, serveDir) || '.';
          log(`[quick-deploy] No Node.js server found. Serving HTML at ${relServeDir} via npx serve`);
          const started = await startServed(repoDir, serveDir, { timeoutMs: options.timeoutMs, onChild });
          return {
            success: true,
            url: started.url,
            pid: started.pid,
            child: started.child,
            repoDir: workingDir,
            packageManager: 'npx',
            scriptName: 'serve',
            monorepoSubPath,
          };
        }
        throw new Error('No runnable app found. Repo must have a dev/start/preview/serve script, or an index.html.');
      }
    } else {
      // No package.json — try HTML fallback
      const htmlFile = scanForHtmlApp(repoDir);
      if (htmlFile) {
        const serveDir = path.dirname(htmlFile);
        const relServeDir = path.relative(repoDir, serveDir) || '.';
        log(`[quick-deploy] No package.json found. Serving HTML at ${relServeDir} via npx serve`);
        const started = await startServed(repoDir, serveDir, { timeoutMs: options.timeoutMs, onChild });
        return {
          success: true,
          url: started.url,
          pid: started.pid,
          child: started.child,
          repoDir: workingDir,
          packageManager: 'npx',
          scriptName: 'serve',
          monorepoSubPath,
        };
      }

      // Try falling back to simple node file
      const nodeFile = scanForNodeFile(repoDir);
      if (nodeFile) {
        log(`[quick-deploy] No package.json found. Running node ${nodeFile}`);
        const started = await startNodeFile(repoDir, nodeFile, { timeoutMs: options.timeoutMs, onChild });
        return {
          success: true,
          url: started.url,
          pid: started.pid,
          child: started.child,
          repoDir: workingDir,
          packageManager: 'node',
          scriptName: nodeFile,
          monorepoSubPath,
        };
      }

      throw new Error('No package.json or index.html found. This MVP currently supports Node.js and static HTML repos.');
    }

    // For a workspace app, install + build run from the repo root with the
    // workspace's package manager; the app itself still starts in its subdir.
    const installDir = workspaceManager ? repoDir : workingDir;
    const packageManager = workspaceManager || detectPackageManager(workingDir);
    const scriptName = selectStartScript(pkg);

    log(`[quick-deploy] Package manager: ${packageManager}`);
    if (workspaceManager) log(`[quick-deploy] Workspace install root: ${installDir}`);
    log(`[quick-deploy] Start script: ${scriptName}`);

    await installDependencies(installDir, packageManager, { onChild });

    if (workspaceManager && appNeedsWorkspaceBuild(pkg, rootPkg)) {
      log('[quick-deploy] App uses workspace:* deps — building sibling packages first');
      await runWorkspaceBuild(repoDir, packageManager, { onChild });
    }

    const started = await startApp(workingDir, packageManager, scriptName, {
      timeoutMs: options.timeoutMs,
      onChild,
    });

    return {
      success: true,
      url: started.url,
      pid: started.pid,
      child: started.child,
      repoDir: workingDir,
      packageManager,
      scriptName,
      monorepoSubPath,
    };
  } catch (error) {
    if (activeChild && !activeChild.killed) {
      try { activeChild.kill(); } catch {}
    }
    throw error;
  }
}

function startServed(repoDir, serveDir, options = {}) {
  const timeoutMs = options.timeoutMs || DEFAULT_URL_TIMEOUT_MS;

  return new Promise((resolve, reject) => {
    const command = process.platform === 'win32' ? 'npx.cmd' : 'npx';
    const args = ['serve', path.relative(repoDir, serveDir) || '.', '-p', '0'];
    let settled = false;

    log(`\n$ ${[command, ...args].join(' ')}`);
    log('[quick-deploy] Starting HTTP file server...');

    const child = spawn(command, args, {
      cwd: repoDir,
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      shell: process.platform === 'win32',
    });

    options.onChild?.(child);

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const fail = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { child.kill(); } catch {}
      reject(error);
    };

    const handleOutput = (chunk, writer) => {
      const text = chunk.toString();
      writer.write(text);
      const url = detectLocalUrl(stripAnsi(text));
      if (url) {
        finish({ url, pid: child.pid, child });
      }
    };

    const timer = setTimeout(() => {
      fail(new Error(`Timed out after ${Math.round(timeoutMs / 1000)}s waiting for the file server URL.`));
    }, timeoutMs);

    child.stdout.on('data', (chunk) => handleOutput(chunk, process.stdout));
    child.stderr.on('data', (chunk) => handleOutput(chunk, process.stderr));

    child.on('error', (error) => fail(new Error(`Failed to start file server: ${error.message}`)));
    child.on('close', (code) => {
      if (!settled) {
        fail(new Error(`File server exited before a URL was detected. Exit code: ${code ?? 'unknown'}`));
      }
    });
  });
}

function startNodeFile(repoDir, fileName, options = {}) {
  const timeoutMs = options.timeoutMs || DEFAULT_URL_TIMEOUT_MS;

  return new Promise((resolve, reject) => {
    const command = 'node';
    const args = [fileName];
    let settled = false;

    log(`\n$ ${[command, ...args].join(' ')}`);
    log('[quick-deploy] Running Node.js file...');

    const child = spawn(command, args, {
      cwd: repoDir,
      env: { ...process.env, FORCE_COLOR: '0' },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      shell: process.platform === 'win32',
    });

    options.onChild?.(child);

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const fail = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { child.kill(); } catch {}
      reject(error);
    };

    const handleOutput = (chunk, writer) => {
      const text = chunk.toString();
      writer.write(text);
      const url = detectLocalUrl(stripAnsi(text));
      if (url) {
        finish({ url, pid: child.pid, child });
      }
    };

    const timer = setTimeout(() => {
      fail(new Error(`Timed out after ${Math.round(timeoutMs / 1000)}s waiting for the Node server URL.`));
    }, timeoutMs);

    child.stdout.on('data', (chunk) => handleOutput(chunk, process.stdout));
    child.stderr.on('data', (chunk) => handleOutput(chunk, process.stderr));

    child.on('error', (error) => fail(new Error(`Failed to start Node server: ${error.message}`)));
    child.on('close', (code) => {
      if (!settled) {
        fail(new Error(`Node server exited before a URL was detected. Exit code: ${code ?? 'unknown'}`));
      }
    });
  });
}

module.exports = {
  WORKSPACE_DIR,
  validateGitHubUrl,
  getRepoDirName,
  ensureWorkspace,
  detectPackageManager,
  detectWorkspaceRoot,
  appNeedsWorkspaceBuild,
  scanMonorepoForAppDir,
  selectStartScript,
  detectLocalUrl,
  setQuiet,
  deploy,
};
