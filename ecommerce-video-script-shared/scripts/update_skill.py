#!/usr/bin/env python3
"""每日首次使用检查 GitHub 发布；--force 强制重试或修复已校验安装。"""

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

NAME = "ecommerce-video-script-shared"
REPOSITORY = "lekker1233/ecommerce-video-script-shared"
FILES = frozenset(("SKILL.md", "agents/openai.yaml", "scripts/shared_client.py",
                   "scripts/shared_store.py", "scripts/update_skill.py", "release.json"))
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_MANIFEST = 64 * 1024


class UpdateError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise UpdateError("manifest", "JSON 包含重复字段")
            result[key] = value
        return result

    def invalid(value):
        raise UpdateError("manifest", "JSON 含非有限数值")

    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError("manifest", "更新资料不是有效 UTF-8 JSON") from exc


def validate_manifest(value):
    required = {"schema_version", "repository", "version", "ref", "skill_path", "files"}
    if (not isinstance(value, dict) or set(value) != required
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("repository") != REPOSITORY or value.get("skill_path") != NAME):
        raise UpdateError("manifest", "发布清单格式或固定仓库不匹配")
    version = value.get("version")
    if (not isinstance(version, str) or len(version) > 64
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,3}", version)
            or value.get("ref") != "v" + version):
        raise UpdateError("manifest", "版本须为数字点分格式，ref 必须是该版本的 v 标签")
    files = value.get("files")
    if (not isinstance(files, dict) or set(files) != FILES
            or any(not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{64}", x) for x in files.values())):
        raise UpdateError("manifest", "发布文件路径或 SHA-256 不在固定白名单")
    return value


def version_tuple(value):
    numbers = tuple(int(part) for part in value.split("."))
    return numbers + (0,) * (4 - len(numbers))


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def private_home():
    return codex_home() / NAME


def is_link(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def installation(root, base=None):
    root = Path(root).absolute()
    base = Path(base or codex_home()).resolve()
    expected = base / "skills" / NAME
    if root != expected or root.resolve() != expected:
        # 开发源码可读规则，但不能把源码工作区替换为公开安装包。
        if (root.name == NAME and root.parent.name == "shared-skill"
                and (root.parent.parent / "docs" / "shared-skill-contract.md").is_file()):
            return False
        raise UpdateError("installation", "自动更新仅支持当前用户 CODEX_HOME/skills 下的正式安装目录")
    if not root.is_dir() or is_link(root) or is_link(root.parent):
        raise UpdateError("installation", "安装目录缺失或是链接，未更新")
    if any((parent / ".git").exists() for parent in (root, root.parent, base)):
        raise UpdateError("source_checkout", "Git 源码目录不自动更新")
    return True


def protect(path, directory=False):
    if os.name == "nt":
        options = {"capture_output": True, "check": True, "creationflags": subprocess.CREATE_NO_WINDOW}
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], text=True, **options)
        match = re.search(r"S-1-[0-9-]+", result.stdout)
        if not match:
            raise UpdateError("permissions", "无法确认当前用户权限")
        grant = f"*{match.group(0)}:" + ("(OI)(CI)F" if directory else "F")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], **options)
    else:
        path.chmod(0o700 if directory else 0o600)


def directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if is_link(path):
        raise UpdateError("filesystem", "更新私有目录不能是链接或 Windows junction")


def write_json(path, value):
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


@contextmanager
def locked(path):
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError("busy", "另一个进程正在检查更新，请稍后重试") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpdateError("redirect", "GitHub 更新请求被重定向，已停止")


