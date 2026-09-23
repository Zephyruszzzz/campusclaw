"""统一错误结构与错误处理器。

所有 JSON 错误统一为 ``{"error":{"code":...,"message":...}}``。
错误信息不得包含口令、哈希、SQL、异常堆栈、绝对路径或他班内容。
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, jsonify
from werkzeug.exceptions import HTTPException


class ApiError(Exception):
    """业务错误：携带固定 HTTP 状态码与错误码。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# 对外统一提示，不区分账号是否存在（登录由 auth_views 直接给出“账号或密码错误”）
MESSAGES = {
    "AUTH_REQUIRED": "需要登录后访问",
    "ROLE_FORBIDDEN": "当前角色不允许执行该操作",
    "CSRF_INVALID": "请求令牌无效或缺失",
    "MATERIAL_NOT_FOUND": "材料不存在",
    "IDENTITY_FIELD_FORBIDDEN": "请求中包含不允许由客户端指定的身份字段",
    "INVALID_INPUT": "请求输入不完整或不合法",
    "INVALID_FILE": "必须提交恰好一个非空文件",
    "INVALID_TITLE": "标题长度须为 1～200 个字符且不含控制字符",
    "INVALID_FIELDS": "请求包含合同之外的字段",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "UNSUPPORTED_FILE_TYPE": "仅支持 .txt 与 .md 文本文件",
    "PARSE_FAILED": "文件无法解析为 UTF-8 文本",
    "STORAGE_ERROR": "文件存储不可用，未保存任何数据",
    "DATABASE_ERROR": "数据库写入失败，未保存任何数据",
    "NOT_FOUND": "资源不存在",
    "METHOD_NOT_ALLOWED": "方法不被允许",
    "INTERNAL_ERROR": "服务器内部错误",
}


def raise_api(code: str, status: int, message: str | None = None) -> None:
    raise ApiError(status, code, message or MESSAGES.get(code, code))


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError) -> tuple[Response, int]:
        return jsonify(error_body(exc.code, exc.message)), exc.status

    @app.errorhandler(413)
    def _too_large(_exc: HTTPException) -> tuple[Response, int]:
        return jsonify(error_body("FILE_TOO_LARGE", MESSAGES["FILE_TOO_LARGE"])), 413

    @app.errorhandler(HTTPException)
    def _http_exception(exc: HTTPException) -> tuple[Response, int]:
        status = exc.code or 500
        if status == 404:
            code = "NOT_FOUND"
        elif status == 405:
            code = "METHOD_NOT_ALLOWED"
        elif status == 415:
            code = "UNSUPPORTED_FILE_TYPE"
        elif status == 400:
            code = "INVALID_INPUT"
        else:
            code = "NOT_FOUND" if status < 500 else "INTERNAL_ERROR"
        return jsonify(error_body(code, MESSAGES.get(code, code))), status

    @app.errorhandler(Exception)
    def _unexpected(_exc: Exception) -> tuple[Response, int]:
        app.logger.exception("unhandled server error")
        return jsonify(error_body("INTERNAL_ERROR", MESSAGES["INTERNAL_ERROR"])), 500
