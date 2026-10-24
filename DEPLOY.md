# 웹 앱 배포 안내 (Streamlit Community Cloud)

목표: 주소(URL)로 접속해서 PDF를 올리면 번역되는 상태. 폰에서도 사용 가능, 비밀번호로 잠금.

## 0. 먼저 알아둘 것

- **API 키와 비밀번호는 Streamlit의 Secrets 입력칸에만** 넣습니다. 채팅, 코드, GitHub 저장소에는 넣지 마세요.
  (`.gitignore`가 `secrets.toml`을 막아 두었지만, 키를 코드 파일에 직접 적으면 올라갑니다.)
- 앱 주소를 아는 사람이 API 비용을 쓰지 못하게 **비밀번호가 없으면 앱이 잠긴 채로 시작**합니다.
- 비밀번호 잠금은 간단한 보호입니다(여러 번 틀리면 그 접속은 막힘). 주소를 아무에게나 공유하지 마세요.
- 각 서비스 콘솔(Google AI Studio, Anthropic)에서 **월 사용 한도(예산 알림)**를 걸어 두세요. 월 5만 원 안쪽을 권장합니다.

## 1. 코드를 GitHub에 올리기

Jay가 빈 저장소 `pdf-translator`를 만든 뒤 알려주시면 제가 코드를 올립니다.

## 2. Streamlit에 배포하기

1. https://share.streamlit.io 접속 → **GitHub로 로그인**하고 저장소 접근을 허용합니다.
2. **Create app** → 저장소 `pdf-translator`, 브랜치 `main`, Main file path `app.py`를 선택합니다.
3. **Advanced settings**를 열어서
   - Python 버전: 3.12 이상을 고릅니다.
   - **Secrets** 칸에 아래 내용을 붙여넣고 값을 채웁니다 (따옴표 유지).

   ```toml
   APP_PASSWORD = "길고-추측하기-어려운-비밀번호"
   GEMINI_API_KEY = "발급받은-Gemini-키"
   ANTHROPIC_API_KEY = "발급받은-Claude-키"
   ```
   쓰지 않는 모델의 줄은 빼도 됩니다. 키가 없는 모델은 화면에서 자동으로 빠집니다.
4. **Deploy**를 누르고 몇 분 기다립니다. 처음에는 패키지 설치 때문에 오래 걸립니다.

## 3. 처음 확인하는 순서

1. 주소로 접속 → 비밀번호 입력
2. 샘플 PDF를 올리고 **고급 옵션 → 테스트 모드**를 켠 뒤 번역 시작 → 결과가 나오면 배포 성공 (비용 없음)
3. 테스트 모드를 끄고 **쪽 범위 `1-3`**으로 실제 번역 → 오류, 비용, 시간을 확인
4. **모델 비교 탭**에서 같은 3쪽을 2개 이상의 모델로 비교하고, 이름이 가려진 상태로 읽으며 투표

## 4. 문제가 생기면

| 증상 | 확인할 것 |
|---|---|
| "비밀번호가 설정되지 않아 잠겨 있습니다" | Secrets에 `APP_PASSWORD` 줄이 있는지 |
| 모델 목록이 비어 있음 | Secrets에 해당 모델의 키 이름(`GEMINI_API_KEY` 등)이 정확한지 |
| 번역 중 "호출 실패" | 키가 올바른지, 콘솔에서 한도·결제가 막히지 않았는지, `config.yaml`의 `model` 이름이 맞는지 |
| 번역이 중간에 끊김 | 오래 걸리는 작업 중 브라우저를 닫거나 폰 화면이 꺼지면 끊길 수 있습니다. 쪽 범위를 나눠서 실행하세요 |
| 앱이 느리게 열림 | 한동안 쓰지 않으면 앱이 절전 상태가 될 수 있습니다. 첫 접속은 깨우는 데 시간이 걸립니다 |

키가 노출됐다고 의심되면 해당 서비스 콘솔에서 즉시 키를 삭제하고 새로 발급받은 뒤 Secrets를 갱신하세요.

## 5. 내 컴퓨터에서 실행하고 싶다면

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # 값 채우기 (이 파일은 git에 올라가지 않음)
streamlit run app.py
```
비밀번호 없이 내 컴퓨터에서만 쓰려면 `ALLOW_NO_PASSWORD=1 streamlit run app.py`.
