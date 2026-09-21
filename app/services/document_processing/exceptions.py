class ParserError(Exception):
    """Base exception for parser failures."""


class UnsupportedFormatError(ParserError):
    """Raised when the file extension is not supported."""


class ExtractionError(ParserError):
    """Raised when text extraction fails."""


class TextCorrectionError(ParserError):
    """Raised when the LLM text-correction pass fails."""
