#!/usr/bin/env python3
"""무음 제거 + 음량 평준화 + 자막 생성 + 캡컷 초안 생성 파이프라인."""
import argparse, json, re, subprocess, sys
from pathlib import Path

SEC = 1_000_000  # pyJianYingDraft 시간 단위(마이크로초)


def run(cmd, capture=False):
    p = subprocess.run(cmd, capture_output=capture, text=True)
    if p.returncode:
        sys.exit(f"명령 실패: {' '.join(map(str, cmd))}\n{p.stderr or ''}")
    return p


def probe(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,r_frame_rate:format=duration",
               "-of", "json", str(path)], capture=True).stdout
    j = json.loads(out)
    s = j["streams"][0]
    n, d = map(int, s["r_frame_rate"].split("/"))
    return s["width"], s["height"], round(n / d), float(j["format"]["duration"])


def normalize(src, dst, target_lufs):
    """영상은 그대로 복사, 오디오만 loudnorm(EBU R128)으로 음량 평준화."""
    run(["ffmpeg", "-y", "-i", str(src), "-c:v", "copy", "-af",
         f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", str(dst)])


def detect_keep(path, duration, noise_db, min_silence, pad, min_keep):
    """silencedetect로 무음 구간을 찾고, 남길 구간(앞뒤 pad 포함) 목록을 반환."""
    err = run(["ffmpeg", "-i", str(path), "-af",
               f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
              capture=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    if len(starts) > len(ends):
        ends.append(duration)
    keep, cur = [], 0.0
    for s, e in zip(starts, ends):
        if s - cur > 0:
            keep.append([cur, s])
        cur = e
    if cur < duration:
        keep.append([cur, duration])
    keep = [[max(0, a - pad), min(duration, b + pad)] for a, b in keep]
    merged = []
    for a, b in keep:  # pad로 겹친 구간 병합
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_keep]


def to_new_time(t, keep):
    """원본 시간 t → 컷 편집 후 타임라인 시간 (컷된 구간이면 None)."""
    acc = 0.0
    for a, b in keep:
        if a <= t <= b:
            return acc + (t - a)
        acc += b - a
    return None


def transcribe(path, model_name, language):
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, compute_type="auto")
    segs, _ = model.transcribe(str(path), language=language, word_timestamps=True,
                               vad_filter=True)
    return [w for s in segs for w in s.words]


def build_subtitles(words, keep, max_chars, max_gap):
    """컷 이후 타임라인 기준으로 단어를 자막 줄로 묶는다."""
    lines, cur = [], None
    for w in words:
        s, e = to_new_time(w.start, keep), to_new_time(w.end, keep)
        if s is None or e is None:
            continue
        text = w.word.strip()
        if not text:
            continue
        if cur and (len(cur["text"]) + len(text) + 1 > max_chars
                    or s - cur["end"] > max_gap or cur["text"][-1] in ".?!。"):
            lines.append(cur)
            cur = None
        if cur:
            cur["text"] += " " + text
            cur["end"] = e
        else:
            cur = {"text": text, "start": s, "end": e}
    if cur:
        lines.append(cur)
    for a, b in zip(lines, lines[1:]):  # 자막 겹침/깜빡임 방지
        a["end"] = min(max(a["end"], a["start"] + 0.3), b["start"])
    return lines


def ts(t):
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02}:{int(m):02}:{s:06.3f}".replace(".", ",")


def write_srt(lines, path):
    path.write_text("".join(
        f"{i}\n{ts(l['start'])} --> {ts(l['end'])}\n{l['text']}\n\n"
        for i, l in enumerate(lines, 1)), encoding="utf-8")


def render_preview(src, keep, dst):
    v = "".join(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}];"
                f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];"
                for i, (a, b) in enumerate(keep))
    j = "".join(f"[v{i}][a{i}]" for i in range(len(keep)))
    run(["ffmpeg", "-y", "-i", str(src), "-filter_complex",
         f"{v}{j}concat=n={len(keep)}:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]", str(dst)])


