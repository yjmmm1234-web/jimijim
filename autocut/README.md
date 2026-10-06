# autocut — 캡컷용 자동 컷편집 파이프라인

원본 영상 → (1) 음량 평준화 → (2) 무음 구간 컷 → (3) Whisper 자막 → (4) 캡컷 초안 생성

## 준비
```
pip install -r autocut/requirements.txt   # ffmpeg/ffprobe 도 필요
```

## 사용
```
python autocut/autocut.py video.mp4 -o out \
  --draft-dir "<캡컷 초안 폴더>" --render
```
- 결과: `out/video_norm.mp4`(음량 평준화본), `out/cuts.json`(컷 목록), `out/video.srt`(자막),
  `--render` 시 `out/video_cut.mp4`(컷 미리보기), `--draft-dir` 시 캡컷 초안 `autocut_video`
- 캡컷 초안 폴더 예(Windows): `C:\Users\<이름>\AppData\Local\CapCut\User Data\Projects\com.lveditor.draft`
  (Mac: `~/Movies/CapCut/User Data/Projects/com.lveditor.draft`). 초안 생성 후 캡컷을 재시작하면 목록에 나타납니다.
- 초안 생성이 캡컷 버전과 안 맞으면 `--draft-dir` 없이 mp4 + SRT를 캡컷에 가져와 사용하세요.

## 주요 옵션
| 옵션 | 기본 | 설명 |
|---|---|---|
| `--noise` | -35 | 무음 기준(dB). 배경소음이 크면 -30 등으로 올림 |
| `--min-silence` | 0.4 | 이 길이 이상 무음만 삭제(초) |
| `--pad` | 0.08 | 컷 앞뒤 여유(초). 말이 잘리면 키움 |
| `--lufs` | -16 | 목표 음량 |
| `--model` | small | whisper 모델 (정확도↑: medium / large-v3) |
| `--max-chars` | 18 | 자막 한 줄 글자수 |