class RawClient:
    def __init__(self):
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def fetch(self, ref, name, limit=MAX_FILE):
        if (ref != "main" and not re.fullmatch(r"v[0-9]+(?:\.[0-9]+){1,3}", ref)
                or name not in FILES | {"distribution.json"}):
            raise UpdateError("path", "更新路径不在白名单")
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{ref}/{NAME}/{name}"
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
            raise UpdateError("host", "更新主机无效")
        # GitHub 公共读取不使用任何成员、管理员或 GitHub 登录凭据。
        request = Request(url, headers={"Accept": "application/octet-stream",
                                       "User-Agent": "Codex-Shared-Skill-Updater/1"})
        try:
            with self.opener.open(request, timeout=12) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise UpdateError("size", "更新文件超过大小限制")
                return raw
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise UpdateError("http", f"公开更新源返回 HTTP {status}；可使用 --force 重试") from exc
        except (URLError, OSError) as exc:
            raise UpdateError("network", "无法通过 HTTPS 校验访问公开更新源；可使用 --force 重试") from exc


def inspect_files(root, manifest):
    modified = []
    total = 0
    for name, wanted in manifest["files"].items():
        path = root / name
        if (not path.is_file() or any(is_link(p) for p in (path, path.parent))
                or path.stat().st_size > MAX_FILE):
            modified.append(name)
            continue
        raw = path.read_bytes()
        total += len(raw)
        if digest(raw) != wanted:
            modified.append(name)
    if total > MAX_TOTAL:
        raise UpdateError("size", "本地安装文件超过总大小限制")
    return modified


def unmanaged_files(root):
    for path in root.rglob("*"):
        if is_link(path):
            raise UpdateError("installation", "安装目录含链接，未替换")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative not in FILES | {"distribution.json"}:
                if "__pycache__" in path.parts and path.suffix == ".pyc":
                    continue
                raise UpdateError("unmanaged_files", "安装目录含用户自行添加的文件，请先移到 Skill 目录外")


def switch_installation(root, stage, backup):
    """同卷 rename 切换完整目录；第二步失败立刻恢复原目录，备份不进入技能发现路径。"""
    if (root.stat().st_dev != stage.stat().st_dev or root.stat().st_dev != backup.parent.stat().st_dev):
        raise UpdateError("filesystem", "更新暂存、备份与安装目录必须在同一文件系统")
    if backup.exists() or stage.resolve() == root.resolve():
        raise UpdateError("filesystem", "更新目标路径冲突")
    # Windows 的当前工作目录句柄不能继续留在待重命名目录内。
    if Path.cwd().resolve().is_relative_to(root.resolve()):
        os.chdir(root.parent)
    os.replace(root, backup)
    try:
        os.replace(stage, root)
    except BaseException:
        os.replace(backup, root)
        raise


