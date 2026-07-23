# Upstream tracking

OpenMontage Engine 是基于 OpenMontage 完整源码和 Git 历史维护的下游视频执行引擎。

## 当前基线

- 上游项目：`calesthio/OpenMontage`
- 上游仓库：`https://github.com/calesthio/OpenMontage.git`
- 上游 Release/Tag：无
- 固定 Commit：`f8d94632ea9bd0057da31904acca1cefecf005dd`
- 集成日期：`2026-07-16`
- 集成类型：经本地测试验证的 Commit 快照

上游当前没有正式 Release 或 Tag，因此本项目不把 `main` 当作稳定版本号，也不会持续自动追踪上游 `main`。每次升级必须固定到一个明确 Commit，并在独立同步分支完成审查和验证。

## 基线本地变更

- 在 README 顶部增加 OpenMontage Engine 的定位、系统边界和演进路线；
- 增加明确的上游版本锁定与升级规则；
- 未修改 OpenMontage 核心功能、流水线或许可证。

后续 `animated-explainer v2.0` 下游功能限定在无模型执行边界：
AI 视频幂等收据、授权音乐混音、确定性质检、镜头复用重做和焦点感知
多画幅渲染。CouncilForge 的 Agent、Brand Kit、预算、配额和 Batch
不复制进本仓库；每次上游同步必须同时运行 Engine 契约和平台集成回归。

## 已完成验证

- Python 契约测试：561 passed，7 skipped；
- Remotion 演示渲染：1920×1080、30fps、H.264/AAC、约 25 秒；
- 本地工作区在验证后无未追踪的运行产物进入 Git。

## 远程与分支规则

- `origin`：OpenMontage Engine 自有仓库；
- `upstream`：OpenMontage 官方仓库；
- `main`：经过验证、可发布的集成版本；
- `develop`：日常集成分支；
- `upstream-sync/<date>-<short-sha>`：上游同步与冲突处理分支；
- 功能开发分支从 `develop` 创建并通过评审合并。

## 上游升级流程

1. 从 `upstream` 获取最新远程引用，但不直接合并到 `main`；
2. 评估上游 Commit 范围、许可证、依赖、迁移和安全变化；
3. 从 `develop` 创建 `upstream-sync/<date>-<short-sha>`；
4. 合并选定的上游 Commit，不重写已发布历史；
5. 运行上游测试、引擎契约测试和 CouncilForge 集成回归；
6. 更新本文件和 `upstream.lock.yaml`；
7. 通过 `develop` 验证后再提升到 `main` 并创建新版本标签。

## 许可证边界

OpenMontage Engine 保留 OpenMontage 的 GNU AGPLv3 许可证。它与 MIT 许可证的 CouncilForge 保持独立仓库、独立构建产物和独立容器，并通过 REST、事件或 CLI 协议通信。OpenMontage Engine 的修改源码和对应许可证义务单独管理。
