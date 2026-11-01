"""鱼鱼找鸡：引用一张图发 /鱼鱼找鸡，反查图里 coser 的社媒账号。

识图引擎来自开源项目「鱼鱼找鸡 v1.0.0」（MIT，作者：小鲸鱼），已删掉读本机文件、
DeepSeek 搜索、公网图床、VPN/系统代理等功能。本插件只做三件事：
取聊天里的图片 → 交给 engine/cli.mjs 反查 → 把结果发回聊天。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Image, Reply

PLUGIN_NAME = "astrbot_plugin_fish_find_chicken"
PLUGIN_DIR = Path(__file__).resolve().parent
CLI_PATH = PLUGIN_DIR / "engine" / "cli.mjs"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
KNOWN_ENGINES = ("lens", "saucenao", "tracemoe", "iqdb", "baidu")
DEFAULT_ENGINES = ["lens", "saucenao", "tracemoe"]
FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
SYSTEM_BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
HELP_TEXT = (
    "鱼鱼找鸡：反查图片里 coser 的社媒账号\n"
    "用法：引用（回复）一条带图的消息，发送 /鱼鱼找鸡\n"
    "也可以把图片和 /鱼鱼找鸡 放在同一条消息里发。\n"
    "一次大约 20 秒到 2 分钟，结果仅供参考，请尊重他人隐私。"
)


def _sniff_image(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return ""


def _str_list(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,，;；]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _is_fake_ipv4(text: str) -> bool:
    try:
        return ipaddress.ip_address(text) in FAKE_IP_NETS[0]
    except ValueError:
        return False


def _is_public_ip(text: str, allow_fake_ip: bool) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    # Clash/mihomo 的 fake-ip：所有域名都解析到 198.18.0.0/15（IPv6 是 2001:2::/48），真实目标由代理决定
    if allow_fake_ip and any(ip in net for net in FAKE_IP_NETS if net.version == ip.version):
        return True
    return ip.is_global


def _astrbot_temp_dirs() -> list[Path]:
    """AstrBot 自己存放已下载聊天图片的临时目录（data/temp）。只有这里的文件允许读取。"""
    dirs: list[Path] = []
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        dirs.append(Path(get_astrbot_temp_path()))
    except Exception:  # noqa: BLE001  旧版本没有这个函数
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            dirs.append(Path(get_astrbot_data_path()) / "temp")
        except Exception:  # noqa: BLE001
            pass
    resolved: list[Path] = []
    for d in dirs:
        try:
            resolved.append(d.resolve(strict=True))
        except OSError:
            continue
    return resolved


def _ref_kind(ref: str) -> str:
    low = ref.lower()
    if low.startswith(("http://", "https://")):
        return "http"
    if low.startswith("base64://"):
        return "base64"
    return "local"


class FishFindChicken(Star):
    """引用图片 + /鱼鱼找鸡，只有被允许的人能用。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.tmp_dir = self.data_dir / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._last_use: dict[str, float] = {}
        self._clean_tmp()

    async def terminate(self):
        self._clean_tmp()

    # ── 配置 ──────────────────────────────────────────────────────────────
    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    def _int_cfg(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _engines(self) -> list[str]:
        engines = [e.lower() for e in _str_list(self._cfg("engines", DEFAULT_ENGINES))]
        engines = [e for e in dict.fromkeys(engines) if e in KNOWN_ENGINES]
        return engines or list(DEFAULT_ENGINES)

    # ── 权限 ──────────────────────────────────────────────────────────────
    def _allowed(self, event: AstrMessageEvent) -> bool:
        if event.is_admin():
            return True
        mode = str(self._cfg("access_mode", "admin")).strip().lower()
        if mode == "all":
            return True
        if mode == "whitelist":
            sender = str(event.get_sender_id() or "")
            group = str(event.get_group_id() or "")
            if sender and sender in _str_list(self._cfg("allowed_users", [])):
                return True
            if group and group in _str_list(self._cfg("allowed_groups", [])):
                return True
        return False

    # ── 取图 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _image_refs(components) -> list[str]:
        refs: list[str] = []
        for comp in components or []:
            if isinstance(comp, Image):
                # AstrBot 预处理会把聊天图片下载到 data/temp，并把 url/file/path 改成本地路径
                for value in (getattr(comp, "url", None), getattr(comp, "file", None),
                              getattr(comp, "path", None)):
                    if isinstance(value, str) and value and value not in refs:
                        refs.append(value)
        return refs

    async def _collect_refs(self, event: AstrMessageEvent) -> list[str]:
        """按顺序收集候选：本条消息的图 → 引用消息的图 → 让协议端重新取引用消息。"""
        messages = event.get_messages() or []
        refs = self._image_refs(messages)
        reply = next((c for c in messages if isinstance(c, Reply)), None)
        if reply is None:
            return refs
        for ref in self._image_refs(getattr(reply, "chain", None)):
            if ref not in refs:
                refs.append(ref)
        if any(self._usable(r) for r in refs):
            return refs
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action") or not getattr(reply, "id", None):
            return refs
        try:
            resp = await bot.call_action("get_msg", message_id=int(reply.id))
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[鱼鱼找鸡] get_msg 失败：{exc}")
            return refs
        message = resp.get("message") if isinstance(resp, dict) else None
        for seg in message if isinstance(message, list) else []:
            if isinstance(seg, dict) and seg.get("type") == "image":
                data = seg.get("data") or {}
                for value in (data.get("url"), data.get("file")):
                    if isinstance(value, str) and value and value not in refs:
                        refs.append(value)
        return refs

    def _local_temp_file(self, ref: str) -> Path | None:
        """只认 AstrBot 自己 data/temp 目录里的普通文件；其它任何本机路径一律拒绝。"""
        text = ref
        if text.lower().startswith("file://"):
            parsed = urlparse(text)
            text = unquote(parsed.path or "")
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", text):
                text = text[1:]
        if not text:
            return None
        try:
            real = Path(text).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        for root in _astrbot_temp_dirs():
            try:
                real.relative_to(root)
            except ValueError:
                continue
            return real if real.is_file() else None
        return None

    def _usable(self, ref: str) -> bool:
        kind = _ref_kind(ref)
        return kind in ("http", "base64") or self._local_temp_file(ref) is not None

    async def _check_host(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("图片地址不是 http(s)")
        host = parsed.hostname
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                           type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"图片域名解析失败：{exc}") from exc
        allow_fake = bool(self._cfg("allow_fake_ip", True))
        addresses = [info[4][0] for info in infos]
        if allow_fake and any(_is_fake_ipv4(a) for a in addresses):
            # fake-ip 时 IPv6 常被分到内网段（如 fdfe:dcba:9876::/64），只看 IPv4
            addresses = [a for a in addresses if ":" not in a]
        if not addresses or not all(_is_public_ip(a, allow_fake) for a in addresses):
            raise ValueError("图片地址指向内网/本机，已拒绝")

    async def _download(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            for _ in range(4):
                await self._check_host(url)
                async with session.get(url, allow_redirects=False,
                                       headers={"User-Agent": DOWNLOAD_UA}) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                        url = urljoin(url, resp.headers["Location"])
                        continue
                    if resp.status != 200:
                        raise ValueError(f"取图失败 HTTP {resp.status}")
                    if int(resp.headers.get("Content-Length") or 0) > MAX_IMAGE_BYTES:
                        raise ValueError("图片超过 20MB")
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        buf.extend(chunk)
                        if len(buf) > MAX_IMAGE_BYTES:
                            raise ValueError("图片超过 20MB")
                    return bytes(buf)
        raise ValueError("图片地址重定向次数过多")

    async def _fetch_image(self, refs: list[str]) -> bytes:
        last_error = "没找到能用的图片"
        # 已经被 AstrBot 下载到 data/temp 的图最省事，排前面；其次 base64、网址
        order = {"local": 0, "base64": 1, "http": 2}
        for ref in sorted(refs, key=lambda r: order[_ref_kind(r)]):
            try:
                kind = _ref_kind(ref)
                if kind == "base64":
                    raw = ref[len("base64://"):]
                    if len(raw) > MAX_IMAGE_BYTES * 4 // 3 + 16:
                        raise ValueError("图片超过 20MB")
                    data = base64.b64decode(raw, validate=False)
                elif kind == "http":
                    data = await self._download(ref)
                else:
                    # 只读 AstrBot 自己的 data/temp；其它本机路径一律不读（原版最危险的地方）
                    local = self._local_temp_file(ref)
                    if local is None:
                        continue
                    if local.stat().st_size > MAX_IMAGE_BYTES:
                        raise ValueError("图片超过 20MB")
                    data = await asyncio.to_thread(local.read_bytes)
            except (ValueError, OSError, binascii.Error, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = str(exc) or exc.__class__.__name__
                continue
            if not data:
                last_error = "图片是空的"
                continue
            if len(data) > MAX_IMAGE_BYTES:
                last_error = "图片超过 20MB"
                continue
            if not _sniff_image(data):
                last_error = "只支持 jpg/png/webp/gif 图片"
                continue
            return data
        kinds = ", ".join(_ref_kind(r) for r in refs) or "无"
        logger.info(f"[鱼鱼找鸡] 取图失败：{last_error}；候选类型：{kinds}")
        raise ValueError(last_error)

    # ── 运行环境 ──────────────────────────────────────────────────────────
    def _node_path(self) -> str:
        configured = str(self._cfg("node_path", "")).strip()
        if configured:
            return configured if Path(configured).is_file() else ""
        return shutil.which("node") or ""

    def _browser(self) -> tuple[str, dict[str, str]]:
        """返回（浏览器路径, 额外环境变量）。找不到就返回空串。"""
        configured = str(self._cfg("browser_path", "")).strip()
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured))
        else:
            candidates.extend(Path(p) for p in SYSTEM_BROWSERS)
            for name in ("chromium", "chromium-browser", "google-chrome", "msedge"):
                found = shutil.which(name)
                if found:
                    candidates.append(Path(found))
            # 借用其它插件（比如 blablalink）装好的 Playwright 浏览器
            plugin_data = self.data_dir.parent
            for pattern in ("*/browsers/chromium_headless_shell-*/chrome-linux/headless_shell",
                            "*/browsers/chromium-*/chrome-linux/chrome"):
                candidates.extend(sorted(plugin_data.glob(pattern), reverse=True))
        for exe in candidates:
            if not exe.is_file():
                continue
            return str(exe), self._native_lib_env(exe)
        return "", {}

    @staticmethod
    def _native_lib_env(exe: Path) -> dict[str, str]:
        """浏览器旁边若有插件私带的系统库（native_libs/usr/lib/*-linux-gnu），加进 LD_LIBRARY_PATH。"""
        for parent in list(exe.parents)[:6]:
            native_root = parent / "native_libs" / "usr" / "lib"
            if native_root.is_dir():
                dirs = [str(p) for p in native_root.glob("*-linux-gnu") if p.is_dir()]
                if dirs:
                    old = os.environ.get("LD_LIBRARY_PATH", "")
                    return {"LD_LIBRARY_PATH": os.pathsep.join(dirs + ([old] if old else []))}
                break
        return {}

    def _clean_tmp(self) -> None:
        try:
            for item in self.tmp_dir.iterdir():
                if item.is_file() and item.name.startswith("img-"):
                    item.unlink(missing_ok=True)
        except OSError:
            pass

    # ── 调引擎 ────────────────────────────────────────────────────────────
    async def _run_engine(self, image: bytes) -> dict:
        node = self._node_path()
        if not node:
            return {"ok": False, "error": "找不到 Node.js（需要 20 以上版本）。请安装 Node.js，或在插件配置里填写 node_path。"}

        engines = self._engines()
        notes: list[str] = []
        browser, browser_env = ("", {})
        if any(e in ("lens", "baidu") for e in engines):
            browser, browser_env = self._browser()
            if not browser:
                engines = [e for e in engines if e not in ("lens", "baidu")]
                notes.append("没找到 Chrome/Edge/Chromium，已跳过谷歌 Lens（最准的那个）")
                if not engines:
                    return {"ok": False, "error": "只配置了需要浏览器的引擎，但没找到浏览器，请在配置里填写 browser_path。"}

        timeout_sec = self._int_cfg("timeout_sec", 45, 10, 180)
        browser_count = sum(1 for e in engines if e in ("lens", "baidu"))
        total_sec = timeout_sec + 30 + browser_count * (max(timeout_sec, 30) + 30)

        img_path = self.tmp_dir / f"img-{secrets.token_hex(8)}.{_sniff_image(image)}"
        img_path.write_bytes(image)
        cfg = {
            "engines": engines,
            "timeoutMs": timeout_sec * 1000,
            "maxAccounts": self._int_cfg("max_accounts", 3, 1, 10),
            "strictConsensus": bool(self._cfg("strict_consensus", True)),
            "proxy": str(self._cfg("proxy", "")).strip(),
            "browserExe": browser,
        }
        env = os.environ.copy()
        env.update(browser_env)
        env["FISH_STATE_DIR"] = str(self.data_dir)
        env["FISH_TOTAL_TIMEOUT_SEC"] = str(total_sec)
        if bool(self._cfg("allow_fake_ip", True)):
            env["FISH_ALLOW_FAKE_IP"] = "1"
        else:
            env.pop("FISH_ALLOW_FAKE_IP", None)
        args = [node, str(CLI_PATH), "--image", str(img_path),
                "--config-b64", base64.b64encode(json.dumps(cfg).encode()).decode()]
        extra = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                 else {"start_new_session": True})
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(PLUGIN_DIR / "engine"), env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **extra)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=total_sec + 20)
            except asyncio.TimeoutError:
                await self._stop(proc)
                return {"ok": False, "error": f"查询超时（{total_sec} 秒），已停止"}
        finally:
            img_path.unlink(missing_ok=True)

        line = next((l for l in reversed(out.decode("utf-8", "replace").splitlines()) if l.startswith("{")), "")
        try:
            result = json.loads(line) if line else {}
        except json.JSONDecodeError:
            result = {}
        if not result:
            tail = err.decode("utf-8", "replace").strip()[-300:]
            logger.warning(f"[鱼鱼找鸡] 引擎没有输出结果，退出码 {proc.returncode}：{tail}")
            return {"ok": False, "error": "识图引擎异常退出，详情看 AstrBot 日志"}
        for item in result.get("logs") or []:
            logger.info(str(item))
        result["notes"] = notes
        return result

    @staticmethod
    async def _stop(proc) -> None:
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                proc.send_signal(signal.SIGTERM)  # 让引擎自己先关浏览器
                try:
                    await asyncio.wait_for(proc.wait(), timeout=8)
                    return
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass

    # ── 指令 ──────────────────────────────────────────────────────────────
    @filter.command("鱼鱼找鸡", alias={"找鸡"})
    async def fish_command(self, event: AstrMessageEvent):
        """引用一张图再发 /鱼鱼找鸡，反查 coser 的社媒账号。"""
        if not self._allowed(event):
            if bool(self._cfg("reply_when_denied", False)):
                yield event.plain_result("你没有使用鱼鱼找鸡的权限")
            return
        event.stop_event()

        text = (event.message_str or "").strip()
        if text.endswith("帮助") or text.endswith("help"):
            yield event.plain_result(HELP_TEXT)
            return

        key = f"{event.get_group_id() or 'private'}:{event.get_sender_id()}"
        cooldown = self._int_cfg("cooldown_sec", 30, 0, 3600)
        now = time.monotonic()
        if cooldown and not event.is_admin():
            wait = cooldown - (now - self._last_use.get(key, -1e9))
            if wait > 0:
                yield event.plain_result(f"刚查过啦，{int(wait) + 1} 秒后再试")
                return

        refs = await self._collect_refs(event)
        if not refs:
            yield event.plain_result("要引用（回复）一条有图的消息，再发 /鱼鱼找鸡")
            return
        if self._lock.locked():
            yield event.plain_result("正在找另一张图，等这次结束再来")
            return

        # 锁里绝对不能 yield：AstrBot 发完 yield 的消息可能不再回到这里，锁就永远不放了
        fetch_error = ""
        result: dict = {}
        async with self._lock:
            self._last_use[key] = now
            try:
                image = await self._fetch_image(refs)
            except ValueError as exc:
                fetch_error = str(exc)
            else:
                result = await self._search(event, image)
        if fetch_error:
            yield event.plain_result(f"取图失败：{fetch_error}")
            return

        if not result.get("ok"):
            yield event.plain_result(f"没查成：{result.get('error') or '未知错误'}")
            return
        reply = str(result.get("text") or "没有结果").replace("**", "")
        notes = result.get("notes") or []
        if notes:
            reply += "\n（" + "；".join(notes) + "）"
        yield event.plain_result(reply)

    async def _search(self, event: AstrMessageEvent, image: bytes) -> dict:
        # 进度提示不能用 yield：AstrBot 把 yield 出去的结果当成本次回复发出，
        # 事件已 stop_event()（rg2 等插件也会停止传播），发完第一条就不再回到处理函数，
        # 后面的识图永远不会执行。所以这里直接发送，最终结果才由处理函数 yield。
        try:
            await event.send(MessageChain().message("收到，开始找鸡（大约要 20 秒到 2 分钟）"))
        except Exception as exc:  # noqa: BLE001  提示发不出去也继续查
            logger.warning(f"[鱼鱼找鸡] 进度提示发送失败：{exc}")
        logger.info("[鱼鱼找鸡] 开始识图")
        try:
            result = await self._run_engine(image)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[鱼鱼找鸡] 运行出错")
            result = {"ok": False, "error": f"运行出错：{exc}"}
        logger.info(f"[鱼鱼找鸡] 识图结束：ok={bool(result.get('ok'))}，"
                    f"耗时 {result.get('elapsedMs', '?')} ms")
        return result
