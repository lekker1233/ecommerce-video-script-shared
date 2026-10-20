#!/usr/bin/env python3
"""共享版本地归档核心；由 owner 核心生成，未包含其 CLI 或连接配置。"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import uuid


class StoreError(ValueError):
    """可向调用者解释的输入或版本冲突。"""


def require(value, expected, path):
    if not isinstance(value, expected):
        raise StoreError(f"{path} 类型错误，应为 {expected.__name__}")
    return value


def field(obj, key, expected, path, nonempty=False):
    if key not in obj:
        raise StoreError(f"缺少字段 {path}.{key}")
    value = require(obj[key], expected, f"{path}.{key}")
    if nonempty and not value.strip():
        raise StoreError(f"{path}.{key} 不能为空")
    return value


def strings(value, path, nonempty=False):
    require(value, list, path)
    for i, item in enumerate(value):
        require(item, str, f"{path}[{i}]")
        if nonempty and not item.strip():
            raise StoreError(f"{path}[{i}] 不能为空")
    return value


def number(value, path, minimum=0, exclusive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreError(f"{path} 必须为有限数字")
    if not math.isfinite(value) or value < minimum or (exclusive and value == minimum):
        raise StoreError(f"{path} 必须为{'大于' if exclusive else '不小于'} {minimum} 的有限数字")
    return value


def choice(value, options, path):
    if value not in options:
        raise StoreError(f"{path} 必须为 {', '.join(options)} 之一")


def canonical(data):
    # 网站导入必须使用同一算法；不裁剪文案，不做 Unicode 或换行规范化。
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def timeline(items, duration, path):
    warnings = []
    cursor = 0
    for i, item in enumerate(items):
        require(item, dict, f"{path}[{i}]")
        for key in ("start", "end"):
            if key not in item:
                raise StoreError(f"缺少字段 {path}[{i}].{key}")
            number(item[key], f"{path}[{i}].{key}")
        if item["end"] <= item["start"]:
            raise StoreError(f"{path}[{i}].end 必须大于 start")
        if not math.isclose(item["start"], cursor, rel_tol=0, abs_tol=1e-6):
            warnings.append(f"{path}[{i}].start 应为 {cursor}，实际为 {item['start']}")
        cursor = item["end"]
    if not math.isclose(cursor, duration, rel_tol=0, abs_tol=1e-6):
        warnings.append(f"{path} 结束时间 {cursor} 与总时长 {duration} 不一致")
    return warnings


def validate(data):
    """返回草稿可保留的时间问题；不修改、裁剪或删除输入的扩展字段。"""
    require(data, dict, "data")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise StoreError("schema_version 必须为整数 1")
    for key in ("title", "mode", "project", "summary", "workflow_status"):
        field(data, key, str, "data", nonempty=key in ("title", "summary"))
    choice(data["mode"], ("original", "reference", "copy"), "mode")
    choice(data["workflow_status"], ("draft", "ready"), "workflow_status")
    strings(field(data, "tags", list, "data"), "tags", nonempty=True)
    product = field(data, "product", dict, "data")
    for key in ("name", "sku", "category"):
        field(product, key, str, "product", nonempty=key == "name")
    settings = field(data, "settings", dict, "data")
    if "duration_seconds" not in settings:
        raise StoreError("缺少字段 settings.duration_seconds")
    duration = number(settings["duration_seconds"], "settings.duration_seconds", exclusive=True)
    for key in ("aspect_ratio", "language", "audio_mode"):
        field(settings, key, str, "settings", nonempty=True)
    source = field(data, "source", dict, "data")
    for key in ("facts", "assumptions"):
        strings(field(source, key, list, "source"), f"source.{key}", nonempty=True)
    if "copy" not in source or (source["copy"] is not None and not isinstance(source["copy"], str)):
        raise StoreError("source.copy 必须存在，且为字符串或 null")
    policy = field(source, "copy_policy", str, "source")
    choice(policy, ("none", "locked", "adapt"), "source.copy_policy")
    if data["mode"] == "copy":
        if policy == "none" or not isinstance(source["copy"], str) or not source["copy"].strip():
            raise StoreError("copy 模式需要非空 source.copy 和 locked/adapt copy_policy")
    elif policy != "none":
        raise StoreError("original/reference 模式的 copy_policy 应为 none；锁定文案请选择 copy")
    assets = field(source, "assets", list, "source")
    asset_ids = set()
    for i, asset in enumerate(assets):
        require(asset, dict, f"source.assets[{i}]")
        for key in ("id", "kind", "location", "usage"):
            field(asset, key, str, f"source.assets[{i}]", nonempty=True)
        if asset["id"] in asset_ids:
            raise StoreError(f"source.assets 的 id 重复：{asset['id']}")
        asset_ids.add(asset["id"])
    script = field(data, "script", dict, "data")
    for key in ("analysis", "global_settings", "voiceover", "master_prompt", "postproduction", "negative_prompt"):
        field(script, key, str, "script", nonempty=key in ("global_settings", "master_prompt"))
    for key in ("issues", "changes"):
        strings(field(script, key, list, "script"), f"script.{key}", nonempty=True)
    shots = field(script, "shots", list, "script")
    if not shots:
        raise StoreError("script.shots 至少需要一个镜头")
    warnings = timeline(shots, duration, "script.shots")
    for i, shot in enumerate(shots):
        for key in ("visual", "camera", "voiceover", "audio"):
            field(shot, key, str, f"script.shots[{i}]", nonempty=key == "visual")
        refs = strings(field(shot, "asset_ids", list, f"script.shots[{i}]"),
                       f"script.shots[{i}].asset_ids", nonempty=True)
        missing = set(refs) - asset_ids
        if missing:
            raise StoreError(f"script.shots[{i}] 引用了未登记素材：{', '.join(sorted(missing))}")
    segments = field(script, "segments", list, "script")
    if segments:
        warnings += timeline(segments, duration, "script.segments")
        for i, segment in enumerate(segments):
            field(segment, "prompt", str, f"script.segments[{i}]", nonempty=True)
    if policy == "locked":
        if script["voiceover"] != source["copy"]:
            raise StoreError("锁定文案校验失败：script.voiceover 必须逐字等于 source.copy")
        if "".join(shot["voiceover"] for shot in shots) != source["copy"]:
            raise StoreError("锁定文案校验失败：按顺序拼接镜头 voiceover 必须逐字等于 source.copy")
    if data["workflow_status"] == "ready":
        if script["issues"]:
            raise StoreError("ready 不允许存在未解决的 script.issues；请保持 draft 或先解决问题")
        if warnings:
            raise StoreError("ready 时间轴不完整：" + "；".join(warnings))
    elif warnings and not script["issues"]:
        raise StoreError("draft 时间轴有冲突时必须在 script.issues 记录：" + "；".join(warnings))
    # 也验证未知扩展字段可无损转换成标准 JSON；拒绝 NaN/Infinity。
    try:
        canonical(data)
    except (TypeError, ValueError) as exc:
        raise StoreError("数据包含不支持的 JSON 值") from exc
    return warnings


def fenced(text, language="text"):
    """保持文本原样，并避免正文中的反引号提前关闭代码块。"""
    longest = max((len(run) for run in __import__("re").findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}\n"


def render_markdown(data):
    # 数据库存储采用排序后的 JSON；渲染也规范化键序，保证写入前后全文一致。
    data = json.loads(canonical(data))
    source, script = data["source"], data["script"]
    lines = ["# 完整电商视频脚本\n"]

    def section(title, value):
        lines.append(f"## {title}\n")
        lines.append(fenced(value))

    meta = {key: data[key] for key in (
        "title", "mode", "project", "product", "tags", "summary", "workflow_status", "settings")}
    section("归档信息", json.dumps(meta, ensure_ascii=False, indent=2))
    section("商品事实", "\n".join(source["facts"]))
    section("创作假设", "\n".join(source["assumptions"]))
    section("素材清单", json.dumps(source["assets"], ensure_ascii=False, indent=2))
    section("原始文案与处理方式", f"copy_policy: {source['copy_policy']}\n\n" + (source["copy"] or ""))
    for title, key in (
        ("分析与创作依据", "analysis"), ("全局设定", "global_settings"), ("完整口播", "voiceover")):
        section(title, script[key])
    lines.append("## 分镜\n")
    for i, shot in enumerate(script["shots"], 1):
        lines.append(f"### 镜头 {i} · {shot['start']}–{shot['end']} 秒\n")
        for title, key in (("画面", "visual"), ("镜头", "camera"), ("口播原句", "voiceover"), ("声音", "audio")):
            lines.append(f"**{title}**\n")
            lines.append(fenced(shot[key]))
        lines.append("**素材引用**\n")
        lines.append(fenced(json.dumps(shot["asset_ids"], ensure_ascii=False)))
    section("完整连续提示词母版", script["master_prompt"])
    lines.append("## 分段提示词\n")
    if not script["segments"]:
        lines.append("本稿无需分段。\n")
    for i, segment in enumerate(script["segments"], 1):
        lines.append(f"### 片段 {i} · {segment['start']}–{segment['end']} 秒\n")
        lines.append(fenced(segment["prompt"]))
    for title, key in (("后期交接", "postproduction"), ("负面约束", "negative_prompt")):
        section(title, script[key])
    section("待解决问题", "\n".join(script["issues"]))
    section("改动说明", "\n".join(script["changes"]))
    # 扩展字段即使尚无专用版式，也保留在可浏览的完整稿中。
    section("完整结构化原稿（含扩展字段）", json.dumps(data, ensure_ascii=False, indent=2))
    return "\n".join(lines)


def database_path(root):
    return Path(root).expanduser().resolve() / "scripts.sqlite3"


def connect(root, writable=False):
    path = database_path(root)
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    else:
        if not path.is_file():
            raise StoreError("归档库不存在，请先 save；读取命令不会创建空库")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS script_versions (
            script_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            parent_version INTEGER,
            created_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            data_json TEXT NOT NULL,
            full_script_md TEXT NOT NULL,
            sync_status TEXT NOT NULL DEFAULT 'pending' CHECK(sync_status = 'pending'),
            PRIMARY KEY (script_id, version)
        )
    """)


