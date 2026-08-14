"""Bounded, versioned model-catalog records shared by host and proxy code."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any

MAX_ID_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 2_048
MAX_MODELS = 256
MAX_REASONING_EFFORTS = 32
MAX_CATALOG_WIRE_BYTES = 256 * 1024

STALE_REASONS = frozenset(
    {"offline", "timeout", "not_authenticated", "unsupported", "protocol_error"}
)


def _bounded_string(value: object, *, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(
            f"{name} must be a non-empty string up to {maximum} characters"
        )
    return value


def _optional_bounded_string(value: object, *, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, name=name, maximum=maximum)


def _bounded_identifier(value: object, *, name: str) -> str:
    identifier = _bounded_string(value, name=name, maximum=MAX_ID_LENGTH)
    if not identifier.strip():
        raise ValueError(f"{name} must contain a non-whitespace character")
    return identifier


@dataclass(frozen=True)
class ReasoningOptions:
    supported: tuple[str, ...]
    default: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.supported, tuple):
            raise ValueError("supported reasoning efforts must be a tuple")
        if len(self.supported) > MAX_REASONING_EFFORTS:
            raise ValueError("too many supported reasoning efforts")
        for effort in self.supported:
            _bounded_identifier(effort, name="reasoning effort ID")
        if len(set(self.supported)) != len(self.supported):
            raise ValueError("supported reasoning efforts must be unique")
        if self.default is not None:
            _bounded_identifier(self.default, name="default reasoning effort ID")
            if self.default not in self.supported:
                raise ValueError("default reasoning effort must be supported")


@dataclass(frozen=True)
class ModelOption:
    id: str
    display_name: str
    description: str | None = None
    is_default: bool = False
    reasoning: ReasoningOptions | None = None

    def __post_init__(self) -> None:
        _bounded_identifier(self.id, name="model ID")
        _bounded_string(
            self.display_name, name="model display name", maximum=MAX_ID_LENGTH
        )
        _optional_bounded_string(
            self.description,
            name="model description",
            maximum=MAX_DESCRIPTION_LENGTH,
        )
        if not isinstance(self.is_default, bool):
            raise ValueError("is_default must be a boolean")
        if self.reasoning is not None and not isinstance(
            self.reasoning, ReasoningOptions
        ):
            raise ValueError("reasoning must be ReasoningOptions or None")


def _validate_models(models: tuple[ModelOption, ...], *, allow_empty: bool) -> None:
    if not isinstance(models, tuple):
        raise ValueError("models must be a tuple")
    if not models and not allow_empty:
        raise ValueError("a successful catalog requires at least one model")
    if len(models) > MAX_MODELS:
        raise ValueError("too many models")
    if any(not isinstance(model, ModelOption) for model in models):
        raise ValueError("models must contain ModelOption values")
    if len({model.id for model in models}) != len(models):
        raise ValueError("model IDs must be unique")
    if sum(model.is_default for model in models) > 1:
        raise ValueError("at most one model may be the default")


@dataclass(frozen=True)
class DiscoveredCatalog:
    account_scope_material: str = field(repr=False, compare=False)
    harness_version: str
    models: tuple[ModelOption, ...]
    source_stale: bool = False

    def __post_init__(self) -> None:
        _bounded_string(
            self.account_scope_material,
            name="account scope material",
            maximum=MAX_DESCRIPTION_LENGTH,
        )
        _bounded_string(
            self.harness_version, name="harness version", maximum=MAX_ID_LENGTH
        )
        _validate_models(self.models, allow_empty=False)
        if not isinstance(self.source_stale, bool):
            raise ValueError("source_stale must be a boolean")


@dataclass(frozen=True)
class CatalogEnvelope:
    host_id: str
    harness: str
    account_scope_id: str | None
    harness_version: str | None
    discovered_at: datetime | None
    stale: bool
    stale_reason: str | None
    models: tuple[ModelOption, ...]

    def __post_init__(self) -> None:
        _bounded_string(self.host_id, name="host ID", maximum=MAX_ID_LENGTH)
        _bounded_string(self.harness, name="harness", maximum=MAX_ID_LENGTH)
        _optional_bounded_string(
            self.account_scope_id,
            name="account scope ID",
            maximum=MAX_ID_LENGTH,
        )
        _optional_bounded_string(
            self.harness_version,
            name="harness version",
            maximum=MAX_ID_LENGTH,
        )
        if self.discovered_at is not None:
            if (
                not isinstance(self.discovered_at, datetime)
                or self.discovered_at.utcoffset() is None
            ):
                raise ValueError("discovered_at must be a timezone-aware datetime")
        if not isinstance(self.stale, bool):
            raise ValueError("stale must be a boolean")
        if self.stale:
            if self.stale_reason not in STALE_REASONS:
                raise ValueError("stale responses require a safe stale reason")
        elif self.stale_reason is not None:
            raise ValueError("live responses cannot have a stale reason")

        _validate_models(self.models, allow_empty=self.stale)
        metadata = (
            self.account_scope_id,
            self.harness_version,
            self.discovered_at,
        )
        if not self.models:
            if not self.stale or metadata != (None, None, None):
                raise ValueError("empty catalogs are only allowed for stale failures")
        elif any(value is None for value in metadata):
            raise ValueError("catalog models require discovery metadata")
        catalog_wire_bytes(self)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "host_id": self.host_id,
            "harness": self.harness,
            "account_scope_id": self.account_scope_id,
            "harness_version": self.harness_version,
            "discovered_at": (
                self.discovered_at.isoformat() if self.discovered_at else None
            ),
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "models": [model_to_wire(model) for model in self.models],
        }

    @classmethod
    def from_wire(
        cls, value: object, expected_host_id: str, expected_harness: str
    ) -> "CatalogEnvelope":
        if not isinstance(value, dict):
            raise ValueError("catalog envelope must be an object")
        if value.get("schema_version") != 1 or isinstance(
            value.get("schema_version"), bool
        ):
            raise ValueError("unsupported catalog schema version")
        if (
            value.get("host_id") != expected_host_id
            or value.get("harness") != expected_harness
        ):
            raise ValueError(
                "catalog identity does not match the requested host and harness"
            )

        models_value = value.get("models")
        if not isinstance(models_value, list):
            raise ValueError("catalog models must be a list")
        models: list[ModelOption] = []
        for model_value in models_value:
            try:
                models.append(model_from_wire(model_value))
            except ValueError:
                continue

        discovered_at = _datetime_from_wire(value.get("discovered_at"))
        return cls(
            host_id=expected_host_id,
            harness=expected_harness,
            account_scope_id=value.get("account_scope_id"),
            harness_version=value.get("harness_version"),
            discovered_at=discovered_at,
            stale=value.get("stale"),
            stale_reason=value.get("stale_reason"),
            models=tuple(models),
        )

    @classmethod
    def empty_failure(
        cls, host_id: str, harness: str, reason: str
    ) -> "CatalogEnvelope":
        if reason not in STALE_REASONS:
            reason = "protocol_error"
        return cls(
            host_id=host_id,
            harness=harness,
            account_scope_id=None,
            harness_version=None,
            discovered_at=None,
            stale=True,
            stale_reason=reason,
            models=(),
        )


def catalog_wire_bytes(envelope: CatalogEnvelope) -> bytes:
    """Encode one catalog exactly as the public host/central JSON response."""
    if not isinstance(envelope, CatalogEnvelope):
        raise TypeError("envelope must be a CatalogEnvelope")
    encoded = json.dumps(envelope.to_wire(), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_CATALOG_WIRE_BYTES:
        raise ValueError("model catalog exceeds public 256 KiB wire limit")
    return encoded


def model_to_wire(model: ModelOption) -> dict[str, object]:
    value: dict[str, object] = {
        "id": model.id,
        "display_name": model.display_name,
        "description": model.description,
        "is_default": model.is_default,
    }
    if model.reasoning is not None:
        value["reasoning"] = {
            "supported": list(model.reasoning.supported),
            "default": model.reasoning.default,
        }
    else:
        value["reasoning"] = None
    return value


def model_from_wire(value: object) -> ModelOption:
    if not isinstance(value, dict):
        raise ValueError("model must be an object")
    reasoning_value = value.get("reasoning")
    reasoning: ReasoningOptions | None = None
    if reasoning_value is not None:
        if not isinstance(reasoning_value, dict):
            raise ValueError("model reasoning must be an object")
        supported = reasoning_value.get("supported")
        if not isinstance(supported, list):
            raise ValueError("supported reasoning efforts must be a list")
        reasoning = ReasoningOptions(
            supported=tuple(supported), default=reasoning_value.get("default")
        )
    return ModelOption(
        id=value.get("id"),
        display_name=value.get("display_name"),
        description=value.get("description"),
        is_default=value.get("is_default", False),
        reasoning=reasoning,
    )


def _datetime_from_wire(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("discovered_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("discovered_at must be an ISO-8601 timestamp") from error
    if parsed.utcoffset() is None:
        raise ValueError("discovered_at must include a timezone")
    return parsed
