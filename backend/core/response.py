"""统一响应信封。

前端只依赖 ``code``（成功恒为 ``"OK"``），不解析 message；``request_id`` 用于日志关联与排障。
错误响应由 ``core.errors`` 的处理器生成，结构与本模块保持一致。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.ids import new_request_id


class ApiResponse[T](BaseModel):
    code: str = "OK"
    message: str = "success"
    data: T | None = None
    request_id: str = Field(default_factory=new_request_id)


class PageMeta(BaseModel):
    page: int = 1
    page_size: int = 20
    total: int = 0

    @property
    def pages(self) -> int:
        return (self.total + self.page_size - 1) // self.page_size if self.page_size else 0


class Page[T](BaseModel):
    items: list[T] = Field(default_factory=list)
    meta: PageMeta = Field(default_factory=PageMeta)


def ok[T](data: T | None = None, message: str = "success") -> ApiResponse[T]:
    return ApiResponse[T](code="OK", message=message, data=data)


def page[T](items: list[T], total: int, *, page_no: int = 1, page_size: int = 20) -> ApiResponse[Page[T]]:
    return ApiResponse[Page[T]](
        code="OK",
        message="success",
        data=Page[T](items=items, meta=PageMeta(page=page_no, page_size=page_size, total=total)),
    )
