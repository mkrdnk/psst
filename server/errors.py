from starlette.exceptions import HTTPException


class APIError(HTTPException):
    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(status_code, detail, headers)
        self.code = code
