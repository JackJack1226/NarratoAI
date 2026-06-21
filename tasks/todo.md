# Tier 2: react-router monorepo 支持

目标：让 `quick-deploy` 能跑起 react-router 这类 **pnpm workspace + 需先 build 库** 的仓库。

## 根因（已确认）
- runnable demo 在 `playground/*`，白名单 `MONOREPO_SUBDIRS` 漏了 `playground`
- 依赖是 `workspace:*` / `catalog:` → 只能用 pnpm，且必须在**仓库根**装
- 当前 `detectPackageManager` 看子目录（无 lockfile）→ 误判 npm
- playground 依赖 20+ 库的 dist → 启动前需 `pnpm build`

## 实施清单
- [x] 1. `MONOREPO_SUBDIRS` 加入 `playground`
- [x] 2. `detectPackageManager` 优先读 `packageManager` 字段，再 pnpm-workspace.yaml，再 lockfile
- [x] 3. 新增 `detectWorkspaceRoot(repoDir, rootPkg)` → 'pnpm'|'yarn'|'npm'|null
- [x] 4. monorepo 分支：install 在**根**跑（workspace 时），PM 用根的
- [x] 5. 新增 `appNeedsWorkspaceBuild(appPkg, rootPkg)`
- [x] 6. 新增 `runWorkspaceBuild(repoDir, pm)`：根目录跑 build，独立超时（默认 600s，env 可调）
- [x] 7. `runCommand` 增加可选 timeout
- [x] 8. deploy 流程串起来：root install → (条件)root build → 子目录 startApp
- [x] 9. 验证：react-router 真实跑通 → http://localhost:5174/ 返回 HTTP 200 ✅
- [x] 10. （额外修复）ANSI 转义导致 URL 解析失败 — `stripAnsi` 后再 detectLocalUrl

## 结果
react-router 全链路打通：clone → pnpm 根 install → workspace build → playground/data dev → URL。
JSON 输出 `success:true, url:http://localhost:5174/, pm:pnpm, subPath:playground/data`，curl 实测 HTTP 200。
非 monorepo 路径保持向后兼容（workspaceManager 为 null 时行为不变）。

## 风险
- pnpm/node 版本要求高（pnpm@11.7, node>=22.22），不满足会失败 → 清晰报错
- build 耗时数分钟、占磁盘 → 已用独立长超时兜底
