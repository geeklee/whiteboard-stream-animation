#!/usr/bin/env python3
"""
流式笔迹动画 - 队列渲染入口

把一组图片按各自时长依次渲染成视频。内部串行调用 stream_render.py，
每个任务一进一出，打印进度；结束后给出汇总。

  --fail-fast   某个任务失败立刻终止整批（默认是失败不阻塞后续）
  --merge       全部渲染完后，把各片段按输入顺序硬切合并为一个总视频
                （无损拼接优先走系统 ffmpeg，缺失时回退 PyAV 重编码）
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RENDER_SCRIPT = SCRIPT_DIR / "stream_render.py"


def _ensure_env() -> bool:
    """
    确认当前解释器（子任务将用 sys.executable 复用它）能导入渲染依赖。
    队列脚本本身不 import cv2/numpy，若用户误用系统 python 而非 prepare_env.py
    产出的 ENV_PY 运行，则每个子任务都会失败。在此提前拦截并给出清晰指引。
    """
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        print(f"[err] 当前解释器缺少渲染依赖 ({exc.name})：{sys.executable}")
        print("      请先运行 prepare_env.py，并用它输出的 ENV_PY 运行本脚本：")
        print(f"      python {SCRIPT_DIR / 'prepare_env.py'}")
        print(f"      <ENV_PY> {Path(__file__).name} --images ... --durations ...")
        return False
    return True


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="批量流式笔迹动画渲染器 - 串行渲染多个视频，可选合并为一个总视频"
    )
    p.add_argument("--images", nargs="+", required=True, help="图片路径列表（空格分隔）")
    p.add_argument(
        "--durations", nargs="+", type=int, required=True,
        help="时长列表（毫秒，与 --images 一一对应）",
    )
    p.add_argument("--out-dir", default="./out", help="输出目录 (默认: ./out)")
    p.add_argument("--bare-tip", action="store_true", help="对所有任务关闭笔尖/手部覆盖")
    p.add_argument("--pen-image", default=None, help="自定义笔尖素材（对所有任务生效）")
    p.add_argument("--fail-fast", action="store_true", help="遇错即止，不再处理后续任务")
    p.add_argument(
        "--merge", action="store_true",
        help="全部渲染完后，把各片段按输入顺序硬切合并为一个总视频（保留单片）",
    )
    p.add_argument(
        "--merged-name", default=None,
        help="合并输出文件名 (默认: merged_YYYYMMDD_HHMMSS.mp4)",
    )
    return p.parse_args(argv)


def _build_cmd(python: str, args: argparse.Namespace, image: str, duration: int) -> list[str]:
    cmd: list[str] = [
        python, str(RENDER_SCRIPT), image,
        "--out-dir", args.out_dir,
        "--total-ms", str(duration),
    ]
    if args.bare_tip:
        cmd.append("--bare-tip")
    if args.pen_image:
        cmd += ["--pen-image", args.pen_image]
    return cmd


def _run_task(cmd: list[str]) -> tuple[int, str | None]:
    """
    运行单个渲染子进程，实时透传其 stdout，并从中捕获 `OUTPUT=<路径>` 末行。
    返回 (returncode, 输出视频路径 or None)。
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    out_path: str | None = None
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        stripped = line.strip()
        if stripped.startswith("OUTPUT="):
            out_path = stripped[len("OUTPUT="):].strip()
    proc.wait()
    return proc.returncode, out_path


# ──────────────────────────────────────────────────────────────
# 片段合并（硬切）
# ──────────────────────────────────────────────────────────────
def _probe_dims(path: str) -> tuple[int, int] | None:
    """用 OpenCV 探测视频宽高，供合并前的一致性判定。"""
    import cv2
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        return None
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if w <= 0 or h <= 0:
        return None
    return w, h


