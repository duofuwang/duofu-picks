#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 GitHub 开源仓库的 Release 自动同步 IPA，生成 AltStore 源 repo.json。

设计要点：
- 不下载、不上传、不重打包 IPA。downloadURL 直接用 Release 资产的原地址，
  所以上游发新版后，你这边只要跑一次脚本，客户端就能看到更新。
- 不同项目的资产命名五花八门（iTorrent.ipa / StikDebug-3.2.0.ipa / 同时挂 iOS 和 tvOS 多个 ipa），
  因此每个仓库可以单独用正则指定要选哪个资产。
- 有些项目只在 pre-release 发版（比如 Provenance 的 alpha 通道），
  支持按仓库单独放行 pre-release。
- 可选：用 HTTP Range 远程读取 IPA 里的 Info.plist，自动探测 bundleIdentifier，
  只拉几十 KB，不下载整个包。结果会缓存到 appinfo.json。

用法：
    python build.py                    # 生成 repo.json
    python build.py --check            # 只检查有无变化，有变化时退出码 1
    python build.py --detect           # 强制刷新 IPA 元数据探测
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import io
import json
import os
import plistlib
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "repo.json"
CANDIDATES_PATH = ROOT / "candidates.json"
CACHE_PATH = ROOT / "appinfo.json"

API_ROOT = "https://api.github.com"
USER_AGENT = "duofu-picks/1.0 (AltStore source builder)"
DEFAULT_ASSET_PATTERN = r"\.ipa$"
VERSION_SPLIT = re.compile(r"[.\-_+ ]")
MAX_DESCRIPTION = 800

# 每次构建都重新从 IPA / 仓库里取图标，取到就覆盖本地那份。
# 关掉（config.refreshIcons = false）则恢复「本地有就跳过」的老行为。
REFRESH_ICONS = True


