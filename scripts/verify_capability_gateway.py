"""Run a real Workspace/Checkpoint acceptance against OpenMontage Gateway."""

from __future__ import annotations

import argparse
import json

import httpx


TENANT_ID = "wbs11-acceptance-tenant"
IDEMPOTENCY_KEY = "wbs11-capability-gateway-v1"


def _research_brief() -> dict:
    return {
        "version": "1.0",
        "topic": "CouncilForge 视频智能体平台",
        "research_date": "2026-07-18",
        "landscape": {
            "existing_content": [
                {"title": "智能体平台概览", "source": "documentation", "angle": "平台架构", "what_it_covers": "任务编排"},
                {"title": "视频自动化实践", "source": "documentation", "angle": "视频流水线", "what_it_covers": "素材与合成"},
                {"title": "多智能体协同", "source": "documentation", "angle": "角色协作", "what_it_covers": "决策与执行"},
            ],
            "saturated_angles": ["只展示聊天界面"],
            "underserved_gaps": ["展示可恢复的真实视频生产链路"],
        },
        "data_points": [
            {"claim": "平台任务状态由 PostgreSQL 持久化", "source_url": "https://example.com/postgresql", "credibility": "primary_source"},
            {"claim": "OpenMontage 通过标准能力协议执行视频工具", "source_url": "https://example.com/openmontage", "credibility": "primary_source"},
            {"claim": "最终媒体进入私有对象存储", "source_url": "https://example.com/minio", "credibility": "primary_source"},
        ],
        "audience_insights": {
            "common_questions": ["是否真实调用引擎？", "任务能否恢复？", "视频能否播放下载？"],
            "misconceptions": [{"myth": "智能体只需要一个 Prompt", "reality": "生产任务还需要确定性流程与状态底座"}],
            "knowledge_level": "了解大模型，但需要看见工程闭环",
        },
        "angles_discovered": [
            {"name": "一个大脑一套引擎", "hook": "决策与执行为什么必须分层？", "type": "evergreen", "why_now": "平台正在建立真实视频主线", "grounded_in": ["OpenMontage 能力协议"]},
            {"name": "任务不会因重启丢失", "hook": "关掉服务后视频还能继续吗？", "type": "data_driven", "why_now": "工程底座进入恢复验收", "grounded_in": ["PostgreSQL 持久化"]},
            {"name": "从需求直到可播放视频", "hook": "不是 Demo，而是一条完整生产链", "type": "narrative", "why_now": "全链路验收即将开始", "grounded_in": ["MinIO 产物"]},
        ],
        "sources": [
            {"url": "https://example.com/councilforge", "title": "CouncilForge", "used_for": "平台架构"},
            {"url": "https://example.com/openmontage", "title": "OpenMontage", "used_for": "引擎能力"},
            {"url": "https://example.com/postgresql", "title": "PostgreSQL", "used_for": "状态持久化"},
            {"url": "https://example.com/redis", "title": "Redis", "used_for": "异步任务"},
            {"url": "https://example.com/minio", "title": "MinIO", "used_for": "媒体存储"},
        ],
        "research_summary": "以一个 Agent 大脑驱动无模型视频能力层，并由持久化状态和对象存储保证真实生产闭环。",
    }


def verify(base_url: str, token: str) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-ID": TENANT_ID}
    with httpx.Client(base_url=base_url, headers=headers, timeout=120, trust_env=False) as client:
        created = client.post(
            "/v1/workspaces",
            headers={**headers, "Idempotency-Key": IDEMPOTENCY_KEY},
            json={
                "request_id": IDEMPOTENCY_KEY,
                "title": "WBS 1.1 Capability Gateway 验收",
                "pipeline": "animated-explainer",
                "metadata": {"acceptance": "wbs-1.1"},
            },
        )
        if created.status_code not in {200, 201}:
            raise RuntimeError(f"Workspace create failed: {created.status_code} {created.text}")
        workspace = created.json()
        workspace_id = workspace["workspace_id"]

        context_response = client.get(f"/v1/workspaces/{workspace_id}/stages/research/context")
        if context_response.status_code != 200:
            raise RuntimeError(
                f"Research context failed: {context_response.status_code} {context_response.text}"
            )
        context = context_response.json()
        if context["stage"]["produces"] != ["research_brief"]:
            raise RuntimeError("Research stage contract drifted")
        if "research_brief" not in context["artifact_schemas"]:
            raise RuntimeError("Research artifact schema is missing")

        checkpoint = client.put(
            f"/v1/workspaces/{workspace_id}/checkpoint",
            json={
                "stage": "research",
                "status": "completed",
                "artifacts": {"research_brief": _research_brief()},
                "human_approval_required": False,
                "human_approved": False,
                "metadata": {"acceptance": "wbs-1.1"},
            },
        )
        if checkpoint.status_code != 200:
            raise RuntimeError(f"Checkpoint failed: {checkpoint.status_code} {checkpoint.text}")

        latest = client.get(f"/v1/workspaces/{workspace_id}/checkpoint").json()["checkpoint"]
        events = client.get(f"/v1/workspaces/{workspace_id}/events").json()["events"]
        cross_tenant = client.get(
            f"/v1/workspaces/{workspace_id}",
            headers={"Authorization": f"Bearer {token}", "X-Tenant-ID": "other-tenant"},
        )
        if latest["stage"] != "research" or latest["status"] != "completed":
            raise RuntimeError("Latest checkpoint did not survive")
        if [event["sequence"] for event in events] != list(range(1, len(events) + 1)):
            raise RuntimeError("Workspace event sequence is not contiguous")
        if cross_tenant.status_code != 404:
            raise RuntimeError("Cross-tenant Workspace lookup was not hidden")
        return {
            "workspace_id": workspace_id,
            "created_status": created.status_code,
            "pipeline": workspace["pipeline"]["name"],
            "checkpoint_stage": latest["stage"],
            "checkpoint_status": latest["status"],
            "event_count": len(events),
            "event_sequence": [event["sequence"] for event in events],
            "cross_tenant_status": cross_tenant.status_code,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.base_url.rstrip("/"), args.token), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
