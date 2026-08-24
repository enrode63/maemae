# Demo Trading Backend

The localhost-safe runtime and explicitly gated Render public demo mode are documented
in [`LOCAL_RUNTIME.md`](LOCAL_RUNTIME.md) and [`RENDER_DEPLOYMENT.md`](RENDER_DEPLOYMENT.md).

외부 API나 자격 증명 없이 `JSON 신호 → 리스크 검사 → PM 승인 → 가상 체결 → 포지션/손익 → append-only 이벤트 로그`를 실행합니다. 유일한 실행 모드는 `simulation`이며 실거래 기능은 없습니다. 금액은 입력 통화 단위, 수량은 주, 수수료율은 소수 비율, 슬리피지는 bp입니다.

## 실행

Python 3.10+에서 추가 런타임 의존성 없이 실행됩니다.

```powershell
cd backend
python -m demo_trading --signals data/signals.json --prices data/prices.csv --output output
```

소스 체크아웃에서 설치 없이 실행하려면:

```powershell
$env:PYTHONPATH = "src"
python -m demo_trading --signals data/signals.json --prices data/prices.csv --output output
```

결과는 `output/events.jsonl`(순서 번호가 있는 append-only 의사결정 이력)과 `output/summary.json`에 기록됩니다. 동일 입력과 옵션은 동일한 `run_id`, 이벤트, 요약을 만듭니다. CSV에서 같은 종목이 여러 번 나오면 마지막 행을 결정론적 스냅샷 가격으로 사용합니다.

## 테스트

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

`pytest`가 설치된 환경에서는 `pytest` 명령도 그대로 사용할 수 있습니다.
