"""写接口管理员鉴权 (阶段1-B 实现, 取代阶段0的 TODO 标记)。

设计:
- 令牌来自环境变量 PMQ_ADMIN_TOKEN, 每次请求实时读取 (便于轮换与测试)。
- fail-closed: 服务端未设置 PMQ_ADMIN_TOKEN 时, 所有写接口一律拒绝 (503)。
- 请求须带 Authorization: Bearer <token> 或 X-Admin-Token: <token> 二者其一。
- 比较使用 hmac.compare_digest 常量时间比较, 防时序侧信道。
- 本模块只依赖标准库; FastAPI 适配层 (require_admin 依赖) 在 server.py 中。
"""
import hmac
import os

ENV_VAR = "PMQ_ADMIN_TOKEN"


class AdminAuthError(Exception):
    """鉴权失败: status_code 为应返回的 HTTP 状态码, detail 为原因说明。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _expected_token() -> str:
    return os.getenv(ENV_VAR, "").strip()


def _provided_token(authorization, x_admin_token) -> str:
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    if x_admin_token:
        return x_admin_token.strip()
    return ""


def verify_admin_token(authorization=None, x_admin_token=None) -> None:
    """校验管理员令牌; 通过则返回 None, 失败抛 AdminAuthError。"""
    expected = _expected_token()
    if not expected:
        raise AdminAuthError(
            503,
            f"服务端未设置 {ENV_VAR}, 写接口已禁用 (fail-closed)",
        )
    provided = _provided_token(authorization, x_admin_token)
    if not provided:
        raise AdminAuthError(401, "缺少管理员令牌 (Authorization: Bearer 或 X-Admin-Token)")
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise AdminAuthError(401, "管理员令牌不正确")
