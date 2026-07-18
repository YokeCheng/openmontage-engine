# OpenMontage Engine API v1 协议

状态：Implemented 1.0
适用范围：CouncilForge ↔ OpenMontage Engine
协议前缀：`/v1`

## 规范来源与兼容规则

- `schemas/api/engine_api.openapi.json` 是 HTTP 路径、方法、operationId 和请求模型的规范快照；
- `schemas/api/engine_api.schema.json` 是 Engine v1 公共对象的规范 JSON Schema；
- `schemas/artifacts/*.schema.json` 是 Pipeline 标准阶段产物的规范来源；
- `schemas/api/engine_api.contract.json` 将版本、必需 Capability operation 和上述来源摘要合成一个确定性清单；
- `GET /v1/contract` 在运行时发布与提交清单相同的兼容表面。

只允许增加可选字段或新操作等经评审的向后兼容变化。删除/重命名 operation、删除字段、收紧已有输入或改变非扩展枚举语义属于破坏性变化，必须进入 `/v2` 并提升 API 主版本。任何变更先运行：

```bash
uv run python scripts/export_engine_api_contract.py
uv run python scripts/export_engine_api_contract.py --check
```

CouncilForge 保存审核锁并在视频服务启动前比较 API/Schema 主版本、必需 operation 和三个规范来源摘要。即使是兼容增加，也必须由 CouncilForge 显式更新锁后才能部署，避免两个独立仓库静默漂移。

## 1. 设计目标

OpenMontage Engine 是异步视频执行引擎，不是第二个 Agent 大脑。CouncilForge 完成用户需求理解、内容决策、模型推理和业务编排，再向引擎提交结构化、可执行的视频任务。

新建 CouncilForge 视频任务使用能力 Workspace 协议：CouncilForge 的 Agent 读取 OpenMontage Pipeline 当前阶段的 Director Skill、产物 Schema 和工具契约，调用允许的媒体工具，并将标准阶段产物写入 Checkpoint。OpenMontage 不调用模型，也不决定跨阶段流程。

## Capability Workspace API

- `GET /v1/contract`：读取版本、必需 operation 和规范 Schema 摘要；
- `GET /v1/tools`、`GET /v1/tools/{tool_name}`：读取工具契约和实时可用状态；
- `GET /v1/agent-skills/{skill_name}`：读取工具声明的运行指导；
- `POST /v1/workspaces`、`GET /v1/workspaces/{workspace_id}`：创建和读取租户隔离的生产工作区；
- `GET /v1/workspaces/{workspace_id}/stages/{stage}/context`：读取当前阶段说明、允许工具和产物 Schema；
- `POST /v1/workspaces/{workspace_id}/executions`：幂等执行当前阶段允许的一个工具；
- `GET/PUT /v1/workspaces/{workspace_id}/checkpoint`：读取或写入标准阶段 Checkpoint；
- `GET /v1/workspaces/{workspace_id}/events`：按序列读取 JSON 事件或订阅 SSE；
- `POST /v1/workspaces/{workspace_id}/cancel`：取消工作区及其活动工具执行；
- `GET /v1/workspaces/{workspace_id}/artifacts`：读取产物索引；内容接口支持 HTTP Range。

工具执行请求除 `stage`、`tool_name` 和 `inputs` 外，还接受三个可选的宿主追踪字段：

- `trace_id`：CouncilForge 端到端追踪号；
- `platform_job_id`：CouncilForge PostgreSQL Job ID；
- `stage_attempt`：当前 Pipeline 阶段 attempt，从 1 开始。

它们不参与工具输入，也不会传给供应商；OpenMontage 将其与 `workspace_id`、`execution_id`、`stage`、`tool_name` 和 `provider` 一起写入执行快照及 queued、started、终态事件。终态事件补充 `duration_seconds`、`cost_usd`、`model`、`error_code` 和已脱敏的 `error_message`，供 CouncilForge 幂等回流 PostgreSQL。

