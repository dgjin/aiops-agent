"""被监控应用标准接口（AIOps Manifest）v1.0：约定、校验与自动探测。

被监控系统在 ``GET /.well-known/aiops.json`` 暴露一份 JSON 描述（Manifest），
AIOps 控制台「从标准接口自动探测」据此预填接入配置（名称 / 服务标识 / 健康关键字 /
日志路径），实现「前台添加 → 自动适配」，无需人工翻代码找日志路径与健康关键字。

规范字段（v1.0，全部 UTF-8 JSON；``*`` 为必填）：

================  =======  ==================================================
字段              必填     说明
================  =======  ==================================================
spec_version      *       接口规范版本（如 "1.0"）；主版本不符仅告警
service           *       服务标识（与告警 / 日志 / 清单中的 ``service`` 一致）
name                      人类可读名称（控制台默认展示名）
probe_keyword             健康页响应须包含的关键字（在线判据 / 修复后验证）
health_path               健康检查路径（相对根地址，校验工具用）
metrics_path              Prometheus 指标路径（观测增强，可空）
log_path                  应用日志文件路径（支持 ``*`` 通配，采集器据此监听）
protected_paths           受保护路径模式列表（命中 → 补丁走二级审批）
test_command              沙箱回归建议命令（信息性；策略文件优先）
requirements_path         需求基线条目导出路径（供主动需求分析，见 requirements_client.py）
================  =======  ==================================================

本模块只做「探测 + 校验」：不落盘、不引入第三方依赖（标准库 urllib 实现），
探测失败是常态（含超时 / 非 JSON / 结构不符），:func:`fetch_manifest` **绝不抛异常**。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

MANIFEST_PATH = "/.well-known/aiops.json"
SPEC_VERSION = "1.0"
DEFAULT_TIMEOUT = 3.0
# 防御性上限：控制台探测不应被（错误配置的）大响应拖垮
MAX_BODY = 256 * 1024

_STR_FIELDS = (
    "name",
    "probe_keyword",
    "health_path",
    "metrics_path",
    "log_path",
    "test_command",
    "requirements_path",
)


def manifest_url(base_url: str) -> str:
    """由应用根地址推导 Manifest 地址（容忍带 / 不带尾斜杠）。"""
    return (base_url or "").strip().rstrip("/") + MANIFEST_PATH


def validate_manifest(payload: object) -> tuple[list[str], list[str]]:
    """校验 Manifest 结构与类型，返回 ``(errors, warnings)``。

    errors 非空 = 不构成有效 Manifest（控制台按「未接入标准接口」处理，不预填）；
    warnings 仅提示（如主版本不同），字段仍可用于预填。
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return ["Manifest 必须是 JSON 对象"], warnings

    version = payload.get("spec_version")
    if not isinstance(version, str) or not version.strip():
        errors.append('缺少必填字段 spec_version（字符串，如 "1.0"）')
    elif version.strip().split(".", 1)[0] != SPEC_VERSION.split(".", 1)[0]:
        warnings.append(
            f"spec_version={version.strip()!r} 与当前支持 {SPEC_VERSION} 主版本不同，按尽力兼容处理"
        )

    service = payload.get("service")
    if not isinstance(service, str) or not service.strip():
        errors.append("缺少必填字段 service（服务标识，须与告警/日志中的 service 一致）")

    for field in _STR_FIELDS:
        if field in payload and not isinstance(payload[field], str):
            errors.append(f"{field} 必须是字符串")

    protected = payload.get("protected_paths", [])
    if not isinstance(protected, list) or any(not isinstance(item, str) for item in protected):
        errors.append("protected_paths 必须是字符串数组")

    return errors, warnings


def fetch_manifest(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """抓取并校验应用 Manifest，**绝不抛异常**（探测失败是常态）。

    返回 ``{"url", "ok", "error", "errors", "warnings", "manifest"}``：
    ``ok=True`` 时 ``manifest`` 为原始对象（控制台据此预填表单）。
    """
    url = manifest_url(base_url)
    result: dict = {
        "url": url,
        "ok": False,
        "error": "",
        "errors": [],
        "warnings": [],
        "manifest": None,
    }
    request = urllib.request.Request(
        url, headers={"User-Agent": "aiops-agent/0.1", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        result["error"] = f"应用返回 HTTP {exc.code}（未部署标准接口文件）"
        return result
    except Exception as exc:  # noqa: BLE001 - 超时/拒连/DNS 等一律转探测失败
        result["error"] = f"无法访问标准接口：{exc}"
        return result

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        result["error"] = f"标准接口响应不是合法 JSON：{exc}"
        return result

    errors, warnings = validate_manifest(payload)
    result["errors"], result["warnings"] = errors, warnings
    if errors:
        result["error"] = "；".join(errors)
        return result
    result["ok"] = True
    result["manifest"] = payload
    return result
