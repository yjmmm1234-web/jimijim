#!/usr/bin/env python3
"""무음 제거 + 음량 평준화 + 자막 생성 + 캡컷 초안 생성 파이프라인."""
import argparse, copy, difflib, json, re, subprocess, sys, uuid
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


def detect_keep(path, duration, noise_db, min_silence, lead, trail, min_keep):
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
    keep = [[max(0, a - lead), min(duration, b + trail)] for a, b in keep]
    merged = []
    for a, b in keep:  # pad로 겹친 구간 병합
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_keep]


STRICT_FILLERS = {"음", "으음", "어", "어어", "아", "아아", "에", "에이", "흠", "으", "음음", "어음"}
SOFT_FILLERS = {"그", "저", "뭐", "이제", "막"}  # 실제 단어일 수 있어 길게 끌 때만 추임새로 취급
RETAKE_MARKERS = ("엔지", "NG", "ng", "다시 갈게", "다시 할게", "다시 하겠", "다시 가겠", "컷")


def norm(text):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text)


def snap_to_words(keep, words, lead, trail):
    """컷 경계가 단어 한가운데에 걸리면 그 단어가 통째로 들어오도록 구간을 넓힌다."""
    out = []
    for a, b in keep:
        for w in words:
            if w.start < a < w.end:
                a = max(0, w.start - lead)
            if w.start < b < w.end:
                b = w.end + trail
        out.append([a, b])
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def find_fillers(words, extra, soft_min, pad=0.03):
    strict = STRICT_FILLERS | set(extra)
    out = []
    for w in words:
        t = norm(w.word)
        dur = w.end - w.start
        if t in strict or (t in SOFT_FILLERS and dur >= soft_min):
            out.append({"start": max(0, w.start - pad), "end": w.end + pad,
                        "reason": "filler", "text": w.word.strip()})
    return out


def split_units(words, gap=0.6):
    """단어를 문장 단위(말 끊김/문장부호 기준)로 묶는다."""
    units, cur = [], []
    for w in words:
        if cur and (w.start - cur[-1].end > gap or cur[-1].word.strip()[-1:] in ".?!。"):
            units.append(cur)
            cur = []
        cur.append(w)
    if cur:
        units.append(cur)
    return [{"start": u[0].start, "end": u[-1].end,
             "text": " ".join(w.word.strip() for w in u)} for u in units]


def similar(a, b):
    a, b = norm(a), norm(b)
    if len(a) < 4 or not b:
        return 0.0
    # 앞부분만 말하다 끊고 다시 시작한 경우도 잡도록 b의 앞부분과도 비교
    full = difflib.SequenceMatcher(None, a, b).ratio()
    prefix = difflib.SequenceMatcher(None, a, b[:len(a) + 2]).ratio()
    return max(full, prefix)


def find_retakes(words, threshold, window=3, pad=0.05):
    """같은 문장을 반복해서 다시 말한 경우, 앞선 시도(NG)를 지우고 마지막 테이크만 남긴다."""
    units = split_units(words)
    drop = set()
    for i, u in enumerate(units):
        if any(m in u["text"] for m in RETAKE_MARKERS) and len(norm(u["text"])) <= 12:
            drop.add((i, "retake-marker"))  # "다시 갈게요" 같은 진행 멘트
            continue
        for j in range(i + 1, min(i + 1 + window, len(units))):
            if similar(u["text"], units[j]["text"]) >= threshold:
                drop.update((k, "retake") for k in range(i, j))
                break
    return [{"start": max(0, units[k]["start"] - pad), "end": units[k]["end"] + pad,
             "reason": r, "text": units[k]["text"]} for k, r in sorted(drop)]


def subtract(keep, removed):
    """keep 구간에서 removed 구간들을 빼고 남은 구간을 반환."""
    for r in sorted(removed, key=lambda x: x["start"]):
        nxt = []
        for a, b in keep:
            if r["end"] <= a or r["start"] >= b:
                nxt.append((a, b))
                continue
            if r["start"] > a:
                nxt.append((a, r["start"]))
            if r["end"] < b:
                nxt.append((r["end"], b))
        keep = nxt
    return keep


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
    # initial_prompt: 추임새를 생략하지 않고 받아적도록 유도 (제거 대상 탐지용)
    segs, _ = model.transcribe(
        str(path), language=language, word_timestamps=True, vad_filter=True,
        vad_parameters={"threshold": 0.3, "min_silence_duration_ms": 500},
        initial_prompt="음... 어... 그, 그러니까, 저기... 네, 안녕하세요. 어, 음, 오늘은요.")
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


