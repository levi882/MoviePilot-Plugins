# FEL 独立处理服务（实验性）

这是 MoviePilot `FelPlayback` 插件的离线处理服务。需要 x86-64-v3/AVX2 CPU、支持 HEVC Main10 编码的 Intel 核显、可用的 `/dev/dri/renderD128` 与 Docker Compose。示例资源上限为3核、4GB；解码和 FEL 重建仍使用 CPU，QSV 负责视频编码，不承诺实时转换。

源码包含 FelBaker 固定提交与小型接口修正，构建时从官方 release 下载并校验 dovi_tool 2.3.4。依赖 FFmpeg、FFMS2、VapourSynth、libdovi、MKVToolNix 和 Intel 驱动。Arch 软件包来自构建时的滚动仓库，镜像记录 `/package-versions.txt`，不宣称二进制可复现。

**已有短片验证来自同一处理逻辑的部署环境；本通用 Dockerfile 尚未在干净环境重新构建验收，整片与长时同步也未验收。先测试短片，勿直接批量重建电影库。** 不包含预制镜像或个人配置。

## 目录约定

下文 `/srv/...` 与 `/media/Movies` 均为示例，需要换成自己的真实位置。

| 项目 | 含义 |
|---|---|
| `MP_FEL_QUEUE` | 主机上实际的 MP 插件数据目录下 `FelPlayback/queue`；不得另建一份脱离 MP 的队列 |
| `MOVIE_SOURCE` | 主机上的原电影库，对应 MP 某条电影目录规则的 `library_path` |
| `worker-libraries.json` 的 `mp_root` | MP 容器内部看到的电影库路径，与 MP 规则完全一致 |
| worker 的 `roots.movies.source` | 固定为容器内部 `/sources/movies`，只读 |
| worker 的临时与中转位置 | 队列内 `work/`、`staging/movies/`，不挂载影片下载目录 |

MP 本身须已有写入原下载目录和电影库的权限，二者需要同文件系统硬链接能力；服务不会获得 Docker socket 或主机根目录。

## 部署步骤

1. 在 MP 安装插件并保持“暂停处理”；确认插件数据目录。查看并填写原电影目录规则，不修改为示例目录。
2. 复制本目录 `.env.example` 为 `.env`，填写主机队列、电影库路径、服务 UID/GID 及核显组。核显组可在主机用 `stat -c '%g' /dev/dri/renderD128` 查询。
3. 将 `worker-runtime.example.json` 复制为**队列目录内**的 `worker-runtime.json`；将 `worker-libraries.example.json` 复制为队列目录内 `worker-libraries.json`，修改其中 `mp_root` 为 MP 内部的真实电影库路径。两份配置的根 ID 保持 `movies` 对应。
4. 在队列创建 `work/`、`staging/movies/` 并赋予所选 UID/GID 访问权限。MP 与处理服务需要对队列共享组可读写。现有默认插件共享 GID 为1000；如使用其他 GID，部署者需同步插件 `shared_gid` 配置，不能只改容器端。不要覆盖现有队列、原片或发布回执。
5. 在本目录构建并启动（构建联网，运行断网）：

   ```bash
   docker compose --env-file .env -f compose.yaml build
   docker compose --env-file .env -f compose.yaml up -d
   ```

6. 重新保存插件配置以读取绑定。核对“目录详情”，先用单个短片、仅检测模式测试。确认路径、核显、磁盘空间和结果后再关闭暂停；无需改变 Kodi 的原电影库入口。

工作盘空间预检查保守预留约原片4倍加10GiB，输出所在盘另做空间检查。实际空间和耗时受片源与编码影响，输出有损；发布前必须通过所有校验。`.FEL-Rebuilt` 与 `original.keep` 不属于日志垃圾，清理维护不会删除。

## 日常维护

暂停开关阻止新任务，当前任务会继续到结束；取消任务走插件的取消入口。停止或中断后临时工作按任务清理，任务标为失败供人工检查后重试。独立 worker 仅允许一个实例使用同一队列。

保留任务请求、状态、发布回执与下载目录中的恢复备份。迁移目录或卸载前先停止处理并做好备份。关于配置页与可恢复的日志/历史清理，见[插件说明](../../plugins.v3/felplayback/README.md)。
