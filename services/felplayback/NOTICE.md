# 来源与许可证

本仓库新增的 FelPlayback 插件与 worker 代码按仓库 GPL-3.0 许可证提供。第三方代码保持原作者版权及许可证，未包含影片、测试码流或厂商专有杜比库。

- [FelBaker](https://github.com/bbeny123/felbaker)，提交 `cae66302433578ec62afa8c5c5809e93d0405b2f`；作者 Chire，基于 erazortt 的 [DoViBaker](https://github.com/erazortt/DoViBaker)。源码及 GPL-3.0 许可证在 `vendor/felbaker/`。本地修改仅将 VapourSynth `Bake`/`ToRGB` 返回 schema 与 `clip` 键对齐，未修改图像重建逻辑，详见 `UPSTREAM.json`。
- [dovi_tool](https://github.com/quietvoid/dovi_tool) 2.3.4，构建时下载官方 Linux x86_64 musl release，校验 SHA-256；下载地址、摘要、许可证和来源记录随镜像保存在 `/opt/gb1/source/dovi_tool/`。本仓库不分发该二进制。
- FFmpeg、FFMS2、VapourSynth、libdovi、MKVToolNix、Intel media driver 等由镜像的软件包管理器安装，按各自许可证提供；构建结果记录安装版本清单。

项目使用 Dolby Vision 名称描述媒体格式，不代表获得 Dolby 官方认证或背书。
