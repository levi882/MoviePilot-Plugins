"""Validated FEL output -> download-side original -> existing library hard link.

Only MP performs this step, using its existing directory access. The offline
worker retains read-only source mounts. Torrent files are never overwritten.
"""
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import time

from .queue import read_json, write_json


def safe_path(root, relative):
    root = Path(root)
    rel = PurePosixPath(relative)
    if root.is_symlink() or not root.is_dir() or rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError("发布路径无效")
    out = root / str(rel)
    if not out.resolve().is_relative_to(root.resolve()):
        raise ValueError("发布路径越界")
    if any(p.is_symlink() for p in [out, *out.parents] if p.is_relative_to(root)):
        raise ValueError("发布路径不能经过符号链接")
    return out


def digest(file):
    with Path(file).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matches(path, request):
    s = path.stat()
    return s.st_size == request["size"] and s.st_mtime_ns == request["mtime_ns"]


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def valid_output(path, report):
    return path.is_file() and path.stat().st_size == report["output_size"] and digest(path) == report["output_sha256"]


class Publisher:
    def __init__(self, queue, mappings):
        self.queue = Path(queue)
        self.mappings = {m["id"]: m for m in mappings}
        (self.queue / "delivery").mkdir(exist_ok=True)

    def _save(self, jobid, record):
        write_json(self.queue / "delivery" / (jobid + ".json"), {**record, "updated_at": time.time()})

    def publish(self, request, report, scrape):
        jobid = request["id"]
        if not re.fullmatch(r"[a-f0-9]{64}", jobid):
            raise ValueError("非法任务 ID")
        if report.get("status") != "passed" or report.get("kind") != "P7 FEL" or not all(
                report.get(k) is True for k in ("rpu_roundtrip_exact", "audio_subtitle_payloads_unchanged", "timing_checked")):
            raise ValueError("只接收完整校验通过的 P7 FEL 重建成品")
        if not re.fullmatch(r"[a-f0-9]{64}", report.get("output_sha256", "")):
            raise ValueError("缺少成品校验信息")
        mapping = self.mappings[request["root_id"]]
        if any(mapping.get(k) != v for k, v in request.get("directory", {}).items()):
            raise ValueError("MP 目录规则在处理期间发生变化，未替换电影库")
        library = safe_path(mapping["mp_root"], request["relative"])
        incoming = safe_path(mapping["staging_root"], request["relative"])
        downloads = Path(mapping["download_root"])
        jobdir = safe_path(downloads, ".FEL-Rebuilt/" + jobid)
        rebuilt = jobdir / (library.stem + ".FEL-P81.mkv")
        backup = jobdir / "original.keep"
        receipt = self.queue / "delivery" / (jobid + ".json")
        record = read_json(receipt, {})
        if record.get("status") in {"published", "legacy"}:
            return record
        if not library.is_file():
            raise ValueError("原电影库文件不存在")
        already_linked = rebuilt.is_file() and os.path.samefile(library, rebuilt)
        if not already_linked and not matches(library, request):
            raise ValueError("库内影片已被替换，保留现有文件并停止发布")
        # Both paths must be on the same filesystem; never fall back to an NFS-
        # unsafe cross-export symlink or silently duplicate the library file.
        if downloads.stat().st_dev != library.stat().st_dev:
            raise ValueError("下载目录与原电影库不在同一文件系统，不能硬链接")
        jobdir.mkdir(parents=True, exist_ok=True)
        record.update(status="preparing", library=str(library), rebuilt=str(rebuilt), backup=str(backup))
        self._save(jobid, record)
        if rebuilt.exists():
            if rebuilt.is_symlink() or not valid_output(rebuilt, report):
                raise ValueError("下载侧已有不同的成品，不覆盖")
        else:
            if not valid_output(incoming, report):
                raise ValueError("中转成品缺失或校验不符")
            partial = jobdir / "rebuilt.partial"
            if partial.exists():
                if partial.is_symlink():
                    raise ValueError("临时成品不能是符号链接")
                partial.unlink()
            try:
                if incoming.stat().st_dev == downloads.stat().st_dev:
                    os.link(incoming, partial)
                else:
                    with incoming.open("rb") as src, partial.open("xb") as dest:
                        while block := src.read(4 * 1024 * 1024):
                            dest.write(block)
                        dest.flush()
                        os.fsync(dest.fileno())
                if not valid_output(partial, report):
                    raise ValueError("下载侧成品校验失败")
                os.link(partial, rebuilt)
            finally:
                partial.unlink(missing_ok=True)
        if not already_linked:
            if not matches(library, request):
                raise ValueError("发布前库内影片发生变化，未替换")
            if backup.exists():
                if backup.is_symlink() or not os.path.samefile(backup, library):
                    raise ValueError("原文件保留记录冲突，未替换")
            else:
                os.link(library, backup)
            sync_directory(jobdir)
            temp = library.parent / (".fel-link-" + jobid + ".partial")
            if temp.exists() and (temp.is_symlink() or not os.path.samefile(temp, rebuilt)):
                raise ValueError("库内临时链接冲突，未替换")
            if not temp.exists():
                os.link(rebuilt, temp)
            if not matches(library, request):
                temp.unlink()
                raise ValueError("替换前库内影片发生变化，未替换")
            os.replace(temp, library)
            sync_directory(library.parent)
        record.update(status="linked", message="已链接重建成品，等待重新刮削")
        self._save(jobid, record)
        # Retry after interruption resumes here: the torrent, backup, and linked
        # output are preserved, without retranscoding or replacing another film.
        ok, message = scrape(library)
        if not ok:
            record.update(status="linked", message="成品已链接，刮削待重试：" + str(message), retry_at=time.time() + 300)
            self._save(jobid, record)
            return record
        record.update(status="published", message="重建成品已存入下载目录，原电影库硬链接及刮削完成", retry_at=0)
        self._save(jobid, record)
        # Drop only the redundant staging link; data remains in Downloads and
        # Movie. Original movie bytes remain in original.keep and the torrent.
        if incoming.exists() and incoming.stat().st_size == report["output_size"]:
            if incoming.stat().st_dev == rebuilt.stat().st_dev and os.path.samefile(incoming, rebuilt):
                incoming.unlink()
            elif valid_output(incoming, report):
                incoming.unlink()
        return record

    def tick(self, scrape):
        with (self.queue / "delivery.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            for path in (self.queue / "requests").glob("*.json"):
                request = read_json(path, {})
                report = read_json(self.queue / "reports" / path.name, {})
                state = read_json(self.queue / "state" / path.name, {})
                prior = read_json(self.queue / "delivery" / path.name, {})
                if state.get("status") != "passed" or report.get("kind") != "P7 FEL" or prior.get("status") in {"published", "legacy"} or prior.get("retry_at", 0) > time.time():
                    continue
                try:
                    self.publish(request, report, scrape)
                except Exception as exc:
                    self._save(path.stem, {**prior, "status": "failed", "message": str(exc), "retry_at": time.time() + 300})
