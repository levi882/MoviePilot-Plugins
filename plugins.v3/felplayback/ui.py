"""Compact native configuration UI. Paths come only from local runtime data."""


def node(component, *, text=None, content=None, **props):
    result = {"component": component}
    if props:
        result["props"] = props
    if text is not None:
        result["text"] = text
    if content is not None:
        result["content"] = content
    return result


def switch(model, label):
    return node("VSwitch", model=model, label=label, density="compact", hideDetails=True, inset=True)


def columns(items, width=6):
    return node("VRow", dense=True, content=[node("VCol", cols=12, md=width, content=[item]) for item in items])


def panel(title, content):
    return node("VExpansionPanel", content=[node("VExpansionPanelTitle", text=title), node("VExpansionPanelText", content=content)])


def config_form(mappings, issues, last):
    defaults = {"enabled": False, "dry_run": True, "paused": False,
                "stable_seconds": 120, "manual_path": "", "enqueue_once": False, "retry_once": False,
                "cleanup_history": True, "cleanup_logs": True, "cleanup_once": False, "restore_once": False}
    count = len(mappings)
    summary = f"已接入 {count} 组电影目录" if count else "尚未接入电影目录"
    if issues:
        summary += f"，另有 {len(issues)} 项目录提示"
    details = [node("div", text="以下信息仅从本机 MP 设置读取，用于核对目录。", class_="text-body-2 mb-3")]
    for mapping in mappings:
        details.append(node("VCard", variant="outlined", class_="mb-3", content=[
            node("VCardTitle", text=mapping["name"], class_="text-subtitle-1"),
            node("VCardText", text="下载目录\n" + mapping["download_root"] + "\n\n电影库\n" + mapping["mp_root"],
                 style={"whiteSpace": "pre-line", "overflowWrap": "anywhere"}),
        ]))
    for issue in issues:
        details.append(node("div", text=issue, class_="text-body-2 mb-2", style={"overflowWrap": "anywhere"}))
    if not mappings:
        details.append(node("div", text="请核对 MP 的电影目录规则与处理服务挂载。", class_="text-body-2"))
    maintenance = [
        node("div", text="先暂停处理并等待当前任务结束。仅清理已结束任务；失败和未完成任务保留。", class_="text-body-2 mb-3"),
        columns([switch("cleanup_history", "清理历史"), switch("cleanup_logs", "归档日志")]),
        node("div", text="原片、成品和防重记录保留，历史与日志可恢复。", class_="text-caption text-medium-emphasis mb-3"),
        switch("cleanup_once", "保存后清理一次"),
        switch("restore_once", "保存后恢复历史与日志"),
    ]
    if last:
        if last.get("success") and "history" in last:
            result = f"上次清理：{last['history']} 条历史，{last.get('log_files', 0)} 个日志"
            if last.get("skipped"):
                result += f"；跳过 {last['skipped']} 个任务"
        elif last.get("success"):
            result = "已恢复归档的历史与日志"
        else:
            result = "上次操作未完成：" + last.get("message", "请稍后重试")
        maintenance.append(node("VAlert", type="info" if last.get("success") else "warning", variant="tonal", density="compact", text=result, class_="mt-3"))
    content = [
        node("div", text="照常下载、刮削和播放，需要重建的 FEL 影片自动排队。", class_="text-body-2 mb-4"),
        node("VCard", variant="outlined", class_="mb-4", content=[
            node("VCardTitle", text="自动处理", class_="text-subtitle-1"),
            node("VCardText", content=[
                columns([switch("enabled", "启用插件"), switch("dry_run", "仅检测"), switch("paused", "暂停处理")], width=4),
                node("VTextField", model="stable_seconds", label="文件稳定等待", type="number", suffix="秒", density="compact", variant="outlined", min=30, hint="刮削后等待影片与 NFO 稳定", persistentHint=True, class_="mt-4"),
                node("div", text=summary, class_="text-body-2 mt-3"),
            ]),
        ]),
        node("VExpansionPanels", variant="accordion", content=[
            panel("目录详情（本机）", details),
            panel("手动处理", [
                node("VTextField", model="manual_path", label="影片路径（可选）", placeholder="填写 MP 中的完整影片路径", density="compact", variant="outlined", hideDetails=True, class_="mb-3"),
                switch("enqueue_once", "保存后加入队列"),
                switch("retry_once", "保存后重试失败及检测完成任务"),
            ]),
            panel("清理与维护", maintenance),
        ]),
    ]
    # Python keyword escaping must not leak into Vuetify props.
    def normalize(item):
        props = item.get("props", {})
        if "class_" in props:
            props["class"] = props.pop("class_")
        for child in item.get("content", []):
            normalize(child)
    form = node("VForm", content=content)
    normalize(form)
    return [form], defaults
