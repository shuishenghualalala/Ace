"""Work 领域值、事项模型与产品模式归属。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, TypeVar


class ProductMode(str, Enum):
    """创建会话时固定的产品模式。"""

    ASSISTANT = "assistant"
    WORK = "work"


class BusinessStatus(str, Enum):
    """用户所理解的事项业务进度。"""

    PENDING_CONFIRMATION = "pending_confirmation"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ExecutionStatus(str, Enum):
    """事项处理空间中 Agent 执行的状态。"""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    FAILED = "failed"
    COMPLETED = "completed"


class SyncStatus(str, Enum):
    """事项与外部事实来源之间的同步状态。"""

    NOT_APPLICABLE = "not_applicable"
    SYNCED = "synced"
    SYNCING = "syncing"
    PENDING_WRITEBACK = "pending_writeback"
    FAILED = "failed"
    CONFLICT = "conflict"
    SOURCE_UNAVAILABLE = "source_unavailable"


class FormalPriority(str, Enum):
    """由用户或来源系统定义的正式优先级。"""

    UNSET = "unset"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Disposition(str, Enum):
    """不与三个状态轴混用的记录处置结果。"""

    ACTIVE = "active"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"
    TRACKING_STOPPED = "tracking_stopped"


_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum_value(enum_type: type[_EnumT], value: Any, field_name: str) -> _EnumT:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}: {value!r}") from exc


def _required_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


@dataclass(frozen=True, slots=True)
class OwnerKey:
    """Owner-scoped storage identity; entity IDs are never queried alone."""

    owner_account_id: str
    resource_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "owner_account_id",
            _required_text(self.owner_account_id, "owner_account_id"),
        )
        object.__setattr__(
            self,
            "resource_id",
            _required_text(self.resource_id, "resource_id"),
        )

    def as_tuple(self) -> tuple[str, str]:
        return self.owner_account_id, self.resource_id


@dataclass(frozen=True, slots=True)
class SourceReference:
    """Stable identity of an approved external source record."""

    connector_key: str
    external_id: str
    external_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "connector_key",
            _required_text(self.connector_key, "connector_key"),
        )
        object.__setattr__(
            self,
            "external_id",
            _required_text(self.external_id, "external_id"),
        )
        object.__setattr__(self, "external_version", str(self.external_version or "").strip())


_BUSINESS_TRANSITIONS = {
    BusinessStatus.PENDING_CONFIRMATION: {BusinessStatus.PENDING},
    BusinessStatus.PENDING: {BusinessStatus.IN_PROGRESS, BusinessStatus.COMPLETED},
    BusinessStatus.IN_PROGRESS: {BusinessStatus.PENDING, BusinessStatus.COMPLETED},
    BusinessStatus.COMPLETED: {BusinessStatus.PENDING, BusinessStatus.IN_PROGRESS},
}

_EXECUTION_TRANSITIONS = {
    ExecutionStatus.NOT_STARTED: {ExecutionStatus.RUNNING},
    ExecutionStatus.RUNNING: {
        ExecutionStatus.WAITING_CONFIRMATION,
        ExecutionStatus.FAILED,
        ExecutionStatus.COMPLETED,
    },
    ExecutionStatus.WAITING_CONFIRMATION: {
        ExecutionStatus.NOT_STARTED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.FAILED,
    },
    ExecutionStatus.FAILED: {ExecutionStatus.RUNNING},
    ExecutionStatus.COMPLETED: {ExecutionStatus.RUNNING},
}

_SYNC_TRANSITIONS = {
    SyncStatus.NOT_APPLICABLE: set(),
    SyncStatus.SYNCED: {
        SyncStatus.SYNCING,
        SyncStatus.PENDING_WRITEBACK,
        SyncStatus.SOURCE_UNAVAILABLE,
    },
    SyncStatus.SYNCING: {
        SyncStatus.SYNCED,
        SyncStatus.FAILED,
        SyncStatus.CONFLICT,
        SyncStatus.SOURCE_UNAVAILABLE,
    },
    SyncStatus.PENDING_WRITEBACK: {
        SyncStatus.SYNCING,
        SyncStatus.SYNCED,
        SyncStatus.FAILED,
        SyncStatus.CONFLICT,
    },
    SyncStatus.FAILED: {SyncStatus.SYNCING, SyncStatus.SOURCE_UNAVAILABLE},
    SyncStatus.CONFLICT: {
        SyncStatus.SYNCING,
        SyncStatus.SYNCED,
        SyncStatus.PENDING_WRITEBACK,
    },
    SyncStatus.SOURCE_UNAVAILABLE: {SyncStatus.SYNCING},
}

_DISPOSITION_TRANSITIONS = {
    Disposition.ACTIVE: {
        Disposition.CANCELLED,
        Disposition.ARCHIVED,
        Disposition.TRACKING_STOPPED,
    },
    Disposition.CANCELLED: set(),
    Disposition.ARCHIVED: set(),
    Disposition.TRACKING_STOPPED: set(),
}


def _transition(
    field_name: str,
    current: _EnumT,
    target: _EnumT,
    allowed: dict[_EnumT, set[_EnumT]],
) -> _EnumT:
    if target is current:
        return current
    if target not in allowed[current]:
        raise ValueError(f"invalid {field_name} transition: {current.value} -> {target.value}")
    return target


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One user-visible item of work, independent from runtime execution tasks."""

    owner_account_id: str
    item_id: str
    title: str
    description: str = ""
    category: str | None = None
    related_system: str | None = None
    workspace_id: str | None = None
    processing_session_id: str | None = None
    business_status: BusinessStatus = BusinessStatus.PENDING
    execution_status: ExecutionStatus = ExecutionStatus.NOT_STARTED
    sync_status: SyncStatus = SyncStatus.NOT_APPLICABLE
    priority: FormalPriority = FormalPriority.UNSET
    disposition: Disposition = Disposition.ACTIVE
    source: SourceReference | None = None
    due_at: float | None = None
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "owner_account_id",
            _required_text(self.owner_account_id, "owner_account_id"),
        )
        object.__setattr__(self, "item_id", _required_text(self.item_id, "item_id"))
        object.__setattr__(self, "title", _required_text(self.title, "title"))
        object.__setattr__(self, "description", str(self.description or "").strip())
        object.__setattr__(self, "category", _optional_text(self.category))
        object.__setattr__(self, "related_system", _optional_text(self.related_system))
        object.__setattr__(self, "workspace_id", _optional_text(self.workspace_id))
        object.__setattr__(
            self,
            "processing_session_id",
            _optional_text(self.processing_session_id),
        )
        object.__setattr__(
            self,
            "business_status",
            _enum_value(BusinessStatus, self.business_status, "business_status"),
        )
        object.__setattr__(
            self,
            "execution_status",
            _enum_value(ExecutionStatus, self.execution_status, "execution_status"),
        )
        object.__setattr__(
            self,
            "sync_status",
            _enum_value(SyncStatus, self.sync_status, "sync_status"),
        )
        object.__setattr__(
            self,
            "priority",
            _enum_value(FormalPriority, self.priority, "priority"),
        )
        object.__setattr__(
            self,
            "disposition",
            _enum_value(Disposition, self.disposition, "disposition"),
        )
        if self.version < 1:
            raise ValueError("version must be positive")
        _validate_combination(self)

    @classmethod
    def create(
        cls,
        *,
        owner_account_id: str,
        title: str,
        item_id: str | None = None,
        now: float | None = None,
        **values: Any,
    ) -> WorkItem:
        """Create a validated owner-scoped item with stable timestamps."""
        timestamp = time.time() if now is None else float(now)
        return cls(
            owner_account_id=owner_account_id,
            item_id=item_id or f"work_{uuid.uuid4().hex}",
            title=title,
            created_at=timestamp,
            updated_at=timestamp,
            **values,
        )

    @property
    def key(self) -> OwnerKey:
        return OwnerKey(self.owner_account_id, self.item_id)

    def with_business_status(self, status: BusinessStatus | str) -> WorkItem:
        target = _enum_value(BusinessStatus, status, "business_status")
        next_value = _transition(
            "business_status",
            self.business_status,
            target,
            _BUSINESS_TRANSITIONS,
        )
        return self._updated(business_status=next_value)

    def with_execution_status(self, status: ExecutionStatus | str) -> WorkItem:
        target = _enum_value(ExecutionStatus, status, "execution_status")
        next_value = _transition(
            "execution_status",
            self.execution_status,
            target,
            _EXECUTION_TRANSITIONS,
        )
        return self._updated(execution_status=next_value)

    def with_sync_status(self, status: SyncStatus | str) -> WorkItem:
        target = _enum_value(SyncStatus, status, "sync_status")
        next_value = _transition("sync_status", self.sync_status, target, _SYNC_TRANSITIONS)
        return self._updated(sync_status=next_value)

    def with_disposition(self, disposition: Disposition | str) -> WorkItem:
        target = _enum_value(Disposition, disposition, "disposition")
        next_value = _transition(
            "disposition",
            self.disposition,
            target,
            _DISPOSITION_TRANSITIONS,
        )
        return self._updated(disposition=next_value)

    def with_updates(self, *, now: float | None = None, **changes: Any) -> WorkItem:
        """Apply one optimistic patch while validating every state transition."""
        allowed_fields = {
            "title",
            "description",
            "category",
            "related_system",
            "workspace_id",
            "processing_session_id",
            "business_status",
            "execution_status",
            "sync_status",
            "priority",
            "disposition",
            "due_at",
        }
        unknown = set(changes) - allowed_fields
        if unknown:
            raise ValueError(f"unsupported WorkItem fields: {sorted(unknown)}")

        prepared = dict(changes)
        state_fields = (
            ("business_status", BusinessStatus, _BUSINESS_TRANSITIONS),
            ("execution_status", ExecutionStatus, _EXECUTION_TRANSITIONS),
            ("sync_status", SyncStatus, _SYNC_TRANSITIONS),
            ("disposition", Disposition, _DISPOSITION_TRANSITIONS),
        )
        for field_name, enum_type, transitions in state_fields:
            if field_name not in prepared:
                continue
            current = getattr(self, field_name)
            target = _enum_value(enum_type, prepared[field_name], field_name)
            prepared[field_name] = _transition(field_name, current, target, transitions)
        if "priority" in prepared:
            prepared["priority"] = _enum_value(
                FormalPriority,
                prepared["priority"],
                "priority",
            )
        if all(getattr(self, name) == value for name, value in prepared.items()):
            return self
        timestamp = time.time() if now is None else float(now)
        return replace(
            self,
            **prepared,
            version=self.version + 1,
            updated_at=max(timestamp, self.updated_at),
        )

    def _updated(self, **changes: Any) -> WorkItem:
        return self.with_updates(**changes)


