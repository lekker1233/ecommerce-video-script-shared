#!/usr/bin/env python3
"""镜稿共享入口 v1：只更新规则文档，代码保持安装时版本。"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, HTTPSHandler, HTTPRedirectHandler, build_opener

import shared_store as store

RUNTIME_VERSION = "2026.10.20.2"
FILES = {"workflow.md", *("references/" + name + ".md" for name in
         ("original", "reference", "copy", "output-contract", "archive", "website-contract"))}
ENVELOPE = ("schema_version", "script_id", "version", "parent_version", "created_at",
            "content_hash", "data", "full_script_md")
DISCLOSURE = ("完整脚本、商品信息和标签将上传至 {server}，该站点管理员可以查看和归纳。"
              "你只能读取自己的脚本；本地素材文件不会自动上传。")


class ClientError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha(data):
    return hashlib.sha256(store.canonical(data).encode("utf-8")).hexdigest()


def private_home():
    return (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") /
            "ecommerce-video-script-shared").expanduser().resolve()


def protect(path, directory=False):
    if os.name == "nt":
        # Windows 用户目录之外不放凭据；明确移除继承，授权当前 Windows 身份。
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, check=True)
        match = re.search(r"S-1-[0-9-]+", result.stdout)
        if not match:
            raise ClientError("permissions", "无法确认当前 Windows 身份，未保存凭据")
        grant = f"*{match.group(0)}:" + ("(OI)(CI)F" if directory else "F")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                       capture_output=True, check=True)
    else:
        path.chmod(0o700 if directory else 0o600)


def ensure_home(home):
    home.mkdir(parents=True, exist_ok=True)
    protect(home, True)


def write_json(path, data, exclusive=False):
    raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(raw)
        protect(path)
    else:
        temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(raw)
        protect(temp)
        os.replace(temp, path)


def read_json(path):
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream, parse_constant=store.reject_constant,
                         object_pairs_hook=store.unique_pairs)


def read_text_exact(path):
    # newline="" 防止 CRLF 被 Python 通用换行转换，规则全文必须逐字核对。
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def origin(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ClientError("server", "站点地址格式无效") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or "\\" in value or re.search(r"\s", value)):
        raise ClientError("server", "共享站点必须为无凭据的 HTTPS 域名地址")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return urlunsplit(("https", host + (f":{port}" if port and port != 443 else ""), "", "", ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("redirect", "接口发生重定向，已停止且未转发成员凭据")


class Transport:
    def __init__(self, server, token=None):
        self.server, self.token = origin(server), token
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def request(self, method, path, data=None, skill_version=None):
        if not path.startswith("/api/") or "://" in path:
            raise ClientError("path", "不允许的接口路径")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if skill_version:
            headers["X-Skill-Version"] = skill_version
        body = None if data is None else store.canonical(data).encode("utf-8")
        request = Request(self.server + path, method=method, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ClientError("response", "接口响应超过限制")
                result = json.loads(raw.decode("utf-8"), parse_constant=store.reject_constant,
                                    object_pairs_hook=store.unique_pairs)
                if not isinstance(result, dict):
                    raise ClientError("response", "接口未返回 JSON 对象")
                return result
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403):
                raise ClientError("authentication", "成员认证失败、邀请码不可用或成员已停用，请联系管理员") from exc
            if code == 409:
                raise ClientError("conflict", "版本、归属或邀请发生冲突；未覆盖内容，请协调后再试") from exc
            raise ClientError("http_" + str(code), f"接口返回 HTTP {code}，本轮停止") from exc
        except (URLError, OSError) as exc:
            raise ClientError("network", "网络或 TLS 校验失败，未能确认线上结果") from exc
        except (UnicodeError, json.JSONDecodeError, store.StoreError) as exc:
            raise ClientError("response", "响应 JSON 无效，未确认线上结果") from exc


def load_config(home):
    path = home / "config.json"
    if not path.is_file():
        raise ClientError("setup", "尚未加入共享站点，请先运行 setup")
    config = read_json(path)
    if (not isinstance(config, dict) or config.get("schema_version") != 1
            or config.get("consent") is not True or not isinstance(config.get("member"), dict)
            or not isinstance(config.get("token"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", config["token"])):
        raise ClientError("config", "共享配置无效；不要把管理员配置复制到这里")
    config["server"] = origin(config.get("server"))
    return config


def setup(home, server, invite_code, consent, transport=None, reset_pending=False):
    if consent is not True:
        raise ClientError("consent", "必须先明确同意完整脚本、商品和标签上传")
    server = origin(server)
    if not isinstance(invite_code, str) or not invite_code.strip() or len(invite_code) > 512:
        raise ClientError("invite", "请输入有效邀请码")
    ensure_home(home)
    if (home / "config.json").exists():
        config = load_config(home)
        if config["server"] != server:
            raise ClientError("config", "已有其他站点配置，不能静默更换上传目标")
        return {"configured": True, "member": config["member"], "server": server}
    pending_path = home / "setup-pending.json"
    invite_hash = hashlib.sha256(invite_code.encode("utf-8")).hexdigest()
    if pending_path.exists():
        pending = read_json(pending_path)
        if pending.get("server") != server or pending.get("invite_hash") != invite_hash:
            if not reset_pending:
                raise ClientError(
                    "setup_pending",
                    "有未完成的邀请兑换：网络中断请用原邀请码继续；明确换新邀请请加 --reset-pending")
            # 保留旧尝试供管理员协调；有 config 的已连接身份在上方返回，不允许重置。
            backup = home / "setup-attempts"
            backup.mkdir(exist_ok=True)
            write_json(backup / (secrets.token_hex(16) + ".json"), pending, exclusive=True)
            pending = {"server": server, "invite_hash": invite_hash, "token": secrets.token_urlsafe(32)}
            write_json(pending_path, pending)
    else:
        pending = {"server": server, "invite_hash": invite_hash, "token": secrets.token_urlsafe(32)}
        write_json(pending_path, pending, exclusive=True)
    result = (transport or Transport(server)).request(
        "POST", "/api/shared/redeem",
        {"invite_code": invite_code, "token": pending["token"], "consent": True})
    member = result.get("member")
    if (not isinstance(member, dict) or not isinstance(member.get("id"), str)
            or not isinstance(member.get("name"), str) or origin(result.get("base_url")) != server):
        raise ClientError("response", "邀请回执身份或站点不一致，未完成连接")
    member = {"id": member["id"], "name": member["name"]}
    config = {"schema_version": 1, "server": server, "member": member,
              "token": pending["token"], "consent": True, "consented_at": now()}
    write_json(home / "config.json", config, exclusive=True)
    return {"configured": True, "member": member, "server": server,
            "upload_disclosure": DISCLOSURE.format(server=server)}


def validate_release(release):
    if (not isinstance(release, dict) or type(release.get("schema_version")) is not int
            or release["schema_version"] != 1 or not isinstance(release.get("version"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", release["version"])):
        raise ClientError("release", "规则版本格式无效")
    files = release.get("files")
    if (not isinstance(files, dict) or "workflow.md" not in files or not set(files) <= FILES
            or any(not isinstance(v, str) or not v.strip() or "\0" in v
                   or len(v.encode("utf-8")) > 256 * 1024 for v in files.values())
            or sum(len(v.encode("utf-8")) for v in files.values()) > 1024 * 1024):
        raise ClientError("release", "规则文件不在 Markdown 白名单或内容过大")
    if not isinstance(release.get("sha256"), str) or not secrets.compare_digest(
            sha(files), release["sha256"] if re.fullmatch(r"[a-f0-9]{64}", release["sha256"]) else ""):
        raise ClientError("release_hash", "规则 SHA-256 不匹配，未使用本次版本")
    return release


def refresh(home, transport=None):
    config = load_config(home)
    release = validate_release((transport or Transport(config["server"], config["token"])).request(
        "GET", "/api/shared/manifest"))
    folder = home / "releases" / (release["version"] + "." + release["sha256"][:16])
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"server": config["server"], "release": release}
    path = folder / "manifest.json"
    if path.exists() and read_json(path) != manifest:
        raise ClientError("release", "缓存版本内容冲突，未覆盖")
    for name, text in release["files"].items():
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if read_text_exact(dest) != text:
                raise ClientError("release", "已缓存规则被修改；请核对后重新获取")
        else:
            with dest.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
    if not path.exists():
        write_json(path, manifest, exclusive=True)
    return {"fresh": True, "checked_at": now(), "version": release["version"],
            "sha256": release["sha256"], "release_file": str(path),
            "release_directory": str(folder), "workflow_file": str(folder / "workflow.md")}


def pinned_release(home, path, server):
    path = Path(path).expanduser().resolve()
    if not path.is_relative_to((home / "releases").resolve()) or path.name != "manifest.json":
        raise ClientError("release", "请使用 refresh 返回的本次 manifest.json 路径")
    cached = read_json(path)
    if cached.get("server") != server:
        raise ClientError("release", "任务规则来自其他站点，未上传")
    release = validate_release(cached.get("release"))
    for name, text in release["files"].items():
        if read_text_exact(path.parent / name) != text:
            raise ClientError("release", "本次规则文件已变动，请核对任务版本")
    return release


def init_shared(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS shared_deliveries (
        script_id TEXT NOT NULL, version INTEGER NOT NULL, server TEXT NOT NULL,
        skill_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        error_code TEXT, verified_at TEXT, PRIMARY KEY(script_id,version))""")


