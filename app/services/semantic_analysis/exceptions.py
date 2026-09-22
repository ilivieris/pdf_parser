class SemanticAnalysisError(Exception):
    """Base exception for semantic analysis failures."""


class SkillsDetectionError(SemanticAnalysisError):
    """Raised when the LLM-based ESCO/DigComp detection call fails."""


class LlmExtractionError(SemanticAnalysisError):
    """Raised when the LLM-based decision-type/CPV/ΑΛΕ extraction call fails."""
