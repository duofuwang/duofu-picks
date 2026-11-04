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
import io
import json
import os
import plistlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "repo.json"
CACHE_PATH = ROOT / "appinfo.json"

API_ROOT = "https://api.github.com"
USER_AGENT = "duofu-picks/1.0 (AltStore source builder)"
DEFAULT_ASSET_PATTERN = r"\.ipa$"
VERSION_SPLIT = re.compile(r"[.\-_+ ]")
MAX_DESCRIPTION = 800


class FetchError(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


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

    per_page = max(10, min(100, max(max_versions, 1) * 4))
    releases = api_get(f"{API_ROOT}/repos/{slug}/releases?per_page={per_page}")
    if not isinstance(releases, list) or not releases:
        raise FetchError("没有任何 Release")

    versions: list[dict] = []
    seen: set[str] = set()
    for release in releases:
        if release.get("draft"):
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
    detected: dict = {}

    cache_key = f"{slug}@{latest['version']}"
    needs_detect = detect or not bundle or not display_name
    if needs_detect:
        detected = cache.get(cache_key) or {}
        if not detected:
            try:
                detected = read_ipa_info(latest["downloadURL"])
                detected["source_repo"] = slug
                cache[cache_key] = detected
                log(f"    探测 IPA 元数据成功：{detected.get('bundleIdentifier')}")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{slug} 读取 IPA 元数据失败：{exc}")
                detected = {}
        else:
            log(f"    使用缓存的 IPA 元数据：{detected.get('bundleIdentifier')}")
    used_keys.add(cache_key)

    if not bundle:
        bundle = detected.get("bundleIdentifier")
    if not bundle:
        raise FetchError("缺少 bundleIdentifier，且无法从 IPA 中探测到")

    name = display_name or detected.get("displayName") or slug.split("/")[-1]

    if detected.get("build") and "buildVersion" not in latest:
        latest["buildVersion"] = str(detected["build"])

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
        try:
            written = extract_icon_from_remote(
                latest["downloadURL"], str(bundle), ROOT / "icons"
            )
            if written:
                icon = f"{base_url.rstrip('/')}/icons/{urllib.parse.quote(written.name)}"
                log(f"    提取图标 → {written.name}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{slug} 图标提取失败，继续尝试仓库 logo：{exc}")
    if not icon and base_url:
        try:
            written = find_repo_icon(slug, str(bundle), ROOT / "icons")
            if written:
                icon = f"{base_url.rstrip('/')}/icons/{urllib.parse.quote(written.name)}"
                log(f"    仓库 logo → {written.name}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{slug} 仓库 logo 探测失败：{exc}")
    if not icon:
        icon = f"https://github.com/{owner}.png?size=512"
        warnings.append(f"{slug} 没有可用图标，已回退为开发者头像")
    app["iconURL"] = icon
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
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", bundle)
    target = icons_dir / f"{safe}.png"
    target.write_bytes(payload)
    return target


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
        if entry.get("minOSVersion"):
            item["minOSVersion"] = str(entry["minOSVersion"])
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
    app["iconURL"] = icon
    app["versions"] = versions

    log(f"  {app['name']:<22} {versions[0]['version']:<14} 直链      {url[:44]}")
    return app


def extract_icon_from_remote(download_url: str, bundle: str, icons_dir: Path) -> Path | None:
    """从远程 IPA 里取一张最大的 AppIcon 存到 icons/，返回写出的文件。

    图标已存在时直接复用，避免每次构建都去拉一遍。
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", bundle)
    target = icons_dir / f"{safe}.png"
    if target.exists() and target.stat().st_size > 0:
        return target

    remote = RemoteFile(download_url)
    try:
        with zipfile.ZipFile(remote) as archive:
            candidates = [
                (archive.getinfo(name).file_size, name)
                for name in archive.namelist()
                if re.match(r"^Payload/[^/]+\.app/AppIcon[^/]*\.png$", name)
            ]
            if not candidates:
                return None
            candidates.sort(reverse=True)
            payload = archive.read(candidates[0][1])
    finally:
        remote.close()

    if not payload:
        return None
    icons_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


ICON_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
ICON_DIR_HINT = re.compile(r"(^|/)(assets?|resources?|images?|icons?|\.github)/", re.I)
ICON_NOISE = re.compile(r"(screenshot|preview|banner|header|background|placeholder|badge)", re.I)


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


def mirror_external_icon(url: str, bundle: str, icons_dir: Path) -> Path | None:
    """把配置里的外链图标下载到 icons/，让源不再依赖第三方图床。

    网络失败时返回 None，调用方保留原地址；拿到的不是图片时抛 IconInvalid，
    调用方改从 IPA 或仓库里找，避免把一个坏链留在源里。
    """
    if not url.startswith(("http://", "https://")):
        return None
    cached = _existing_icon(bundle, icons_dir)
    if cached:
        return cached
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except Exception as exc:  # noqa: BLE001
        log(f"    镜像图标失败，保留原地址：{exc}")
        return None
    suffix = _icon_suffix(data)
    if not suffix or len(data) < 100:
        raise IconInvalid(f"{len(data)} 字节，开头 {data[:12]!r}")
    icons_dir.mkdir(parents=True, exist_ok=True)
    target = icons_dir / f"{_safe_name(bundle)}{suffix}"
    target.write_bytes(data)
    if len(data) < 2048:
        log(f"    图标偏小（{len(data)} 字节），可能不够清晰")
    return target


def find_repo_icon(slug: str, bundle: str, icons_dir: Path) -> Path | None:
    """IPA 里没有独立 PNG 时（图标编进 Assets.car），从仓库源码里找一张 logo。"""
    cached = _existing_icon(bundle, icons_dir)
    if cached:
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
        if re.search(r"icon|logo", base):
            value += 40
        if project and project in base.replace("-", "").replace("_", ""):
            value += 25
        if ICON_DIR_HINT.search(low):
            value += 20
        if low.count("/") <= 1:
            value += 10
        if ICON_NOISE.search(low):
            value -= 60
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
        icons_dir.mkdir(parents=True, exist_ok=True)
        target = icons_dir / f"{_safe_name(bundle)}{suffix}"
        target.write_bytes(data)
        return target
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
    if entry.get("minOSVersion"):
        item["minOSVersion"] = str(entry["minOSVersion"])

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
    app["iconURL"] = icon
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

def render(repository: dict) -> str:
    return json.dumps(repository, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="从 GitHub Release 生成 AltStore 源")
    parser.add_argument("--check", action="store_true", help="只检查是否有变化")
    parser.add_argument("--detect", action="store_true", help="强制刷新 IPA 元数据探测")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        raise SystemExit(f"找不到配置文件：{CONFIG_PATH}")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    repo_meta = config.get("repository") or {}
    entries = config.get("apps") or []
    if not entries:
        raise SystemExit("config.json 的 apps 为空。")

    max_versions = int(config.get("maxVersions") or 0)
    default_prereleases = bool(config.get("includePrereleases") or False)
    detect = bool(config.get("detectAppInfo") or args.detect)

    cache = load_cache()
    used_keys: set[str] = set()
    warnings: list[str] = []

    log(f"读取 {len(entries)} 个上游仓库…")
    apps: list[dict] = []
    featured: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
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
            label = entry.get("repo") or entry.get("file") or entry.get("ipa") or "未命名条目"
            warnings.append(f"{label} 已跳过：{exc}")
            continue
        if app is None:
            continue
        apps.append(app)
        if entry.get("featured"):
            featured.add(app["bundleIdentifier"])

    if not apps:
        for warning in warnings:
            log(f"警告：{warning}")
        raise SystemExit("没有取到任何可用应用，中止以免覆盖现有 repo.json。")

    apps.sort(key=lambda a: a["name"].lower())

    repository: dict = {}
    for field in ("name", "identifier", "subtitle", "description",
                  "website", "iconURL", "headerURL", "tintColor"):
        value = repo_meta.get(field)
        if isinstance(value, str) and value.strip():
            repository[field] = value.strip()
    if not repository.get("name"):
        raise SystemExit("repository.name 不能为空。")
    if not repository.get("identifier"):
        warnings.append("repository.identifier 为空，建议补上以免换域名后重复添加")

    featured_apps = [a["bundleIdentifier"] for a in apps if a["bundleIdentifier"] in featured]
    if featured_apps:
        repository["featuredApps"] = featured_apps
    repository["apps"] = apps

    text = render(repository)
    previous = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else None

    for warning in warnings:
        log(f"警告：{warning}")

    log("")
    log(f"共 {len(apps)} 个应用：")
    for app in apps:
        log(f"  {app['name']:<22} {app['versions'][0]['version']:<14} {app['bundleIdentifier']}")

    save_cache(cache, used_keys)

    if previous == text:
        log("")
        log("repo.json 无变化。")
        return 0

    if args.check:
        log("")
        log("repo.json 需要更新（--check 模式不写入）。")
        return 1

    OUTPUT_PATH.write_text(text, encoding="utf-8", newline="\n")
    log("")
    log(f"已写入 {OUTPUT_PATH.name}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