def _merge_clips(clips: list[str], dst: Path) -> Path | None:
    """
    把多个片段按给定顺序硬切合并成一个视频。

    优先级：
      1. 系统 ffmpeg：尺寸一致时用 concat demuxer 无损拼接（-c copy，最快、不重编码）；
         尺寸不一致时用 concat filter 缩放补边后重编码到第一段尺寸。
      2. PyAV：逐段解码再编码进同一条 H.264 流（尺寸不一致时按目标尺寸重排）。
      3. 两者都没有：返回 None，保留单片不合并。
    """
    if len(clips) < 2:
        print("  [merge] 有效片段不足 2 个，跳过合并")
        return None

    dims = [_probe_dims(c) for c in clips]
    uniform = all(d is not None and d == dims[0] for d in dims)
    if not uniform:
        print(f"  [merge] 片段尺寸不一致 {dims}，将缩放补边到第一段尺寸后重编码")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is not None:
        merged = _merge_with_ffmpeg(ffmpeg, clips, dst, uniform, dims)
        if merged is not None:
            return merged

    try:
        return _merge_with_pyav(clips, dst, dims)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"  [merge][warn] PyAV 合并失败: {exc}")

    print("  [merge][warn] 未找到 ffmpeg 和 PyAV，无法合并；单片已保留")
    return None