def equal_envelope(left, right):
    return all(key in right for key in ENVELOPE) and store.canonical(
        {key: left[key] for key in ENVELOPE}) == store.canonical({key: right[key] for key in ENVELOPE})


def synchronize(home, script_id=None, transport=None):
    config = load_config(home)
    root = home / "library"
    if script_id:
        script_id = store.valid_uuid(script_id)
    if not store.database_path(root).is_file():
        return {"status": "synced", "synced": 0, "pending": 0}
    with closing(store.connect(root, writable=True)) as conn:
        init_shared(conn)
        query = """SELECT v.*,d.skill_version,d.status AS delivery_status,d.server
                   FROM script_versions v LEFT JOIN shared_deliveries d
                   ON v.script_id=d.script_id AND v.version=d.version"""
        args = []
        if script_id:
            query += " WHERE v.script_id=?"
            args.append(script_id)
        rows = conn.execute(query + " ORDER BY v.script_id,v.version", args).fetchall()
    client = transport or Transport(config["server"], config["token"])
    completed = sum(row["delivery_status"] == "synced" for row in rows)
    for row in rows:
        if row["server"] != config["server"] or not row["skill_version"]:
            return {"status": "pending", "error_code": "release",
                    "message": "本地版本缺少同站点规则归属，未上传", "synced": completed,
                    "pending": len(rows) - completed}
        if row["delivery_status"] == "synced":
            continue
        record = store.envelope(row)
        payload = {key: record[key] for key in ENVELOPE}
        endpoint = f"/api/v1/scripts/{record['script_id']}/versions/{record['version']}"
        try:
            receipt = client.request("POST", "/api/v1/script-versions", payload, row["skill_version"])
            if (receipt.get("script_id") != record["script_id"]
                    or type(receipt.get("version")) is not int or receipt["version"] != record["version"]
                    or receipt.get("content_hash") != record["content_hash"]):
                raise ClientError("verification", "保存回执与本地版本不一致")
            received = client.request("GET", endpoint)
            if not equal_envelope(record, received) or sha(received["data"]) != record["content_hash"]:
                raise ClientError("verification", "线上全文或数据回读不一致，仍为待同步")
            with closing(store.connect(root, writable=True)) as conn:
                conn.execute("""UPDATE shared_deliveries SET status='synced',error_code=NULL,verified_at=?
                    WHERE script_id=? AND version=?""", (now(), record["script_id"], record["version"]))
            completed += 1
        except ClientError as exc:
            with closing(store.connect(root, writable=True)) as conn:
                conn.execute("UPDATE shared_deliveries SET error_code=? WHERE script_id=? AND version=?",
                             (exc.code, record["script_id"], record["version"]))
            return {"status": "pending", "error_code": exc.code, "message": str(exc),
                    "synced": completed, "pending": len(rows) - completed}
    return {"status": "synced", "synced": completed, "pending": 0}


