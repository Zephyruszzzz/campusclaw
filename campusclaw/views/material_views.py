"""受保护的材料 API：列表、详情、下载、上传、标题修改、删除。"""

from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request

from campusclaw import materials as svc
from campusclaw import request_context as rc
from campusclaw.errors import MESSAGES, ApiError

bp = Blueprint("materials_api", __name__, url_prefix="/api/materials")


def parse_material_id(raw: str) -> int:
    """非法格式的资源 ID 与不存在 ID 一律 404。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(404, "MATERIAL_NOT_FOUND", MESSAGES["MATERIAL_NOT_FOUND"]) from None
    if value <= 0:
        raise ApiError(404, "MATERIAL_NOT_FOUND", MESSAGES["MATERIAL_NOT_FOUND"])
    return value


def _safe_download_name(original: str) -> str:
    cleaned = original.replace("\r", "").replace("\n", "")
    cleaned = cleaned.replace('"', "").replace("\\", "").replace("/", "")
    cleaned = cleaned.strip()
    return cleaned or "material.txt"


@bp.route("/", methods=["GET"], strict_slashes=False)
def list_api():
    user = rc.require_api_auth()
    rc.reject_identity_fields()
    conn = rc.get_conn()
    return jsonify({"items": svc.list_materials(conn, int(user["class_id"]))}), 200


@bp.get("/<material_id>")
def detail_api(material_id: str):
    user = rc.require_api_auth()
    rc.reject_identity_fields()
    material_id_int = parse_material_id(material_id)
    conn = rc.get_conn()
    material = svc.get_active_material(conn, material_id_int, int(user["class_id"]))
    if material is None:
        raise ApiError(404, "MATERIAL_NOT_FOUND", MESSAGES["MATERIAL_NOT_FOUND"])
    summary = {key: material[key] for key in material.keys() if key != "storage_key"}
    body = svc.get_material_body(conn, material_id_int, int(user["class_id"]))
    response = jsonify({"material": summary, "body_text": body})
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response, 200


@bp.get("/<material_id>/file")
def download_api(material_id: str):
    user = rc.require_api_auth()
    rc.reject_identity_fields()
    material_id_int = parse_material_id(material_id)
    conn = rc.get_conn()
    material = svc.get_active_material(conn, material_id_int, int(user["class_id"]))
    if material is None:
        raise ApiError(404, "MATERIAL_NOT_FOUND", MESSAGES["MATERIAL_NOT_FOUND"])
    settings = rc.get_settings()
    data = svc.read_material_file(settings, int(user["class_id"]), material["storage_key"])
    response = Response(data, mimetype="text/plain; charset=utf-8")
    response.headers["Content-Disposition"] = (
        f"attachment; filename=\"{_safe_download_name(material['original_filename'])}\""
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response, 200


@bp.post("/upload")
def upload_api():
    user = rc.require_write()
    rc.upload_identity_check(request.form)

    unknown = [key for key in request.form.keys() if key not in svc.UPLOAD_ALLOWED_FIELDS]
    if unknown:
        raise ApiError(400, "INVALID_FIELDS", MESSAGES["INVALID_FIELDS"])

    files = request.files.getlist(svc.UPLOAD_FIELD_NAME)
    extra_fields = [
        key for key in request.files.keys() if key != svc.UPLOAD_FIELD_NAME
    ]
    if extra_fields or len(files) != 1:
        raise ApiError(400, "INVALID_FILE", MESSAGES["INVALID_FILE"])
    uploaded = files[0]
    explicit_title = request.form.get("title")
    settings = rc.get_settings()
    data = uploaded.read()
    material_id = svc.upload_material(
        rc.get_conn(),
        settings,
        data,
        uploaded.filename or "",
        explicit_title,
        teacher_id=int(user["user_id"]),
        class_id=int(user["class_id"]),
    )
    return jsonify({"material_id": material_id, "status": "active"}), 201


@bp.patch("/<material_id>")
def patch_api(material_id: str):
    user = rc.require_write()
    conn = rc.get_conn()
    material_id_int = parse_material_id(material_id)

    # 判定顺序：角色 → CSRF → 路径 ID 所属班级/有效状态 → 输入校验 → 业务操作。
    if svc.get_active_material(conn, material_id_int, int(user["class_id"])) is None:
        raise ApiError(404, "MATERIAL_NOT_FOUND", MESSAGES["MATERIAL_NOT_FOUND"])

    raw_body = request.get_data(cache=True)
    body: dict = {}
    if raw_body.strip():
        if not request.is_json:
            raise ApiError(400, "INVALID_FIELDS", MESSAGES["INVALID_FIELDS"])
        try:
            parsed = request.get_json(silent=False)
        except json.JSONDecodeError:
            raise ApiError(400, "INVALID_FIELDS", MESSAGES["INVALID_FIELDS"]) from None
        if not isinstance(parsed, dict):
            raise ApiError(400, "INVALID_FIELDS", MESSAGES["INVALID_FIELDS"])
        body = parsed

    rc.reject_identity_fields(body)
    unknown = [key for key in body.keys() if key not in svc.PATCH_ALLOWED_FIELDS]
    if unknown:
        raise ApiError(400, "INVALID_FIELDS", MESSAGES["INVALID_FIELDS"])
    if "title" not in body:
        raise ApiError(400, "INVALID_TITLE", MESSAGES["INVALID_TITLE"])

    conn = rc.get_conn()
    title = svc.validate_title(str(body["title"]))
    svc.rename_material(conn, material_id_int, int(user["class_id"]), title)
    return jsonify({"material_id": material_id_int, "title": title}), 200


@bp.delete("/<material_id>")
def delete_api(material_id: str):
    user = rc.require_write()
    rc.reject_identity_fields()
    material_id_int = parse_material_id(material_id)
    conn = rc.get_conn()
    settings = rc.get_settings()
    material = svc.mark_deleted(conn, material_id_int, int(user["class_id"]))
    cleanup_status = svc.finish_cleanup(
        conn, settings, material_id_int, int(material["class_id"]), material["storage_key"]
    )
    if cleanup_status == "done":
        return Response(status=204)
    return (
        jsonify(
            {
                "material_id": material_id_int,
                "status": "deleted",
                "cleanup_status": "pending",
            }
        ),
        202,
    )
