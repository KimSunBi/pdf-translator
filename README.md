# PDF 번역기 (웹 앱 + 스크립트 + 모델 비교)

**웹 앱**: `app.py` (Streamlit). 브라우저에서 PDF를 올리면 번역하고, 번역 탭과 모델 비교 탭이 있습니다. 배포 방법은 **DEPLOY.md**.
```bash
pip install -r requirements.txt
ALLOW_NO_PASSWORD=1 streamlit run app.py     # 내 컴퓨터에서만 쓸 때 (온라인 배포 때는 APP_PASSWORD 필수)
```
아래 명령줄 도구(`translate.py`, `compare.py`)는 같은 엔진을 터미널에서 쓰는 방법입니다.

PDF를 번역하고, 코드 검수 8가지 + LLM 검수로 품질을 확인합니다. 모델은 **Gemini / Claude / 오픈소스(OpenAI 호환)** 중에서
설정 파일로 바꿔 끼울 수 있고, 같은 PDF를 여러 모델로 번역해 DeepL과 나란히 비교하는 `compare.py`가 있습니다.

## 준비

```bash
pip install -r requirements.txt
```

쓰려는 모델의 API 키를 발급받아 **환경변수로만** 설정합니다. 키를 코드·설정 파일·채팅에 붙여넣지 마세요.

| 프로필 | 발급처 | 환경변수 |
|---|---|---|
| `gemini-3.1-pro` | Google AI Studio (aistudio.google.com) | `GEMINI_API_KEY` |
| `claude-sonnet` | Anthropic 콘솔 (console.anthropic.com) | `ANTHROPIC_API_KEY` |
| `open-source` | 오픈소스 모델을 호스팅해 주는 서비스 (아래 참고) | `OPENAI_COMPAT_API_KEY` |

```bash
# macOS / Linux
export GEMINI_API_KEY="발급받은키"
# Windows (PowerShell)
$env:GEMINI_API_KEY="발급받은키"
```

> Claude.ai나 ChatGPT 구독과 API 키는 별개입니다. API는 쓴 만큼 요금이 부과됩니다.

## 1) 모델 비교 (먼저 이것부터)

```bash
# 앞 3쪽만, 키가 설정된 모든 프로필로 번역해서 비교 페이지 생성
python compare.py my.pdf --pages 1-3 --glossary glossary.csv --domain 교육

# 특정 모델만 지정, DeepL 결과도 같이 놓기
python compare.py my.pdf --pages 1-3 --profiles gemini-3.1-pro,claude-sonnet --deepl-text deepl.txt
```

결과 `output/<이름>-compare/compare.html`을 컴퓨터 브라우저로 여세요.
- **모델 이름이 가려진 상태**(번역 A, B, C)로 원문과 나란히 읽습니다. 편견 없이 비교하기 위해서입니다.
- 문단마다 "이게 제일 좋음"을 누르면 위쪽에 득표수가 집계됩니다. 버튼으로 이름을 공개할 수 있습니다.
- DeepL 열에는 DeepL 번역을 붙여넣거나, `--deepl-text`로 txt(문단 사이 빈 줄)를 주면 자동으로 짝지어 줍니다.
  문단 수가 맞지 않으면 페이지 아래에 전체 텍스트로 붙습니다.
- 위쪽 요약표에 모델별 자동 검수 점수, 확인 필요 문단 수, 토큰, 비용, 시간이 나옵니다.
  단, 점수는 규칙 검사 기준이라 번역의 자연스러움은 반영하지 못합니다. 투표가 진짜 기준입니다.
- 모델 수만큼 비용이 듭니다. 처음에는 `--pages 1-3`과 `--no-review`로 싸게 시험하세요.

## 2) 단일 모델로 번역

```bash
python translate.py my.pdf --extract-only                     # API 없이 추출 결과 확인
python translate.py my.pdf --pages 1-3 --profile claude-sonnet # 앞 3쪽 시험
python translate.py my.pdf --glossary glossary.csv --domain 교육 --style formal
```

옵션: `--direction en2ko|ko2en`, `--style formal|plain`(~입니다 / ~이다), `--profile 이름`(기본은 active_profile),
`--no-review`(LLM 검수 생략), `--dry-run`(API 없이 가짜 번역으로 흐름만 확인, 점수는 의미 없음)

결과(`output/<파일명>/`): `.ko.docx`(확인 필요 문단은 노란색), `.ko.md`, `report.html`(검수 보고서), `report.csv`, `result.json`

## 오픈소스 모델 연결