def make_draft(draft_dir, name, media, keep, lines, size, fps, text_size):
    import pyJianYingDraft as jy
    from pyJianYingDraft import Timerange, TrackType
    script = jy.DraftFolder(str(draft_dir)).create_draft(
        name, size[0], size[1], fps, allow_replace=True)
    vtrack = script.append_track(jy.TrackSpec(TrackType.video, "video"))
    ttrack = script.append_track(jy.TrackSpec(TrackType.text, "subtitle"))
    mat = jy.VideoMaterial(str(media))
    pos = 0
    for a, b in keep:
        st = int(a * SEC)
        d = min(int((b - a) * SEC), mat.duration - st)  # 컨테이너/영상 길이 차이 보정
        if d <= 0:
            continue
        script.add_segment(jy.VideoSegment(
            mat, Timerange(pos, d), source_timerange=Timerange(st, d)), vtrack)
        pos += d
    style = jy.TextStyle(size=text_size, color=(1, 1, 1), align=1, auto_wrapping=True)
    clip = jy.ClipSettings(transform_y=-0.8)
    border = jy.TextBorder(color=(0, 0, 0), width=40.0)
    for l in lines:
        script.add_segment(jy.TextSegment(
            l["text"], Timerange(int(l["start"] * SEC), int((l["end"] - l["start"]) * SEC)),
            style=style, clip_settings=clip, border=border), ttrack)
    script.save()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video")
    ap.add_argument("-o", "--out", default="out", help="결과 폴더")
    ap.add_argument("--noise", type=float, default=-35, help="무음 기준 dB (낮을수록 엄격)")
    ap.add_argument("--min-silence", type=float, default=0.4, help="이 길이 이상 무음만 삭제(초)")
    ap.add_argument("--pad", type=float, default=0.08, help="컷 앞뒤 여유(초)")
    ap.add_argument("--min-keep", type=float, default=0.3, help="이보다 짧은 조각은 버림(초)")
    ap.add_argument("--lufs", type=float, default=-16, help="목표 음량 (유튜브 -14, 쇼츠 -14~-16)")
    ap.add_argument("--no-normalize", action="store_true")
    ap.add_argument("--no-subs", action="store_true")
    ap.add_argument("--model", default="small", help="whisper 모델 (small/medium/large-v3)")
    ap.add_argument("--lang", default="ko")
    ap.add_argument("--max-chars", type=int, default=18, help="자막 한 줄 최대 글자수")
    ap.add_argument("--text-size", type=float, default=8.0)
    ap.add_argument("--render", action="store_true", help="컷 편집 미리보기 mp4도 생성")
    ap.add_argument("--draft-dir", help="캡컷 초안 폴더 (예: .../CapCut Drafts)")
    a = ap.parse_args()

    src, out = Path(a.video).resolve(), Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    media = src
    if not a.no_normalize:
        media = out / f"{src.stem}_norm.mp4"
        print("[1/4] 음량 평준화"); normalize(src, media, a.lufs)
    w, h, fps, dur = probe(media)

    print("[2/4] 무음 구간 탐지")
    keep = detect_keep(media, dur, a.noise, a.min_silence, a.pad, a.min_keep)
    kept = sum(b - a_ for a_, b in keep)
    print(f"      {dur:.1f}s → {kept:.1f}s ({len(keep)}개 컷, {dur - kept:.1f}s 삭제)")
    (out / "cuts.json").write_text(json.dumps(
        [{"start": s, "end": e} for s, e in keep], indent=2))

    lines = []
    if not a.no_subs:
        print("[3/4] 음성 인식(Whisper)")
        lines = build_subtitles(transcribe(media, a.model, a.lang), keep, a.max_chars, 0.7)
        write_srt(lines, out / f"{src.stem}.srt")
        print(f"      자막 {len(lines)}줄")

    print("[4/4] 결과물 생성")
    if a.render:
        render_preview(media, keep, out / f"{src.stem}_cut.mp4")
    if a.draft_dir:
        make_draft(a.draft_dir, f"autocut_{src.stem}", media, keep, lines,
                   (w, h), fps, a.text_size)
        print(f"      캡컷 초안 생성: autocut_{src.stem}")
    print(f"완료 → {out}")


if __name__ == "__main__":
    main()
