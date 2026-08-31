"""Turn gate + red-team results into an audit-grade assurance report.

A dashboard tells engineers what happened; a compliance reviewer, an enterprise
vendor-review team, or an EU AI Act technical file needs a document: what was
tested, the verdict, the evidence, and how it maps to the frameworks they care
about (OWASP Agentic Top 10, NIST AI RMF, EU AI Act logging). This package
assembles that from a signed gate certificate and a red-team report.
"""
from .report import build_assurance, render_markdown

__all__ = ["build_assurance", "render_markdown"]
