"""
统一响应渲染器：所有响应包装为 {code, message, data, request_id}
"""
from __future__ import annotations

from rest_framework.renderers import JSONRenderer


class StandardJSONRenderer(JSONRenderer):
    """统一响应结构。"""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        renderer_context = renderer_context or {}
        request = renderer_context.get("request")
        response = renderer_context.get("response")

        request_id = ""
        if request is not None:
            request_id = getattr(request, "request_id", "") or ""

        status_code = response.status_code if response is not None else 200

        if status_code >= 400 or isinstance(data, dict) and "detail" in data and len(data) == 1:
            # 错误响应
            message = "请求失败"
            if isinstance(data, dict):
                if "detail" in data and len(data) == 1:
                    message = str(data["detail"])
                elif "message" in data:
                    message = str(data["message"])
                if "code" in data:
                    code = data["code"]
                else:
                    code = status_code
            else:
                message = str(data)
                code = status_code
            payload = {
                "code": code,
                "message": message,
                "data": None,
                "request_id": request_id,
            }
        else:
            payload = {
                "code": 0,
                "message": "success",
                "data": data,
                "request_id": request_id,
            }

        return super().render(payload, accepted_media_type, renderer_context)