`config.yaml`의 `open-source` 프로필에 두 값을 채우면 됩니다.
- `base_url`, `model`: 오픈소스 모델(gpt-oss, Qwen, Gemma 등)을 호스팅해 주는 서비스의 **OpenAI 호환 주소**와 모델 이름
- 내 PC에서 직접 돌리는 경우(Ollama, vLLM 등)는 `http://localhost:11434/v1` 같은 주소를 쓰고, 키가 필요 없으면 `api_key_env` 줄을 지웁니다. 단 쓸 만한 크기의 모델은 성능 좋은 GPU가 필요합니다.
- 값이 `<...>` 상태이면 비교에서 자동 제외됩니다.
- 이미지 입력이 안 되는 모델은 `vision: false`로 두세요(표·2단 편집 페이지를 이미지로 읽는 기능만 꺼지고 텍스트 추출로 대체).
- 서버가 JSON 응답 형식 옵션을 지원하지 않으면 자동으로 해제하고 재시도합니다.

## 파이프라인

1. **분석**: 문서를 훑어 요약·분야·문체·핵심 용어 추출
2. **번역**: 용어집 + 자동 추출 용어 + 앞 문맥을 붙여 문단 단위(배치) 번역
3. **검수**: 코드 검사 8가지 → 위반 시 자동 재번역(최대 2회) → 남은 위반은 노란색 표시 → 모든 문단을 LLM이 의미 중심으로 검수. LLM 수정안도 코드 검사를 다시 통과해야 적용됩니다.
4. **통일**: 핵심 용어를 모든 번역 호출에 같이 넘기고, 문체는 검사·재번역으로 통일

코드 검사 8가지(`config.yaml`의 `checks`에서 켜고 끄기, 기준값 조정):
숫자·날짜 일치 / URL·인용·수식·코드 보존 / 용어집 적용 / 문단 누락·중복 / 길이 비율 / 미번역 잔존 / 문체 일관성 / 괄호·따옴표 짝

## 처리 규칙

- 표: 셀 단위 번역(숫자·기호뿐인 셀은 원문 유지). 표·2단 편집 페이지는 이미지를 읽을 수 있는 모델이 구조화
- 수식, 코드, 참고문헌: 번역하지 않고 원문 유지. **"References" 제목 이후는 전부 원문 유지**이므로 뒤에 부록이 있으면 `skip_references: false`
- 머리글·바닥글·쪽번호 제거, 쪽을 넘어 이어지는 문단 병합
- 스캔본(글자가 없는 페이지)은 경고 후 건너뜀 (OCR은 3단계)

## 용어집

`glossary.example.csv`처럼 `en,ko` 두 열의 CSV. 번역에 강제 적용되고 코드 검수로 위반을 잡습니다.

## 아직 검증되지 않은 것

- **실제 API 호출은 이 코드를 만든 환경에서 한 번도 실행하지 못했습니다**(키·네트워크 없음). 세 어댑터는 가짜 클라이언트로 요청 형태·토큰 집계·재시도를 테스트했고, 실제 SDK 클라이언트 생성까지만 확인했습니다. 첫 실행은 `--pages 1-3`으로 하세요.
- 웹 앱의 화면 흐름(업로드, 테스트 모드 번역, 다운로드, 모델 비교, 투표)은 헤드리스 브라우저로 확인했지만 **가짜 번역(테스트 모드)으로만** 확인했습니다. 실제 모델 호출과 Streamlit Community Cloud 배포는 아직 해보지 못했습니다.
- 이미지로 읽기(표·2단 편집)는 API가 필요해 실제 동작을 확인하지 못했습니다. 실패하면 자동으로 텍스트 추출로 대체됩니다.
- 비용은 프로필 또는 `pricing`에 단가(100만 토큰당 USD)를 넣어야 계산됩니다. 비워두면 토큰 수만 나옵니다.
- 검수 기준값(길이 비율 등)은 초기 추정치입니다. 실제 PDF로 돌려보며 오탐이 많으면 조정이 필요합니다.
- 모델 이름(`gemini-3.1-pro-preview`, `claude-sonnet-5-5`)은 확인한 값을 넣었지만, 서비스 쪽에서 바뀔 수 있습니다. 오류가 나면 `config.yaml`의 `model`부터 확인하세요.

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest -q     # 50개: 검수 8가지, PDF 추출, 파이프라인, 모델 어댑터, 비교 페이지, 실행기, 웹 앱 잠금
```

## 다음 단계

웹 앱 배포 → 비교 결과로 모델 확정 → 3단계 레이아웃 유지 PDF 출력, OCR.