完成或待审批 Checkpoint 必须包含该阶段声明的全部标准产物，并通过 `schemas/artifacts/` 中的 JSON Schema。工具文件路径只能位于对应 Workspace；HTTP、HTTPS 和 data URL 可作为远程输入，但不会被解释为本地路径。

v1 协议必须满足：

- 提交操作可幂等重试；
- 长任务可查询、可观察、可审批、可取消；
- 状态迁移可持久化、可恢复、可审计；
- 事件允许断线续读和重复消费；
- 产物具有稳定标识、版本和完整性校验；
- 平台级模型密钥不在引擎中长期保存；
- 对外协议不暴露本地绝对路径和内部实现细节。

媒体工具失败使用结构化的 `error_code` 和 `retryable` 语义。Gateway
只对供应商限流、超时、连接失败和明确的临时错误执行有界指数退避；参数拒绝、
无效响应和不可重试错误直接终止。每次尝试写入 `attempt_count`、
`retry_history` 和 `execution.retrying` 事件，重试耗尽后由 CouncilForge
创建 `provider_fallback` 人工动作，不能静默伪造媒体或自动切换到未批准的供应商。

`animated-explainer` 的 `assets` 阶段要求标准 `asset_manifest` 至少登记实际媒体的
MIME、字节数、SHA-256、媒体元数据、供应商、模型和费用；启用字幕时必须同时由
批准脚本生成 SRT。运行时凭证只通过 `/v1/runtime/config` 注入内存，不写入
Workspace、Checkpoint、执行快照、事件或产物。

可使用真实 DashScope 配置执行重复验收：

```bash
source .env
.venv/bin/python scripts/verify_wbs24_real_media.py
```

脚本在同一租户 Workspace 中生成一张真实图片、一条中文 WAV 和一份中文 SRT，
写入通过 Schema 的 `asset_manifest`，并扫描整个 Workspace，确认服务令牌、
供应商密钥和签名 URL 均未落盘。该命令会产生真实供应商费用。

引擎进程启动时会检查持久化的活动执行。上一进程未能写入终态的执行会转为可重试的 `ENGINE_RESTARTED` 失败，同时追加 `execution.failed` 事件（包含 `execution_id`、`stage`和错误码）。CouncilForge 应用新 attempt 和新幂等键重试，不应覆盖原执行记录。

## 2. API 概览

| Method | Path | 用途 | 成功响应 |
|---|---|---|---|
| GET | `/health` | 存活和就绪检查 | `200`；未就绪时 `503` |
| GET | `/capabilities` | 查询当前运行环境可用能力 | `200` |
| GET | `/pipelines` | 查询可执行流水线及输入要求 | `200` |
| POST | `/jobs` | 幂等创建异步任务 | `202`；重复请求返回原任务 |
| GET | `/jobs` | 按租户列出任务 | `200` |
| GET | `/jobs/{job_id}` | 查询任务快照 | `200` |
| POST | `/jobs/{job_id}/approve` | 处理当前人工审批点 | `200` |
| POST | `/jobs/{job_id}/cancel` | 请求取消非终态任务 | `202` 或已终止时 `200` |
| GET | `/jobs/{job_id}/actions` | 查询待处理动作 | `200` |
| POST | `/jobs/{job_id}/actions/{action_id}/resolve` | 处理待决动作 | `200` |
| GET | `/jobs/{job_id}/artifacts` | 查询产物清单 | `200` |
| GET | `/jobs/{job_id}/artifacts/{artifact_id}/content` | 支持 Range 的媒体读取 | `200/206` |
| GET | `/jobs/{job_id}/events` | 游标分页或 SSE 读取事件 | `200` |

所有业务响应使用 `application/json`。事件接口在请求头为 `Accept: text/event-stream` 时返回 SSE，否则返回 JSON 分页结果。

## 3. 通用约定

### 3.1 版本与时间

- URL 主版本为 `/v1`；对象包含 `schema_version: "1.0"`；
- 时间统一使用带时区的 ISO 8601 UTC 字符串；
- 协议只允许向后兼容地增加可选字段，破坏性变化进入 `/v2`。

