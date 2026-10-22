"""Single-job, offline worker. It never opens the Docker socket or original files for writing."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import time

from .spool import read_json, write_json


TERMINAL = {"passed", "detected", "failed", "skipped", "cancelled"}


def source_path(root, relative):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or any(p in {"", ".", ".."} for p in rel.parts):
        raise ValueError("非法的相对路径")
    root = Path(root).resolve(strict=True)
    file = (root / str(rel)).resolve(strict=True)
    if not file.is_relative_to(root) or not file.is_file():
        raise ValueError("源文件越界或不是普通文件")
    return file


def unchanged(source, request):
    s = source.stat()
    return s.st_size == request["size"] and s.st_mtime_ns == request["mtime_ns"]


def output_path(root, relative):
    root = Path(root).resolve(strict=True)
    out = root / relative
    if not out.resolve().is_relative_to(root):
        raise ValueError("播放库路径越界")
    # Refuse symlink directories even if they currently point inside the library.
    if any(p.is_symlink() for p in [out] + list(out.parents) if p.is_relative_to(root)):
        raise ValueError("播放库路径中不能含符号链接")
    return out


def digest(file):
    with Path(file).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def copy_file(source, target, cancel):
    h = hashlib.sha256()
    with source.open("rb") as i, target.open("xb") as o:
        while data := i.read(4 * 1024 * 1024):
            if cancel():
                raise InterruptedError("任务已取消")
            o.write(data)
            h.update(data)
        o.flush()
        os.fsync(o.fileno())
    if digest(target) != h.hexdigest():
        raise RuntimeError("复制校验失败")
    return h.hexdigest()


def sidecars(source, root):
    suffixes = {".nfo", ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".jpg", ".jpeg", ".png", ".webp"}
    common = {"movie.nfo", "tvshow.nfo", "poster.jpg", "poster.png", "fanart.jpg", "fanart.png", "folder.jpg", "banner.jpg", "clearlogo.png", "landscape.jpg", "season-poster.jpg"}
    found = set()
    for p in source.parent.iterdir():
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in suffixes and (
                p.name.startswith(source.stem + ".") or p.name.startswith(source.stem + "-") or p.name.lower() in common):
            found.add(p)
    # Series-level NFO and artwork retain their original relative directory structure.
    parent = source.parent.parent
    root = Path(root).resolve()
    while parent.is_relative_to(root) and parent != root:
        for name in ("tvshow.nfo", "poster.jpg", "poster.png", "fanart.jpg", "season-poster.jpg"):
            p = parent / name
            if p.is_file() and not p.is_symlink():
                found.add(p)
        parent = parent.parent
    return found


class Worker:
    def __init__(self, config):
        self.config = config
        self.queue = Path(config["queue"]).resolve(strict=True)
        self.scratch = Path(config["scratch"]).resolve(strict=True)
        self.stop = False
        for sub in ("state", "requests", "retry", "cancel", "reports"):
            (self.queue / sub).mkdir(exist_ok=True)
        for item in config["roots"].values():
            s, o = Path(item["source"]).resolve(strict=True), Path(item["output"]).resolve(strict=True)
            if s.is_relative_to(o) or o.is_relative_to(s):
                raise ValueError("原片目录和播放库必须分开，不能互相包含")

    def control(self):
        return read_json(self.queue / "control.json", {"enabled": False})

    def cancelled(self, jobid):
        return self.stop or not self.control().get("enabled", False) or (self.queue / "cancel" / (jobid + ".json")).exists()

    def state(self, jobid, **kwargs):
        write_json(self.queue / "state" / (jobid + ".json"), {"updated_at": time.time(), **kwargs})

    def cleanup_job(self, request):
        """Archive diagnostic logs and remove only this job's unpublished files."""
        jobid = request["id"]
        if not re.fullmatch(r"[a-f0-9]{64}", jobid):
            raise ValueError("拒绝非法任务 ID")
        work = self.scratch / jobid
        mapping = self.config["roots"][request["root_id"]]
        stage = output_path(mapping["output"], ".gb1-staging/" + jobid)
        if work.is_symlink():
            raise ValueError("临时工作目录不能是符号链接")
        if work.exists():
            logs = self.queue / "reports" / jobid
            logs.mkdir(exist_ok=True)
            for p in work.iterdir():
                if p.is_file() and not p.is_symlink() and p.suffix in {".log", ".json", ".txt"} and p.stat().st_size < 5_000_000:
                    shutil.copy2(p, logs / p.name)
            shutil.rmtree(work)
        if stage.exists():
            shutil.rmtree(stage)

    def supervise(self, request, source, stage, work, inspect):
        report = work / "report.json"
        cmd = [sys.executable, "-m", "felworker.convert", "--source", str(source), "--output", str(stage / (source.name + ".partial")),
               "--scratch", str(work), "--report", str(report), "--encoder", self.config.get("encoder", "qsv")]
        for name in ("dovi_tool", "mkvmerge", "plugin", "ffms2"):
            if self.config.get(name):
                cmd += ["--" + name.replace("_", "-"), self.config[name]]
        if inspect:
            cmd += ["--inspect"]
        started = time.monotonic()
        refreshed = 0
        with (work / "supervisor.log").open("wb") as log:
            child = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
            try:
                while child.poll() is None:
                    if self.cancelled(request["id"]):
                        raise InterruptedError("任务已取消或插件停用")
                    if time.monotonic() - started > self.config.get("max_hours", 168) * 3600:
                        raise TimeoutError("超过单部影片处理时限")
                    if time.monotonic() - refreshed >= 5:
                        phase = read_json(work / "phase.json", {})
                        progress = None
                        progress_file = work / "encoding-progress.txt"
                        if phase.get("frames") and progress_file.exists():
                            with progress_file.open("rb") as stream:
                                stream.seek(max(0, progress_file.stat().st_size - 4096))
                                matches = re.findall(rb"(?:^|\n)frame=(\d+)", stream.read())
                            if matches:
                                progress = min(100, round(int(matches[-1]) / phase["frames"] * 100, 1))
                        self.state(request["id"], status="running", message=phase.get("message", "正在处理"), progress=progress)
                        write_json(self.queue / "heartbeat.json", {"at": time.time(), "active": request["id"]})
                        refreshed = time.monotonic()
                    time.sleep(1)
                result = read_json(report, {})
                if child.returncode != 0 or not result:
                    raise RuntimeError(result.get("message", "处理进程失败，请查看报告"))
                return result
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()

    def process(self, request):
        jobid = request.get("id", "")
        if not isinstance(jobid, str) or not re.fullmatch(r"[a-f0-9]{64}", jobid):
            raise ValueError("拒绝非法任务 ID")
        work = self.scratch / jobid
        stage = None
        try:
            if request.get("schema") != 1 or not re.fullmatch(r"[a-f0-9]{64}", jobid):
                raise ValueError("未知任务格式")
            mapping = self.config["roots"][request["root_id"]]
            source = source_path(mapping["source"], request["relative"])
            out = output_path(mapping["output"], request["relative"])
            if not unchanged(source, request):
                raise ValueError("源片已变化，请重新加入队列")
            if out.exists():
                raise FileExistsError("播放库已有同名文件，不自动覆盖")
            if self.cancelled(jobid):
                raise InterruptedError("任务已取消")
            inspect = bool(self.control().get("dry_run", True))
            reserve = int(float(self.config.get("reserve_gib", 10)) * 1024**3)
            need = (0 if inspect else source.stat().st_size * 4) + reserve
            if shutil.disk_usage(self.scratch).free < need:
                raise ValueError("工作盘剩余空间不足，原片保留")
            if not inspect and shutil.disk_usage(out.parent if out.parent.exists() else mapping["output"]).free < source.stat().st_size * 2 + reserve:
                raise ValueError("播放库剩余空间不足，原片保留")
            work.mkdir(exist_ok=False)
            stage = output_path(mapping["output"], ".gb1-staging/" + jobid)
            stage.mkdir(parents=True, exist_ok=False)
            self.state(jobid, status="running", message="识别双层结构与检查时间轴")
            result = self.supervise(request, source, stage, work, inspect)
            if result["status"] == "copy":
                if inspect:
                    result.update(status="detected", message="非 P7 FEL；实际运行时按原码复制")
                elif self.config.get("non_fel", "copy") == "copy":
                    result["output_sha256"] = copy_file(source, stage / (source.name + ".partial"), lambda: self.cancelled(jobid))
                    result.update(status="passed", message="非 FEL，原码复制完成")
                else:
                    result.update(status="skipped", message="非 FEL，按配置跳过")
            if result["status"] == "passed":
                if not unchanged(source, request) or self.cancelled(jobid):
                    raise InterruptedError("发布前源片发生变化或任务取消")
                for side in sidecars(source, Path(mapping["source"])):
                    rel = side.relative_to(Path(mapping["source"]).resolve())
                    target = output_path(mapping["output"], str(rel))
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temp = target.parent / ("." + target.name + "." + jobid + ".partial")
                        try:
                            copy_file(side, temp, lambda: self.cancelled(jobid))
                            os.link(temp, target)
                        except FileExistsError:
                            pass
                        finally:
                            temp.unlink(missing_ok=True)
                if self.cancelled(jobid) or not unchanged(source, request):
                    raise InterruptedError("发布前取消，成品未进入播放库")
                out.parent.mkdir(parents=True, exist_ok=True)
                # Same output volume; hard-link publication is atomic and refuses replacement.
                os.link(stage / (source.name + ".partial"), out)
                result.update(output=str(out), message=result.get("message", "已校验并发布到播放库"))
            write_json(self.queue / "reports" / (jobid + ".json"), result)
            self.state(jobid, status=result["status"], message=result.get("message", "处理完成"), result=result)
        except InterruptedError as e:
            self.state(jobid, status="cancelled", message=str(e))
        except Exception as e:
            self.state(jobid, status="failed", message=str(e))
        finally:
            if request.get("root_id") in self.config["roots"]:
                try:
                    self.cleanup_job(request)
                except Exception as e:
                    current = read_json(self.queue / "state" / (jobid + ".json"), {})
                    current.pop("updated_at", None)
                    current.setdefault("status", "failed")
                    current["cleanup_warning"] = str(e)
                    self.state(jobid, **current)

    def tick(self):
        write_json(self.queue / "heartbeat.json", {"at": time.time(), "active": None})
        control = self.control()
        if not control.get("enabled") or control.get("paused"):
            return False
        requests = sorted((self.queue / "requests").glob("*.json"), key=lambda p: p.stat().st_mtime)
        for file in requests:
            request = read_json(file)
            if not request or request.get("id") != file.stem or not re.fullmatch(r"[a-f0-9]{64}", file.stem):
                continue
            status = read_json(self.queue / "state" / file.name, {}).get("status")
            retry = self.queue / "retry" / file.name
            if retry.exists() and status in TERMINAL:
                try:
                    self.cleanup_job(request)
                except Exception as e:
                    self.state(request["id"], status="failed", message="重试前清理失败：" + str(e))
                    continue
                retry.unlink()
                (self.queue / "cancel" / file.name).unlink(missing_ok=True)
                status = None
            if status == "running":
                try:
                    self.cleanup_job(request)
                    message = "处理服务曾中断，临时文件已清理，可手动重试"
                except Exception as e:
                    message = "处理服务曾中断，临时文件清理失败：" + str(e)
                self.state(request["id"], status="failed", message=message)
                continue
            if status in TERMINAL:
                continue
            self.process(request)
            return True
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=Path("/config/worker.json"))
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    worker = Worker(read_json(args.config))
    signal.signal(signal.SIGTERM, lambda *_: setattr(worker, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(worker, "stop", True))
    with (worker.queue / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not worker.stop:
            active = worker.tick()
            if args.once:
                break
            if not active:
                time.sleep(5)


if __name__ == "__main__":
    main()