def check_update(root=None, force=False, *, base=None, fetcher=None, today=None):
    root = Path(root or Path(__file__).resolve().parents[1]).absolute()
    base = Path(base or codex_home()).resolve()
    skill_file = str(root / "SKILL.md")
    if not installation(root, base):
        return {"status": "skipped", "reason": "source_checkout", "updated": False, "checked": False,
                "version": None, "skill_file": skill_file, "message": "源码开发目录不自动安装公开更新"}
    home = base / NAME
    directory(home)
    protect(home, True)
    state_dir = home / "update-state"
    directory(state_dir)
    key = digest(str(root).encode("utf-8"))[:16]
    state_path = state_dir / (key + ".json")
    lock_path = state_dir / (key + ".lock")
    for path in (state_path, lock_path):
        if path.exists() and is_link(path):
            raise UpdateError("filesystem", "更新状态文件不能是链接")
    day = today or datetime.now().astimezone().date().isoformat()
    with locked(lock_path):
        local_path = root / "distribution.json"
        if local_path.stat().st_size > MAX_MANIFEST or is_link(local_path):
            raise UpdateError("manifest", "本地发布清单过大或是链接")
        local = validate_manifest(decode(local_path.read_bytes()))
        local_id = digest(canonical(local).encode("utf-8"))
        result = {"status": "ready", "updated": False, "checked": False, "cached": False,
                  "version": local["version"], "skill_file": skill_file, "date": day}
        try:
            modified = inspect_files(root, local)
            if modified and not force:
                raise UpdateError("local_modified", "本地安装文件已修改或损坏；未覆盖，可用 --force 核验并备份修复")
            if state_path.is_file() and not force:
                cached = decode(state_path.read_bytes())
                if cached.get("date") == day and cached.get("installation") == local_id:
                    if cached.get("status") == "error":
                        return {**result, **cached["result"], "cached": True, "checked": False, "updated": False}
                    return {**result, "cached": True}
            fetcher = fetcher or RawClient()
            raw_manifest = fetcher.fetch("main", "distribution.json", MAX_MANIFEST)
            if len(raw_manifest) > MAX_MANIFEST:
                raise UpdateError("size", "发布清单超过大小限制")
            remote = validate_manifest(decode(raw_manifest))
            if version_tuple(remote["version"]) < version_tuple(local["version"]):
                raise UpdateError("downgrade", "远端发布版本较旧，未自动降级")
            same_version = version_tuple(remote["version"]) == version_tuple(local["version"])
            if same_version and remote != local:
                raise UpdateError("immutable_version", "同一版本的发布清单已改变，拒绝覆盖，请发布新版本")
            result["checked"] = True
            if not same_version or modified:
                unmanaged_files(root)
                stage_parent, backup_parent = home / "update-staging", home / "update-backups"
                directory(stage_parent)
                directory(backup_parent)
                suffix = secrets.token_hex(12)
                stage, backup = stage_parent / suffix, backup_parent / (local["version"] + "-" + suffix)
                stage.mkdir()
                total = 0
                for name, wanted in remote["files"].items():
                    raw = fetcher.fetch(remote["ref"], name, MAX_FILE)
                    total += len(raw)
                    if len(raw) > MAX_FILE or total > MAX_TOTAL:
                        raise UpdateError("size", "更新文件超过总大小限制")
                    if digest(raw) != wanted:
                        raise UpdateError("hash", "更新文件 SHA-256 不匹配，原安装保持不变")
                    if name.endswith(".py"):
                        try:
                            compile(raw, name, "exec")
                        except (SyntaxError, UnicodeError) as exc:
                            raise UpdateError("python", "新版 Python 文件未通过语法校验") from exc
                    destination = stage / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(raw)
                write_json(stage / "distribution.json", remote)
                if inspect_files(stage, remote):
                    raise UpdateError("hash", "暂存目录复核失败，原安装保持不变")
                switch_installation(root, stage, backup)
                result.update(updated=True, version=remote["version"], backup_directory=str(backup),
                              repaired=bool(modified))
                local_id = digest(canonical(remote).encode("utf-8"))
            write_json(state_path, {"date": day, "installation": local_id, "status": "ready"})
            return result
        except (UpdateError, OSError, subprocess.SubprocessError) as exc:
            code = exc.code if isinstance(exc, UpdateError) else "local_error"
            message = str(exc) if isinstance(exc, UpdateError) else "更新未完成；原安装或备份保留，请检查文件权限后 --force 重试"
            if isinstance(exc, PermissionError):
                code = "file_in_use"
                message = "安装目录正被占用或权限不足；请退出该目录并关闭占用程序，从 Skill 目录外用更新脚本绝对路径加 --force 重试"
            failed = {**result, "status": "error", "checked": False, "error_code": code, "message": message}
            write_json(state_path, {"date": day, "installation": local_id, "status": "error", "result": failed})
            return failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="强制检查；本地包损坏时备份后恢复经过哈希核验的发布文件")
    args = parser.parse_args(argv)
    try:
        result = check_update(force=args.force)
    except (UpdateError, OSError, subprocess.SubprocessError) as exc:
        result = {"status": "error", "updated": False, "checked": False,
                  "error_code": exc.code if isinstance(exc, UpdateError) else "local_error",
                  "message": str(exc) if isinstance(exc, UpdateError) else "安装目录或更新缓存不可读写"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