### 3.2 身份与追踪

- `Authorization: Bearer <service-token>`：CouncilForge 到引擎的服务身份；
- `Idempotency-Key`：创建任务必填，在同一租户内唯一；
- `X-Correlation-ID`：端到端追踪标识；未提供时由引擎生成；
- 每个请求都校验 `tenant_id`，不得仅凭 `job_id` 跨租户访问。

### 3.3 错误格式

错误采用 `application/problem+json`：

```json
{
  "type": "https://openmontage.dev/problems/invalid-transition",
  "title": "Invalid job state transition",
  "status": 409,
  "detail": "Only a waiting_approval job can be approved.",
  "instance": "/v1/jobs/job_01.../approve",
  "error_code": "JOB_INVALID_TRANSITION",
  "retryable": false,
  "correlation_id": "corr_01..."
}
```

常用状态码：`400` 参数错误、`401/403` 身份或权限错误、`404` 不存在、`409` 幂等冲突或非法状态迁移、`422` 协议有效但任务不可执行、`429` 配额限制、`503` 引擎未就绪。

## 4. 任务对象

任务是外部 API 的聚合根。规范 JSON Schema 位于 [`schemas/api/engine_api.schema.json`](../schemas/api/engine_api.schema.json)。

```json
{
  "schema_version": "1.0",
  "job_id": "job_019...",
  "request_id": "req_councilforge_019...",
  "tenant_id": "tenant_123",
  "created_by": "user_456",
  "pipeline": {
    "name": "animated-explainer",
    "version": "2.0"
  },
  "status": "running",
  "stage": "assets",
  "progress": {
    "percent": 55,
    "message": "Generating visual assets",
    "updated_at": "2026-07-16T08:00:00Z"
  },
  "input": {
    "title": "示例视频",
    "objective": "解释一个产品概念",
    "script": {},
    "style": {},
    "output": {
      "aspect_ratio": "16:9",
      "duration_seconds": 60,
      "language": "zh-CN"
    }
  },
  "config_version": "councilforge-config-42",
  "approval": null,
  "artifacts": [],
  "error": null,
  "created_at": "2026-07-16T07:50:00Z",
  "updated_at": "2026-07-16T08:00:00Z"
}
```

### 4.1 创建任务

`POST /v1/jobs` 至少包含：

- `request_id`：CouncilForge 侧业务请求标识；
- `tenant_id`、`created_by`；
- `pipeline.name`，可选固定版本；
- `input`：已经由 CouncilForge 补齐的视频结构化参数；
- `config_version`：本次任务对应的平台配置版本。
- `execution_mode`：`engine_managed` 保留引擎审批，`platform_managed`
  表示 CouncilForge 已完成审批并由引擎直接进入确定性执行。

`platform_managed` 的 `animated-explainer` 会按已批准清单和分镜逐段调用 TTS，
把每段真实配音放入对应镜头时间区间，再执行字幕、Remotion 和 FFmpeg。任务
媒体必须位于租户 Job 目录并通过 Remotion `--public-dir` 读取，禁止使用
`file://` 或跨 Job 路径。每段旁白在合成前用 FFprobe 测量；轻度超长使用
FFmpeg `atempo` 自动适配且登记原始时长、适配时长、倍率和镜头时间区间，
超过安全倍率则返回明确失败，不能通过截断或挪到其他镜头伪装成功。

可选 `credential_grants` 是只写字段，只能携带任务范围、短时有效的凭证或不透明引用。引擎不得在任务快照、日志、事件、异常或产物中返回或明文持久化该字段。

相同租户和 `Idempotency-Key`：

- 请求体摘要相同：返回已创建任务，不重复执行；
- 请求体摘要不同：返回 `409 IDEMPOTENCY_KEY_REUSED`。

### 4.2 状态机

```text
created → planning → running → waiting_approval → running
                         └──→ waiting_action ────→ running
                         │             │
                         └─────────────┴────→ rendering → succeeded

任一非终态 ───────────────────────────→ failed
任一非终态 ───────────────────────────→ cancelled
```

