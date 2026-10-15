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
