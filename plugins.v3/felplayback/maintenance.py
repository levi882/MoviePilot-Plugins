"""Recoverable history/log cleanup; processing receipts are never removed."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import time
import uuid
import zipfile

from .queue import read_json, write_json


@contextmanager
def maintenance_lock(root):
    with (Path(root) / "maintenance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def revision(root, jobid):
    """A retry or new delivery state makes an archived job visible again."""
    h = hashlib.sha256()
    for folder in ("state", "delivery"):
        file = Path(root) / folder / (jobid + ".json")
        h.update(folder.encode())
        h.update(file.read_bytes() if file.is_file() else b"missing")
    return h.hexdigest()


def hidden(root, jobid):
    marker = read_json(Path(root) / "history-archive" / (jobid + ".json"), {})
    return marker.get("revision") == revision(root, jobid)


class Maintenance:
    def __init__(self, spool):
        self.spool = spool
        self.root = spool.root

    def _storage_safe(self):
        # Refuse redirected queue subdirectories before any archive writes.
        if self.root.is_symlink():
            raise ValueError("队列目录不能是符号链接")
        for name in ("reports", "archive", "archive/logs", "history-archive", "state", "delivery", "retry", "work"):
            path = self.root / name
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError("清理目录存在异常链接，未执行：" + name)

    def _idle(self, worker_lock):
        try:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True  # Worker stopped; holding its lock prevents startup.
        except BlockingIOError:
            control = read_json(self.root / "control.json", {})
            beat = read_json(self.root / "heartbeat.json", {})
            return bool((control.get("paused") or not control.get("enabled"))
                        and beat.get("active") is None and "at" in beat
                        and 0 <= time.time() - beat["at"] <= 30
                        and beat["at"] >= (self.root / "control.json").stat().st_mtime)

    def clean(self, history=True, logs=True):
        if not history and not logs:
            return {"success": False, "message": "请选择历史或日志"}
        self._storage_safe()
        with maintenance_lock(self.root), (self.root / "worker.lock").open("a") as worker, (self.root / "delivery.lock").open("a") as delivery:
            if not self._idle(worker):
                return {"success": False, "message": "请先暂停处理，待当前任务结束后再清理；未改动记录"}
            try:
                fcntl.flock(delivery, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"success": False, "message": "正在发布成品，请稍后重试；未改动记录"}
            now = time.time()
            batch = str(int(now)) + "-" + uuid.uuid4().hex[:8]
            result = {"success": True, "batch": batch, "at": now, "history": 0, "log_files": 0, "log_bytes": 0, "skipped": 0}
            for job in self.spool.history(include_archived=True):
                jobid = job["id"]
                if not re.fullmatch(r"[a-f0-9]{64}", jobid):
                    continue
                state = read_json(self.root / "state" / (jobid + ".json"), {})
                # Failed jobs keep their diagnostics and retry entry. Pending
                # publication is not complete, even when encoding has passed.
                if job["status"] not in {"passed", "skipped", "detected", "cancelled"} or (self.root / "retry" / (jobid + ".json")).exists() or (self.root / "work" / jobid).exists() or now - state.get("updated_at", 0) < 60:
                    result["skipped"] += 1
                    continue
                if logs:
                    count, size = self._archive_logs(jobid, batch)
                    result["log_files"] += count
                    result["log_bytes"] += size
                if history and not hidden(self.root, jobid):
                    write_json(self.root / "history-archive" / (jobid + ".json"), {"revision": revision(self.root, jobid), "at": now})
                    result["history"] += 1
            result["message"] = f"已归档 {result['history']} 条历史、{result['log_files']} 个日志文件；跳过 {result['skipped']} 个未完成、失败或待重试任务。可恢复显示历史，日志压缩包保存在 queue/archive/logs。"
            write_json(self.root / "maintenance-last.json", result)
            return result

    def _archive_logs(self, jobid, batch):
        folder = self.root / "reports" / jobid
        if not folder.exists() or folder.is_symlink():
            return 0, 0
        files = list(folder.iterdir())
        if not files or any(not f.is_file() or f.is_symlink() or f.suffix not in {".log", ".txt", ".json"} for f in files):
            return 0, 0
        target = self.root / "archive" / "logs" / batch / (jobid + ".zip")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".partial")
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                archive.write(file, file.name)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("日志归档校验失败，原日志保留")
            for file in files:
                if hashlib.sha256(archive.read(file.name)).digest() != hashlib.sha256(file.read_bytes()).digest():
                    raise ValueError("日志在归档时变化，原日志保留")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        size = sum(f.stat().st_size for f in files)
        # The verified archive is durable before originals are removed. Source
        # is an allowlisted job log directory, never a media/work directory.
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        for file in files:
            file.unlink()
        folder.rmdir()
        return len(files), size

    def restore(self):
        """Restore display and archived logs, without changing job state."""
        self._storage_safe()
        with maintenance_lock(self.root), (self.root / "worker.lock").open("a") as worker:
            if not self._idle(worker):
                return {"success": False, "message": "请先暂停处理，待当前任务结束后再恢复"}
            logs = 0
            for archive_path in sorted((self.root / "archive" / "logs").glob("*/*.zip")):
                if archive_path.is_symlink() or archive_path.parent.is_symlink() or not re.fullmatch(r"[a-f0-9]{64}", archive_path.stem):
                    continue
                folder = self.root / "reports" / archive_path.stem
                if folder.is_symlink():
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(archive_path) as archive:
                    for item in archive.infolist():
                        if item.is_dir() or Path(item.filename).name != item.filename or Path(item.filename).suffix not in {".log", ".json", ".txt"}:
                            raise ValueError("非法日志归档条目")
                        dest = folder / item.filename
                        if not dest.exists() and not dest.is_symlink():
                            with dest.open("xb") as stream, archive.open(item) as source:
                                shutil.copyfileobj(source, stream)
                            logs += 1
            count = 0
            for marker in (self.root / "history-archive").glob("*.json"):
                write_json(marker, {"restored_at": time.time()})
                count += 1
            result = {"success": True, "at": time.time(), "message": f"已恢复 {count} 条历史显示、{logs} 个日志文件；任务不会重新排队"}
            write_json(self.root / "maintenance-last.json", result)
            return result
