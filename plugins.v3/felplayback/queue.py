"""File spool shared with an offline, unprivileged worker. No Docker API."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import xml.etree.ElementTree as ET


def write_json(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        os.fchmod(fd, 0o660)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            try:
                os.link(name, path)
            except FileExistsError:
                return False
        else:
            os.replace(name, path)
        return True
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def confined(root, value):
    root = Path(root).resolve(strict=True)
    value = Path(value).resolve(strict=True)
    if value == root or not value.is_relative_to(root):
        raise ValueError("路径不在配置的影片目录中")
    return value


def fingerprint(path):
    s = Path(path).stat()
    return {"size": s.st_size, "mtime_ns": s.st_mtime_ns}


def select_nfo(video):
    video = Path(video)
    candidates = [video.with_suffix(".nfo")]
    # movie.nfo is only unambiguous in a one-video movie directory.
    if len([p for p in video.parent.iterdir() if p.suffix.lower() in {".mkv", ".mp4", ".m4v", ".m2ts", ".ts", ".iso"}]) == 1:
        candidates.append(video.parent / "movie.nfo")
    for p in candidates:
        if p.is_file() and not p.is_symlink():
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                node = ET.fromstring(p.read_bytes())
                if node.tag in {"movie", "episodedetails"} and node.findtext("title"):
                    return p
            except (ET.ParseError, OSError):
                continue
    return None


class Spool:
    def __init__(self, root, shared_gid=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if shared_gid is not None:
            os.chown(self.root, -1, int(shared_gid))
            self.root.chmod(0o2770)
        for sub in ("candidates", "requests", "state", "cancel", "retry"):
            folder = self.root / sub
            folder.mkdir(parents=True, exist_ok=True)
            if shared_gid is not None:
                os.chown(folder, -1, int(shared_gid))
                folder.chmod(0o2770)

    def offer(self, path, mappings):
        for m in mappings:
            try:
                p = confined(m["mp_root"], path)
            except (ValueError, FileNotFoundError):
                continue
            if not p.is_file() or p.suffix.lower() not in {".mkv", ".mp4", ".m4v", ".m2ts", ".ts", ".iso"}:
                return None
            rel = p.relative_to(Path(m["mp_root"]).resolve()).as_posix()
            stamp = fingerprint(p)
            identity = {"root_id": m["id"], "relative": rel, **stamp}
            jobid = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            candidate = {"schema": 1, "id": jobid, **identity, "mp_path": str(p), "offered_at": time.time()}
            write_json(self.root / "candidates" / (jobid + ".json"), candidate, exclusive=True)
            return jobid
        raise ValueError("影片不在插件允许的目录中，或是越界的符号链接")

    def ready(self, mappings, stable_seconds=120, predicate=None):
        """MetadataScrape is a request event, never a completion notification."""
        roots = {m["id"]: m for m in mappings}
        for file in (self.root / "candidates").glob("*.json"):
            item = read_json(file)
            if not item or (self.root / "requests" / file.name).exists():
                continue
            if read_json(self.root / "state" / file.name, {}).get("status") == "skipped":
                continue
            m = roots.get(item["root_id"])
            if not m:
                continue
            try:
                p = confined(m["mp_root"], item["mp_path"])
                if fingerprint(p) != {k: item[k] for k in ("size", "mtime_ns")}:
                    continue
                nfo = select_nfo(p)
                if not nfo:
                    continue
                snapshot = {"video": fingerprint(p), "nfo": fingerprint(nfo)}
                if item.get("ready_snapshot") != snapshot:
                    item.update(ready_snapshot=snapshot, stable_since=time.time())
                    write_json(file, item)
                    continue
                if time.time() - item.get("stable_since", time.time()) < stable_seconds:
                    continue
                if predicate is not None and not predicate(p):
                    write_json(self.root / "state" / file.name, {"status": "skipped", "message": "非 P7 双层，沿用 MP 原流程", "updated_at": time.time()})
                    continue
                request = {k: item[k] for k in ("schema", "id", "root_id", "relative", "size", "mtime_ns")}
                request.update(queued_at=time.time(), readiness="valid NFO and stable video/NFO", nfo=nfo.name)
                request["directory"] = {k: m[k] for k in ("mp_root", "download_root") if k in m}
                write_json(self.root / "requests" / file.name, request, exclusive=True)
            except (OSError, ValueError):
                continue

    def history(self, include_archived=False):
        result = []
        for p in (self.root / "candidates").glob("*.json"):
            c = read_json(p)
            if not include_archived and (self.root / "history-archive" / p.name).exists():
                from .maintenance import hidden
                if hidden(self.root, p.stem):
                    continue
            if not c:
                continue
            s = read_json(self.root / "state" / p.name, {})
            ready = (self.root / "requests" / p.name).exists()
            d = read_json(self.root / "delivery" / p.name, {})
            if d.get("status") == "published":
                s = {**s, "status": "passed", "message": d["message"]}
            elif d.get("status") in {"preparing", "linked", "failed"}:
                s = {**s, "status": "publishing" if d["status"] != "failed" else "delivery_failed", "message": d.get("message", "正在存入下载目录并链接原电影库")}
            elif d.get("status") == "legacy":
                s = {**s, "message": d.get("message", "旧版测试记录")}
            elif s.get("status") == "passed" and s.get("result", {}).get("kind") == "P7 FEL":
                s = {**s, "status": "publishing", "message": "重建校验通过，等待下载侧存放、原库硬链接及刮削"}
            result.append({"id": c["id"], "file": c["relative"],
                           "status": s.get("status", "queued" if ready else "waiting_metadata"),
                           "message": s.get("message", "已就绪，等待处理服务" if ready else "等待有效 NFO 与文件稳定"),
                           "progress": s.get("progress"), "time": c["offered_at"]})
        return sorted(result, key=lambda r: r["time"], reverse=True)
