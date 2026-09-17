"""Static + dynamic checks for implementation-substitution failures.

This module gives Lobe B concrete, evidence-based signals for the failure mode
where a builder (Lobe A) replaces a requested real capability with a polished but
fake substitute: UI mockups, scripted progress, deleted integrations, missing
providers, or premature completion claims.

It is intentionally separate from the model-based review so that:
- findings are reproducible from files and process state,
- B can cite exact code evidence, not just model intuition,
- the same checks can run in CI or as a gateway sensor.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("dual_lobe.b.implementation_auditor")


@dataclass
class AuditorFinding:
    check: str
    severity: "literal['BLOCKER', 'HIGH', 'MEDIUM', 'INFO']"
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""


class ImplementationAuditor:
    """Run targeted checks against a workspace for fake-completion patterns."""

    # Patterns that strongly suggest simulated / canned behavior.
    SIMULATION_PATTERNS: list[tuple[str, re.Pattern]] = [
        ("setInterval_progress", re.compile(r"setInterval\s*\(", re.I)),
        ("setTimeout_progress", re.compile(r"setTimeout\s*\(", re.I)),
        ("hardcoded_steps", re.compile(r"steps\s*[:=]\s*(\[|Array\()", re.I)),
        ("canned_done_message", re.compile(r"(Done|Completed|Finished)\s*[—\-–]", re.I)),
        ("fake_working_indicator", re.compile(r"Working\s+in\s+(browser|agent|background)", re.I)),
        ("mock_agent_flow", re.compile(r"(deterministic|demo|fake|mock|simulated)\s+(agent|flow|backend)", re.I)),
    ]

    # Evidence that a real provider/model backend is present.
    REAL_BACKEND_MARKERS: list[tuple[str, re.Pattern]] = [
        ("openai_compatible_call", re.compile(r"openai\.(chat\.completions|completions|responses)", re.I)),
        ("anthropic_call", re.compile(r"anthropic\.(messages|completions)", re.I)),
        ("provider_api_key", re.compile(r"(OPENAI_API_KEY|ANTHROPIC_API_KEY|DEEPINFRA_API_KEY|FEATHERLESS_API_KEY|GEMINI_API_KEY|GROQ_API_KEY)", re.I)),
        ("browser_use_import", re.compile(r"(from\s+browser_use|import\s+browser_use|browser_use\.(agent|browser|controller))", re.I)),
        ("playwright_control", re.compile(r"(playwright|chromium|new_browser|new_context|new_page)\.(launch|goto|click|fill)", re.I)),
        ("selenium_control", re.compile(r"webdriver\.(Chrome|Firefox|Edge)", re.I)),
    ]

    # Signs that an existing integration was removed.
    DELETION_PATTERNS: list[tuple[str, re.Pattern]] = [
        ("removed_spawn_integration", re.compile(r"spawn\s*\(\s*['\"]python['\"].*browser_use", re.I | re.S)),
        ("removed_backend_import", re.compile(r"#\s*(spawn|browser_use|agent_backend|provider)", re.I)),
    ]

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()

    def _walk_code_files(self) -> list[Path]:
        exts = {".js", ".ts", ".jsx", ".tsx", ".py", ".html", ".css", ".json", ".md"}
        skip_dirs = {
            ".git", ".venv", ".venv3.11", "node_modules", "__pycache__",
            "dist", "build", ".next", ".turbo", "coverage", ".pytest_cache",
        }
        files: list[Path] = []
        for p in self.workspace.rglob("*"):
            if not p.is_file():
                continue
            if any(part in skip_dirs for part in p.parts):
                continue
            if p.suffix.lower() in exts:
                files.append(p)
        return files

    def _grep(self, pattern: re.Pattern, content: str) -> list[tuple[int, str]]:
        return [(i + 1, line.strip()) for i, line in enumerate(content.splitlines()) if pattern.search(line)]

    def check_simulated_behavior(self) -> list[AuditorFinding]:
        findings: list[AuditorFinding] = []
        for path in self._walk_code_files():
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for check_name, pattern in self.SIMULATION_PATTERNS:
                matches = self._grep(pattern, content)
                if matches:
                    line_nos = [m[0] for m in matches[:3]]
                    findings.append(
                        AuditorFinding(
                            check=f"simulation:{check_name}",
                            severity="HIGH",
                            message=f"Possible simulated/canned behavior in {path.name}",
                            evidence={
                                "path": str(path.relative_to(self.workspace)),
                                "lines": line_nos,
                                "samples": [m[1][:120] for m in matches[:3]],
                            },
                            suggestion="Replace timers/canned steps with real backend events; label any demo as SIMULATED.",
                        )
                    )
                    break  # one finding per file per pattern is enough
        return findings

    def check_missing_backend(self) -> list[AuditorFinding]:
        """Detect when a project has a polished UI but no real model/agent backend."""
        findings: list[AuditorFinding] = []
        files = self._walk_code_files()

        has_backend = False
        backend_evidence: list[dict] = []
        for path in files:
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for check_name, pattern in self.REAL_BACKEND_MARKERS:
                if pattern.search(content):
                    has_backend = True
                    backend_evidence.append({
                        "path": str(path.relative_to(self.workspace)),
                        "marker": check_name,
                    })

        # Heuristic: is there a UI but no backend?
        has_ui = any(
            "webview" in f.name.lower() or "index.html" in f.name.lower() or "renderer" in f.name.lower()
            for f in files
        )

        if has_ui and not has_backend:
            findings.append(
                AuditorFinding(
                    check="missing_backend:ui_without_provider",
                    severity="BLOCKER",
                    message="UI files exist but no real model/provider/agent backend was detected.",
                    evidence={
                        "ui_files_found": has_ui,
                        "backend_markers_checked": [m[0] for m in self.REAL_BACKEND_MARKERS],
                        "backend_evidence": backend_evidence,
                    },
                    suggestion="Prove a real command-line agent task first, then connect the UI to that backend.",
                )
            )
        return findings

    def check_integration_deletion(self, baseline_paths: list[str] | None = None) -> list[AuditorFinding]:
        """Flag when known integration code seems to have been removed."""
        findings: list[AuditorFinding] = []
        for rel in baseline_paths or []:
            path = self.workspace / rel
            if not path.exists():
                findings.append(
                    AuditorFinding(
                        check="integration_deletion:missing_file",
                        severity="HIGH",
                        message=f"Expected integration file no longer exists: {rel}",
                        evidence={"expected_path": rel},
                        suggestion="Restore the file or explicitly document why it was removed.",
                    )
                )
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            for check_name, pattern in self.DELETION_PATTERNS:
                if pattern.search(content):
                    findings.append(
                        AuditorFinding(
                            check=f"integration_deletion:{check_name}",
                            severity="HIGH",
                            message=f"Integration code in {rel} appears commented/removed.",
                            evidence={"path": rel, "matches": [m[1][:120] for m in self._grep(pattern, content)[:3]]},
                            suggestion="Test the existing integration before replacing it; preserve it until replacement is proven.",
                        )
                    )
        return findings

    def check_completion_claims(self, response_text: str) -> list[AuditorFinding]:
        """Surface when an assistant response declares completion without evidence."""
        findings: list[AuditorFinding] = []
        strong_done = re.compile(
            r"\b(done|completed|finished|built|running successfully|it's working)\b",
            re.I,
        )
        evidence_words = re.compile(
            r"\b(test|verified|proof|log|output|curl|process|screenshot|trace|agent|model|provider)\b",
            re.I,
        )

        sentences = re.split(r"(?<=[.!?])\s+", response_text)
        for sentence in sentences:
            if strong_done.search(sentence) and not evidence_words.search(sentence):
                findings.append(
                    AuditorFinding(
                        check="completion_claim:unsubstantiated",
                        severity="MEDIUM",
                        message="Completion claim lacks evidence keywords in the same sentence.",
                        evidence={"sentence": sentence[:200]},
                        suggestion="Require the assistant to cite a command, log, URL, or process ID when claiming completion.",
                    )
                )
                break
        return findings

    def run_all(
        self,
        response_text: str = "",
        baseline_paths: list[str] | None = None,
    ) -> list[AuditorFinding]:
        findings: list[AuditorFinding] = []
        findings.extend(self.check_simulated_behavior())
        findings.extend(self.check_missing_backend())
        findings.extend(self.check_integration_deletion(baseline_paths))
        findings.extend(self.check_completion_claims(response_text))
        return findings


def findings_to_text(findings: list[AuditorFinding]) -> str:
    """Compact markdown-ish summary for injection into B's prompt."""
    if not findings:
        return ""
    lines = ["## Implementation-auditor signals"]
    for f in findings:
        lines.append(f"- **[{f.severity}]** `{f.check}`: {f.message}")
        if f.evidence:
            lines.append(f"  evidence: {json.dumps(f.evidence, ensure_ascii=False)[:300]}")
        if f.suggestion:
            lines.append(f"  suggestion: {f.suggestion}")
    return "\n".join(lines)
