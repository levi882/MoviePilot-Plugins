"""Conservative MKV-only FEL conversion. Originals are opened read-only."""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

from .spool import write_json


class Unsupported(ValueError):
    pass


def run(cmd, log, timeout=604800):
    with Path(log).open("ab") as f:
        p = subprocess.run(list(map(str, cmd)), stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
    if p.returncode:
        raise RuntimeError("处理步骤失败：" + str(cmd[0]) + "，请查看任务日志")


def capture(cmd, timeout=604800):
    return subprocess.check_output(list(map(str, cmd)), text=True, timeout=timeout)


def probe(file, first=False):
    args = ["ffprobe", "-v", "error"]
    if first:
        args += ["-select_streams", "v:0", "-read_intervals", "%+#1", "-show_frames"]
    else:
        args += ["-show_streams", "-show_format", "-show_chapters"]
    return json.loads(capture(args + ["-of", "json", file]))


def dv_config(stream):
    return next((s for s in stream.get("side_data_list", []) if s.get("side_data_type") == "DOVI configuration record"), {})


def video_stream(metadata):
    videos = [s for s in metadata["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
    if len(videos) != 1:
        raise Unsupported("首版只支持单主视频轨的 MKV")
    return videos[0]


def packet_times(file, selector="v:0"):
    raw = capture(["ffprobe", "-v", "error", "-select_streams", selector, "-show_packets",
                   "-show_entries", "packet=pts_time", "-of", "csv=p=0", file])
    try:
        return sorted(float(s.split(",")[0]) for s in raw.splitlines() if s.strip())
    except ValueError as e:
        raise Unsupported("视频缺少可靠时间戳") from e


def assert_cfr(times, fps):
    if not times or abs(times[0]) > 0.002:
        raise Unsupported("首版不处理视频非零起始时间")
    if any(abs(t - i / float(fps)) > 0.002 for i, t in enumerate(times)):
        raise Unsupported("视频不是恒定帧率，停止以免 RPU 或音轨错位")


def mastering(summary, first_frame):
    # Never use the old five-second test's mastering values for other films.
    match = re.search(r"L6 metadata: Mastering display:\s*([\d.]+)/([\d.]+) nits\. MaxCLL:\s*(\d+) nits, MaxFALL:\s*(\d+) nits", summary)
    if not match:
        raise Unsupported("缺少唯一、可解析的 RPU L6 静态信息，需人工检查")
    low, high = map(float, match.group(1, 2))
    cll, fall = map(int, match.group(3, 4))
    if not (0 <= low < high <= 10000 and 0 <= fall <= cll <= 10000):
        raise Unsupported("RPU L6 数值不合理")
    hdr = next((s for s in first_frame.get("frames", [{}])[0].get("side_data_list", [])
                if s.get("side_data_type") == "Mastering display metadata"), None)
    primaries = None
    if hdr:
        primaries = [float(Fraction(hdr[k])) for k in ("red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y", "white_point_x", "white_point_y")]
        if not all(math.isfinite(x) and 0 <= x <= 1 for x in primaries):
            raise Unsupported("源片母版色度坐标无效")
    return {"min": low, "max": high, "cll": cll, "fall": fall, "primaries": primaries}


def color_args(m):
    args = ["--color-matrix-coefficients", "0:9", "--color-transfer-characteristics", "0:16",
            "--color-primaries", "0:9", "--color-range", "0:1", "--max-content-light", "0:" + str(m["cll"]),
            "--max-frame-light", "0:" + str(m["fall"]), "--max-luminance", "0:" + str(m["max"]),
            "--min-luminance", "0:" + str(m["min"])]
    if m["primaries"]:
        args += ["--chromaticity-coordinates", "0:" + ",".join(map(str, m["primaries"][:6])),
                 "--white-color-coordinates", "0:" + ",".join(map(str, m["primaries"][6:]))]
    return args


def track_summary(metadata):
    return Counter((s["codec_type"], s.get("codec_name"), s.get("tags", {}).get("language", "und"))
                   for s in metadata["streams"] if s["codec_type"] in {"audio", "subtitle", "attachment"})


def payload_hash(file, selector):
    return capture(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", file,
                    "-map", selector, "-c", "copy", "-f", "hash", "-hash", "sha256", "-"]).strip()


def encode(bl, el, dest, scratch, fps, frames, args):
    import vapoursynth as vs
    core = vs.core
    core.num_threads = 3
    core.max_cache_size = 768
    if not hasattr(core, "ffms2"):
        core.std.LoadPlugin(str(Path(args.ffms2).resolve()))
    core.std.LoadPlugin(str(Path(args.plugin).resolve()))
    clips = [core.ffms2.Source(str(p), threads=2, cachefile=str(scratch / (p.name + ".ffindex"))) for p in (bl, el)]
    b, e = clips
    if b.num_frames != frames or e.num_frames != frames:
        raise Unsupported("BL、EL、原视频时间戳帧数不一致")
    if b.width != 3840 or b.height != 2160 or e.width != 1920 or e.height != 1080:
        raise Unsupported("首版限 3840×2160 BL 与 1920×1080 EL")
    if "DolbyVisionRPU" not in e.get_frame(0).props:
        raise Unsupported("EL 解码器未提供 RPU，拒绝降级为仅基础层")
    out = core.fel.Bake(b, e, dovi_metadata=1)
    out = core.resize.Bicubic(out, format=vs.YUV420P10, matrix_s="2020ncl", range_in_s="full", range_s="limited", dither_type="error_diffusion")
    out = core.std.AssumeFPS(out, fpsnum=fps.numerator, fpsden=fps.denominator)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-nostats"]
    if args.encoder == "qsv":
        cmd += ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs"]
    cmd += ["-f", "yuv4mpegpipe", "-i", "pipe:0", "-an"]
    if args.encoder == "qsv":
        cmd += ["-vf", "format=p010le,hwupload=extra_hw_frames=8", "-c:v", "hevc_qsv", "-profile:v", "main10",
                "-preset", "slow", "-global_quality", "18", "-async_depth", "2"]
    else:
        cmd += ["-c:v", "libx265", "-preset", "fast", "-crf", "16", "-x265-params", "pools=3:frame-threads=2:repeat-headers=1"]
    cmd += ["-stats_period", "5", "-progress", str(scratch / "encoding-progress.txt"),
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv",
            "-bsf:v", "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9:video_full_range_flag=0", str(dest)]
    with (scratch / "encode.log").open("wb") as log:
        child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log)
        try:
            # Same bounded producer/cache arrangement as the successful N100 trial.
            out.output(child.stdin, y4m=True, prefetch=2)
            child.stdin.close()
            if child.wait():
                raise RuntimeError("视频编码失败")
        except BaseException:
            child.kill()
            child.wait()
            raise


def convert(args):
    source, output, scratch = args.source.resolve(strict=True), args.output.resolve(), args.scratch.resolve()
    if output.exists():
        raise FileExistsError("成品已存在，不覆盖")
    if source.suffix.lower() not in {".mkv", ".mp4", ".m4v", ".m2ts", ".ts"}:
        raise Unsupported("首版不处理 ISO 或 BDMV 原盘目录")
    scratch.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    log = scratch / "steps.log"
    def phase(message, frames=None):
        write_json(scratch / "phase.json", {"message": message, "frames": frames})
    phase("识别视频与双层元数据")
    original = probe(source)
    video = video_stream(original)
    config = dv_config(video)
    result = {"status": "running", "source": str(source), "source_size": source.stat().st_size,
              "original_retained": True, "encoder": args.encoder}
    if config.get("dv_profile") != 7:
        return {**result, "status": "copy", "message": "无需 FEL 重建"}
    if source.suffix.lower() != ".mkv":
        raise Unsupported("P7 重建首版仅支持 MKV，其他双层封装暂时跳过")
    if video.get("codec_name") != "hevc" or config.get("el_present_flag") != 1:
        raise Unsupported("P7 视频缺少预期的 HEVC 双层结构")
    dovi = args.dovi_tool
    rpu7 = scratch / "RPU-P7.bin"
    run([dovi, "extract-rpu", "-i", source, "-o", rpu7], log)
    summary = capture([dovi, "info", "-i", rpu7, "--summary"])
    (scratch / "rpu-summary.txt").write_text(summary)
    if not re.search(r"Profile:\s*7\s*\(FEL\)", summary):
        raise Unsupported("P7 MEL 或未知增强层，首版不自动转换")
    fps = Fraction(video["avg_frame_rate"])
    if not (0 < fps <= 60):
        raise Unsupported("无效帧率")
    times = packet_times(source)
    assert_cfr(times, fps)
    frames = len(times)
    if not re.search(r"Frames:\s*" + str(frames) + r"\b", summary):
        raise Unsupported("源视频与 RPU 帧数不一致")
    master = mastering(summary, probe(source, first=True))
    result.update(kind="P7 FEL", frames=frames, fps=str(fps), mastering=master)
    if args.inspect:
        return {**result, "status": "detected", "message": "确认 P7 FEL，尚未编码"}
    # Read metadata from the original Matroska, including its EL codec-private
    # headers. Standalone EL cuts can omit the SPS IDs needed by dovi_tool.
    rpu8 = scratch / "RPU-P81.bin"
    run([dovi, "-m", "2", "extract-rpu", "-i", source, "-o", rpu8], log)
    summary8 = capture([dovi, "info", "-i", rpu8, "--summary"])
    if not re.search(r"Frames:\s*" + str(frames) + r"\b", summary8) or not re.search(r"Profile:\s*8\b", summary8):
        raise Unsupported("转换后 RPU 的帧数或 Profile 不符")
    raw, bl, el = (scratch / s for s in ("source.hevc", "BL.hevc", "EL.hevc"))
    phase("分离 BL、EL 和 RPU", frames)
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-n", "-i", source, "-map", "0:v:0", "-c", "copy",
         "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", raw], log)
    run([dovi, "demux", "-i", raw, "--bl-out", bl, "--el-out", el], log)
    raw.unlink()
    encoded = scratch / "encoded.mkv"
    phase("重建 FEL 画面并编码", frames)
    encode(bl, el, encoded, scratch, fps, frames, args)
    baked, injected = scratch / "baked.hevc", scratch / "baked-P81.hevc"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-n", "-i", encoded, "-map", "0:v:0", "-c", "copy",
         "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", baked], log)
    run([dovi, "inject-rpu", "-i", baked, "--rpu-in", rpu8, "-o", injected], log)
    # mkvmerge preserves source audio/subtitles/attachments/chapters, without encoding them.
    phase("封装 P8.1 并保留音轨、字幕、章节", frames)
    run([args.mkvmerge, "-o", output, "--default-duration", "0:" + str(fps) + "p"] + color_args(master) +
        [injected, "--no-video", source], log)
    final = probe(output)
    phase("校验视频、动态元数据与音画时间轴", frames)
    out_video = video_stream(final)
    expected = {"dv_profile": 8, "bl_present_flag": 1, "el_present_flag": 0, "rpu_present_flag": 1, "dv_bl_signal_compatibility_id": 1}
    if any(dv_config(out_video).get(k) != v for k, v in expected.items()):
        raise RuntimeError("成品 P8.1 配置校验失败")
    for k, value in {"pix_fmt": "yuv420p10le", "width": 3840, "height": 2160, "color_primaries": "bt2020", "color_transfer": "smpte2084", "color_space": "bt2020nc"}.items():
        if out_video.get(k) != value:
            raise RuntimeError("成品格式不符：" + k)
    out_times = packet_times(output)
    if len(out_times) != frames or any(abs(a-b) > .002 for a, b in zip(times, out_times)):
        raise RuntimeError("成品视频时间戳与原片不一致")
    if track_summary(original) != track_summary(final):
        raise RuntimeError("音轨、字幕或附件的数量/编码/语言不一致")
    for typ in ("audio", "subtitle"):
        for i, _ in enumerate(s for s in original["streams"] if s["codec_type"] == typ):
            selector = "0:" + ("a" if typ == "audio" else "s") + ":" + str(i)
            if payload_hash(source, selector) != payload_hash(output, selector):
                raise RuntimeError("音轨或字幕数据改变")
            original_times = packet_times(source, selector[2:])
            final_times = packet_times(output, selector[2:])
            if len(original_times) != len(final_times) or any(abs(a-b) > .002001 for a, b in zip(original_times, final_times)):
                raise RuntimeError("音轨或字幕时间戳改变")
    a_ch, b_ch = original.get("chapters", []), final.get("chapters", [])
    if len(a_ch) != len(b_ch) or any(abs(float(a[k])-float(b[k])) > .002 or a.get("tags", {}) != b.get("tags", {})
            for a, b in zip(a_ch, b_ch) for k in ("start_time", "end_time")):
        raise RuntimeError("章节校验失败")
    roundtrip = scratch / "RPU-roundtrip.bin"
    # dovi_tool selects its container reader from the extension. The unpublished
    # file ends in .partial, so expose a private verification alias outside Kodi.
    container_alias = scratch / "container-verification.mkv"
    container_alias.symlink_to(output)
    run([dovi, "extract-rpu", "-i", container_alias, "-o", roundtrip], log)
    if rpu8.read_bytes() != roundtrip.read_bytes():
        raise RuntimeError("RPU 回提取校验失败")
    # Decode all output frames, so a truncated/corrupt encode is never published.
    count = json.loads(capture(["ffprobe", "-v", "error", "-threads", "2", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "json", output]))
    if int(count["streams"][0]["nb_read_frames"]) != frames:
        raise RuntimeError("成品完整解码帧数不符")
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-threads", "2", "-i", output,
         "-map", "0:v:0", "-f", "null", "-"], log)
    return {**result, "status": "passed", "rpu_roundtrip_exact": True,
            "audio_subtitle_payloads_unchanged": True, "timing_checked": True,
            "output_size": output.stat().st_size, "output_sha256": hashlib.file_digest(output.open("rb"), "sha256").hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "output", "scratch", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--encoder", choices=("qsv", "x265"), default="qsv")
    parser.add_argument("--dovi-tool", default="/opt/gb1/dovi_tool")
    parser.add_argument("--mkvmerge", default="mkvmerge")
    parser.add_argument("--plugin", default="/opt/gb1/libfelbaker.so")
    parser.add_argument("--ffms2", default="/usr/lib/libffms2.so")
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    start = time.monotonic()
    result = {}
    try:
        result = convert(args)
    except Unsupported as e:
        result = {"status": "skipped", "message": str(e)}
    except BaseException as e:
        result = {"status": "failed", "message": str(e)}
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - start, 3)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
