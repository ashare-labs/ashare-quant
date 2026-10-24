"""Read-only derived analysis of a verified frozen result."""

from .policy import AnalysisInputError, AnalysisPolicy
from .returns import analyze_result

__all__ = ["AnalysisInputError", "AnalysisPolicy", "analyze_result"]
