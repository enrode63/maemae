from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ROLES = ("PM", "Bull", "Bear", "Risk", "Research")
PROPOSAL_KINDS = ("strategy", "code")
METADATA_FIELDS = ("channel", "team_label")


class ChatError(ValueError):
    """Base class for expected chat-domain errors."""


class ValidationError(ChatError):
    pass


class DuplicateRequest(ChatError):
    pass


class InvalidTransition(ChatError):
    pass


class AuthorizationError(ChatError):
    """Raised when an actor is not allowed to perform a chat operation."""


def require_text(value: Any, name: str, maximum: int = 4000) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    result = value.strip()
    if not result:
        raise ValidationError(f"{name} must not be empty")
    if len(result) > maximum:
        raise ValidationError(f"{name} must be at most {maximum} characters")
    return result


def optional_context(role: Any = None, team: Any = None,
                     metadata: Any = None) -> tuple[str | None, str | None, dict[str, str]]:
    """Validate the optional frontend conversation context."""
    if role is not None and role not in ROLES:
        raise ValidationError(f"role must be one of: {', '.join(ROLES)}")
    valid_team = require_text(team, "team", 128) if team is not None else None
    if metadata is None:
        return role, valid_team, {}
    if not isinstance(metadata, dict):
        raise ValidationError("metadata must be an object")
    unknown = set(metadata) - set(METADATA_FIELDS)
    if unknown:
        raise ValidationError(f"metadata contains unsupported fields: {', '.join(sorted(unknown))}")
    valid_metadata = {
        key: require_text(value, f"metadata.{key}", 128)
        for key, value in metadata.items()
    }
    return role, valid_team, valid_metadata


@dataclass
class Proposal:
    proposal_id: str
    conversation_id: str
    source_message_id: str
    kind: str
    title: str
    changes: dict[str, Any]
    status: str = "pending"
    decision_reason: str = ""
    applied: bool = False

    def json(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class Conversation:
    conversation_id: str
    role: str | None = None
    team: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    proposals: dict[str, Proposal] = field(default_factory=dict)