class FetchError(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def _windows_system_proxy() -> str | None:
    """读 Windows 系统代理（Internet Settings），非 Windows 或未开启时返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except OSError:
        return None
    if not enabled or not isinstance(server, str) or not server.strip():
        return None
    server = server.strip()
    # 可能是 "host:port"，也可能是 "http=a:1;https=b:2"
    if "=" in server:
        parts = dict(
            chunk.split("=", 1) for chunk in server.split(";") if "=" in chunk
        )
        server = parts.get("https") or parts.get("http") or ""
    if not server:
        return None
    return server if "://" in server else f"http://{server}"


def _is_loopback_proxy(url: str) -> bool:
    host = urllib.parse.urlsplit(url if "://" in url else f"http://{url}").hostname
    return host in {"127.0.0.1", "localhost", "::1"}


PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def apply_proxy(choice: str) -> None:
    """决定本次构建走哪个代理：auto 跟随系统代理，none 直连，或显式 URL。"""
    value = (choice or "auto").strip()

    if value.lower() == "none":
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        log("代理：已关闭，直连")
        return

    if value.lower() != "auto":
        url = value if "://" in value else f"http://{value}"
        for key in PROXY_ENV_KEYS:
            os.environ[key] = url
        log(f"代理：{url}")
        return

    current = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    system = _windows_system_proxy()
    # 环境变量里的回环端口通常是 IDE / 沙箱注入的中转，链路可能只放行部分域名，
    # 系统代理才是真正在用的那个，所以这种情况下换成系统代理。
    if system and (not current or (
        _is_loopback_proxy(current) and current.rstrip("/") != system.rstrip("/")
    )):
        for key in PROXY_ENV_KEYS:
            os.environ[key] = system
        if current:
            log(f"代理：{system}（系统代理，已绕开本地中转 {current}）")
        else:
            log(f"代理：{system}（系统代理）")
        return

    log(f"代理：{current or '未设置，直连'}")


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------

def github_headers() -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_get(url: str) -> object:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=github_headers())
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and exc.headers.get("x-ratelimit-remaining") == "0":
                reset = exc.headers.get("x-ratelimit-reset", "?")
                raise FetchError(f"GitHub API 触发限流，重置时间戳 {reset}") from exc
            last_error = exc
            if exc.code >= 500 and attempt < 2:
                time.sleep(2)
                continue
            raise FetchError(f"{url} -> HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001 - 网络异常种类过多
            last_error = exc
            if attempt < 2:
                time.sleep(2)
                continue
    raise FetchError(f"{url} -> {last_error}")


# --------------------------------------------------------------------------
# 版本与资产处理
# --------------------------------------------------------------------------

def version_key(value: object) -> list[int]:
    text = str(value or "")
    parts: list[int] = []
    for chunk in VERSION_SPLIT.split(text):
        match = re.match(r"^(\d+)", chunk)
        if match:
            parts.append(int(match.group(1)))
        elif chunk:
            parts.append(0)
    return parts or [0]


def strip_tag(tag: str) -> str:
    text = (tag or "").strip()
    text = re.sub(r"^[vV](?=\d)", "", text)
    return text or "0"


def version_from_asset(name: str) -> str | None:
    match = re.search(r"(\d+(?:\.\d+)+)", name or "")
    return match.group(1) if match else None


def pick_asset(release: dict, pattern: str) -> dict | None:
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise FetchError(f"资产正则有误：{pattern}（{exc}）") from exc

    for asset in release.get("assets") or []:
        name = asset.get("name") or ""
        if not regex.search(name):
            continue
        if (asset.get("size") or 0) <= 0:
            continue
        if not asset.get("browser_download_url"):
            continue
        return asset
    return None


def trim(text: object, limit: int = MAX_DESCRIPTION, word_boundary: bool = False) -> str | None:
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value:
        return None
    if len(value) > limit:
        value = value[:limit]
        if word_boundary and " " in value:
            value = value.rsplit(" ", 1)[0].rstrip(" ,.;:-—")
        value = value + "…"
    return value or None


# --------------------------------------------------------------------------
# 远程读取 IPA 内的 Info.plist（HTTP Range + zip 随机访问）
# --------------------------------------------------------------------------

class RemoteFile(io.RawIOBase):
    """把远程文件包装成可 seek/read 的对象，供 zipfile 随机访问。"""

    BLOCK = 128 * 1024

    def __init__(self, url: str):
        super().__init__()
        self._url = url
        self._final_url = url
        self._pos = 0
        self._size = 0
        self._buffer = b""
        self._buffer_start = 0
        self._resolve()

    def _resolve(self) -> None:
        request = urllib.request.Request(
            self._url,
            headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            self._final_url = response.geturl()
            content_range = response.headers.get("Content-Range") or ""
            match = re.search(r"/(\d+)\s*$", content_range)
            if match:
                self._size = int(match.group(1))
            else:
                length = response.headers.get("Content-Length")
                self._size = int(length) if length and length.isdigit() else 0
        if self._size <= 0:
            raise FetchError("无法确定远程文件大小，可能不支持 Range 请求")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._pos + offset
        elif whence == os.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError(f"不支持的 whence: {whence}")
        if position < 0:
            raise ValueError("不允许 seek 到负数位置")
        self._pos = position
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = max(self._size - self._pos, 0)
        if size == 0 or self._pos >= self._size:
            return b""

        end = min(self._pos + size, self._size)
        if (
            self._buffer
            and self._buffer_start <= self._pos
            and end <= self._buffer_start + len(self._buffer)
        ):
            data = self._buffer[self._pos - self._buffer_start: end - self._buffer_start]
        else:
            fetch_end = min(self._pos + max(size, self.BLOCK), self._size) - 1
            fetched = self._fetch(self._pos, fetch_end)
            if not fetched:
                return b""
            self._buffer = fetched
            self._buffer_start = self._pos
            data = fetched[: end - self._pos]

        self._pos += len(data)
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def _fetch(self, start: int, end: int) -> bytes:
        request = urllib.request.Request(
            self._final_url,
            headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()

    @property
    def size(self) -> int:
        return self._size

    def close(self) -> None:
        self._buffer = b""
        super().close()


def read_ipa_info(download_url: str) -> dict:
    remote = RemoteFile(download_url)
    try:
        with zipfile.ZipFile(remote) as archive:
            names = [
                n for n in archive.namelist()
                if re.match(r"^Payload/[^/]+\.app/Info\.plist$", n)
            ]
            if not names:
                names = [n for n in archive.namelist() if n.endswith(".app/Info.plist")]
            if not names:
                raise FetchError("IPA 内找不到 Info.plist")
            payload = archive.read(names[0])
        total_size = remote.size
    finally:
        remote.close()

    info = plistlib.loads(payload)
    return {
        "bundleIdentifier": info.get("CFBundleIdentifier"),
        "displayName": info.get("CFBundleDisplayName") or info.get("CFBundleName"),
        "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"),
        "minOSVersion": info.get("MinimumOSVersion"),
        "size": total_size,
    }


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------

def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict, used_keys: set[str]) -> None:
    pruned = {k: v for k, v in cache.items() if k in used_keys}
    if pruned == load_cache():
        return
    CACHE_PATH.write_text(
        json.dumps(pruned, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# --------------------------------------------------------------------------
# 单个仓库 -> 一个 App 条目
# --------------------------------------------------------------------------

def build_app(
    entry: dict,
    max_versions: int,
    default_prereleases: bool,
    detect: bool,
    cache: dict,
    used_keys: set[str],
    warnings: list[str],
    base_url: str | None = None,
) -> dict | None:
    slug = entry.get("repo")
    if not isinstance(slug, str) or "/" not in slug:
        raise FetchError("repo 字段必须是 'owner/name' 形式")
    slug = slug.strip()

    pattern = entry.get("asset") or DEFAULT_ASSET_PATTERN
    include_pre = entry.get("includePrereleases")
    if include_pre is None:
        include_pre = default_prereleases
    exclude_tags = {
        str(tag).strip() for tag in (entry.get("excludeTags") or []) if str(tag).strip()
    }

    per_page = max(10, min(100, max(max_versions, 1) * 4))
    releases = api_get(f"{API_ROOT}/repos/{slug}/releases?per_page={per_page}")
    if not isinstance(releases, list) or not releases:
        raise FetchError("没有任何 Release")

    versions: list[dict] = []
    seen: set[str] = set()
    for release in releases:
        if release.get("draft"):
            continue
        if str(release.get("tag_name") or "") in exclude_tags:
            continue
        if release.get("prerelease") and not include_pre:
            continue

        asset = pick_asset(release, pattern)
        if not asset:
            continue

        mode = entry.get("versionFrom") or "tag"
        if mode == "asset":
            version = version_from_asset(asset.get("name") or "")
            if not version:
                version = strip_tag(release.get("tag_name") or "")
        else:
            version = strip_tag(release.get("tag_name") or "")
        if not version or version in seen:
            continue
        seen.add(version)

        item: dict = {"version": version}
        build = entry.get("buildVersion")
        if build and len(versions) == 0:
            item["buildVersion"] = str(build)
        if release.get("published_at"):
            item["date"] = release["published_at"]
        description = trim(release.get("body"))
        if description:
            item["localizedDescription"] = description
        item["downloadURL"] = asset["browser_download_url"]
        if asset.get("size"):
            item["size"] = int(asset["size"])
        if entry.get("minOSVersion") and len(versions) == 0:
            item["minOSVersion"] = str(entry["minOSVersion"])

        versions.append(item)

    if not versions:
        raise FetchError(f"近 {len(releases)} 个 Release 里没有匹配 {pattern} 的资产")

    versions.sort(
        key=lambda e: (version_key(e["version"]), str(e.get("date") or "")),
        reverse=True,
    )
    if max_versions > 0:
        versions = versions[:max_versions]

    latest = versions[0]
    owner = slug.split("/")[0]

    # --- 标识信息 ---
    bundle = entry.get("bundleIdentifier")
    display_name = entry.get("name")

    cache_key = f"{slug}@{latest['version']}"
    cached_info = cache.get(cache_key) or {}
    detected: dict = cached_info
    # 没配 minOSVersion、缓存里也没有时也探一次，用来补系统要求
    wants_min_os = not entry.get("minOSVersion") and not cached_info.get("minOSVersion")
    needs_detect = detect or not bundle or not display_name or wants_min_os
    if needs_detect:
        need_fetch = not detected or (wants_min_os and not detected.get("minOSVersion"))
        if need_fetch:
            try:
                fresh = read_ipa_info(latest["downloadURL"])
                fresh["source_repo"] = slug
                cache[cache_key] = fresh
                detected = fresh
                log(f"    探测 IPA 元数据成功：{detected.get('bundleIdentifier')}")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{slug} 读取 IPA 元数据失败：{exc}")
        elif detected:
            log(f"    使用缓存的 IPA 元数据：{detected.get('bundleIdentifier')}")
    used_keys.add(cache_key)

    if not bundle:
        bundle = detected.get("bundleIdentifier")
    if not bundle:
        raise FetchError("缺少 bundleIdentifier，且无法从 IPA 中探测到")

    name = display_name or detected.get("displayName") or slug.split("/")[-1]

    if detected.get("build") and "buildVersion" not in latest:
        latest["buildVersion"] = str(detected["build"])
    if not latest.get("minOSVersion") and detected.get("minOSVersion"):
        latest["minOSVersion"] = str(detected["minOSVersion"])

    repo_meta = {}
    try:
        repo_meta = api_get(f"{API_ROOT}/repos/{slug}") or {}
    except FetchError as exc:
        warnings.append(f"{slug} 仓库信息获取失败：{exc}")

    app: dict = {"name": str(name), "bundleIdentifier": str(bundle)}
    developer = entry.get("developerName") or (repo_meta.get("owner") or {}).get("login") or owner
    app["developerName"] = str(developer)
    subtitle = entry.get("subtitle") or trim(repo_meta.get("description"), 80, word_boundary=True)
    if subtitle:
        app["subtitle"] = subtitle
    description = entry.get("description") or trim(repo_meta.get("description"))
    if description:
        app["localizedDescription"] = description
    icon = entry.get("iconURL")
    if icon and base_url:
        # 配置里给的是外链时，顺手镜像一份到 icons/，避免依赖图床存活
        try:
            mirrored = mirror_external_icon(str(icon), str(bundle), ROOT / "icons")
        except IconInvalid as exc:
            warnings.append(f"{slug} 配置的图标不是图片（{exc}），改用其他来源")
            icon = None
        else:
            if mirrored:
                icon = f"{base_url.rstrip('/')}/icons/{urllib.parse.quote(mirrored.name)}"
                log(f"    镜像图标 → {mirrored.name}")
    if not icon and base_url:
        icons_dir = ROOT / "icons"
        cached = _existing_icon(str(bundle), icons_dir)
        cached_source = _icon_source(str(bundle), icons_dir) if cached else None
        written = None
        label = ""
        try:
            written = extract_icon_from_remote(
                latest["downloadURL"], str(bundle), icons_dir
            )
            label = "提取图标"
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{slug} 图标提取失败：{exc}")
        # 只有本地没有图标、或旧图本来就取自仓库 logo 时才去翻仓库。
        # 否则一次网络抖动就会把 IPA 里的高清图标换成仓库里的小图。
        if written is None and (cached is None or cached_source == "assets"):
            try:
                written = find_repo_icon(slug, str(bundle), icons_dir)
                label = "仓库 logo"
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{slug} 仓库 logo 探测失败：{exc}")
        if written is None and cached:
            written = cached
            label = "沿用旧图"
            warnings.append(f"{slug} 本次没取到新图标，沿用 {cached.name}")
        if written:
            icon = f"{base_url.rstrip('/')}/icons/{urllib.parse.quote(written.name)}"
            log(f"    {label} → {written.name}")
    if not icon:
        icon = f"https://github.com/{owner}.png?size=512"
        warnings.append(f"{slug} 没有可用图标，已回退为开发者头像")
    app["iconURL"] = versioned_icon_url(str(icon))
    if entry.get("tintColor"):
        app["tintColor"] = str(entry["tintColor"])
    if entry.get("category"):
        app["category"] = str(entry["category"])
    screenshots = entry.get("screenshotURLs")
    if isinstance(screenshots, list) and screenshots:
        app["screenshotURLs"] = [s for s in screenshots if isinstance(s, str) and s.startswith("http")]
    app["versions"] = versions

    log(
        f"  {app['name']:<22} {latest['version']:<14} "
        f"{len(versions)} 个版本  {slug}"
    )
    return app


def read_ipa_info_local(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = [
            n for n in archive.namelist()
            if re.match(r"^Payload/[^/]+\.app/Info\.plist$", n)
        ]
        if not names:
            names = [n for n in archive.namelist() if n.endswith(".app/Info.plist")]
        if not names:
            raise FetchError(f"{path.name} 里找不到 Payload/*.app/Info.plist")
        payload = archive.read(names[0])

    info = plistlib.loads(payload)
    return {
        "bundleIdentifier": info.get("CFBundleIdentifier"),
        "displayName": info.get("CFBundleDisplayName") or info.get("CFBundleName"),
        "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"),
        "minOSVersion": info.get("MinimumOSVersion"),
        "size": path.stat().st_size,
    }


def extract_icon(ipa_path: Path, bundle: str, icons_dir: Path) -> Path | None:
    """从 IPA 里挑一张最大的 AppIcon 存到 icons/，返回写出的文件。"""
    with zipfile.ZipFile(ipa_path) as archive:
        candidates = [
            (archive.getinfo(name).file_size, name)
            for name in archive.namelist()
            if re.match(r"^Payload/[^/]+\.app/AppIcon[^/]*\.png$", name)
        ]
        if not candidates:
            return None
        candidates.sort(reverse=True)
        payload = archive.read(candidates[0][1])

    icons_dir.mkdir(parents=True, exist_ok=True)
    return _write_icon(icons_dir, bundle, payload, ".png")


# --------------------------------------------------------------------------
# 直链条目：不走 GitHub Release 的 IPA
# --------------------------------------------------------------------------

def build_manual_app(
    entry: dict,
    detect: bool,
    cache: dict,
    used_keys: set[str],
    warnings: list[str],
    repository_icon: str | None,
) -> dict | None:
    url = entry.get("ipa")
    if not isinstance(url, str) or not url.startswith("http"):
        raise FetchError("ipa 字段必须是 http(s) 直链")

    cache_key = f"direct@{url}"
    info = cache.get(cache_key) or {}
    if detect or not info:
        try:
            info = read_ipa_info(url)
            cache[cache_key] = info
            log(f"    探测直链 IPA 成功：{info.get('bundleIdentifier')} {info.get('version')}")
        except Exception as exc:  # noqa: BLE001
            if not info:
                raise FetchError(f"无法读取 IPA 元数据：{exc}") from exc
            warnings.append(f"{url} 元数据刷新失败，沿用缓存值：{exc}")
    used_keys.add(cache_key)

    bundle = entry.get("bundleIdentifier") or info.get("bundleIdentifier")
    if not bundle:
        raise FetchError("缺少 bundleIdentifier，且无法从 IPA 中探测到")

    version = entry.get("version") or info.get("version")
    if not version:
        raise FetchError("无法确定版本号，请在配置里显式写 version")

    name = entry.get("name") or info.get("displayName") or str(bundle)

    versions = entry.get("versions")
    if not isinstance(versions, list) or not versions:
        item: dict = {"version": str(version)}
        build = entry.get("buildVersion") or info.get("build")
        if build:
            item["buildVersion"] = str(build)
        if entry.get("date"):
            item["date"] = str(entry["date"])
        description = trim(entry.get("versionDescription") or entry.get("description"))
        if description:
            item["localizedDescription"] = description
        item["downloadURL"] = url
        if info.get("size"):
            item["size"] = int(info["size"])
        min_os = entry.get("minOSVersion") or info.get("minOSVersion")
        if min_os:
            item["minOSVersion"] = str(min_os)
        versions = [item]

    app: dict = {"name": str(name), "bundleIdentifier": str(bundle)}
    if entry.get("developerName"):
        app["developerName"] = str(entry["developerName"])
    for field in ("subtitle", "category", "tintColor"):
        if entry.get(field):
            app[field] = str(entry[field])
    description = trim(entry.get("description"))
    if description:
        app["localizedDescription"] = description

    icon = entry.get("iconURL") or repository_icon
    if not icon:
        raise FetchError("直链条目必须提供 iconURL，或在 repository 里配置一个兜底图标")
    app["iconURL"] = versioned_icon_url(str(icon))
    app["versions"] = versions

    log(f"  {app['name']:<22} {versions[0]['version']:<14} 直链      {url[:44]}")
    return app


def _primary_icon_candidates(archive: zipfile.ZipFile, names: list[str]) -> list[tuple[int, str]]:
    """按 Info.plist 的 CFBundlePrimaryIcon 声明找主图标 PNG。

    旧式项目用 CFBundleIconFiles 列出 IPA 内的 PNG 基名，但文件名未必以
    AppIcon 开头（"App Icon60x60" / "LinuxRunnerAppIcon60x60" /
    "AltLoad60x60" / "teal-icon-composer60x60"…），上面那条正则匹配不到。
    新式项目只有 CFBundleIconName，图标编进 Assets.car，zip 里没有独立 PNG，
    这里会返回空列表，交给上层回退到仓库素材。
    """
    plists = [n for n in names if re.match(r"^Payload/[^/]+\.app/Info\.plist$", n)]
    if not plists:
        return []
    try:
        info = plistlib.loads(archive.read(plists[0]))
    except Exception:  # noqa: BLE001
        return []
    icons = info.get("CFBundleIcons") or info.get("CFBundleIcons~ipad") or {}
    primary = icons.get("CFBundlePrimaryIcon") or {}
    bases: list[str] = []
    files = primary.get("CFBundleIconFiles")
    if isinstance(files, list):
        bases.extend(str(x) for x in files)
    elif isinstance(files, str):
        bases.append(files)
    if isinstance(primary.get("CFBundleIconName"), str):
        bases.append(primary["CFBundleIconName"])
    bases = [x for x in bases if x]
    if not bases:
        return []

    appdir = plists[0].rsplit("/", 1)[0] + "/"
    found = []
    for name in names:
        if not name.startswith(appdir) or not name.lower().endswith(".png"):
            continue
        stem = name[len(appdir):]
        if "/" in stem:  # 排除 PlugIns / appex / bundle 里的同名资源
            continue
        if any(stem.startswith(base) for base in bases):
            found.append((archive.getinfo(name).file_size, name))
    return found


def extract_icon_from_remote(
    download_url: str, bundle: str, icons_dir: Path, *, refresh: bool | None = None
) -> Path | None:
    """从远程 IPA 里取一张最大的主图标存到 icons/，返回写出的文件。

    先按文件名 AppIcon*.png 找（老规则，保证既有条目行为不变），找不到再
    按 Info.plist 的 CFBundlePrimaryIcon 声明定向匹配。只传 zip 目录和一张
    PNG 的字节，不全量下载。默认每次重读，这样上游换了图标能跟上；
    refresh=False 时本地有就跳过。
    """
    if refresh is None:
        refresh = REFRESH_ICONS
    if not refresh:
        cached = _existing_icon(bundle, icons_dir)
        if cached:
            return cached

    remote = RemoteFile(download_url)
    try:
        with zipfile.ZipFile(remote) as archive:
            names = archive.namelist()
            candidates = [
                (archive.getinfo(name).file_size, name)
                for name in names
                if re.match(r"^Payload/[^/]+\.app/AppIcon[^/]*\.png$", name)
            ]
            if not candidates:
                candidates = _primary_icon_candidates(archive, names)
            if not candidates:
                return None
            candidates.sort(reverse=True)
            payload = archive.read(candidates[0][1])
    finally:
        remote.close()

    if not payload:
        return None
    return _write_icon(icons_dir, bundle, payload, ".png")


ICON_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
ICON_DIR_HINT = re.compile(r"(^|/)(assets?|resources?|images?|icons?|\.github)/", re.I)
ICON_NOISE = re.compile(
    r"(screenshot|preview|banner|header|background|placeholder|badge"
    r"|smoke|splash|launch|transparent|monochrome|adaptive|rounded)",
    re.I,
)
NON_IOS_DIR = re.compile(r"(^|/)(android|macos|windows|linux|web|tvos|watchos|visionos|fastlane)/", re.I)
VENDORED_DIR = re.compile(r"(^|/)(packages|vendor|third_party|examples?|samples?|docs?)/", re.I)


class IconInvalid(Exception):
    """下载回来的是 HTML 错误页之类，不是能用的图片。"""


def _icon_suffix(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _safe_name(bundle: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", bundle)


def _existing_icon(bundle: str, icons_dir: Path) -> Path | None:
    """icons/ 里已经有现成的就复用，避免每次构建重新下载。"""
    safe = _safe_name(bundle)
    for suffix in ICON_SUFFIXES:
        candidate = icons_dir / f"{safe}{suffix}"
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def _load_icon_sources(icons_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((icons_dir / ".sources.json").read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in data.items()}
    except Exception:  # noqa: BLE001
        return {}


def _icon_source(bundle: str, icons_dir: Path) -> str | None:
    return _load_icon_sources(icons_dir).get(bundle)


def _set_icon_source(bundle: str, source: str, icons_dir: Path) -> None:
    sources = _load_icon_sources(icons_dir)
    if sources.get(bundle) == source:
        return
    sources[bundle] = source
    target = icons_dir / ".sources.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dict(sorted(sources.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize_apple_png(data: bytes) -> bytes:
    """把 Xcode pngcrush 产出的 CgBI 图标转成标准 PNG。

    IPA 里的 AppIcon*.png 常被 Xcode 改成 Apple 私有变体：IHDR 前多一个
    CgBI chunk、IDAT 用 raw deflate（无 zlib 头）、通道顺序是 BGRA 且 alpha
    预乘。iOS 的 UIImage 能读，但浏览器和 Pillow 等标准解码器会直接报
    "broken data stream"，导致源里图标在网页上显示不出来。

    只处理 8bit / 非隔行的 RGB(A)；遇到看不懂的格式原样返回，别让构建挂掉。
    """
    sig = b"\x89PNG\r\n\x1a\n"
    if len(data) < 33 or data[:8] != sig or data[12:16] != b"CgBI":
        return data

    idat, keep, ihdr = b"", [], None
    pos = 8
    while pos + 12 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        ctype = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        if pos + 12 + length > len(data):
            return data
        if ctype == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if depth != 8 or color not in (2, 6) or interlace != 0:
                return data
            ihdr = (width, height, 4 if color == 6 else 3)
            keep.append((ctype, payload))
        elif ctype == b"IDAT":
            idat += payload
        elif ctype not in (b"CgBI", b"iDOT", b"IEND"):
            keep.append((ctype, payload))
        pos += 12 + length

    if ihdr is None or not idat:
        return data
    width, height, bpp = ihdr
    try:
        try:
            raw = zlib.decompress(idat)
        except zlib.error:
            raw = zlib.decompress(idat, -15)  # Apple 用 raw deflate
    except zlib.error:
        return data
    stride = width * bpp
    if len(raw) < height * (stride + 1):
        return data

    prev = bytearray(stride)
    out = bytearray()
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if ftype > 4:
            return data
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 255
            elif ftype == 2:
                line[x] = (line[x] + b) & 255
            elif ftype == 3:
                line[x] = (line[x] + ((a + b) // 2)) & 255
            elif ftype == 4:
                line[x] = (line[x] + paeth(a, b, c)) & 255
        if bpp == 4:
            for x in range(0, stride, 4):
                blue, green, red, alpha = line[x], line[x + 1], line[x + 2], line[x + 3]
                if 0 < alpha < 255:  # 撤销 alpha 预乘
                    red = min(255, red * 255 // alpha)
                    green = min(255, green * 255 // alpha)
                    blue = min(255, blue * 255 // alpha)
                line[x], line[x + 2] = red, blue
        else:
            for x in range(0, stride, 3):
                line[x], line[x + 2] = line[x + 2], line[x]
        out += b"\x00" + line  # 全部改用 filter 0 重新编码
        prev = line

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + ctype
            + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
        )

    return (
        sig
        + b"".join(chunk(ct, body) for ct, body in keep)
        + chunk(b"IDAT", zlib.compress(bytes(out), 9))
        + chunk(b"IEND", b"")
    )


def _write_icon(
    icons_dir: Path, bundle: str, data: bytes, suffix: str, source: str = "ipa"
) -> Path:
    """写入图标，并清掉同一应用其他扩展名的旧文件，避免一份图标留两份。"""
    if suffix.lower() == ".png":
        try:
            data = normalize_apple_png(data)
        except (ValueError, struct.error, zlib.error):
            pass  # 转换不了就照原样写，退回到旧行为
    icons_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(bundle)
    for other in ICON_SUFFIXES:
        if other == suffix:
            continue
        stale = icons_dir / f"{safe}{other}"
        if stale.exists():
            stale.unlink()
    target = icons_dir / f"{safe}{suffix}"
    target.write_bytes(data)
    _set_icon_source(bundle, source, icons_dir)
    return target


def versioned_icon_url(icon: str) -> str:
    """给本站 icons/ 下的图标 URL 追加内容 hash，图标一变就换 URL。

    AltStore / SideStore 按 URL 缓存图标，而文件名一直是 {bundleId}.png：
    上游换了图标、或我们修了图标编码，URL 没变，客户端就一直抱着旧图不放
    （阅读从 1.0.4-2 换成 3.0.0 后就卡在水墨风旧图标）。加 ?v=<hash> 后
    内容变化会自然触发重新下载；内容没变时 URL 稳定，不会造成缓存抖动。

    只处理 icons/ 下确实存在的本地文件，外链和开发者头像原样返回。
    """
    try:
        parts = urllib.parse.urlsplit(icon)
    except ValueError:
        return icon
    marker = "/icons/"
    if marker not in parts.path:
        return icon
    filename = urllib.parse.unquote(parts.path.rsplit(marker, 1)[-1])
    if not filename or "/" in filename:
        return icon
    local = ROOT / "icons" / filename
    if not local.is_file():
        return icon
    digest = hashlib.sha256(local.read_bytes()).hexdigest()[:12]
    params = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "v"]
    params.append(("v", digest))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(params), parts.fragment)
    )


# --------------------------------------------------------------------------
# tintColor：从本地图标里取主色
# --------------------------------------------------------------------------

TINT_SAMPLE_MAX = 96       # 统计前先降采样到 96×96 以内
TINT_HUE_BUCKETS = 24      # 色相分 24 桶，每桶 15°
TINT_MIN_LIGHTNESS = 0.25     # HSL 明度下限，更暗的像素按「背景」排除
TINT_TARGET_LIGHTNESS = 0.55  # HSL 明度目标（Vibrant 用 0.5，这里略偏亮）


def paeth(a: int, b: int, c: int) -> int:
    """PNG 滤波用的 Paeth 预测器，CgBI 转换和 PNG 解码共用。"""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def _grid_step(width: int, height: int, max_side: int) -> int:
    return max(1, (max(width, height) + max_side - 1) // max_side)


def decode_png(data: bytes, max_side: int = TINT_SAMPLE_MAX) -> tuple[int, int, bytes] | None:
    """把标准 PNG 解成降采样后的 (宽, 高, RGBA 字节串)。

    只用标准库，不装 Pillow 也能跑；更重要的是本地和 Actions 走的是同一条
    解码路径，算出来的 hex 完全一致，不会让 repo.json 每轮来回抖。
    支持 8/16bit 的灰 / RGB / 带 alpha 组合，以及调色板 1/2/4/8bit；16bit
    只取每个通道的高 8 位（低 8 位对取主色没影响）。隔行 PNG 和非调色板
    tRNS 直接返回 None，图标里基本见不到。
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    width = height = 0
    depth, color, interlace = 8, 0, 0
    palette, trns, idat = b"", b"", bytearray()
    while pos + 12 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        ctype = data[pos + 4: pos + 8]
        if pos + 12 + length > len(data):
            return None
        body = data[pos + 8: pos + 8 + length]
        if ctype == b"IHDR":
            if length != 13:
                return None
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
        pos += 12 + length

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if not width or not height or interlace or channels is None:
        return None
    if depth == 16:
        if color not in (0, 2, 4, 6):  # 16bit 调色板不存在
            return None
    elif depth != 8 and not (color == 3 and depth in (1, 2, 4)):
        return None
    if width * height > 40_000_000:  # 异常文件别吃满内存
        return None

    stride = (width * channels * depth + 7) // 8
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    if len(raw) < height * (stride + 1):
        return None

    step = _grid_step(width, height, max_side)
    xs = range(0, width, step)
    ys = range(0, height, step)
    grid_w, grid_h = len(xs), len(ys)
    out = bytearray(grid_w * grid_h * 4)
    row_index = {y: i for i, y in enumerate(ys)}
    bpp = max(1, (channels * depth + 7) // 8)
    prev = bytearray(stride)
    pos = 0

    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos: pos + stride])
        pos += stride
        # 滤波是行间依赖的，非采样行也得还原，只是不用转成像素。
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ftype == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + paeth(a, prev[i], c)) & 255
        elif ftype != 0:
            return None

        if y in row_index:
            base = row_index[y] * grid_w * 4
            for col, x in enumerate(xs):
                offset = base + col * 4
                if color == 3:  # 调色板
                    if depth == 8:
                        index = line[x]
                    else:
                        per_byte = 8 // depth
                        byte = line[x // per_byte]
                        shift = 8 - depth - (x % per_byte) * depth
                        index = (byte >> shift) & ((1 << depth) - 1)
                    p = index * 3
                    if p + 2 < len(palette):
                        alpha = trns[index] if index < len(trns) else 255
                        out[offset: offset + 4] = bytes((
                            palette[p], palette[p + 1], palette[p + 2], alpha,
                        ))
                else:  # 灰 / RGB / 带 alpha，8bit 或 16bit 通用
                    unit = 2 if depth == 16 else 1
                    src = x * channels * unit
                    if color == 6:
                        pixel = (
                            line[src], line[src + unit],
                            line[src + 2 * unit], line[src + 3 * unit],
                        )
                    elif color == 2:
                        pixel = (line[src], line[src + unit], line[src + 2 * unit], 255)
                    elif color == 4:
                        gray = line[src]
                        pixel = (gray, gray, gray, line[src + unit])
                    else:
                        gray = line[src]
                        pixel = (gray, gray, gray, 255)
                    out[offset: offset + 4] = bytes(pixel)
        prev = line
    return grid_w, grid_h, bytes(out)


def decode_image_with_pillow(
    data: bytes, max_side: int = TINT_SAMPLE_MAX
) -> tuple[int, int, bytes] | None:
    """非 PNG 图标（jpg / webp / gif）交给 Pillow；没装就放弃这张图。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            rgba = image.convert("RGBA")
            step = _grid_step(rgba.width, rgba.height, max_side)
            grid = rgba.resize(
                (len(range(0, rgba.width, step)), len(range(0, rgba.height, step))),
                getattr(Image, "Resampling", Image).NEAREST,
            )
            return grid.width, grid.height, grid.tobytes()
    except Exception:  # noqa: BLE001 - 第三方解码器异常种类多，不能让构建挂掉
        return None


def dominant_tint(grid: tuple[int, int, bytes]) -> str | None:
    """从采样网格里挑主色，返回 #RRGGBB。

    思路：先扔掉透明、近黑、近白和低饱和的像素（App 图标里这些通常是背景
    或留白），再把剩下的按色相分桶加权。选桶时用环形三桶滑窗，这样跨 0°
    的红色不会被劈成两半；桶内做加权 RGB 平均，最后把饱和度/明度收进一个
    适合当强调色的区间。灰阶或纯黑白图标直接返回 None，不硬凑一个颜色。

    两个借鉴 Vibrant 的调整：一是把 HSL 明度低于 0.25 的像素当深色背景排除，
    否则「大面积深色底 + 小面积亮色 logo」会稳稳挑到背景；二是权重和桶打分
    都带上对中等明度、高饱和色的偏好。面积仍是主导项 —— 连续渐变图标必须
    取面积重心，不能为了「最醒目」挑走渐变里最饱和的一小段。
    """
    width, height, rgba = grid
    count = TINT_HUE_BUCKETS
    weights = [0.0] * count
    sums = [[0.0, 0.0, 0.0] for _ in range(count)]
    sat_sums = [0.0] * count
    light_sums = [0.0] * count
    useful = 0
    for i in range(width * height):
        r, g, b, alpha = rgba[i * 4: i * 4 + 4]
        if alpha < 128:
            continue
        high, low = max(r, g, b), min(r, g, b)
        # 近黑、近白和灰阶都在这里被排除（白色/浅灰的 high-low 都很小）。
        if high < 24 or high - low < 20:
            continue
        lightness = (high + low) / 510.0
        if lightness < TINT_MIN_LIGHTNESS:
            continue
        hue, saturation, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if saturation < 0.15:
            continue
        useful += 1
        tone = 1.0 - abs(lightness - TINT_TARGET_LIGHTNESS)
        weight = (0.4 + saturation) * (0.45 + value) * (0.55 + 0.45 * tone)
        bucket = min(count - 1, int(hue * count))
        weights[bucket] += weight
        sums[bucket][0] += r * weight
        sums[bucket][1] += g * weight
        sums[bucket][2] += b * weight
        sat_sums[bucket] += saturation * weight
        light_sums[bucket] += lightness * weight

    if useful < max(12, width * height * 0.01) or not any(weights):
        return None

    def bucket_score(index: int) -> float:
        group = ((index - 1) % count, index, (index + 1) % count)
        area = sum(weights[j] for j in group)
        if area <= 0:
            return 0.0
        avg_saturation = sum(sat_sums[j] for j in group) / area
        avg_lightness = sum(light_sums[j] for j in group) / area
        tone = 1.0 - abs(avg_lightness - TINT_TARGET_LIGHTNESS)
        return area * (0.5 + 0.5 * avg_saturation) * (0.5 + 0.5 * tone)

    center = max(range(count), key=bucket_score)
    group = ((center - 1) % count, center, (center + 1) % count)
    total = sum(weights[i] for i in group)
    if total <= 0:
        return None
    r = sum(sums[i][0] for i in group) / total / 255
    g = sum(sums[i][1] for i in group) / total / 255
    b = sum(sums[i][2] for i in group) / total / 255
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    saturation = min(0.90, max(0.50, saturation * 1.06))
    value = min(0.96, max(0.70, value * 1.02))
    r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def tint_from_icon(path: Path) -> str | None:
    """PNG 走标准库解码，其他格式才用 Pillow（可选），失败返回 None。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if path.suffix.lower() == ".png":
        grid = decode_png(data)
    else:
        grid = decode_image_with_pillow(data)
    return dominant_tint(grid) if grid else None


def local_icon_path(app: dict, icons_dir: Path) -> Path | None:
    """按 iconURL 找到实际用的那张本地图，找不到再按 bundle ID 回退。"""
    icon = str(app.get("iconURL") or "")
    try:
        parts = urllib.parse.urlsplit(icon)
    except ValueError:
        parts = None
    marker = "/icons/"
    if parts and marker in parts.path:
        name = urllib.parse.unquote(parts.path.rsplit(marker, 1)[-1])
        if name and "/" not in name and ".." not in name:
            candidate = icons_dir / name
            if candidate.is_file():
                return candidate
    return _existing_icon(str(app.get("bundleIdentifier") or ""), icons_dir)


def set_app_tint(app: dict, color: str | None) -> None:
    """写入 tintColor 并保持它紧跟 iconURL 的字段顺序。"""
    if not color:
        app.pop("tintColor", None)
        return
    rebuilt: dict = {}
    for key, value in app.items():
        if key == "tintColor":
            continue
        rebuilt[key] = value
        if key == "iconURL":
            rebuilt["tintColor"] = color
    if "tintColor" not in rebuilt:
        rebuilt["tintColor"] = color
    app.clear()
    app.update(rebuilt)


def manual_tint_map(entries: list) -> dict[str, str]:
    """config.json 里手写的 tintColor 是唯一真源，永远优先于自动取色。"""
    return {
        str(entry["bundleIdentifier"]): str(entry["tintColor"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("bundleIdentifier") and entry.get("tintColor")
    }


def source_tint(config: dict) -> str | None:
    """源自身的主色，作为「有图标但取不到彩色」时的兜底。"""
    raw = str((config.get("repository") or {}).get("tintColor") or "").strip()
    if not raw:
        return None
    return raw if raw.startswith("#") else "#" + raw


def apply_auto_tints(
    apps: list[dict],
    manual: dict[str, str],
    icons_dir: Path,
    enabled: bool = True,
    fallback: str | None = None,
) -> list[tuple[str, str | None, str]]:
    """给每个 app 定 tintColor，返回 (名称, 颜色, 来源) 列表。"""
    results: list[tuple[str, str | None, str]] = []
    cache: dict[Path, str | None] = {}
    for app in apps:
        bundle = str(app.get("bundleIdentifier") or "")
        if bundle in manual:
            color, source = manual[bundle], "manual"
        elif not enabled:
            color, source = None, "off"
        else:
            path = local_icon_path(app, icons_dir)
            if path is None:
                color, source = None, "none"
            else:
                if path not in cache:
                    cache[path] = tint_from_icon(path)
                color = cache[path]
                if color:
                    source = "auto"
                elif fallback:
                    # 纯黑白图标没有可提取的彩色；用源主色兜底，
                    # 免得客户端对这几个应用显示成另一套默认蓝。
                    color, source = fallback, "fallback"
                else:
                    source = "none"
        set_app_tint(app, color)
        results.append((str(app.get("name") or bundle), color, source))
    return results


def refresh_tints(check: bool) -> int:
    """离线模式：不联网，只重算 repo.json 里的 tintColor，并补齐顶层 size。"""
    if not CONFIG_PATH.exists() or not OUTPUT_PATH.exists():
        raise SystemExit("离线刷新需要 config.json 和 repo.json 都存在。")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    repository = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    apps = repository.get("apps") or []
    before = [(app.get("tintColor"), app.get("size")) for app in apps]
    manual = manual_tint_map(config.get("apps") or [])
    enabled = bool(config.get("tintFromIcons", True))
    fallback = source_tint(config) if config.get("tintFallback", True) else None
    results = apply_auto_tints(apps, manual, ROOT / "icons", enabled, fallback)
    sized = apply_app_sizes(apps)

    labels = {
        "manual": "配置指定", "auto": "图标取色",
        "fallback": "无彩色·回退源主色", "none": "无可用颜色", "off": "已关闭",
    }
    for name, color, source in results:
        log(f"  {name:<22} {color or '—':<9} {labels[source]}")
    log("")
    log(f"共 {sum(1 for _, _, s in results if s == 'auto')} 个取自图标，"
        f"{sum(1 for _, _, s in results if s == 'manual')} 个沿用配置，"
        f"{sum(1 for _, _, s in results if s == 'fallback')} 个回退源主色。")
    log(f"顶层 size：{sized}/{len(apps)} 个应用取自最新版本。")

    after = [(app.get("tintColor"), app.get("size")) for app in apps]
    if before == after:
        log("repo.json 的 tintColor / size 无变化。")
        return 0
    if check:
        log("repo.json 需要更新（--check 模式不写入）。")
        return 1
    OUTPUT_PATH.write_text(render(repository), encoding="utf-8", newline="\n")
    log("已写入 repo.json。")
    return 0


def prune_icons(apps: list[dict], icons_dir: Path, dry_run: bool = False) -> list[str]:
    """删掉没有任何应用引用的图标文件和来源记录，让仓库和 repo.json 对齐。"""
    keep_stems = {
        _safe_name(str(app.get("bundleIdentifier") or "")) for app in apps
    }
    keep_stems.discard("")
    removed: list[str] = []

    if icons_dir.is_dir():
        for path in sorted(icons_dir.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in ICON_SUFFIXES:
                continue
            if path.stem in keep_stems:
                continue
            removed.append(path.name)
            if not dry_run:
                path.unlink()

    sources = _load_icon_sources(icons_dir)
    bundles = {
        str(app.get("bundleIdentifier")) for app in apps if app.get("bundleIdentifier")
    }
    stale = sorted(bundle for bundle in sources if bundle not in bundles)
    if stale:
        removed.extend(f".sources.json · {bundle}" for bundle in stale)
        if not dry_run:
            for bundle in stale:
                sources.pop(bundle, None)
            (icons_dir / ".sources.json").write_text(
                json.dumps(dict(sorted(sources.items())), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return removed


def api_get_bytes(url: str) -> bytes:
    headers = github_headers()
    headers["Accept"] = "application/vnd.github.raw"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(2)
    raise FetchError(f"{url} -> {last_error}")


def mirror_external_icon(
    url: str, bundle: str, icons_dir: Path, refresh: bool | None = None
) -> Path | None:
    """把配置里的外链图标下载到 icons/，让源不再依赖第三方图床。

    refresh=True（默认跟随全局 REFRESH_ICONS）时每次都重新拉一次上游，
    上游换了图标就能跟上；内容没变时不写盘，配合 versioned_icon_url 的
    ?v= 也不会造成缓存抖动。网络失败或拿到的不是图片时回退本地缓存，
    本地也没有才返回 None / 抛 IconInvalid，让调用方改从 IPA 或仓库找。
    """
    if not url.startswith(("http://", "https://")):
        return None
    if refresh is None:
        refresh = REFRESH_ICONS
    cached = _existing_icon(bundle, icons_dir)
    if cached and not refresh:
        return cached
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except Exception as exc:  # noqa: BLE001
        if cached:
            log(f"    镜像图标失败，沿用本地缓存：{exc}")
            return cached
        log(f"    镜像图标失败，保留原地址：{exc}")
        return None
    suffix = _icon_suffix(data)
    if not suffix or len(data) < 100:
        if cached:
            log(f"    镜像图标不是图片（{len(data)} 字节），沿用本地缓存")
            return cached
        raise IconInvalid(f"{len(data)} 字节，开头 {data[:12]!r}")
    # 内容没变就别写盘：文件名固定，重复写会让每轮构建都动一次产物。
    comparable = data
    if suffix.lower() == ".png":
        try:
            comparable = normalize_apple_png(data)
        except (ValueError, struct.error, zlib.error):
            pass
    if cached and cached.suffix.lower() == suffix.lower() and cached.read_bytes() == comparable:
        return cached
    target = _write_icon(icons_dir, bundle, data, suffix, source="mirror")
    if len(data) < 2048:
        log(f"    图标偏小（{len(data)} 字节），可能不够清晰")
    return target


def find_repo_icon(slug: str, bundle: str, icons_dir: Path) -> Path | None:
    """IPA 里没有独立 PNG 时（图标编进 Assets.car），从仓库源码里找一张 logo。"""
    cached = _existing_icon(bundle, icons_dir)
    if cached and not REFRESH_ICONS:
        return cached

    tree = api_get(f"{API_ROOT}/repos/{slug}/git/trees/HEAD?recursive=1") or {}
    blobs = [
        item
        for item in (tree.get("tree") or [])
        if item.get("type") == "blob"
        and str(item.get("path", "")).lower().endswith(ICON_SUFFIXES)
        and 2048 <= int(item.get("size") or 0) <= 2_000_000
    ]
    if not blobs:
        return None

    project = slug.split("/")[-1].replace("-", "").replace("_", "").lower()

    def score(item: dict) -> tuple[int, int]:
        path = str(item.get("path", ""))
        low = path.lower()
        base = low.rsplit("/", 1)[-1]
        value = 0
        if "appicon.appiconset" in low:
            # Xcode 的应用图标目录，最可靠
            value += 120
        if "appicon" in base:
            value += 30
        elif "icon" in base:
            value += 35
        elif "logo" in base:
            value += 18
        if project and project in base.replace("-", "").replace("_", ""):
            value += 25
        if ICON_DIR_HINT.search(low):
            value += 15
        if low.count("/") <= 1:
            value += 10
        if low.endswith(".png"):
            value += 8
        if NON_IOS_DIR.search(low):
            value -= 45
        if VENDORED_DIR.search(low):
            value -= 100
        if ICON_NOISE.search(low):
            value -= 90
        return value, int(item.get("size") or 0)

    blobs.sort(key=score, reverse=True)
    for item in blobs[:5]:
        path = str(item.get("path", ""))
        try:
            data = api_get_bytes(
                f"{API_ROOT}/repos/{slug}/contents/{urllib.parse.quote(path)}"
            )
        except FetchError:
            continue
        suffix = _icon_suffix(data)
        if not suffix:
            continue
        return _write_icon(icons_dir, bundle, data, suffix, source="assets")
    return None


# --------------------------------------------------------------------------
# 本地文件条目：自己上传的 IPA
# --------------------------------------------------------------------------

def build_file_app(
    entry: dict,
    cache: dict,
    used_keys: set[str],
    warnings: list[str],
    base_url: str | None,
    repository_icon: str | None,
) -> dict | None:
    relative = entry.get("file")
    if not isinstance(relative, str) or not relative.strip():
        raise FetchError("file 字段必须是仓库内的相对路径，例如 ipas/MyApp.ipa")
    relative = relative.strip().replace("\\", "/").lstrip("/")
    if relative.startswith("./"):
        relative = relative[2:]

    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT):
        raise FetchError("file 路径必须位于仓库目录内")
    if not path.is_file():
        raise FetchError(f"找不到文件：{relative}")

    stat = path.stat()
    cache_key = f"file@{relative}"
    cached = cache.get(cache_key) or {}
    if cached.get("mtime") != stat.st_mtime or cached.get("bytes") != stat.st_size:
        info = read_ipa_info_local(path)
        info["mtime"] = stat.st_mtime
        info["bytes"] = stat.st_size
        info["size"] = stat.st_size
        cache[cache_key] = info
        log(f"    读取 {relative} → {info.get('bundleIdentifier')} {info.get('version')}")
    else:
        info = cached
    used_keys.add(cache_key)

    bundle = entry.get("bundleIdentifier") or info.get("bundleIdentifier")
    if not bundle:
        raise FetchError("缺少 bundleIdentifier，且无法从 IPA 中读到")
    version = entry.get("version") or info.get("version")
    if not version:
        raise FetchError("缺少版本号，且无法从 IPA 中读到")
    name = entry.get("name") or info.get("displayName") or str(bundle)

    if not base_url:
        raise FetchError("上传文件必须配置 repository.baseURL，否则无法生成下载地址")
    base = base_url.rstrip("/")
    download_url = f"{base}/{urllib.parse.quote(relative)}"

    icon = entry.get("iconURL")
    if not icon:
        try:
            written = extract_icon(path, str(bundle), ROOT / "icons")
        except Exception as exc:  # noqa: BLE001
            written = None
            warnings.append(f"{relative} 图标提取失败：{exc}")
        if written:
            icon = f"{base}/icons/{urllib.parse.quote(written.name)}"
    if not icon:
        icon = repository_icon
    if not icon:
        raise FetchError("没有可用图标：请提供 iconURL，或让脚本从 IPA 里提取（需 baseURL）")

    item: dict = {"version": str(version)}
    build = entry.get("buildVersion") or info.get("build")
    if build:
        item["buildVersion"] = str(build)
    if entry.get("date"):
        item["date"] = str(entry["date"])
    description = trim(entry.get("versionDescription") or entry.get("description"))
    if description:
        item["localizedDescription"] = description
    item["downloadURL"] = download_url
    if info.get("size"):
        item["size"] = int(info["size"])
    min_os = entry.get("minOSVersion") or info.get("minOSVersion")
    if min_os:
        item["minOSVersion"] = str(min_os)

    versions = entry.get("versions")
    if not isinstance(versions, list) or not versions:
        versions = [item]

    app: dict = {"name": str(name), "bundleIdentifier": str(bundle)}
    if entry.get("developerName"):
        app["developerName"] = str(entry["developerName"])
    for field in ("subtitle", "category", "tintColor"):
        if entry.get(field):
            app[field] = str(entry[field])
    description = trim(entry.get("description"))
    if description:
        app["localizedDescription"] = description
    app["iconURL"] = versioned_icon_url(str(icon))
    app["versions"] = versions

    log(f"  {app['name']:<22} {versions[0]['version']:<14} 本地文件   {relative}")
    return app


def build_repo_with_retry(
    entry: dict,
    max_versions: int,
    default_prereleases: bool,
    detect: bool,
    cache: dict,
    used_keys: set[str],
    warnings: list[str],
    base_url: str | None = None,
    attempts: int = 2,
) -> dict | None:
    """GitHub 类条目在网络抖动时重试一次，避免整条应用从源里消失。"""
    last_error: FetchError | None = None
    for attempt in range(attempts):
        try:
            return build_app(
                entry, max_versions, default_prereleases, detect, cache,
                used_keys, warnings, base_url,
            )
        except FetchError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                log(f"    重试 {entry.get('repo')}：{exc}")
                time.sleep(3)
    raise last_error if last_error else FetchError("未知错误")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def apply_app_sizes(apps: list[dict]) -> int:
    """把默认安装版本的 size 补一份到 app 顶层，返回补上的应用数。

    versions 里每个版本本来就有 size，但部分客户端在应用列表/详情页读的是
    app 级别的 size，顶层缺了就显示不出体积。统一取 versions[0]（脚本已按
    版本号和日期倒序排过，也是用户默认装的那版），顺序放在 versions 之前。
    最新版没有 size 时清掉旧值，避免留下过期数据。
    """
    count = 0
    for app in apps:
        versions = app.get("versions") or []
        size = versions[0].get("size") if versions and isinstance(versions[0], dict) else None
        if not isinstance(size, int) or size <= 0:
            app.pop("size", None)
            continue
        rebuilt: dict = {}
        for key, value in app.items():
            if key == "size":
                continue
            if key == "versions":
                rebuilt["size"] = size
            rebuilt[key] = value
        if "size" not in rebuilt:
            rebuilt["size"] = size
        app.clear()
        app.update(rebuilt)
        count += 1
    return count


def render(repository: dict) -> str:
    return json.dumps(repository, ensure_ascii=False, indent=2) + "\n"


def shrink_guard(previous: str | None, apps: list[dict]) -> list[str]:
    """对比上一次的 repo.json，返回本次「异常消失」的 bundle 列表。

    额度耗尽、上游抽风、网络故障都会让成批应用被跳过，写出去就是把源写瘸。
    少掉一两个可能是真的删了配置，少掉两成以上就要先停下来问一句。
    """
    if not previous:
        return []
    try:
        old_apps = {a["bundleIdentifier"] for a in json.loads(previous).get("apps", [])}
    except Exception:  # noqa: BLE001
        return []
    missing = sorted(old_apps - {a["bundleIdentifier"] for a in apps})
    if len(missing) > max(2, int(len(old_apps) * 0.2)):
        return missing
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="从 GitHub Release 生成 AltStore 源")
    parser.add_argument("--check", action="store_true", help="只检查是否有变化")
    parser.add_argument("--detect", action="store_true", help="强制刷新 IPA 元数据探测")
    parser.add_argument(
        "--no-refresh-icons", action="store_true", help="跳过图标重取，直接用本地已有文件"
    )
    parser.add_argument(
        "--prune", action="store_true", help="清理没有任何应用引用的图标与来源记录"
    )
    parser.add_argument(
        "--proxy",
        metavar="auto|none|URL",
        default="auto",
        help="auto 跟随系统代理并绕开本地中转，none 直连，或写 http://127.0.0.1:7897",
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="允许一次性少掉大量应用（默认会中止，防止额度耗尽或上游异常时写坏源）",
    )
    parser.add_argument(
        "--tints", action="store_true",
        help="不联网，只按 icons/ 重算 repo.json 里的 tintColor",
    )
    parser.add_argument(
        "--no-tints", action="store_true", help="本次构建不自动生成 tintColor",
    )
    parser.add_argument(
        "--no-candidates", action="store_true",
        help="本次构建不生成候选源 candidates.json",
    )
    args = parser.parse_args()

    if args.tints:
        return refresh_tints(args.check)

    apply_proxy(args.proxy)

    if not CONFIG_PATH.exists():
        raise SystemExit(f"找不到配置文件：{CONFIG_PATH}")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    repo_meta = config.get("repository") or {}
    entries = config.get("apps") or []
    if not entries:
        raise SystemExit("config.json 的 apps 为空。")
    # 候选源是独立的第二个源：不与正式源去重，共用一个缓存与图标目录。
    candidate_meta = config.get("candidatesRepository") or {}
    candidate_entries = config.get("candidates") or []

    global REFRESH_ICONS
    REFRESH_ICONS = bool(config.get("refreshIcons", True)) and not args.no_refresh_icons

    max_versions = int(config.get("maxVersions") or 0)
    default_prereleases = bool(config.get("includePrereleases") or False)
    detect = bool(config.get("detectAppInfo") or args.detect)

    cache = load_cache()
    used_keys: set[str] = set()
    warnings: list[str] = []

    def build_group(group_entries, group_label, loose=False):
        """构建一组条目。候选源用 loose=True 放宽异常，避免任何一条拖垮正式源。"""
        group_apps: list[dict] = []
        group_featured: set[str] = set()
        group_failed: list[str] = []
        for entry in group_entries:
            if not isinstance(entry, dict):
                continue
            label = entry.get("repo") or entry.get("file") or entry.get("ipa") or "未命名条目"
            try:
                if entry.get("file"):
                    app = build_file_app(
                        entry, cache, used_keys, warnings, repo_meta.get("baseURL"),
                        repo_meta.get("iconURL"),
                    )
                elif entry.get("ipa"):
                    app = build_manual_app(
                        entry, detect, cache, used_keys, warnings, repo_meta.get("iconURL")
                    )
                else:
                    app = build_repo_with_retry(
                        entry, max_versions, default_prereleases, detect, cache,
                        used_keys, warnings, repo_meta.get("baseURL"),
                    )
            except FetchError as exc:
                # 候选源的失败只进终端汇总，不写 warnings：否则 --prune 会因为
                # 「本轮有条目被跳过」而每轮都放弃清理图标。
                if not loose:
                    warnings.append(f"{label} 已跳过：{exc}")
                group_failed.append(f"{label}：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                # 只有候选源放宽；正式源维持原有严格行为。
                if not loose:
                    raise
                group_failed.append(f"{label}：{exc}")
                continue
            if app is None:
                continue
            group_apps.append(app)
            if entry.get("featured"):
                group_featured.add(app["bundleIdentifier"])
        return group_apps, group_featured, group_failed

    log(f"读取 {len(entries)} 个上游仓库…")
    apps, featured, _main_failed = build_group(entries, "正式")

    if not apps:
        for warning in warnings:
            log(f"警告：{warning}")
        raise SystemExit("没有取到任何可用应用，中止以免覆盖现有 repo.json。")

    apps.sort(key=lambda a: a["name"].lower())

    # 候选源：每次构建顺带生成，不去重（两个源相互独立），单条失败只警告跳过。
    candidate_apps: list[dict] = []
    candidate_failed: list[str] = []
    candidates_enabled = bool(candidate_entries) and not args.no_candidates
    if candidates_enabled:
        log(f"读取 {len(candidate_entries)} 个候选仓库…")
        candidate_apps, _ignored, candidate_failed = build_group(
            candidate_entries, "候选", loose=True
        )
        candidate_apps.sort(key=lambda a: a["name"].lower())
        if not candidate_apps:
            log("候选源本轮没有取到任何应用，跳过写入以免覆盖现有 candidates.json。")
        if candidate_failed:
            log("")
            log(f"候选源跳过 {len(candidate_failed)}/{len(candidate_entries)} 个：")
            for line in candidate_failed:
                log(f"  - {line}")

    # 顶层 size 取默认安装版本，供只读 app.size 的客户端显示体积。
    sized = apply_app_sizes(apps)
    if candidate_apps:
        apply_app_sizes(candidate_apps)

    # tintColor 在图标定稿后统一算：本地和 Actions 用同一份 PNG 解码，
    # 图标换了颜色会跟着变，config.json 里手写的那些始终优先。
    enabled = bool(config.get("tintFromIcons", True)) and not args.no_tints
    fallback = source_tint(config) if config.get("tintFallback", True) else None
    tint_results = apply_auto_tints(
        apps, manual_tint_map(entries), ROOT / "icons", enabled, fallback
    )
    if candidate_apps:
        candidate_tints = apply_auto_tints(
            candidate_apps, manual_tint_map(candidate_entries),
            ROOT / "icons", enabled, fallback
        )
        log(f"候选源 tintColor：{sum(1 for _, _, s in candidate_tints if s == 'auto')} 个取自图标。")
    log("")
    log(
        f"tintColor：{sum(1 for _, _, s in tint_results if s == 'auto')} 个取自图标，"
        f"{sum(1 for _, _, s in tint_results if s == 'manual')} 个沿用配置，"
        f"{sum(1 for _, _, s in tint_results if s == 'fallback')} 个回退源主色，"
        f"{sum(1 for _, _, s in tint_results if s == 'none')} 个没取到。"
    )
    log(f"顶层 size：{sized}/{len(apps)} 个应用取自最新版本。")

    def make_repository(meta: dict, group_apps: list[dict],
                        group_featured: set[str] | None = None,
                        label: str = "repository") -> dict:
        repository: dict = {}
        for field in ("name", "identifier", "subtitle", "description",
                      "website", "iconURL", "headerURL", "tintColor"):
            value = meta.get(field)
            if isinstance(value, str) and value.strip():
                repository[field] = value.strip()
        if not repository.get("name"):
            raise SystemExit(f"{label}.name 不能为空。")
        if not repository.get("identifier"):
            warnings.append(f"{label}.identifier 为空，建议补上以免换域名后重复添加")
        if group_featured:
            featured_apps = [
                a["bundleIdentifier"] for a in group_apps
                if a["bundleIdentifier"] in group_featured
            ]
            if featured_apps:
                repository["featuredApps"] = featured_apps
        repository["apps"] = group_apps
        return repository

    text = render(make_repository(repo_meta, apps, featured))
    previous = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else None

    # 候选源不做比例 shrink_guard，只防「一条都没取到」把源写成空壳。
    candidate_text: str | None = None
    candidate_previous: str | None = None
    if candidates_enabled and candidate_apps:
        candidate_text = render(
            make_repository(candidate_meta, candidate_apps, label="candidatesRepository")
        )
        candidate_previous = (
            CANDIDATES_PATH.read_text(encoding="utf-8")
            if CANDIDATES_PATH.exists() else None
        )

    for warning in warnings:
        log(f"警告：{warning}")

    log("")
    log(f"共 {len(apps)} 个应用：")
    for app in apps:
        log(f"  {app['name']:<22} {app['versions'][0]['version']:<14} {app['bundleIdentifier']}")
    if candidate_apps:
        log(f"候选源共 {len(candidate_apps)} 个应用。")

    save_cache(cache, used_keys)

    pruned: list[str] = []
    if args.prune:
        if any("已跳过" in warning for warning in warnings):
            log("")
            log("本轮有条目被跳过，跳过图标清理，避免把暂时取不到的应用当成已删除。")
        else:
            # 两源共用 icons/，清理时按并集保留，避免后一个源把前一个的图标删掉。
            pruned = prune_icons(
                apps + candidate_apps, ROOT / "icons", dry_run=args.check
            )
            if pruned:
                log("")
                log(("将清理 " if args.check else "已清理 ") + f"{len(pruned)} 项无引用图标：")
                for name in pruned:
                    log(f"  - {name}")

    changed_main = previous != text
    changed_candidates = bool(candidate_text and candidate_previous != candidate_text)

    # 额度耗尽或上游抽风时，会有成批应用被跳过。宁可这轮不写，也别把源写瘸。
    # shrink_guard 只作用于正式源；候选源允许部分缺失。
    if changed_main and not args.allow_shrink:
        missing = shrink_guard(previous, apps)
        if missing:
            log("")
            log(f"本次会少掉 {len(missing)} 个应用：")
            for bundle in missing[:10]:
                log(f"  - {bundle}")
            raise SystemExit(
                "疑似额度耗尽或上游异常，已中止且未写入。确认无误后加 --allow-shrink 再跑。"
            )

    if args.check:
        if changed_main or changed_candidates or pruned:
            log("")
            log(
                f"检测到变化（--check 不写入）：repo.json "
                f"{'有' if changed_main else '无'}、candidates.json "
                f"{'有' if changed_candidates else '无'}。"
            )
            return 1
        log("")
        log("repo.json / candidates.json 均无变化。")
        return 0

    if changed_main:
        OUTPUT_PATH.write_text(text, encoding="utf-8", newline="\n")
        log("")
        log("已写入 repo.json")
    else:
        log("")
        log("repo.json 无变化。")
    if changed_candidates:
        CANDIDATES_PATH.write_text(candidate_text, encoding="utf-8", newline="\n")
        log("已写入 candidates.json")
    elif candidate_text:
        log("candidates.json 无变化。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
