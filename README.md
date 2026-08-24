# EDGE Trading Journal

브라우저에서 바로 사용할 수 있는 개인용 트레이딩 매매일지입니다.

## 기능

- 종목, 손익비, 승/패, 투입 금액, 손익 금액, 매매 근거 기록
- 차트 이미지 업로드 (최대 2MB)
- 누적 손익, 승률, 평균 손익비, 누적 수익률 그래프
- 매매 10회 단위 자동 사이클 및 복기 기록
- 자동 저장되는 메모장과 검색
- 반응형 모바일 레이아웃
- 브라우저 `localStorage` 기반 데이터 보존

## 실행

별도 설치 없이 `index.html`을 브라우저로 열면 됩니다. 로컬 서버로 실행하려면:

```powershell
npx serve .
```

> 데이터는 현재 브라우저에만 저장됩니다. 브라우저 데이터를 삭제하면 기록도 사라질 수 있습니다.

초기 화면은 예시 데이터 없이 빈 상태로 시작합니다. 이전 버전에서 이미 예시 데이터가 저장된 브라우저는 업데이트 후에도 `localStorage` 내용이 보존되므로 기존 항목이 계속 보일 수 있습니다. 해당 브라우저에 보존할 실제 기록이 없는 경우에만 개발자 콘솔에서 아래 명령을 실행한 뒤 새로고침하세요.

```js
localStorage.removeItem('edge-journal-v1');
localStorage.removeItem('edge-fund-chat-v1');
location.reload();
```

## Fund Team 로컬 API 연결

로컬 개발에서 채팅은 기본적으로 `http://localhost:8765`의 Fund Team API를 사용합니다. Vercel hostname에서는 동일 출처 `/api/fund`가 기본값입니다. API 주소는 다음 우선순위로 설정됩니다.

1. 페이지 로드 전에 지정한 `window.EDGE_FUND_API_BASE`
2. URL 쿼리 `?apiBase=http://localhost:8765`
3. `localStorage`의 `edge-fund-api-base`
4. 기본값: Vercel에서는 `/api/fund`, 그 외 환경에서는 `http://localhost:8765`

콘솔에서 로컬 설정을 저장하는 예시:

```js
localStorage.setItem('edge-fund-api-base', 'http://localhost:8765');
location.reload();
```

프런트엔드는 아래 JSON API를 호출합니다.

- `POST /chat/conversations`: `{ role, team, metadata }`
- `POST /chat/send`: `{ request_id, conversation_id, content, role, team, metadata }`

`role`/`target_role`에는 채널별 대상 역할을, 웹 사용자의 `actor_role`에는 명시적으로 `PM`을 전달합니다.

대화 생성 응답은 `id`, `conversation_id`, 또는 `conversation.id`를 사용할 수 있습니다. 메시지 응답은 `messages[]`와 `proposals[]`를 사용합니다. 채널별 backend 역할은 `PM`, `Bull`, `Bear`, `Risk`, `Research`이며, 화면 팀 라벨은 metadata로 보존됩니다.

API가 꺼져 있거나 CORS/네트워크 오류가 발생하면 화면에 `OFFLINE DEMO`라고 명확히 표시합니다. 이 응답은 실제 분석으로 간주하면 안 됩니다. 제안의 PM 승인 버튼은 인증되지 않은 승인을 방지하기 위해 비활성화되어 있으며, 향후 승인 API 호출 시 반드시 `actor_role=PM`을 명시해야 합니다.

## Vercel Fund API proxy

Vercel 프로젝트의 **Settings → Environment Variables**에서 다음 서버 전용 변수를 설정합니다.

- `FUND_API_URL=https://maemae.onrender.com`
- `FUND_API_TOKEN=Render token`

설정 후 다시 배포하면 브라우저는 동일 origin의 `/api/fund/*`를 호출합니다. Render 주소나 token을 브라우저 Console, `localStorage`, HTML 또는 클라이언트 번들에 설정할 필요가 없습니다. 토큰은 Vercel 서버 측 proxy에서만 Render 요청에 추가되므로 브라우저에 노출되지 않습니다.

로컬 개발의 기본 API는 계속 `http://localhost:8765`이며, 연결되지 않으면 기존 `OFFLINE DEMO` 응답을 유지합니다. 사용자 지정 절대 API URL은 기존 allowlist 검사를 그대로 거치고, allowlist에 없는 origin과 URL 자격 증명, query 또는 hash가 포함된 API base는 계속 차단됩니다.