def apply_style_template(draft_path, template_path):
    """템플릿 초안(캡컷에서 '기본텍스트'를 올려둔 초안)의 텍스트 서식을 자막 전체에 복제한다."""
    tpl = json.loads(Path(template_path, "draft_content.json").read_text(encoding="utf-8"))
    tmats = {m["id"]: m for m in tpl["materials"].get("texts", [])}
    tseg = next((sg for t in tpl["tracks"] if t["type"] == "text" for sg in t["segments"]
                 if sg["material_id"] in tmats), None)
    if tseg is None:
        sys.exit(f"템플릿 초안에 텍스트가 없습니다: {template_path}")
    tmat = tmats[tseg["material_id"]]
    tcontent = json.loads(tmat["content"]) if isinstance(tmat.get("content"), str) else None

    f = Path(draft_path, "draft_content.json")
    d = json.loads(f.read_text(encoding="utf-8"))
    mats = d["materials"]
    all_tpl = {m["id"]: (k, m) for k, v in tpl["materials"].items() if isinstance(v, list)
               for m in v if isinstance(m, dict) and "id" in m}
    for track in d["tracks"]:
        if track["type"] != "text":
            continue
        for seg in track["segments"]:
            old = next(m for m in mats["texts"] if m["id"] == seg["material_id"])
            new = copy.deepcopy(tmat)
            new["id"] = uuid.uuid4().hex.upper()
            text = json.loads(old["content"])["text"] if isinstance(old.get("content"), str) else old["content"]
            if tcontent is not None:
                c = copy.deepcopy(tcontent)
                c["text"] = text
                for st in c.get("styles", []):
                    st["range"] = [0, len(text)]
                new["content"] = json.dumps(c, ensure_ascii=False)
            else:
                new["content"] = text
            for key in ("words", "base_content"):
                if key in new:
                    new[key] = type(new[key])()
            mats["texts"] = [m for m in mats["texts"] if m["id"] != old["id"]] + [new]
            seg["material_id"] = new["id"]
            seg["clip"] = copy.deepcopy(tseg.get("clip", seg.get("clip")))
            refs = []  # 애니메이션·효과 등 부속 소재도 새 id로 복제
            for rid in tseg.get("extra_material_refs", []):
                if rid in all_tpl:
                    k, m = all_tpl[rid]
                    m = copy.deepcopy(m)
                    m["id"] = uuid.uuid4().hex.upper()
                    mats.setdefault(k, []).append(m)
                    refs.append(m["id"])
            seg["extra_material_refs"] = refs
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def make_draft(draft_dir, name, media, keep, lines, size, fps, text_size, style_draft=None):
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
    if style_draft and lines:
        apply_style_template(Path(draft_dir, name), Path(draft_dir, style_draft))


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
    ap.add_argument("--no-clean", action="store_true", help="더듬는 말/NG 자동삭제 끄기")
    ap.add_argument("--no-filler", action="store_true", help="추임새 삭제만 끄기")
    ap.add_argument("--no-ng", action="store_true", help="NG(재촬영) 삭제만 끄기")
    ap.add_argument("--filler-words", default="", help="추가 추임새, 쉼표 구분 (예: 그니까,뭐지)")
    ap.add_argument("--soft-filler-min", type=float, default=0.4, help="'그/저/뭐'를 추임새로 볼 최소 길이(초)")
    ap.add_argument("--retake-threshold", type=float, default=0.7, help="NG 판정 유사도(0~1, 낮을수록 공격적)")
    ap.add_argument("--style-draft", help="'기본텍스트'가 올려진 캡컷 초안 이름 (자막 서식 템플릿)")
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
    keep = detect_keep(media, dur, a.noise, a.min_silence, a.lead, a.pad, a.min_keep)
    kept = sum(b - a_ for a_, b in keep)
    print(f"      {dur:.1f}s → {kept:.1f}s ({len(keep)}개 컷, {dur - kept:.1f}s 삭제)")
    (out / "cuts.json").write_text(json.dumps(
        [{"start": s, "end": e} for s, e in keep], indent=2))

    lines, removed = [], []
    need_words = not a.no_subs or not a.no_clean
    if need_words:
        print("[3/4] 음성 인식(Whisper)")
        words = transcribe(media, a.model, a.lang)
        keep = snap_to_words(keep, words, a.lead, a.pad)
        if not a.no_clean:
            if not a.no_filler:
                removed += find_fillers(words, [x for x in a.filler_words.split(",") if x],
                                        a.soft_filler_min)
            if not a.no_ng:
                removed += find_retakes(words, a.retake_threshold)
            keep = [(x, y) for x, y in subtract(keep, removed) if y - x >= a.min_keep]
            (out / "removed.json").write_text(
                json.dumps(removed, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"      추임새/NG {len(removed)}건 삭제 (확인: removed.json), "
                  f"최종 {sum(y - x for x, y in keep):.1f}s")
            (out / "cuts.json").write_text(json.dumps(
                [{"start": x, "end": y} for x, y in keep], indent=2))
        if not a.no_subs:
            lines = build_subtitles(words, keep, a.max_chars, 0.7)
            write_srt(lines, out / f"{src.stem}.srt")
            print(f"      자막 {len(lines)}줄")

    print("[4/4] 결과물 생성")
    if a.render:
        render_preview(media, keep, out / f"{src.stem}_cut.mp4")
    if a.draft_dir:
        make_draft(a.draft_dir, f"autocut_{src.stem}", media, keep, lines,
                   (w, h), fps, a.text_size, a.style_draft)
        print(f"      캡컷 초안 생성: autocut_{src.stem}")
    print(f"완료 → {out}")


if __name__ == "__main__":
    main()