@dataclass(frozen=True, slots=True)
class WorkSessionLink:
    """Owner-scoped session ownership fixed at session creation."""

    owner_account_id: str
    session_id: str
    product_mode: ProductMode
    work_item_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "owner_account_id",
            _required_text(self.owner_account_id, "owner_account_id"),
        )
        object.__setattr__(self, "session_id", _required_text(self.session_id, "session_id"))
        object.__setattr__(
            self,
            "product_mode",
            _enum_value(ProductMode, self.product_mode, "product_mode"),
        )
        object.__setattr__(self, "work_item_id", _optional_text(self.work_item_id))
        if self.product_mode is ProductMode.ASSISTANT and self.work_item_id is not None:
            raise ValueError("work_item_id is only valid for work sessions")

    @property
    def key(self) -> OwnerKey:
        return OwnerKey(self.owner_account_id, self.session_id)


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _validate_combination(item: WorkItem) -> None:
    active_execution = {
        ExecutionStatus.RUNNING,
        ExecutionStatus.WAITING_CONFIRMATION,
    }
    if item.source is None and item.sync_status is not SyncStatus.NOT_APPLICABLE:
        raise ValueError("source is required when sync_status is applicable")
    if item.source is not None and item.sync_status is SyncStatus.NOT_APPLICABLE:
        raise ValueError("sync_status cannot be not_applicable for a sourced item")
    if (
        item.business_status is BusinessStatus.PENDING_CONFIRMATION
        and item.execution_status is not ExecutionStatus.NOT_STARTED
    ):
        raise ValueError("business_status pending_confirmation cannot execute")
    if (
        item.business_status is BusinessStatus.COMPLETED
        and item.execution_status in active_execution
    ):
        raise ValueError("business_status completed cannot keep active execution")
    if item.disposition is not Disposition.ACTIVE and item.execution_status in active_execution:
        raise ValueError("disposition cannot terminate an active execution")