def _merge_with_ffmpeg(
    ffmpeg: str, clips: list[str], dst: Path,
    uniform: bool, dims: list[tuple[int, int] | None],
) -> Path | None:
    if uniform:
        # concat demuxer：无损拼接，要求所有片段编码参数一致（同一渲染器产出，满足）
        list_file = dst.with_suffix(".concat.txt")
        lines = [f"file '{Path(c).resolve().as_posix()}'" for c in clips]
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(dst),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        list_file.unlink(missing_ok=True)
        if res.returncode == 0:
            print(f"  [merge] 无损拼接完成(ffmpeg concat): {dst}")
            return dst
        print(f"  [merge][warn] concat 无损拼接失败，改用重编码: {res.stderr.strip()}")

    # 尺寸不一致或无损拼接失败：concat filter 缩放补边到第一段尺寸后重编码
    target = next((d for d in dims if d is not None), None)
    if target is None:
        print("  [merge][warn] 无法确定目标尺寸，放弃 ffmpeg 合并")
        return None
    tw, th = target
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", c]
    filters = []
    for idx in range(len(clips)):
        # 等比缩放进 tw×th 内，再补边（背景取画布米色近似黑边更自然，这里用黑边保通用）
        filters.append(
            f"[{idx}:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{idx}]"
        )
    concat_inputs = "".join(f"[v{idx}]" for idx in range(len(clips)))
    filter_complex = ";".join(filters) + f";{concat_inputs}concat=n={len(clips)}:v=1:a=0[out]"
    cmd = [
        ffmpeg, "-y", "-loglevel", "error", *inputs,
        "-filter_complex", filter_complex, "-map", "[out]",
        "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(dst),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        print(f"  [merge] 重编码合并完成(ffmpeg filter): {dst}")
        return dst
    print(f"  [merge][warn] ffmpeg 重编码合并失败: {res.stderr.strip()}")
    return None


def _merge_with_pyav(
    clips: list[str], dst: Path, dims: list[tuple[int, int] | None],
) -> Path | None:
    """逐段解码再编码进同一条 H.264 流；尺寸不一致时按第一段尺寸重排。"""
    from fractions import Fraction

    import av

    target = next((d for d in dims if d is not None), None)
    if target is None:
        return None
    tw, th = target

    first = av.open(clips[0], mode="r")
    fps = first.streams.video[0].average_rate
    first.close()

    output = av.open(str(dst), mode="w")
    out_stream = output.add_stream("h264", rate=fps)
    out_stream.width = tw
    out_stream.height = th
    out_stream.pix_fmt = "yuv420p"
    out_stream.options = {"crf": "23", "preset": "medium"}

    # 逐段 pts 各自从 0 起，直接复用会导致跨段 DTS 非单调而 mux 失败；
    # PyAV 18 又不接受 frame.pts=None。故用全局帧计数在输出时基里重排连续 pts。
    time_base = Fraction(1, int(round(fps)))
    frame_index = 0
    for clip in clips:
        container = av.open(clip, mode="r")
        for frame in container.decode(video=0):
            if frame.width != tw or frame.height != th:
                frame = frame.reformat(width=tw, height=th)
            frame.pts = frame_index
            frame.time_base = time_base
            frame_index += 1
            for packet in out_stream.encode(frame):
                output.mux(packet)
        container.close()
    for packet in out_stream.encode(None):
        output.mux(packet)
    output.close()
    print(f"  [merge] 重编码合并完成(PyAV): {dst}")
    return dst


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not _ensure_env():
        return 1

    images, durations = args.images, args.durations

    if len(images) != len(durations):
        print(f"[err] 图片数 ({len(images)}) 与时长数 ({len(durations)}) 不一致")
        return 1

    for i, img in enumerate(images):
        if not os.path.exists(img):
            print(f"[err] 第 {i + 1} 张图片不存在: {img}")
            return 1

    total = len(images)
    print("=" * 60)
    banner = f"队列渲染器 - 共 {total} 个任务"
    if args.fail_fast:
        banner += "（fail-fast）"
    if args.merge:
        banner += "（渲染后合并）"
    print(banner)
    print("=" * 60)

    results: list[dict] = []
    aborted = False

    for i, (image, duration) in enumerate(zip(images, durations)):
        print(f"\n{'=' * 60}")
        print(f"[{i + 1}/{total}] {os.path.basename(image)} ({duration}ms / {duration / 1000:.3f}s)")
        print(f"{'=' * 60}")

        cmd = _build_cmd(sys.executable, args, image, duration)
        rc, out_path = _run_task(cmd)
        ok = rc == 0
        results.append({"image": image, "duration": duration, "ok": ok, "output": out_path})

        if not ok:
            print(f"[warn] 第 {i + 1} 个任务失败: {image}")
            if args.fail_fast:
                print("[stop] fail-fast 已触发，中止后续任务")
                aborted = True
                break
        else:
            print(f"[done] {i + 1}/{total}")

    print(f"\n{'=' * 60}")
    print("队列渲染汇总")
    print(f"{'=' * 60}")
    ok_n = sum(1 for r in results if r["ok"])
    fail_n = sum(1 for r in results if not r["ok"])
    print(f"  成功: {ok_n}/{total}")
    if fail_n:
        print(f"  失败: {fail_n}/{total}")
        for r in results:
            if not r["ok"]:
                print(f"    - {r['image']}")
    if aborted:
        print(f"  未处理: {total - len(results)}（已被 fail-fast 中止）")

    # 合并（保留单片）：按输入顺序拼接所有成功片段
    merged_path: Path | None = None
    if args.merge:
        clips = [r["output"] for r in results if r["ok"] and r["output"]]
        missing = [r for r in results if r["ok"] and not r["output"]]
        if missing:
            print(f"  [merge][warn] {len(missing)} 个成功任务未捕获到输出路径，将不参与合并")
        print(f"\n{'=' * 60}")
        print(f"合并 {len(clips)} 个片段为一个总视频（硬切）")
        print(f"{'=' * 60}")
        name = args.merged_name or f"merged_{datetime.datetime.now():%Y%m%d_%H%M%S}.mp4"
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        merged_path = _merge_clips(clips, out_dir / name)

    print(f"\n输出目录: {os.path.abspath(args.out_dir)}")
    if merged_path is not None:
        print(f"合并视频: {os.path.abspath(str(merged_path))}")
        print(f"MERGED={merged_path}")

    merge_failed = args.merge and merged_path is None and ok_n >= 2
    return 0 if fail_n == 0 and not aborted and not merge_failed else 1


if __name__ == "__main__":
    sys.exit(main())
