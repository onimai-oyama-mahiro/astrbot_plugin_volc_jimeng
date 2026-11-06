"""即梦AI-图片生成4.6（火山视觉智能开放平台）AstrBot 插件。

- 通过官方 volcengine SDK 调用 visual.volcengineapi.com（AK/SK 签名）
- 注册 LLM 工具，让模型自主决定是否画图
- 提供插件可视化视图：概览 / 生成 / 历史 / 预设 / 设置
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from PIL import Image as PILImage
from PIL import ImageOps

PLUGIN_NAME = "astrbot_plugin_volc_jimeng"
PLUGIN_VERSION = "1.1.1"

# req_key -> 中文展示名（目前只开放 4.6）
REQ_KEYS: dict[str, str] = {
    "jimeng_seedream46_cvtob": "即梦AI-图片生成4.6",
}

# 尺寸档位：内部值 -> 展示名
SIZE_PRESETS: dict[str, str] = {
    "1024x1024": "1K · 1:1（1024×1024）",
    "2048x2048": "2K · 1:1（2048×2048）",
    "2304x1728": "2K · 4:3（2304×1728）",
    "2496x1664": "2K · 3:2（2496×1664）",
    "2560x1440": "2K · 16:9（2560×1440）",
    "3024x1296": "2K · 21:9（3024×1296）",
    "4096x4096": "4K · 1:1（4096×4096）",
    "5404x3040": "4K · 16:9（5404×3040）",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "access_key_id": "",
    "secret_access_key": "",
    "req_key": "jimeng_seedream46_cvtob",
    "size_preset": "2048x2048",
    "scale": 50,
    "force_single": True,
    "enable_img2img": True,
    "max_input_images": 4,
    "watermark": False,
    "timeout": 180,
    "poll_interval": 2.0,
    "max_concurrency": 2,
    "enable_llm_tool": True,
    "tool_wait": False,
    "inject_hint": True,
    "enable_command": True,
    "daily_limit": 50,
    "user_daily_limit": 10,
    "cooldown": 10,
    "whitelist": [],
    "admin_bypass": True,
    "keep_days": 7,
    "max_history": 200,
}

MAX_PROMPT_CHARS = 800
MAX_IMAGES_PER_TASK = 6
DRAW_HINT = (
    "\n\n[画图能力]\n"
    "当用户希望得到一张图片（插画、照片、表情包、海报、示意图、头像等）或明确要求画图时，"
    "调用 jimeng_draw_image 工具；纯聊天、知识问答、文字任务不要调用。"
)

SIZE_HINT = (
    "提示词建议包含主体、场景、风格、光线和构图，不超过 800 字；"
    "v4.6 一次可能生成多张，插件默认强制单图。"
)


class JimengError(RuntimeError):
    """面向用户的生成错误。"""


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _mask(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as fp:
                return json.load(fp)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jimeng] 读取 %s 失败: %s", path, exc)
    return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def _parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{3,5})\s*[xX*×]\s*(\d{3,5})\s*", value or "")
    if not match:
        raise JimengError(f"无效的尺寸：{value}")
    width, height = int(match.group(1)), int(match.group(2))
    area = width * height
    if area < 1024 * 1024 or area > 4096 * 4096:
        raise JimengError("尺寸超出范围：面积需在 1024×1024 到 4096×4096 之间")
    return width, height


class JimengClient:
    """对 volcengine VisualService 的薄封装（同步调用，需放线程池执行）。"""

    def __init__(self, access_key_id: str, secret_access_key: str) -> None:
        self._ak = access_key_id
        self._sk = secret_access_key

    def _service(self):
        # 延迟导入：依赖未安装时插件仍能加载，并在页面上给出明确提示
        try:
            from volcengine.visual.VisualService import VisualService
        except Exception as exc:  # noqa: BLE001
            raise JimengError(
                "缺少依赖 volcengine，请在插件管理页安装依赖或手动 pip install volcengine"
            ) from exc

        service = VisualService()
        service.set_ak(self._ak)
        service.set_sk(self._sk)
        return service

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._service().cv_sync2async_submit_task(payload)

    def fetch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._service().cv_sync2async_get_result(payload)


@register(PLUGIN_NAME, "shira", "即梦AI-图片生成4.6 生图与可视化控制台", PLUGIN_VERSION)
class VolcJimengPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

        data_root = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self._data_dir = data_root
        self._images_dir = data_root / "images"
        self._images_dir.mkdir(parents=True, exist_ok=True)
        self._inputs_dir = data_root / "inputs"
        self._inputs_dir.mkdir(parents=True, exist_ok=True)

        self._history_path = data_root / "history.json"
        self._presets_path = data_root / "presets.json"
        self._usage_path = data_root / "usage.json"

        self._lock = asyncio.Lock()
        self._history: list[dict[str, Any]] = _load_json(self._history_path, [])
        self._presets: list[dict[str, Any]] = _load_json(self._presets_path, [])
        self._usage: dict[str, Any] = _load_json(self._usage_path, {})
        self._cooldown: dict[str, float] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(max(1, self._cfg_int("max_concurrency", 2)))

        if not self._presets:
            self._presets = [
                {
                    "id": uuid.uuid4().hex[:8],
                    "name": "电影感人像",
                    "prompt": "电影感半身人像，柔和侧逆光，浅景深，背景虚化，细腻皮肤质感，35mm 胶片色调",
                    "size": "2048x2048",
                },
                {
                    "id": uuid.uuid4().hex[:8],
                    "name": "二次元插画",
                    "prompt": "干净细腻的日系动漫插画，清晰线稿，柔和配色，全身构图，纯色背景，高清细节",
                    "size": "2048x2048",
                },
            ]
            _save_json(self._presets_path, self._presets)

        self._cleanup_old()

        for route, handler, methods, desc in (
            ("overview", self.api_overview, ["GET"], "概览数据"),
            ("config", self.api_config, ["GET"], "读取配置"),
            ("config/save", self.api_config_save, ["POST"], "保存配置"),
            ("generate", self.api_generate, ["POST"], "创建生成任务"),
            ("upload", self.api_upload, ["POST"], "上传参考图"),
            ("task", self.api_task, ["GET"], "查询任务"),
            ("history", self.api_history, ["GET"], "生成历史"),
            ("history/delete", self.api_history_delete, ["POST"], "删除历史"),
            ("image", self.api_image, ["GET"], "读取图片"),
            ("presets", self.api_presets, ["GET"], "预设列表"),
            ("presets/save", self.api_presets_save, ["POST"], "保存预设"),
            ("presets/delete", self.api_presets_delete, ["POST"], "删除预设"),
        ):
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}/{route}", handler, methods, desc
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[jimeng] 注册接口 %s 失败: %s", route, exc)

    # ------------------------------------------------------------------ config

    def _cfg(self, key: str) -> Any:
        value = self.config.get(key, DEFAULT_CONFIG.get(key))
        if value is None:
            return DEFAULT_CONFIG.get(key)
        return value

    def _cfg_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._cfg(key))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self._cfg(key))
        except (TypeError, ValueError):
            return default

    def _configured(self) -> bool:
        return bool(
            str(self._cfg("access_key_id") or "").strip()
            and str(self._cfg("secret_access_key") or "").strip()
        )

    def _client(self) -> JimengClient:
        return JimengClient(
            str(self._cfg("access_key_id") or "").strip(),
            str(self._cfg("secret_access_key") or "").strip(),
        )

    def _req_key(self) -> str:
        req_key = str(self._cfg("req_key") or "").strip()
        if req_key not in REQ_KEYS:
            req_key = next(iter(REQ_KEYS))
        return req_key

    def _size_key(self, size: str | None = None) -> str:
        candidate = (size or str(self._cfg("size_preset") or "")).strip()
        if candidate not in SIZE_PRESETS:
            candidate = "2048x2048"
        return candidate

    # ------------------------------------------------------------------ quota

    def _is_allowed_user(self, user_id: str, is_admin: bool) -> bool:
        whitelist = [str(item).strip() for item in (self._cfg("whitelist") or []) if str(item).strip()]
        if not whitelist:
            return True
        if is_admin and bool(self._cfg("admin_bypass")):
            return True
        return user_id in whitelist

    def _check_quota(self, user_id: str, is_admin: bool) -> None:
        if not self._is_allowed_user(user_id, is_admin):
            raise JimengError("你不在画图白名单中。")

        if is_admin and bool(self._cfg("admin_bypass")):
            return

        cooldown = max(0, self._cfg_int("cooldown", 10))
        if cooldown:
            last = self._cooldown.get(user_id, 0.0)
            remain = cooldown - (time.monotonic() - last)
            if remain > 0:
                raise JimengError(f"冷却中，请 {int(remain) + 1} 秒后再试。")

        today = date.today().isoformat()
        usage = self._usage.setdefault(today, {"total": 0, "users": {}})
        daily_limit = max(0, self._cfg_int("daily_limit", 50))
        user_limit = max(0, self._cfg_int("user_daily_limit", 10))
        if daily_limit and int(usage.get("total", 0)) >= daily_limit:
            raise JimengError(f"今日总生成次数已达上限（{daily_limit} 次）。")
        if user_limit and int(usage.get("users", {}).get(user_id, 0)) >= user_limit:
            raise JimengError(f"你今日的生成次数已达上限（{user_limit} 次）。")

    def _consume_quota(self, user_id: str, is_admin: bool) -> None:
        if is_admin and bool(self._cfg("admin_bypass")):
            return
        today = date.today().isoformat()
        usage = self._usage.setdefault(today, {"total": 0, "users": {}})
        usage["total"] = int(usage.get("total", 0)) + 1
        users = usage.setdefault("users", {})
        users[user_id] = int(users.get(user_id, 0)) + 1
        self._cooldown[user_id] = time.monotonic()
        # 只保留最近 30 天
        for key in list(self._usage):
            if key < (date.today() - timedelta(days=30)).isoformat():
                self._usage.pop(key, None)
        _save_json(self._usage_path, self._usage)

    # ---------------------------------------------------------------- history

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record.get("id"),
            "prompt": record.get("prompt"),
            "size": record.get("size"),
            "status": record.get("status"),
            "error": record.get("error") or "",
            "created_at": record.get("created_at"),
            "duration_ms": record.get("duration_ms") or 0,
            "source": record.get("source") or "",
            "user_name": record.get("user_name") or "",
            "images": len(record.get("files") or []),
            "inputs": int(record.get("inputs") or 0),
        }

    async def _append_history(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self._history.insert(0, record)
            max_history = max(20, self._cfg_int("max_history", 200))
            removed = self._history[max_history:]
            self._history = self._history[:max_history]
            _save_json(self._history_path, self._history)
        for item in removed:
            self._delete_files(item)

    def _delete_files(self, record: dict[str, Any]) -> None:
        for file_info in record.get("files") or []:
            for key in ("image", "thumb"):
                name = file_info.get(key)
                if not name:
                    continue
                path = self._images_dir / name
                try:
                    if path.exists():
                        path.unlink()
                except OSError as exc:
                    logger.warning("[jimeng] 删除图片 %s 失败: %s", path, exc)

    def _cleanup_old(self) -> None:
        keep_days = max(0, self._cfg_int("keep_days", 7))
        if keep_days == 0:
            return
        threshold = _now() - timedelta(days=keep_days)
        kept: list[dict[str, Any]] = []
        for record in self._history:
            try:
                created = datetime.fromisoformat(str(record.get("created_at")))
            except (TypeError, ValueError):
                created = threshold - timedelta(days=1)
            if created < threshold:
                self._delete_files(record)
            else:
                kept.append(record)
        if len(kept) != len(self._history):
            self._history = kept
            _save_json(self._history_path, self._history)

        # 清理过期的参考图（含 WebUI 上传）
        for path in self._inputs_dir.glob("*"):
            try:
                if not path.is_file():
                    continue
                mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
                if mtime < threshold:
                    path.unlink()
            except OSError:
                continue

    async def _find_record(self, record_id: str) -> dict[str, Any] | None:
        async with self._lock:
            for record in self._history:
                if record.get("id") == record_id:
                    return record
        return None

    # -------------------------------------------------------------- generation

    async def _call(self, fn, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(fn, payload)
        except JimengError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise JimengError(self._friendly_error(exc)) from exc

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        text = str(exc)
        if "InvalidAuthorization" in text or "Invalid Authorization" in text:
            return "鉴权失败：请检查 AccessKey ID / SecretAccessKey，以及服务器时间是否准确。"
        if "50413" in text or "Text Risk Not Pass" in text:
            return "提示词未通过内容审核，请换一种描述后重试。"
        if "unsupported protocol scheme" in text or "50220" in text:
            return (
                "参考图必须是公网 http(s) URL；即梦 4.6 不接受 base64/data URI。"
                "请在 AstrBot 系统设置里配置 callback_api_base，或直接使用图片链接。"
            )
        if "Request Has Reached API Limit" in text or "Too Many Requests" in text or "HTTP 429" in text:
            return "调用过于频繁，已触发服务端限流，请稍后再试。"
        if "AccessDenied" in text or "HTTP 403" in text:
            return "没有权限：请在控制台确认已开通「即梦AI-图片生成4.6」，并检查子账号策略。"
        return text[:300] or "未知错误"

    def _check_response(self, resp: dict[str, Any]) -> dict[str, Any]:
        code = resp.get("code")
        if str(code) != "10000":
            message = resp.get("message") or "未知错误"
            request_id = resp.get("request_id") or ""
            if "Text Risk Not Pass" in str(message):
                raise JimengError("提示词未通过内容审核，请换一种描述后重试。")
            low = str(message).lower()
            if any(
                key in low
                for key in (
                    "image_url",
                    "download image",
                    "invalid image",
                    "image format",
                    "image size",
                    "image not",
                    "unsupported protocol",
                    "scheme",
                )
            ) or str(code) == "50220":
                raise JimengError(
                    f"参考图无法被服务端读取（{code}: {message}）。"
                    "即梦 4.6 只接受公网 http(s) URL，请配置 callback_api_base 或改用图片链接。"
                )
            raise JimengError(f"{code}: {message}" + (f"（request_id: {request_id}）" if request_id else ""))
        return resp.get("data") or {}

    # ------------------------------------------------------------ input images

    @staticmethod
    def _component_image_ref(component) -> str:
        """从 AstrBot 的 Image 组件里取出一个可用的图片引用。"""
        for attr in ("url", "file", "path"):
            value = getattr(component, attr, "")
            if not isinstance(value, str):
                continue
            value = value.strip()
            if not value:
                continue
            if value.startswith(("http://", "https://")):
                return value
            if value.startswith("base64://"):
                return value
            if value.startswith("file://"):
                from urllib.parse import unquote, urlparse

                local = unquote(urlparse(value).path)
                if re.match(r"^/[A-Za-z]:", local):
                    local = local[1:]
                if os.path.exists(local):
                    return local
            if os.path.exists(value):
                return value
        return ""

    def _collect_event_images(self, event: AstrMessageEvent) -> list[str]:
        """收集当前消息与引用消息中的图片引用（URL / 本地路径）。"""
        if not bool(self._cfg("enable_img2img")):
            return []
        limit = max(1, min(14, self._cfg_int("max_input_images", 4)))
        refs: list[str] = []
        seen: set[str] = set()

        def add(component) -> None:
            ref = self._component_image_ref(component)
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)

        try:
            messages = event.get_messages()
        except Exception:  # noqa: BLE001
            messages = []
        for component in messages:
            if isinstance(component, Comp.Image):
                add(component)
            elif isinstance(component, Comp.Reply):
                for sub in getattr(component, "chain", None) or []:
                    if isinstance(sub, Comp.Image):
                        add(sub)
        return refs[:limit]

    def _normalize_local_image(self, source: str | Path) -> Path:
        """把本地图片统一成 API 可接受的大小与格式。"""
        path = Path(source)
        with PILImage.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((4096, 4096))
            target = self._inputs_dir / f"in_{uuid.uuid4().hex[:12]}.jpg"
            image.save(target, "JPEG", quality=92)
        return target

    async def _to_public_image_ref(self, path: Path) -> str:
        """把本地图片注册到 AstrBot 文件服务，拿到公网可访问的 URL。

        即梦 4.6 的 image_urls 只接受 http(s) URL（实测 data URI 会报
        unsupported protocol scheme），所以本地图片必须能变成公网链接。
        """
        try:
            url = await Comp.Image.fromFileSystem(str(path)).register_to_file_service()
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
        except Exception as exc:  # noqa: BLE001
            logger.debug("[jimeng] 文件服务不可用: %s", exc)
        raise JimengError(
            "参考图需要公网可访问的 URL。请在 AstrBot「系统设置 → 网络」配置可被外网访问的 "
            "callback_api_base，或改为发送图片链接（即梦 4.6 不接受 base64/data URI）。"
        )

    async def _prepare_input_images(self, refs: list[str]) -> list[str]:
        if not refs:
            return []
        if not bool(self._cfg("enable_img2img")):
            raise JimengError("图生图已在设置中关闭。")
        limit = max(1, min(14, self._cfg_int("max_input_images", 4)))
        prepared: list[str] = []
        for ref in refs[:limit]:
            ref = (ref or "").strip()
            if not ref:
                continue
            if ref.startswith(("http://", "https://")):
                prepared.append(ref)
                continue
            try:
                if ref.startswith("base64://"):
                    raw = base64.b64decode(ref[len("base64://") :])
                    source = self._inputs_dir / f"b64_{uuid.uuid4().hex[:12]}.png"
                    source.write_bytes(raw)
                else:
                    source = Path(ref)
                normalized = await asyncio.to_thread(self._normalize_local_image, source)
                prepared.append(await self._to_public_image_ref(normalized))
            except JimengError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[jimeng] 参考图处理失败 %s: %s", ref[:80], exc)
        if not prepared:
            raise JimengError(
                "参考图读取失败。请确认图片存在，且能通过公网 URL 访问（即梦 4.6 不接受 base64）。"
            )
        return prepared

    async def _submit(
        self,
        prompt: str,
        width: int,
        height: int,
        image_urls: list[str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "req_key": self._req_key(),
            "prompt": prompt,
            "scale": self._cfg_int("scale", 50),
            "force_single": bool(self._cfg("force_single")),
            "min_ratio": 1 / 3,
            "max_ratio": 3,
        }
        if image_urls:
            # 图生图：只给面积，让模型结合参考图决定宽高比
            payload["size"] = width * height
            payload["image_urls"] = image_urls
        else:
            payload["width"] = width
            payload["height"] = height
        resp = await self._call(self._client().submit, payload)
        data = self._check_response(resp)
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise JimengError("提交成功但没有返回 task_id，请稍后重试。")
        return task_id

    def _query_payload(self, task_id: str, *, return_url: bool = True) -> dict[str, Any]:
        req_json = {
            "return_url": return_url,
            "logo_info": {
                "add_logo": bool(self._cfg("watermark")),
                "position": 0,
                "language": 0,
                "opacity": 1,
            },
        }
        return {
            "req_key": self._req_key(),
            "task_id": task_id,
            "req_json": json.dumps(req_json, ensure_ascii=False),
        }

    async def _wait(self, task_id: str) -> dict[str, Any]:
        timeout = max(30, self._cfg_int("timeout", 180))
        interval = max(0.5, self._cfg_float("poll_interval", 2.0))
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(interval)
            resp = await self._call(self._client().fetch, self._query_payload(task_id))
            data = self._check_response(resp)
            status = str(data.get("status") or "")
            if status == "done":
                return data
            if status in ("not_found", "expired"):
                raise JimengError(f"任务已失效（{status}），请重新生成。")
        raise JimengError(f"生成超时（超过 {timeout} 秒），请稍后在历史中查看或重试。")

    @staticmethod
    def _http_get(url: str) -> bytes:
        import requests

        resp = requests.get(url, timeout=90)
        resp.raise_for_status()
        return resp.content

    def _save_image(self, content: bytes, record_id: str, index: int) -> dict[str, str]:
        image_name = f"{record_id}_{index}.png"
        image_path = self._images_dir / image_name
        image_path.write_bytes(content)

        thumb_name = f"{record_id}_{index}_thumb.jpg"
        thumb_path = self._images_dir / thumb_name
        try:
            with PILImage.open(image_path) as img:
                img = img.convert("RGB")
                img.thumbnail((480, 480))
                img.save(thumb_path, "JPEG", quality=82)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[jimeng] 生成缩略图失败: %s", exc)
            thumb_name = ""
        return {"image": image_name, "thumb": thumb_name}

    async def _collect_images(self, data: dict[str, Any], record_id: str) -> list[dict[str, str]]:
        files: list[dict[str, str]] = []
        urls = [str(url) for url in (data.get("image_urls") or []) if str(url).strip()]
        contents: list[bytes] = []
        for url in urls[:MAX_IMAGES_PER_TASK]:
            try:
                contents.append(await asyncio.to_thread(self._http_get, url))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[jimeng] 下载图片失败 %s: %s", url, exc)
        if not contents:
            for b64 in (data.get("binary_data_base64") or [])[:MAX_IMAGES_PER_TASK]:
                try:
                    contents.append(base64.b64decode(str(b64)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[jimeng] 解码 base64 失败: %s", exc)
        if not contents:
            raise JimengError("生成完成但没有拿到图片，请稍后重试。")
        for index, content in enumerate(contents):
            files.append(await asyncio.to_thread(self._save_image, content, record_id, index))
        return files

    async def _generate(
        self,
        prompt: str,
        *,
        size: str | None = None,
        input_images: list[str] | None = None,
        user_id: str = "web",
        user_name: str = "WebUI",
        source: str = "web",
        is_admin: bool = False,
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise JimengError("提示词不能为空。")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise JimengError(f"提示词过长（{len(prompt)} 字），请控制在 {MAX_PROMPT_CHARS} 字以内。")
        if not self._configured():
            raise JimengError("尚未配置 AccessKey ID / SecretAccessKey，请到插件设置页填写。")

        size_key = self._size_key(size)
        width, height = _parse_size(size_key)
        record_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        record: dict[str, Any] = {
            "id": record_id,
            "prompt": prompt,
            "size": size_key,
            "req_key": self._req_key(),
            "status": "running",
            "created_at": _iso(),
            "duration_ms": 0,
            "source": source,
            "user_id": user_id,
            "user_name": user_name,
            "files": [],
            "inputs": 0,
        }

        try:
            prepared_images = await self._prepare_input_images(list(input_images or []))
            record["inputs"] = len(prepared_images)
            async with self._sem:
                task_id = await self._submit(prompt, width, height, prepared_images)
                data = await self._wait(task_id)
                try:
                    files = await self._collect_images(data, record_id)
                except JimengError:
                    # 图片链接下载失败时，退回 base64 再取一次
                    fallback = await self._call(
                        self._client().fetch,
                        self._query_payload(task_id, return_url=False),
                    )
                    files = await self._collect_images(
                        self._check_response(fallback), record_id
                    )
            record.update(
                status="ok",
                files=files,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc) if isinstance(exc, JimengError) else self._friendly_error(exc)
            record.update(
                status="failed",
                error=message,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await self._append_history(record)
            raise JimengError(message) from exc

        await self._append_history(record)
        self._consume_quota(user_id, is_admin)
        return record

    async def _send_record(self, umo: str, record: dict[str, Any]) -> None:
        chain = MessageChain().message(
            f"🎨 画好了 · {SIZE_PRESETS.get(record['size'], record['size'])}\n"
            f"提示词：{record['prompt'][:120]}"
        )
        for file_info in record.get("files") or []:
            path = self._images_dir / file_info["image"]
            if path.exists():
                chain.file_image(str(path))
        await self.context.send_message(umo, chain)

    async def _background_generate(
        self,
        umo: str,
        prompt: str,
        input_images: list[str],
        user_id: str,
        user_name: str,
        is_admin: bool,
    ) -> None:
        try:
            record = await self._generate(
                prompt,
                input_images=input_images,
                user_id=user_id,
                user_name=user_name,
                source="tool",
                is_admin=is_admin,
            )
            await self._send_record(umo, record)
        except Exception as exc:  # noqa: BLE001
            try:
                await self.context.send_message(
                    umo, MessageChain().message(f"❌ 生成失败：{exc}")
                )
            except Exception as send_exc:  # noqa: BLE001
                logger.warning("[jimeng] 发送失败提示失败: %s", send_exc)

    # ---------------------------------------------------------------- handlers

    @filter.command("画图")
    async def cmd_draw(self, event: AstrMessageEvent):
        """用「即梦AI-图片生成4.6」画一张图：/画图 一只在雨里的猫"""
        if not bool(self._cfg("enable_command")):
            return

        match = re.match(r"^[/／]?\s*画图\s*(.*)$", event.message_str or "", re.S)
        prompt = (match.group(1).strip() if match else "")
        if not prompt:
            yield event.plain_result("用法：/画图 一只在窗边晒太阳的橘猫，插画风")
            return

        user_id = event.get_sender_id() or "unknown"
        is_admin = bool(event.is_admin())
        try:
            self._check_quota(user_id, is_admin)
        except JimengError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return

        yield event.plain_result("🎨 正在生成，请稍候…")
        input_images = self._collect_event_images(event)
        try:
            record = await self._generate(
                prompt,
                input_images=input_images,
                user_id=user_id,
                user_name=event.get_sender_name() or user_id,
                source="command",
                is_admin=is_admin,
            )
        except JimengError as exc:
            yield event.plain_result(f"❌ 生成失败：{exc}")
            return

        chain = [
            Comp.Plain(f"🎨 {SIZE_PRESETS.get(record['size'], record['size'])}\n")
        ]
        for file_info in record.get("files") or []:
            path = self._images_dir / file_info["image"]
            if path.exists():
                chain.append(Comp.Image.fromFileSystem(str(path)))
        yield event.chain_result(chain)

    @filter.llm_tool(name="jimeng_draw_image")
    async def tool_jimeng_draw_image(self, event: AstrMessageEvent, prompt: str):
        """根据文字描述生成并发送一张图片。

        当用户想要看到图片（插画、照片、表情包、海报、示意图、头像等），
        或明确要求画图/生成图片、改图/换背景时调用。纯聊天、知识问答、文字创作不要调用。
        如果用户同时发了图片（或引用了一张图片），插件会自动把它作为参考图做图生图。

        Args:
            prompt(string): 完整的画面描述，包含主体、场景、风格、光线、构图；改图时说明要改什么、保留什么
        """
        if not bool(self._cfg("enable_llm_tool")):
            return "画图工具已关闭。"

        prompt = (prompt or "").strip()
        if not prompt:
            return "提示词为空，无法生成。"
        if len(prompt) > MAX_PROMPT_CHARS:
            prompt = prompt[:MAX_PROMPT_CHARS]

        user_id = event.get_sender_id() or "unknown"
        user_name = event.get_sender_name() or user_id
        is_admin = bool(event.is_admin())
        try:
            self._check_quota(user_id, is_admin)
        except JimengError as exc:
            return str(exc)

        input_images = self._collect_event_images(event)

        if bool(self._cfg("tool_wait")):
            try:
                record = await self._generate(
                    prompt,
                    input_images=input_images,
                    user_id=user_id,
                    user_name=user_name,
                    source="tool",
                    is_admin=is_admin,
                )
            except JimengError as exc:
                return f"生成失败：{exc}"
            chain = MessageChain()
            for file_info in record.get("files") or []:
                path = self._images_dir / file_info["image"]
                if path.exists():
                    chain.file_image(str(path))
            await event.send(chain)
            return "图片已生成并发送给用户。"

        umo = event.unified_msg_origin
        task = asyncio.create_task(
            self._background_generate(
                umo, prompt, input_images, user_id, user_name, is_admin
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return "已开始生成图片，画好后会直接发送到当前会话，请告诉用户稍等片刻。"

    @filter.on_llm_request()
    async def inject_draw_hint(self, event: AstrMessageEvent, req) -> None:
        if not bool(self._cfg("enable_llm_tool")) or not bool(self._cfg("inject_hint")):
            return
        if not self._is_allowed_user(
            event.get_sender_id() or "unknown", bool(event.is_admin())
        ):
            return
        current = getattr(req, "system_prompt", "") or ""
        if "[画图能力]" in current:
            return
        req.system_prompt = current + DRAW_HINT

    async def terminate(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()

    # --------------------------------------------------------------- web apis

    def _config_public(self) -> dict[str, Any]:
        return {
            "access_key_id": _mask(str(self._cfg("access_key_id") or "")),
            "secret_access_key": "******" if str(self._cfg("secret_access_key") or "").strip() else "",
            "configured": self._configured(),
            "req_key": self._req_key(),
            "req_key_label": REQ_KEYS.get(self._req_key(), self._req_key()),
            "size_preset": self._size_key(),
            "scale": self._cfg_int("scale", 50),
            "force_single": bool(self._cfg("force_single")),
            "enable_img2img": bool(self._cfg("enable_img2img")),
            "max_input_images": self._cfg_int("max_input_images", 4),
            "watermark": bool(self._cfg("watermark")),
            "timeout": self._cfg_int("timeout", 180),
            "poll_interval": self._cfg_float("poll_interval", 2.0),
            "max_concurrency": self._cfg_int("max_concurrency", 2),
            "enable_llm_tool": bool(self._cfg("enable_llm_tool")),
            "tool_wait": bool(self._cfg("tool_wait")),
            "inject_hint": bool(self._cfg("inject_hint")),
            "enable_command": bool(self._cfg("enable_command")),
            "daily_limit": self._cfg_int("daily_limit", 50),
            "user_daily_limit": self._cfg_int("user_daily_limit", 10),
            "cooldown": self._cfg_int("cooldown", 10),
            "whitelist": [str(x) for x in (self._cfg("whitelist") or [])],
            "admin_bypass": bool(self._cfg("admin_bypass")),
            "keep_days": self._cfg_int("keep_days", 7),
            "max_history": self._cfg_int("max_history", 200),
        }

    async def api_overview(self):
        today = date.today().isoformat()
        total = len(self._history)
        ok = sum(1 for item in self._history if item.get("status") == "ok")
        today_records = [item for item in self._history if str(item.get("created_at", "")).startswith(today)]
        durations = [int(item.get("duration_ms") or 0) for item in self._history if item.get("status") == "ok"]
        return json_response(
            {
                "configured": self._configured(),
                "version": PLUGIN_VERSION,
                "stats": {
                    "today": len(today_records),
                    "total": total,
                    "success_rate": round(ok / total * 100, 1) if total else 0,
                    "avg_ms": int(sum(durations) / len(durations)) if durations else 0,
                },
                "recent": [self._public_record(item) for item in self._history[:8]],
                "summary": {
                    "req_key_label": REQ_KEYS.get(self._req_key(), self._req_key()),
                    "size_label": SIZE_PRESETS.get(self._size_key(), self._size_key()),
                    "daily_limit": self._cfg_int("daily_limit", 50),
                    "user_daily_limit": self._cfg_int("user_daily_limit", 10),
                    "enable_llm_tool": bool(self._cfg("enable_llm_tool")),
                },
                "server_time": _iso(),
            }
        )

    async def api_config(self):
        return json_response(
            {
                "config": self._config_public(),
                "models": [{"value": key, "label": label} for key, label in REQ_KEYS.items()],
                "sizes": [{"value": key, "label": label} for key, label in SIZE_PRESETS.items()],
                "size_hint": SIZE_HINT,
            }
        )

    async def api_config_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("无效的配置数据")

        patch: dict[str, Any] = {}
        if payload.get("clear_credentials"):
            patch["access_key_id"] = ""
            patch["secret_access_key"] = ""
        else:
            # 空值/掩码表示“保持不变”，避免保存其它设置时误清空凭证
            if "access_key_id" in payload:
                ak = str(payload.get("access_key_id") or "").strip()
                if ak and "****" not in ak:
                    patch["access_key_id"] = ak
            if "secret_access_key" in payload:
                sk = str(payload.get("secret_access_key") or "").strip()
                if sk and sk != "******":
                    patch["secret_access_key"] = sk

        if "req_key" in payload:
            req_key = str(payload.get("req_key") or "").strip()
            if req_key not in REQ_KEYS:
                return error_response("不支持的 req_key")
            patch["req_key"] = req_key

        if "size_preset" in payload:
            size = str(payload.get("size_preset") or "").strip()
            if size not in SIZE_PRESETS:
                return error_response("不支持的尺寸")
            patch["size_preset"] = size

        int_fields = {
            "scale": (1, 100),
            "max_input_images": (1, 14),
            "timeout": (30, 600),
            "max_concurrency": (1, 8),
            "daily_limit": (0, 1000),
            "user_daily_limit": (0, 200),
            "cooldown": (0, 600),
            "keep_days": (0, 90),
            "max_history": (20, 2000),
        }
        for key, (low, high) in int_fields.items():
            if key in payload:
                try:
                    value = int(payload[key])
                except (TypeError, ValueError):
                    return error_response(f"{key} 必须是整数")
                patch[key] = max(low, min(high, value))

        if "poll_interval" in payload:
            try:
                value = float(payload["poll_interval"])
            except (TypeError, ValueError):
                return error_response("poll_interval 必须是数字")
            patch["poll_interval"] = max(0.5, min(10.0, value))

        for key in (
            "force_single",
            "enable_img2img",
            "watermark",
            "enable_llm_tool",
            "tool_wait",
            "inject_hint",
            "enable_command",
            "admin_bypass",
        ):
            if key in payload:
                patch[key] = bool(payload[key])

        if "whitelist" in payload:
            raw = payload.get("whitelist") or []
            if isinstance(raw, str):
                raw = re.split(r"[,，\s]+", raw)
            patch["whitelist"] = [str(item).strip() for item in raw if str(item).strip()]

        try:
            await self.config.save_config_async(patch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[jimeng] 保存配置失败")
            return error_response(f"保存失败：{exc}")

        self._sem = asyncio.Semaphore(max(1, self._cfg_int("max_concurrency", 2)))
        self._cleanup_old()
        return json_response({"saved": True, "config": self._config_public()})

    def _resolve_web_inputs(self, payload: dict[str, Any]) -> list[str]:
        """把控制台上传的 image_id / image_url 解析成可提交的引用。"""
        refs: list[str] = []
        ids: list[str] = []
        single = str(payload.get("image_id") or "").strip()
        if single:
            ids.append(single)
        for item in payload.get("image_ids") or []:
            value = str(item or "").strip()
            if value:
                ids.append(value)
        for image_id in ids[:14]:
            if not re.fullmatch(r"[0-9a-f]{12}", image_id):
                continue
            matches = sorted(self._inputs_dir.glob(f"upload_{image_id}.*"))
            if matches:
                refs.append(str(matches[0]))
        url = str(payload.get("image_url") or "").strip()
        if url.startswith(("http://", "https://")) and url not in refs:
            refs.append(url)
        return refs

    @staticmethod
    def _validate_image_file(path: Path) -> None:
        with PILImage.open(path) as image:
            image.verify()

    @staticmethod
    def _image_preview(path: Path, size: int = 240) -> str:
        with PILImage.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((size, size))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    async def api_upload(self):
        if not bool(self._cfg("enable_img2img")):
            return error_response("图生图已在设置中关闭")
        try:
            files = await request.files()
        except Exception as exc:  # noqa: BLE001
            return error_response(f"读取上传内容失败：{exc}")
        upload = files.get("file") if files else None
        if upload is None:
            return error_response("缺少文件字段 file")
        suffix = Path(str(getattr(upload, "filename", "") or "")).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            return error_response("仅支持 jpg / png / webp / bmp 图片")

        image_id = uuid.uuid4().hex[:12]
        target = self._inputs_dir / f"upload_{image_id}{suffix}"
        try:
            await upload.save(target)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"保存图片失败：{exc}")
        try:
            if target.stat().st_size > 15 * 1024 * 1024:
                target.unlink(missing_ok=True)
                return error_response("图片不能超过 15MB")
            await asyncio.to_thread(self._validate_image_file, target)
            # 4.6 只认公网 URL：上传时就确认文件服务可用，避免生成时才报错
            try:
                await Comp.Image.fromFileSystem(str(target)).register_to_file_service()
            except Exception as exc:  # noqa: BLE001
                target.unlink(missing_ok=True)
                return error_response(
                    "本地上传的参考图需要公网访问：请先在 AstrBot「系统设置 → 网络」配置 "
                    "callback_api_base，或直接使用图片 URL。"
                )
            preview = await asyncio.to_thread(self._image_preview, target)
        except Exception as exc:  # noqa: BLE001
            target.unlink(missing_ok=True)
            return error_response(f"不是有效的图片：{exc}")
        return json_response(
            {"id": image_id, "name": getattr(upload, "filename", "") or target.name, "preview": preview}
        )

    async def api_generate(self):
        payload = await request.json(default={}) or {}
        prompt = str(payload.get("prompt") or "").strip()
        size = payload.get("size")
        if not prompt:
            return error_response("提示词不能为空")
        if not self._configured():
            return error_response("请先在「设置」中填写 AccessKey ID 和 SecretAccessKey")
        input_images = self._resolve_web_inputs(payload)

        task_id = uuid.uuid4().hex[:12]
        username = request.username or "WebUI"
        task = {
            "id": task_id,
            "status": "running",
            "prompt": prompt,
            "created_at": _iso(),
            "result": None,
            "error": "",
        }
        self._tasks[task_id] = task
        for old_id in list(self._tasks)[:-50]:
            self._tasks.pop(old_id, None)

        async def runner() -> None:
            try:
                record = await self._generate(
                    prompt,
                    size=size,
                    input_images=input_images,
                    user_id="web",
                    user_name=username,
                    source="web",
                    is_admin=True,
                )
                task["status"] = "done"
                task["result"] = self._public_record(record)
            except Exception as exc:  # noqa: BLE001
                task["status"] = "failed"
                task["error"] = str(exc)

        handle = asyncio.create_task(runner())
        self._bg_tasks.add(handle)
        handle.add_done_callback(self._bg_tasks.discard)
        return json_response({"task_id": task_id})

    async def api_task(self):
        task_id = str(request.query.get("id") or "").strip()
        task = self._tasks.get(task_id)
        if not task:
            return error_response("任务不存在或已过期", status_code=404)
        return json_response(task)

    async def api_history(self):
        try:
            page = max(1, request.query.get("page", 1, type=int))
            page_size = max(1, min(50, request.query.get("page_size", 12, type=int)))
        except (TypeError, ValueError):
            page, page_size = 1, 12
        start = (page - 1) * page_size
        records = self._history[start : start + page_size]
        return json_response(
            {
                "items": [self._public_record(item) for item in records],
                "total": len(self._history),
                "page": page,
                "page_size": page_size,
            }
        )

    async def api_history_delete(self):
        payload = await request.json(default={})
        record_id = str((payload or {}).get("id") or "").strip()
        if not record_id:
            return error_response("缺少记录 ID")
        async with self._lock:
            target = next((item for item in self._history if item.get("id") == record_id), None)
            if target:
                self._history.remove(target)
                _save_json(self._history_path, self._history)
        if target:
            self._delete_files(target)
        return json_response({"deleted": bool(target)})

    async def api_image(self):
        record_id = str(request.query.get("id") or "").strip()
        index = request.query.get("index", 0, type=int) or 0
        full = request.query.get("full", "0") in ("1", "true", "True")
        record = await self._find_record(record_id)
        if not record:
            return error_response("记录不存在", status_code=404)
        files = record.get("files") or []
        if index < 0 or index >= len(files):
            return error_response("图片不存在", status_code=404)
        name = files[index].get("image" if full else "thumb") or files[index].get("image")
        path = self._images_dir / str(name)
        if not path.exists():
            return error_response("文件已被清理", status_code=404)
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        try:
            data = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()
        except OSError as exc:
            return error_response(f"读取失败：{exc}", status_code=500)
        return json_response({"data": f"data:{mime};base64,{data}", "mime": mime})

    async def api_presets(self):
        return json_response({"items": self._presets})

    async def api_presets_save(self):
        payload = await request.json(default={})
        name = str((payload or {}).get("name") or "").strip()
        prompt = str((payload or {}).get("prompt") or "").strip()
        size = str((payload or {}).get("size") or "2048x2048").strip()
        preset_id = str((payload or {}).get("id") or "").strip()
        if not name or not prompt:
            return error_response("名称和提示词都不能为空")
        if size not in SIZE_PRESETS:
            size = "2048x2048"
        if preset_id:
            target = next((item for item in self._presets if item.get("id") == preset_id), None)
            if not target:
                return error_response("预设不存在", status_code=404)
            target.update({"name": name, "prompt": prompt, "size": size})
        else:
            self._presets.append(
                {"id": uuid.uuid4().hex[:8], "name": name, "prompt": prompt, "size": size}
            )
        _save_json(self._presets_path, self._presets)
        return json_response({"items": self._presets})

    async def api_presets_delete(self):
        payload = await request.json(default={})
        preset_id = str((payload or {}).get("id") or "").strip()
        self._presets = [item for item in self._presets if item.get("id") != preset_id]
        _save_json(self._presets_path, self._presets)
        return json_response({"items": self._presets})

