"""Strict, advisory-only model analysis of redacted configuration content."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from drover.server.advisory.content_targets import (
    BundledTarget,
    ContentBundle,
)
from drover.server.advisory.model_analyzer import (
    build_configured_analysis_backend,
    ModelConfigurationAnalyzer,
    ModelFindingError,
    select_analysis_backend,
)
from drover.server.advisory.types import AnalyzerClass, Confidence
from drover.server.summarizer.backends import SummarizerBackendConfig
from drover.server.wol import GpuRig

NOW = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)


class FakeBackend:
    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.requests.append((system, user))
        return self.response


@pytest.fixture()
def bundle() -> ContentBundle:
    return ContentBundle(
        host_id="mac-mini",
        created_at=NOW,
        bundle_hash="b" * 64,
        targets=(
            BundledTarget(
                target_id="global-agents",
                content_hash="c" * 64,
                redacted_content="Always inspect the repository. Always inspect the repository.",
            ),
        ),
    )


def _response(**updates: object) -> str:
    finding: dict[str, object] = {
        "rule_id": "prompt.repetition",
        "target_id": "global-agents",
        "severity": "medium",
        "confidence": "likely",
        "title": "Repeated instruction",
        "impact": "Repeated text consumes context without adding guidance.",
        "evidence_excerpt": "Always inspect the repository.",
        "remediation": [
            "Keep one copy of the instruction.",
            "Run Check Again after reviewing the edited file.",
        ],
    }
    finding.update(updates)
    return json.dumps({"findings": [finding]})


def test_model_analyzer_returns_only_uncertain_advisory_candidates(
    bundle: ContentBundle,
) -> None:
    backend = FakeBackend(_response())

    findings = ModelConfigurationAnalyzer(backend).analyze(bundle)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.analyzer_class is AnalyzerClass.MODEL
    assert finding.confidence is Confidence.LIKELY
    assert finding.target_id == "mac-mini/global-agents"
    assert finding.content_hash == "c" * 64
    assert finding.evidence[0].excerpt == "Always inspect the repository."
    assert finding.remediation == (
        "Keep one copy of the instruction.",
        "Run Check Again after reviewing the edited file.",
    )
    assert len(backend.requests) == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_response(confidence="confirmed"), "confirmed"),
        (_response(target_id="not-allowlisted"), "unknown target"),
        (_response(evidence_excerpt="content that was never submitted"), "excerpt"),
        (json.dumps({"findings": [], "notes": "extra"}), "top-level"),
        (_response(mutation_actions=[{"operation": "replace"}]), "field"),
        ("```json\n" + _response() + "\n```", "JSON object"),
    ],
)
def test_model_analyzer_rejects_untrusted_output(
    bundle: ContentBundle, response: str, message: str
) -> None:
    backend = FakeBackend(response)

    with pytest.raises(ModelFindingError, match=message):
        ModelConfigurationAnalyzer(backend).analyze(bundle)


def test_confirmed_confidence_is_rejected_even_when_other_fields_are_missing(
    bundle: ContentBundle,
) -> None:
    backend = FakeBackend('{"findings":[{"confidence":"confirmed"}]}')

    with pytest.raises(ModelFindingError, match="confirmed"):
        ModelConfigurationAnalyzer(backend).analyze(bundle)


def test_model_analyzer_bounds_remediation(bundle: ContentBundle) -> None:
    backend = FakeBackend(
        _response(
            remediation=[f"Step {index}" for index in range(17)],
        )
    )

    with pytest.raises(ModelFindingError, match="remediation"):
        ModelConfigurationAnalyzer(backend).analyze(bundle)


def test_model_analyzer_bounds_excerpt(bundle: ContentBundle) -> None:
    backend = FakeBackend(
        _response(evidence_excerpt=bundle.targets[0].redacted_content)
    )

    with pytest.raises(ModelFindingError, match="excerpt"):
        ModelConfigurationAnalyzer(backend, excerpt_max_chars=32).analyze(bundle)


def test_backend_failure_does_not_echo_request_or_configuration_content(
    bundle: ContentBundle,
) -> None:
    secret_marker = bundle.targets[0].redacted_content

    class ExplodingBackend:
        def complete(self, system: str, user: str) -> str:
            raise RuntimeError(f"request failed for {secret_marker}")

    with pytest.raises(ModelFindingError) as captured:
        ModelConfigurationAnalyzer(ExplodingBackend()).analyze(bundle)

    assert secret_marker not in str(captured.value)


def test_backend_selection_enforces_local_and_external_consent() -> None:
    local = FakeBackend('{"findings":[]}')
    cloud = FakeBackend('{"findings":[]}')

    assert (
        select_analysis_backend(
            backend_policy="local",
            external_consent=False,
            local_backend=local,
            cloud_backend=cloud,
        )
        is local
    )
    with pytest.raises(ValueError, match="external consent"):
        select_analysis_backend(
            backend_policy="cloud",
            external_consent=False,
            local_backend=local,
            cloud_backend=cloud,
        )
    assert (
        select_analysis_backend(
            backend_policy="cloud",
            external_consent=True,
            local_backend=local,
            cloud_backend=cloud,
        )
        is cloud
    )


def test_configured_backend_reuses_policy_specific_summarizer_transport() -> None:
    config = SummarizerBackendConfig(
        backend_policy="hybrid",
        api_key="test-api-key",
        gpu_rig=GpuRig(relay_url="", ollama_url="http://ollama.invalid"),
    )

    local = build_configured_analysis_backend(
        config=config, backend_policy="local", external_consent=False
    )
    cloud = build_configured_analysis_backend(
        config=config, backend_policy="cloud", external_consent=True
    )

    assert local.transport.name == "ollama"
    assert cloud.transport.name == "anthropic"
    with pytest.raises(ValueError, match="external consent"):
        build_configured_analysis_backend(
            config=config, backend_policy="cloud", external_consent=False
        )
