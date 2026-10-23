from typing import Any
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=200)
    name: str = Field(default="", max_length=100)
    workspace_name: str = Field(default="My Workspace", max_length=100)


class LoginRequest(BaseModel):
    email: str
    password: str


class WorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class SpaceJoinRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class SpaceJoinCodeRequest(BaseModel):
    github_collaborator_confirmed: bool = False


class SpaceMemberRoleRequest(BaseModel):
    role: str = Field(pattern=r"^(admin|member|viewer)$")


class SpaceAgentRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    role: str = Field(pattern=r"^(planner|coder|tester|reviewer|security)$")


class SpaceAgentRoleRequest(BaseModel):
    role: str = Field(pattern=r"^(planner|coder|tester|reviewer|security)$")


class SpaceContextRequest(BaseModel):
    project_goal: str = Field(default="", max_length=3000)
    guidelines: list[str] = Field(default_factory=list, max_length=30)
    patterns: list[str] = Field(default_factory=list, max_length=30)
    instructions: list[str] = Field(default_factory=list, max_length=30)


class SpaceMemoryRequest(BaseModel):
    kind: str = Field(pattern=r"^(fact|decision|summary)$")
    content: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="manual", max_length=120)


class SpaceSkillRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    instructions: list[str] = Field(default_factory=list, max_length=30)
    patterns: list[str] = Field(default_factory=list, max_length=30)


class SpaceTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(default="", max_length=3000)
    status: str = Field(default="todo", pattern=r"^(todo|in_progress|review|done)$")
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")
    task_type: str = Field(default="code", pattern=r"^(plan|code|fix|review|test|security)$")
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    assignee_agent_id: str | None = Field(default=None, max_length=64)


class SpaceTaskStatusRequest(BaseModel):
    status: str = Field(pattern=r"^(todo|in_progress|review|done)$")


class SpaceTaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    description: str | None = Field(default=None, max_length=3000)
    task_type: str | None = Field(default=None, pattern=r"^(plan|code|fix|review|test|security)$")
    priority: str | None = Field(default=None, pattern=r"^(low|normal|high|urgent)$")
    assignee_agent_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, pattern=r"^(todo|in_progress|review|done)$")


class SpaceTaskBatchRequest(BaseModel):
    mode: str = Field(default="parallel", pattern=r"^(sequential|parallel)$")


class SpaceTaskBranchRequest(BaseModel):
    branch: str | None = Field(default=None, max_length=120)


class SpaceTaskPullRequestRequest(BaseModel):
    title: str = Field(default="", max_length=256)
    body: str = Field(default="", max_length=10000)


class SpaceTaskApplyProposalRequest(BaseModel):
    message: str = Field(default="", max_length=200)
    paths: list[str] = Field(default_factory=list, max_length=20)


class SpaceTaskRejectProposalRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


class SpaceApprovalRequest(BaseModel):
    action_type: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=180)
    details: str = Field(default="", max_length=3000)
    task_id: str | None = Field(default=None, max_length=64)


class SpaceProjectRequest(BaseModel):
    repository_url: str = Field(min_length=10, max_length=500)
    default_branch: str = Field(default="main", min_length=1, max_length=120)


class SpaceProjectSettingsRequest(BaseModel):
    default_branch: str = Field(default="main", min_length=1, max_length=120)
    pull_before_commit: bool = True
    require_branch_for_non_owners: bool = True
    require_pull_request_for_non_owners: bool = True


class GitHubBranchRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=120)
    from_branch: str = Field(default="main", min_length=1, max_length=120)


class GitHubCommitRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=200)
    files: list[dict[str, str]] = Field(min_length=1, max_length=20)


class GitHubPullRequestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=10000)
    head: str = Field(min_length=1, max_length=120)
    base: str = Field(min_length=1, max_length=120)


class FlowSaveRequest(BaseModel):
    flow: dict[str, Any]


class SessionCreateRequest(BaseModel):
    flow_id: str | None = None


class PublicChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class ResourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    config: dict[str, Any] = Field(default_factory=dict)
