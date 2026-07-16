# OpenMontage Engine API v1 协议

状态：Draft 1（进入实现前冻结）  
适用范围：CouncilForge ↔ OpenMontage Engine  
协议前缀：`/v1`

## 1. 设计目标

OpenMontage Engine 是异步视频执行引擎，不是第二个 Agent 大脑。CouncilForge 完成用户需求理解、内容决策、模型推理和业务编排，再向引擎提交结构化、可执行的视频任务。

v1 协议必须满足：

- 提交操作可幂等重试；
- 长任务可查询、可观察、可审批、可取消；
- 状态迁移可持久化、可恢复、可审计；
- 事件允许断线续读和重复消费；
- 产物具有稳定标识、版本和完整性校验；
- 平台级模型密钥不在引擎中长期保存；
- 对外协议不暴露本地绝对路径和内部实现细节。

## 2. API 概览

| Method | Path | 用途 | 成功响应 |
|---|---|---|---|
| GET | `/health` | 存活和就绪检查 | `200`；未就绪时 `503` |
| GET | `/capabilities` | 查询当前运行环境可用能力 | `200` |
| GET | `/pipelines` | 查询可执行流水线及输入要求 | `200` |
| POST | `/jobs` | 幂等创建异步任务 | `202`；重复请求返回原任务 |
| GET | `/jobs/{job_id}` | 查询任务快照 | `200` |
| POST | `/jobs/{job_id}/approve` | 处理当前人工审批点 | `200` |
| POST | `/jobs/{job_id}/cancel` | 请求取消非终态任务 | `202` 或已终止时 `200` |
| GET | `/jobs/{job_id}/artifacts` | 查询产物清单 | `200` |
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

可选 `credential_grants` 是只写字段，只能携带任务范围、短时有效的凭证或不透明引用。引擎不得在任务快照、日志、事件、异常或产物中返回或明文持久化该字段。

相同租户和 `Idempotency-Key`：

- 请求体摘要相同：返回已创建任务，不重复执行；
- 请求体摘要不同：返回 `409 IDEMPOTENCY_KEY_REUSED`。

### 4.2 状态机

```text
created → planning → running → waiting_approval → running
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