状态语义：

| 状态 | 含义 |
|---|---|
| `created` | 请求已持久化，尚未开始准备 |
| `planning` | 校验流水线、能力、输入和资源；不进行新的业务或创意模型决策 |
| `running` | 执行研究结果之后的素材、编辑或其他非最终渲染阶段 |
| `waiting_approval` | 已到达人工检查点，执行暂停且等待 CouncilForge 决策 |
| `waiting_action` | 等待缺失输入、预算、供应商降级或质量修订等显式动作 |
| `rendering` | 正在执行最终合成、编码或质量验证 |
| `succeeded` | 成功完成且最终产物已登记 |
| `failed` | 不可自动恢复或重试耗尽，包含结构化错误 |
| `cancelled` | 取消请求已安全生效，不再启动新步骤 |

约束：

- `progress.percent` 在单个任务生命周期中不得倒退；
- 取消为协作式操作，正在运行的外部调用可能需要短暂收尾；
- 终态不可恢复；如需重做，创建新任务并通过 `parent_job_id` 关联；
- 内部 checkpoint 的 `awaiting_human` 映射为 `waiting_approval`，`completed` 仅代表阶段完成，不直接等同于任务成功；
- 每次状态变化必须先持久化任务快照，再写入对应事件。

## 5. 审批对象

任务进入 `waiting_approval` 时必须包含：

```json
{
  "approval_id": "approval_019...",
  "checkpoint_id": "script-3",
  "stage": "script",
  "status": "pending",
  "summary": "请确认脚本和预计成本后继续",
  "requested_at": "2026-07-16T08:10:00Z",
  "expires_at": null,
  "review_artifacts": ["artifact_019..."]
}
```

`POST /v1/jobs/{job_id}/approve` 请求：

```json
{
  "approval_id": "approval_019...",
  "decision": "approved_with_changes",
  "decided_by": "user_456",
  "comment": "保留结构，将语速调整为每分钟 220 字",
  "changes": {
    "narration.words_per_minute": 220
  }
}
```

规则：

- `decision` 仅为 `approved` 或 `approved_with_changes`；用户拒绝继续时调用取消接口；
- 只能处理当前 `pending` 的 approval，重复相同决议幂等返回当前结果；
- `approval_id` 过期或已被替换时返回 `409`；
- `changes` 必须通过该流水线公开的可修改字段校验，不允许任意路径覆盖；
- 决议先持久化，再把任务从 `waiting_approval` 转回 `running` 或 `rendering`。

## 6. 事件协议

事件是 append-only 的事实记录，默认至少一次投递。消费者必须使用 `event_id` 去重，并通过 `sequence` 检测缺口。

```json
{
  "schema_version": "1.0",
  "event_id": "evt_019...",
  "job_id": "job_019...",
  "tenant_id": "tenant_123",
  "sequence": 12,
  "type": "approval.required",
  "occurred_at": "2026-07-16T08:10:00Z",
  "correlation_id": "corr_019...",
  "data": {
    "approval_id": "approval_019...",
    "stage": "script"
  }
}
```

v1 标准事件：

- `job.created`、`job.status_changed`、`job.cancel_requested`；
- `stage.started`、`stage.progressed`、`stage.completed`、`stage.failed`；
- `approval.required`、`approval.resolved`；
- `artifact.created`、`artifact.updated`；
- `job.succeeded`、`job.failed`、`job.cancelled`。

读取方式：

- JSON：`GET /events?after_sequence=12&limit=100`；
- SSE：同一路径并设置 `Accept: text/event-stream`，支持 `Last-Event-ID`；
- `sequence` 在单个任务内从 1 严格递增；
- 保活消息不是业务事件，不增加 `sequence`。

现有 `events.jsonl` 可作为 v1 的初始持久层，但必须由适配器补齐稳定 ID、序号、租户和事件类型；不得直接把内部原始日志当作公共协议返回。

## 7. 产物协议

