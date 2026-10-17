from typing import Any


class RoutingError(Exception):
    """A public, deliberately redacted service error."""

    def __init__(self, code: str, message: str, status: int = 422, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = {"cost_unknown": False} if details is None else details

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
