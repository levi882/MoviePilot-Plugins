"""MoviePilot V3: rebuild FEL on the download side, retain existing libraries."""
import json
from collections.abc import Mapping
from pathlib import Path
import re
import subprocess
import time

from apscheduler.triggers.interval import IntervalTrigger
from app.sdk.plugin import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.schemas.types import EventType

from .queue import Spool, read_json, write_json
from .publish import Publisher
from .directories import resolve_directories
from .maintenance import Maintenance, maintenance_lock
from .ui import config_form


def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


class FelPlayback(_PluginBase):
    plugin_name = "FEL 自动重建"
    plugin_desc = "只重建 P7 双层，成品存入下载目录，硬链接回原电影库并重新刮削。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/Moviepilot_A.png"
    plugin_version = "0.3.2"
    plugin_author = "levi882"
    author_url = "https://github.com/levi882/MoviePilot-Plugins"
    plugin_config_prefix = "felplayback_"
    plugin_order = 60
    auth_level = 1

    def init_plugin(self, config=None):
        self.stop_service()
        self._config = config or {}
        self._enabled = bool(self._config.get("enabled", False))
        self._spool = Spool(self.get_data_path() / "queue", shared_gid=int(self._config.get("shared_gid", 1000)))
        if "mappings" in self._config:
            write_json(self._spool.root / "legacy-manual-mappings.json", self._config.pop("mappings"))
            self.update_config(self._config)
        self._refresh_mappings()
        self._control()
        if self._enabled:
            self._ensure_excluded()
        self._migrate()
        # One-shot requests are consumed before executing, so reload cannot
        # repeat a cleanup after a partially failed archive.
        action = "restore_once" if self._config.get("restore_once") else "cleanup_once" if self._config.get("cleanup_once") else None
        if action:
            self._config["cleanup_once"] = self._config["restore_once"] = False
            self.update_config(self._config)
            try:
                service = Maintenance(self._spool)
                result = service.restore() if action == "restore_once" else service.clean(
                    history=bool(self._config.get("cleanup_history", True)), logs=bool(self._config.get("cleanup_logs", True)))
                write_json(self._spool.root / "maintenance-last.json", result)
            except Exception as exc:
                write_json(self._spool.root / "maintenance-last.json", {"success": False, "message": "清理未完成，原片和任务记录保留：" + str(exc)})
        if self._enabled and self._config.get("enqueue_once"):
            jobs = self._offer([self._config.get("manual_path")])
            for jobid in jobs:
                receipt = self._spool.root / "delivery" / (jobid + ".json")
                if read_json(receipt, {}).get("status") == "legacy":
                    write_json(receipt, {"status": "pending"})
            self._config["enqueue_once"] = False
            self.update_config(self._config)
        if self._enabled and self._config.get("retry_once"):
            for j in self._spool.history():
                if j["status"] in {"failed", "cancelled", "detected"}:
                    self._signal("retry", {"id": j["id"]})
                elif j["status"] in {"delivery_failed", "publishing"}:
                    path = self._spool.root / "delivery" / (j["id"] + ".json")
                    write_json(path, {**read_json(path, {}), "retry_at": 0})
            self._config["retry_once"] = False
            self.update_config(self._config)

    def _refresh_mappings(self):
        from app.application.directory import DirectoryHelper
        bindings = read_json(self._spool.root / "worker-libraries.json", [])
        self._mappings, self._directory_issues = resolve_directories(DirectoryHelper.get_dirs(), bindings, self._spool.root)
        self._publisher = Publisher(self._spool.root, self._mappings)

    def _control(self):
        control = {
            "enabled": self._enabled, "dry_run": bool(self._config.get("dry_run", True)),
            "paused": bool(self._config.get("paused", False)),
        }
        if read_json(self._spool.root / "control.json", {}) != control:
            write_json(self._spool.root / "control.json", control)

    def get_state(self):
        return getattr(self, "_enabled", False)

    def stop_service(self):
        self._enabled = False
        # Host/plugin reload must not cancel an independent worker. Explicit
        # enable/pause settings are persisted only by init_plugin/_control.

    def get_service(self):
        return [{"id": self.__class__.__name__ + ".ready", "name": "FEL 就绪及原库发布检查",
                 "trigger": IntervalTrigger(seconds=30), "func": self.check_ready, "kwargs": {}}] if self.get_state() else []

    def check_ready(self):
        if self.get_state():
            self._refresh_mappings()
            self._spool.ready(self._mappings, max(30, int(self._config.get("stable_seconds", 120))), predicate=self._is_p7)
            if not self._config.get("dry_run", True) and not self._config.get("paused", False):
                if self._delivery_excluded():
                    self._publisher.tick(self._scrape)
                else:
                    logger.warning("FEL 成品尚未发布：请将 .FEL-Rebuilt 目录加入 MP 整理排除词，防止循环整理")

    @staticmethod
    def _is_p7(path):
        try:
            p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("探测超时，等待下次检查") from exc
        if p.returncode:
            raise ValueError("探测暂未成功，等待下次检查")
        streams = json.loads(p.stdout).get("streams", [])
        return any(d.get("dv_profile") == 7 and d.get("el_present_flag") == 1 for s in streams for d in s.get("side_data_list", []))

    def _delivery_excluded(self):
        from app.application.configuration import get_configured_system_config
        from app.schemas.types import SystemConfigKey
        rules = get_configured_system_config().get(SystemConfigKey.TransferExcludeWords) or []
        try:
            valid = []
            for rule in rules:
                try:
                    if rule:
                        valid.append(re.compile(rule))
                except (re.error, TypeError):
                    continue
            return bool(self._mappings) and all(any(rule.search(str(Path(m["download_root"]) / ".FEL-Rebuilt/probe.mkv")) for rule in valid) for m in self._mappings)
        except KeyError:
            return False

    def _ensure_excluded(self):
        from app.schemas.types import SystemConfigKey
        rule = r"(^|/)\.FEL-Rebuilt(/|$)"
        def append_rule(_session, current):
            if current is not None and not isinstance(current, list):
                raise ValueError("整理排除词格式异常，未修改原设置")
            values = list(current or [])
            if rule not in values:
                values.append(rule)
            return True, values
        self.systemconfig.update_atomically(SystemConfigKey.TransferExcludeWords, append_rule)

    @staticmethod
    def _scrape(path):
        from app.chain.scraping import ScrapingChain
        from app.schemas.file import FileItem
        return ScrapingChain().scrape_metadata(fileitem=FileItem(storage="local", type="file", path=str(path), name=path.name, size=path.stat().st_size), init_folder=False, overwrite=True, recursive=False)

    def _migrate(self):
        marker = self._spool.root / "original-library-mode.json"
        if marker.exists():
            return
        for f in (self._spool.root / "requests").glob("*.json"):
            state = read_json(self._spool.root / "state" / f.name, {})
            if state.get("status") == "passed":
                write_json(self._spool.root / "delivery" / f.name, {"status": "legacy", "message": "旧版测试记录，未自动替换原电影库"})
            elif state.get("status") in {None, "queued"}:
                item = read_json(f, {})
                mapping = next((m for m in self._mappings if m["id"] == item.get("root_id")), None)
                try:
                    if mapping and not self._is_p7(Path(mapping["mp_root"]) / item["relative"]):
                        write_json(self._spool.root / "state" / f.name, {"status": "skipped", "message": "非 P7 双层，沿用 MP 原流程", "updated_at": time.time()})
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        write_json(marker, {"at": time.time()})

    def _offer(self, paths):
        result = []
        if not self.get_state():
            return result
        self._refresh_mappings()
        for p in dict.fromkeys(str(p) for p in paths if p):
            try:
                jobid = self._spool.offer(p, self._mappings)
                if jobid:
                    result.append(jobid)
            except (OSError, ValueError) as e:
                logger.info(f"FEL 重建未接收文件：{e}")
        return result

    @eventmanager.register(EventType.TransferComplete)
    def transferred(self, event: Event):
        if not self.get_state() or not event:
            return
        info = field(event.event_data or {}, "transferinfo", {})
        self._offer(field(info, "file_list_new", []) or [])

    @eventmanager.register(EventType.MetadataScrape)
    def scrape_requested(self, event: Event):
        if not self.get_state() or not event:
            return
        data = event.event_data or {}
        paths = field(data, "file_list", []) or []
        item = field(data, "fileitem")
        if not paths and field(item, "type") == "file":
            paths = [field(item, "path")]
        self._offer(paths)

    def get_actions(self):
        return [{"id": "FelPlayback.Queue", "name": "加入 FEL 重建队列", "func": self.after_scrape, "kwargs": {}}]

    def after_scrape(self, context):
        # Can also follow MP's synchronous ScrapeFile workflow action.
        self._offer(field(i, "path") for i in field(context, "fileitems", []) or [])
        self.check_ready()
        return True, context

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        return [{"path": "/jobs", "endpoint": self.jobs, "methods": ["GET"], "auth": "bear", "summary": "FEL 重建任务"},
                {"path": "/enqueue", "endpoint": self.enqueue, "methods": ["POST"], "auth": "bear", "summary": "加入单个历史文件"},
                {"path": "/cancel", "endpoint": self.cancel, "methods": ["POST"], "auth": "bear", "summary": "取消任务"},
                {"path": "/retry", "endpoint": self.retry, "methods": ["POST"], "auth": "bear", "summary": "重试任务"}]

    def jobs(self):
        return {"jobs": self._spool.history(), "worker": read_json(self._spool.root / "heartbeat.json", {}),
                "queue_directory": str(self._spool.root)}

    def enqueue(self, payload: dict):
        return {"jobs": self._offer([payload.get("path")])}

    def _signal(self, action, payload):
        jobid = str(payload.get("id", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", jobid) or not (self._spool.root / "requests" / (jobid + ".json")).exists():
            return {"success": False, "message": "任务不存在"}
        with maintenance_lock(self._spool.root):
            write_json(self._spool.root / action / (jobid + ".json"), {"requested": True})
            write_json(self._spool.root / "history-archive" / (jobid + ".json"), {"restored_at": time.time()})
        return {"success": True}

    def cancel(self, payload: dict):
        return self._signal("cancel", payload)

    def retry(self, payload: dict):
        return self._signal("retry", payload)

    def get_form(self):
        self._refresh_mappings()
        return config_form(self._mappings, self._directory_issues,
                           read_json(self._spool.root / "maintenance-last.json", {}))

    def get_page(self):
        jobs = self._spool.history()
        labels = {"waiting_metadata": "等待刮削就绪", "queued": "排队中", "running": "处理中",
                  "passed": "已完成", "detected": "检测完成", "failed": "失败", "skipped": "沿用原流程", "cancelled": "已取消", "publishing": "正在链接及刮削", "delivery_failed": "入库未完成"}
        content = [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text":
                    "只重建需要处理的双层片源。成品存入下载目录，硬链接回原电影库并重新刮削；Kodi 沿用原入口。未校验成功不替换原文件。"}}]
        if not jobs:
            content.append({"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "暂无未归档任务。清理及恢复入口在插件配置中。"}})
        for j in jobs[:100]:
            content.append({"component": "VCard", "props": {"variant": "outlined", "class": "mb-2"}, "content": [
                {"component": "VCardTitle", "text": j["file"]},
                {"component": "VCardText", "text": labels.get(j["status"], j["status"]) + " · " + str(j["message"])}]})
        return content