def valid_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise StoreError("script_id 必须为 UUID") from exc


def envelope(row):
    result = dict(row)
    result["data"] = json.loads(result.pop("data_json"))
    result["schema_version"] = 1
    return result


def save(root, data, script_id=None, parent_version=None, before_commit=None):
    warnings = validate(data)
    payload = canonical(data)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    supplied_id = script_id is not None
    script_id = valid_uuid(script_id) if supplied_id else str(uuid.uuid4())
    if parent_version is not None and (type(parent_version) is not int or parent_version < 1):
        raise StoreError("parent_version 必须为正整数")
    if not supplied_id and parent_version is not None:
        raise StoreError("parent_version 必须与 script_id 一起使用")
    with closing(connect(root, writable=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            init_schema(conn)
            latest = conn.execute(
                "SELECT * FROM script_versions WHERE script_id=? ORDER BY version DESC LIMIT 1",
                (script_id,)).fetchone()
            if latest:
                # 最新稿相同可以安全重试；不会把历史旧内容当作最新稿返回。
                if latest["content_hash"] == digest and latest["data_json"] == payload:
                    result = envelope(latest)
                    created = False
                else:
                    if parent_version != latest["version"]:
                        raise StoreError(f"版本冲突：当前最新版本为 {latest['version']}，请重新读取后提交 parent_version")
                    result = None
                    created = True
            else:
                if parent_version is not None:
                    raise StoreError("版本冲突：script_id 尚不存在，首次保存不能指定 parent_version")
                result = None
                created = True
            if result is None:
                version = latest["version"] + 1 if latest else 1
                parent = latest["version"] if latest else None
                created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                conn.execute(
                    """INSERT INTO script_versions
                    (script_id,version,parent_version,created_at,content_hash,data_json,full_script_md)
                    VALUES (?,?,?,?,?,?,?)""",
                    (script_id, version, parent, created_at, digest, payload, render_markdown(data)))
                result = envelope(conn.execute(
                    "SELECT * FROM script_versions WHERE script_id=? AND version=?",
                    (script_id, version)).fetchone())
            if before_commit is not None:
                before_commit(conn, result, created)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "script_id": result["script_id"], "version": result["version"],
        "parent_version": result["parent_version"], "created": created,
        "workflow_status": data["workflow_status"], "sync_status": "pending",
        "database": str(database_path(root)), "validation_warnings": warnings,
    }


def show(root, script_id, version=None):
    script_id = valid_uuid(script_id)
    if version is not None and (type(version) is not int or version < 1):
        raise StoreError("version 必须为正整数")
    with closing(connect(root)) as conn:
        if version is None:
            row = conn.execute(
                "SELECT * FROM script_versions WHERE script_id=? ORDER BY version DESC LIMIT 1",
                (script_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM script_versions WHERE script_id=? AND version=?",
                (script_id, version)).fetchone()
    if row is None:
        raise StoreError("未找到指定脚本或版本")
    return envelope(row)


def list_records(root, mode=None, project=None, limit=100):
    if limit < 1 or limit > 1000:
        raise StoreError("limit 应为 1–1000")
    if not database_path(root).exists():
        return []
    with closing(connect(root)) as conn:
        rows = conn.execute("""
            SELECT v.* FROM script_versions v
            JOIN (SELECT script_id, MAX(version) AS latest FROM script_versions GROUP BY script_id) h
            ON v.script_id=h.script_id AND v.version=h.latest ORDER BY v.created_at DESC
        """)
        records = []
        for row in rows:
            data = json.loads(row["data_json"])
            if (mode is not None and data["mode"] != mode) or (project is not None and data["project"] != project):
                continue
            records.append({
                "script_id": row["script_id"], "version": row["version"], "created_at": row["created_at"],
                "title": data["title"], "mode": data["mode"], "project": data["project"],
                "product": data["product"], "tags": data["tags"], "summary": data["summary"],
                "workflow_status": data["workflow_status"], "sync_status": row["sync_status"],
            })
            if len(records) >= limit:
                break
    return records


def export_record(root, script_id, output, version=None):
    record = show(root, script_id, version)
    output = Path(output).expanduser().resolve()
    if not output.parent.is_dir():
        raise StoreError("导出父目录不存在，请先选择或创建明确的目录")
    # x 模式避免覆盖旧导出、源资料或数据库。
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return {"script_id": record["script_id"], "version": record["version"],
            "sync_status": "pending", "export_file": str(output)}


def reject_constant(value):
    raise StoreError(f"JSON 不允许 {value}")


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StoreError(f"JSON 存在重复字段：{key}")
        result[key] = value
    return result
