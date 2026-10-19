"""Bounded Apple error messages with upload credentials and local paths removed."""

import os
from pathlib import Path
import re


def diagnostic_messages(response, stderr, secrets=()):
    sensitive = list(secrets)
    sensitive.extend(
        value
        for name, value in os.environ.items()
        if re.search(r"SECRET|TOKEN|PASSWORD|PRIVATE_KEY|KEY_ID|ISSUER", name, re.I)
        and value
    )

    def sanitize(message):
        for secret in sorted(set(sensitive), key=len, reverse=True):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        message = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
            "[REDACTED]",
            message,
            flags=re.S,
        )
        message = re.sub(
            r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
            "[REDACTED]",
            message,
            flags=re.I,
        )
        message = re.sub(
            r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
            lambda m: "[REDACTED]" if m[0].startswith("eyJ") else m[0],
            message,
        )
        message = re.sub(
            r"(?i)\b(?:bearer\s+\S+|(?:token|password|secret|api[-_ ]?key|issuer(?:[-_ ]?id)?)"
            r"\s*[=:]\s*[\"']?[^\s,;}\"']+)",
            "[REDACTED]",
            message,
        )
        # Include the actual HOME and common runner homes. Preserve relative
        # bundle paths such as Drover.app/PlugIns/DroverWidget.appex.
        for home in {str(Path.home()), os.environ.get("HOME", "")} - {"", "/"}:
            message = message.replace(home, "[HOME]")
        message = re.sub(
            r"""(["'])(?:\[HOME\]|/Users/[^/\s]+|/home/[^/\s]+|/var/root)/.*?\1""",
            "[REDACTED_PATH]",
            message,
        )
        message = re.sub(
            r"(?:\[HOME\]|/Users/[^/\s]+|/home/[^/\s]+|/var/root)(?:/[^\s\"'<>;,)}]*)?",
            "[REDACTED_PATH]",
            message,
        )
        message = re.sub(r"\b[A-Za-z0-9_+/=-]{32,}\b", "[REDACTED]", message)
        return " ".join(message.split())[:4096]

    messages = []

    def add(message):
        if isinstance(message, str) and len(messages) < 20:
            clean = sanitize(message)
            if clean and clean not in messages:
                messages.append(clean)

    def visit(item, depth=0):
        if depth > 12 or len(messages) >= 20:
            return
        if isinstance(item, dict):
            for key, child in list(item.items())[:100]:
                if key.lower().replace("-", "").replace("_", "") in {
                    "message",
                    "detail",
                    "description",
                    "nslocalizeddescription",
                    "nslocalizedfailurereason",
                    "nslocalizedrecoverysuggestion",
                }:
                    add(child)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item[:100]:
                visit(child, depth + 1)

    visit(response)
    # stderr may be the only source when altool emits malformed/non-JSON output.
    for line in stderr.splitlines():
        if re.search(
            r"ITMS-\d+|Error Domain=|\b(?:ERROR|Error):|"
            r"\b(?:NSLocalizedDescription|NSLocalizedFailureReason|detail|message)\s*[=:]",
            line,
        ):
            add(line)
    return messages
