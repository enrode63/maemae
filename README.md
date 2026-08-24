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

## Fund Team 로컬 API 연결

채팅은 기본적으로 `http://localhost:8765`의 Fund Team API를 사용합니다. API 주소는 다음 우선순위로 설정됩니다.

1. 페이지 로드 전에 지정한 `window.EDGE_FUND_API_BASE`
2. URL 쿼리 `?apiBase=http://localhost:8765`
3. `localStorage`의 `edge-fund-api-base`
4. 기본값 `http://localhost:8765`

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