def save(home, data, release_file, script_id=None, parent_version=None, transport=None):
    config = load_config(home)
    release = pinned_release(home, release_file, config["server"])
    root = home / "library"
    provenance = {}

    def record_provenance(conn, record, created):
        # 与全文使用同一事务：崩溃不会留下规则版本不明的稿件。
        init_shared(conn)
        old = conn.execute("SELECT * FROM shared_deliveries WHERE script_id=? AND version=?",
                           (record["script_id"], record["version"])).fetchone()
        if old is None:
            if not created:
                raise ClientError("release", "已有版本缺少原创作规则记录，不能猜测归属")
            conn.execute("""INSERT INTO shared_deliveries(script_id,version,server,skill_version)
                VALUES (?,?,?,?)""",
                         (record["script_id"], record["version"], config["server"], release["version"]))
            provenance["version"] = release["version"]
        elif old["server"] != config["server"]:
            raise ClientError("release", "已有版本属于其他站点，未更换上传目标")
        else:
            provenance["version"] = old["skill_version"]

    saved = store.save(root, data, script_id, parent_version, before_commit=record_provenance)
    saved["local_saved"] = True
    try:
        saved["remote_sync"] = synchronize(home, saved["script_id"], transport)
    except (ClientError, OSError, sqlite3.Error, ValueError):
        saved["remote_sync"] = {"status": "pending", "message": "已保存本地，远程核验未完成，请稍后 sync"}
    saved["skill_version"] = provenance["version"]
    return saved


