"""Strict validation boundary for model-derived configuration advisories."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Protocol, runtime_checkable

from drover.server.advisory.content_targets import ContentBundle
from drover.server.advisory.prompt import SYSTEM_PROMPT, build_analysis_request
from drover.server.advisory.types import (
    AnalyzerClass,
    Confidence,
    FindingCandidate,
    FindingEvidence,
    MAX_REMEDIATION_STEP_CHARS,
    MAX_REMEDIATION_STEPS,
    Severity,
)

MODEL_ANALYZER_ID = "model.configuration"
MAX_MODEL_FINDINGS = 50
MAX_MODEL_RESPONSE_BYTES = 262_144
MAX_RULE_ID_CHARS = 128
_TOP_LEVEL_FIELDS = {"findings"}
_FINDING_FIELDS = {
    "rule_id",
    "target_id",
    "severity",
    "confidence",
    "title",
    "impact",
    "evidence_excerpt",
    "remediation",
}


class ModelFindingError(ValueError):
    """A backend response is unsafe or does not match the finding contract."""


class AnalysisConsentRevoked(ModelFindingError):
    """Consent changed after the ephemeral bundle was fetched."""


@runtime_checkable
class AnalysisBackend(Protocol):
    """Narrow transport adapter; it owns no advisory prompt or parsing rules."""

    def complete(self, system: str, user: str) -> str: ...


@dataclass
class ConfiguredAnalysisBackend:
    """Raw JSON adapter over an existing Anthropic or Ollama transport."""

    transport: Any

    def complete(self, system: str, user: str) -> str:
        from drover.server.summarizer.backends.anthropic import AnthropicBackend
        from drover.server.summarizer.backends.ollama import OllamaBackend

        if isinstance(self.transport, AnthropicBackend):
            return self._complete_anthropic(system, user)
        if isinstance(self.transport, OllamaBackend):
            return self._complete_ollama(system, user)
        raise RuntimeError("unsupported configured analysis transport")

    def _complete_anthropic(self, system: str, user: str) -> str:
        transport = self.transport
        client = transport._client
        if client is None:
            import anthropic

            kwargs: dict[str, Any] = {}
            if transport.auth_token:
                kwargs["auth_token"] = transport.auth_token
                kwargs["default_headers"] = {"anthropic-beta": "oauth-2025-04-20"}
            else:
                kwargs["api_key"] = transport.api_key
            if transport.base_url:
                kwargs["base_url"] = transport.base_url
            client = anthropic.Anthropic(**kwargs)
        response = client.messages.create(
            model=transport.model,
            max_tokens=transport.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            text for block in response.content if (text := getattr(block, "text", None))
        )

    def _complete_ollama(self, system: str, user: str) -> str:
        import requests

        transport = self.transport
        if transport.wake_on_first_call and not transport._awoken:
            transport.ensure_ready()
        response = requests.post(
            f"{transport.rig.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": transport.model,
                "system": system,
                "prompt": user,
                "stream": False,
                "format": "json",
                "keep_alive": transport.keep_alive,
                "options": {"temperature": 0.2},
            },
            timeout=transport.request_timeout_s,
        )
        if not response.ok:
            raise RuntimeError("configured Ollama analysis request failed")
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                "configured Ollama analysis response was invalid"
            ) from None
        raw = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(raw, str) or not raw:
            raise RuntimeError("configured Ollama analysis response was empty")
        return raw


def build_configured_analysis_backend(
    *, config: Any, backend_policy: str, external_consent: bool
) -> ConfiguredAnalysisBackend:
    """Reuse configured transports while forcing the content consent policy."""

    if backend_policy == "cloud" and not external_consent:
        raise ValueError("external consent is required for cloud analysis")
    if backend_policy not in {"local", "cloud"}:
        raise ValueError("analysis backend policy must be local or cloud")
    from drover.server.summarizer.backends import select_backend

    transport = select_backend(
        job_kind="advisory_content",
        config=replace(config, backend_policy=backend_policy),
    )
    return ConfiguredAnalysisBackend(transport)


def select_analysis_backend(
    *,
    backend_policy: str,
    external_consent: bool,
    local_backend: AnalysisBackend | None,
    cloud_backend: AnalysisBackend | None,
) -> AnalysisBackend:
    """Select exactly the disclosed transport, without silent cloud fallback."""

    if backend_policy == "local":
        if local_backend is None:
            raise ValueError("local analysis backend is unavailable")
        return local_backend
    if backend_policy == "cloud":
        if not external_consent:
            raise ValueError("external consent is required for cloud analysis")
        if cloud_backend is None:
            raise ValueError("cloud analysis backend is unavailable")
        return cloud_backend
    raise ValueError("analysis backend policy must be local or cloud")


class ModelConfigurationAnalyzer:
    analyzer_id = MODEL_ANALYZER_ID

    def __init__(
        self, backend: AnalysisBackend, *, excerpt_max_chars: int = 320
    ) -> None:
        if excerpt_max_chars <= 0:
            raise ValueError("excerpt_max_chars must be positive")
        self.backend = backend
        self.excerpt_max_chars = excerpt_max_chars

    def analyze(self, bundle: ContentBundle) -> list[FindingCandidate]:
        request = build_analysis_request(bundle)
        try:
            raw = self.backend.complete(SYSTEM_PROMPT, request)
        except AnalysisConsentRevoked:
            raise
        except Exception:
            # Backend errors are deliberately collapsed: transports sometimes
            # include request bodies in their exception text.
            raise ModelFindingError("analysis backend request failed") from None
        finally:
            request = ""
        return self._parse(raw, bundle)

    def _parse(self, raw: str, bundle: ContentBundle) -> list[FindingCandidate]:
        if not isinstance(raw, str):
            raise ModelFindingError("analysis response must be a JSON object")
        if len(raw) > MAX_MODEL_RESPONSE_BYTES:
            raise ModelFindingError("analysis response exceeds byte limit")
        if len(raw.encode("utf-8")) > MAX_MODEL_RESPONSE_BYTES:
            raise ModelFindingError("analysis response exceeds byte limit")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ModelFindingError("analysis response must be a JSON object") from None
        if not isinstance(payload, dict):
            raise ModelFindingError("analysis response must be a JSON object")
        if set(payload) != _TOP_LEVEL_FIELDS:
            raise ModelFindingError("analysis response has invalid top-level fields")
        findings = payload["findings"]
        if not isinstance(findings, list) or len(findings) > MAX_MODEL_FINDINGS:
            raise ModelFindingError("analysis findings must be a bounded array")
        targets = {target.target_id: target for target in bundle.targets}
        return [self._candidate(item, bundle, targets) for item in findings]

    def _candidate(self, item, bundle, targets) -> FindingCandidate:
        if isinstance(item, dict) and item.get("confidence") == "confirmed":
            raise ModelFindingError("model finding confidence cannot be confirmed")
        if not isinstance(item, dict) or set(item) != _FINDING_FIELDS:
            raise ModelFindingError("analysis finding has invalid fields")
        target_id = _required_string(item["target_id"], "target_id")
        target = targets.get(target_id)
        if target is None:
            raise ModelFindingError("analysis finding references an unknown target")

        confidence_value = _required_string(item["confidence"], "confidence")
        if confidence_value == Confidence.CONFIRMED.value:
            raise ModelFindingError("model finding confidence cannot be confirmed")
        try:
            confidence = Confidence(confidence_value)
        except ValueError:
            raise ModelFindingError("model finding confidence is invalid") from None
        if confidence not in {Confidence.LIKELY, Confidence.SPECULATIVE}:
            raise ModelFindingError("model finding confidence is invalid")
        try:
            severity = Severity(_required_string(item["severity"], "severity"))
        except ValueError:
            raise ModelFindingError("model finding severity is invalid") from None

        excerpt = _required_string(item["evidence_excerpt"], "evidence_excerpt")
        if len(excerpt) > self.excerpt_max_chars:
            raise ModelFindingError(
                "analysis evidence excerpt exceeds configured limit"
            )
        if excerpt not in target.redacted_content:
            raise ModelFindingError("analysis evidence excerpt is absent from target")
        remediation = item["remediation"]
        if (
            not isinstance(remediation, list)
            or not remediation
            or not all(isinstance(step, str) and step.strip() for step in remediation)
        ):
            raise ModelFindingError(
                "analysis remediation must be an ordered string array"
            )
        if len(remediation) > MAX_REMEDIATION_STEPS or any(
            len(step) > MAX_REMEDIATION_STEP_CHARS for step in remediation
        ):
            raise ModelFindingError("analysis remediation exceeds bounded contract")

        try:
            return FindingCandidate(
                analyzer_id=self.analyzer_id,
                rule_id=_required_bounded_string(
                    item["rule_id"], "rule_id", MAX_RULE_ID_CHARS
                ),
                target_type="configuration_target",
                target_id=f"{bundle.host_id}/{target_id}",
                analyzer_class=AnalyzerClass.MODEL,
                severity=severity,
                confidence=confidence,
                title=_required_string(item["title"], "title"),
                impact=_required_string(item["impact"], "impact"),
                remediation=tuple(remediation),
                evidence=(
                    FindingEvidence(
                        source_ref=(
                            f"content:{bundle.host_id}/{target_id}#{target.content_hash}"
                        ),
                        observed_at=bundle.created_at,
                        fields={
                            "bundle_hash": bundle.bundle_hash,
                            "content_hash": target.content_hash,
                        },
                        excerpt=excerpt,
                    ),
                ),
                content_hash=target.content_hash,
            )
        except ModelFindingError:
            raise
        except (TypeError, ValueError) as exc:
            raise ModelFindingError(
                "analysis finding violates bounded contract"
            ) from exc


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelFindingError(f"analysis finding {field_name} is required")
    return value.strip()


def _required_bounded_string(value: object, field_name: str, max_chars: int) -> str:
    result = _required_string(value, field_name)
    if len(result) > max_chars:
        raise ModelFindingError(f"analysis finding {field_name} exceeds limit")
    return result


__all__ = [
    "AnalysisBackend",
    "AnalysisConsentRevoked",
    "ConfiguredAnalysisBackend",
    "MODEL_ANALYZER_ID",
    "ModelConfigurationAnalyzer",
    "ModelFindingError",
    "build_configured_analysis_backend",
    "select_analysis_backend",
]
