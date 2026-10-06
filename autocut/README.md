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

## 더듬는 말 / NG 자동삭제
기본으로 켜져 있으며 삭제 내역은 `out/removed.json`에 기록됩니다 (잘못 지워졌는지 확인용).
- **추임새:** `음, 어, 아, 에, 흠…`은 항상 삭제. `그, 저, 뭐, 이제, 막`은 0.4초 이상 길게 끌 때만 삭제.
  추가 단어는 `--filler-words 그니까,뭐지`
- **NG(재촬영):** 같은 문장을 다시 말하면 앞선 시도를 지우고 마지막 테이크만 남김. 말하다 끊고 다시 시작한 경우도 포함.
  "다시 갈게요", "NG", "컷" 같은 짧은 진행 멘트도 삭제. 판정 민감도는 `--retake-threshold`(기본 0.7, 낮을수록 공격적)
- 끄기: `--no-clean`(전체), `--no-filler`, `--no-ng`

## 자막 서식: '기본텍스트' 사용
캡컷 '내 보관함'의 텍스트 프리셋은 외부에서 직접 읽을 수 없어, **템플릿 초안**을 거쳐 복제합니다.
1. 캡컷에서 새 프로젝트를 만들고 내 보관함 > 텍스트 > '기본텍스트'를 타임라인에 한 번 올린 뒤 저장 (예: 프로젝트명 `style_base`)
2. 실행: `python autocut/autocut.py video.mp4 --draft-dir "<초안 폴더>" --style-draft style_base`
3. 폰트·색·테두리·그림자·위치·애니메이션이 모든 자막에 복제됩니다. 글자 크기·위치도 템플릿 기준입니다.