def runtime_update(force=False):
    """由独立进程更换安装目录，避免沿用已导入的旧客户端代码。"""
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve().is_relative_to(root):
        os.chdir(root.parent)
    command = [sys.executable, "-B", "-X", "utf8", str(root / "scripts" / "update_skill.py")]
    if force:
        command.append("--force")
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                             cwd=root.parent, timeout=150,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ClientError("update", "更新检查未返回有效结果，未开始新创作") from exc
    if process.returncode != 0 or result.get("status") == "error":
        raise ClientError("update", result.get("message", "无法确认客户端更新，请手动使用 --force 重试"))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("setup")
    p.add_argument("--server", required=True)
    p.add_argument("--invite-code")
    p.add_argument("--consent-upload", action="store_true")
    p.add_argument("--reset-pending", action="store_true",
                   help="尚未连接时明确换新邀请，旧未完成兑换记录保留私有备份；同邀请码仍复用原令牌")
    p = subs.add_parser("refresh")
    p.add_argument("--force-update", action="store_true", help="强制检查 GitHub 客户端新版本")
    p.add_argument("--after-runtime-update", action="store_true", help=argparse.SUPPRESS)
    p = subs.add_parser("save")
    p.add_argument("--input", required=True)
    p.add_argument("--release-file", required=True)
    p.add_argument("--script-id")
    p.add_argument("--parent-version", type=int)
    p = subs.add_parser("sync")
    p.add_argument("--script-id")
    for command in ("show", "export"):
        p = subs.add_parser(command)
        p.add_argument("--script-id", required=True)
        p.add_argument("--version", type=int)
        if command == "export":
            p.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        home = private_home()
        if args.command == "setup":
            consent = args.consent_upload
            if not consent:
                print(DISCLOSURE.format(server=origin(args.server)), file=sys.stderr)
                consent = input("明确同意请键入 YES：").strip() == "YES"
            if not consent:
                raise ClientError("consent", "未同意上传；未兑换邀请或保存凭据")
            code = args.invite_code or getpass.getpass("邀请码（隐藏输入）：")
            result = setup(home, args.server, code, consent, reset_pending=args.reset_pending)
        elif args.command == "refresh":
            update = runtime_update(args.force_update)
            if update.get("updated"):
                if args.after_runtime_update:
                    raise ClientError("update", "刷新过程中再次发现客户端变化，请重新运行 refresh")
                # 子进程重新导入新代码；同日成功缓存阻止二次网络检查。
                root = Path(__file__).resolve().parents[1]
                process = subprocess.run(
                    [sys.executable, "-B", "-X", "utf8", str(root / "scripts/shared_client.py"),
                     "refresh", "--after-runtime-update"], capture_output=True, text=True,
                    encoding="utf-8", cwd=root.parent, timeout=150,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if process.returncode != 0:
                    raise ClientError("refresh", "客户端已更新，重新读取站点规则未完成，请重试 refresh")
                result = json.loads(process.stdout)
                result["runtime_update"] = update
                result["skill_file"] = update["skill_file"]
                print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
                return 0
            result = refresh(home)
            result["runtime_update"] = update
            result["skill_file"] = update["skill_file"]
        elif args.command == "save":
            result = save(home, read_json(Path(args.input)), args.release_file,
                          args.script_id, args.parent_version)
        elif args.command == "sync":
            result = synchronize(home, args.script_id)
        elif args.command == "show":
            result = store.show(home / "library", args.script_id, args.version)
        else:
            result = store.export_record(home / "library", args.script_id, args.output, args.version)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 3 if args.command == "sync" and result["status"] != "synced" else 0
    except (ClientError, store.StoreError, OSError, sqlite3.Error, ValueError,
            subprocess.SubprocessError, EOFError) as exc:
        code = exc.code if isinstance(exc, ClientError) else "local_error"
        message = str(exc) if isinstance(exc, (ClientError, store.StoreError)) else "本地配置或文件操作未完成"
        print(json.dumps({"error_code": code, "message": message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