```json
{
  "artifact_id": "artifact_019...",
  "job_id": "job_019...",
  "kind": "video",
  "role": "final",
  "media_type": "video/mp4",
  "uri": "s3://bucket/tenant/job/final-v1.mp4",
  "version": 1,
  "size_bytes": 3735741,
  "checksum": {
    "algorithm": "sha256",
    "value": "..."
  },
  "created_at": "2026-07-16T08:30:00Z",
  "metadata": {
    "duration_seconds": 25.045,
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "video_codec": "h264",
    "audio_codec": "aac"
  }
}
```

规则：

- `kind` 首版支持 `video`、`audio`、`subtitle`、`image`、`script`、`manifest`、`report` 和 `archive`；
- `role` 首版支持 `intermediate`、`preview`、`review`、`final`；
- 对象存储 URI 是规范位置，下载时可转换为短时签名 URL；
- 本地文件路径只能存在于引擎内部，不作为跨服务标识；
- 同一逻辑产物更新时递增 `version`，旧版本不被静默覆盖；
- 最终视频必须包含 SHA-256、大小、媒体类型和可用的媒体探测信息。
- 真实配音产物还应包含供应商、工具、实际音频编码、时长、`scene_id`、
  `timeline_start_seconds` 和 `timeline_end_seconds`；发生时间轴适配时，
  必须包含 `timeline_repaired`、`original_duration_seconds`、
  `fitted_duration_seconds` 和 `timeline_speed_factor`。

## 8. 能力与流水线发现

`GET /capabilities` 返回运行时实际可用能力，而不是代码中理论支持的能力。每项至少包含：`name`、`available`、`provider`、`runtime`、`stability`、`constraints` 和不可用原因。

`GET /pipelines` 每项至少包含：

- `name`、`version`、`category` 和描述；
- 输入 JSON Schema 或其引用；
- 阶段列表和可能的审批点；
- 所需能力、输出类型和默认资源估算；
- 当前运行环境是否可执行及缺失依赖。

CouncilForge 必须在创建任务前查询或缓存这些信息，避免提交当前引擎无法执行的流水线。

## 9. 安全与租户边界

- 服务令牌按环境轮换，并限制为调用引擎所需权限；
- 任务目录、对象存储前缀、事件和查询均以 `tenant_id/job_id` 隔离；
- 输入路径不得允许目录穿越，外部 URL 必须经过协议、域名、大小和超时限制；
- 日志、事件和错误统一执行密钥与个人信息脱敏；
- Tool 输入、Prompt 和凭证不得写入结构化执行日志；Bearer/Basic 凭证、敏感键值、当前进程敏感环境变量和签名 URL 必须在事件或错误落盘前替换；
- 临时凭证最小权限、短时有效、只写、不可查询；
- 引擎不得自行调用通用 LLM 进行需求理解或业务决策。

## 10. 一致性与恢复

v1 允许单实例文件持久化起步，但实现必须保持以下不变量：

1. `job_id`、`tenant_id` 和幂等键映射持久存在；
2. 状态快照写入采用原子替换；
3. 状态变化和事件写入具有可恢复的顺序；
4. 重启后从最后一个有效 checkpoint 恢复，不重复已确认的付费操作；
5. 外部生成调用保存供应商请求标识，重试前先查询原请求；
6. 任何无法确认结果的操作标记为需要人工处理，不静默重复扣费。

## 11. v1 实现验收条件

- OpenAPI 文档与 JSON Schema 可自动校验；
- 非法状态迁移、跨租户访问和幂等冲突有契约测试；
- 进程重启后任务、审批、事件游标和产物仍可恢复；
- 可以用零付费的 `framework-smoke` 或演示流水线跑通创建、查询、审批、事件、取消和产物流程；
- CouncilForge 只通过公共协议调用，不读取 OpenMontage 本地目录；
- 端到端日志中不出现服务令牌、供应商密钥或临时凭证。
- `animated-explainer` 的真实配音按已批准分镜分别生成并落入对应时间线，
  尾段不能因整段音频从零秒播放而静音。
