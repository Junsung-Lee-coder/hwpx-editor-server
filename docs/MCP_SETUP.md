# HWPX MCP 어댑터

기존 HWPX REST 서버를 MCP 클라이언트에서 호출하는 별도 프로세스입니다. 설치 프로그램이나 기존 서버의 시작 방식을 바꾸지 않습니다. 이 패키지는 로컬 개발·검증용 소스이며, 운영 배포나 BOOK 실행 승인을 뜻하지 않습니다. 검증 결과는 함께 제공되는 증거 목록을 확인하세요.

## 연결 조건

- MCP `2026-07-28`, 공식 Python SDK `mcp==2.1.1`
- Streamable HTTP, 기본 주소 `http://127.0.0.1:18766/mcp`
- stdio, 구형 `initialize` 연결, HTTP+SSE 구형 전송, resources, prompts는 지원하지 않습니다.
- 같은 컴퓨터에서 실행하는 신뢰할 수 있는 클라이언트만 사용합니다. 별도 토큰을 매 요청에 전달해야 합니다. 이것은 로컬 전용 사전 공유 토큰 방식이며, OAuth 자동 로그인 기능이 아닙니다.
- 문서 작업에는 Hancom이 설치된 Windows와 준비 상태가 정상인 기존 HWPX 백엔드가 필요합니다. PDF 페이지 증명에는 기존 Poppler 설치도 필요합니다.
- 백엔드는 활성 문서 하나만 관리합니다. 여러 MCP 클라이언트가 접속해도 동시에 여러 문서를 여는 기능이 생기지는 않습니다.

## 시작하기

소스 폴더에서 실행합니다. 먼저 폐기 가능한 전용 백엔드의 `/health`와 `/runtime-readiness`를 확인하세요. 아래의 `18767`은 예시 전용 백엔드 포트입니다. 어댑터가 백엔드를 만들거나 실행해 주지는 않습니다. 운영 중인 문서 세션에 연결하지 마세요.

Windows PowerShell 예시:

```powershell
py -3.14 -m venv .mcp-venv
.mcp-venv\Scripts\python.exe -m pip install --require-hashes -r requirements-mcp.lock
$env:HWPX_MCP_TOKEN = (& .mcp-venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))")
$env:HWPX_MCP_SOURCE_ROOT = 'C:\HWPX-MCP-Example\input'
$env:HWPX_MCP_ARTIFACT_ROOT = 'C:\HWPX-MCP-Example\proof'
$env:HWPX_MCP_BACKEND = 'http://127.0.0.1:18767'
$env:HWPX_MCP_PORT = '18766'
$mcpProcess = Start-Process -FilePath '.mcp-venv\Scripts\python.exe' -ArgumentList '-m','hwpx_mcp' -NoNewWindow -PassThru
.mcp-venv\Scripts\python.exe -m hwpx_mcp.client --url http://127.0.0.1:18766/mcp
.mcp-venv\Scripts\python.exe -m hwpx_mcp.client --url http://127.0.0.1:18766/mcp --tool hwpx_health
```

`input` 폴더는 미리 만들고 시험용 HWP/HWPX 복사본만 넣으세요. `source_path`는 클라이언트가 아닌 어댑터 컴퓨터의 절대 경로입니다. 입력은 25 MiB 이하로 제한됩니다. 토큰을 화면에 출력하거나 설정 파일·저장소에 기록하지 마세요. 다른 터미널의 클라이언트에는 안전한 방법으로 같은 토큰을 전달해야 합니다.

일반 MCP 클라이언트에는 전송 종류를 Streamable HTTP로, URL을 위 주소로, `Authorization` 헤더를 `Bearer <전용 토큰>`으로 지정합니다. MCP `2026-07-28`의 요청별 메타데이터와 `Mcp-Method`·`Mcp-Name` 헤더를 지원하는 클라이언트가 필요합니다. 제품마다 설정 형식이 다르므로 확인하지 않은 설정 JSON을 공통 형식으로 제공하지 않습니다. 함께 제공한 `hwpx_mcp.client`가 공식 SDK를 사용하는 연결 확인용 클라이언트입니다.

## 도구와 문서 수명

도구 목록의 JSON Schema가 정확한 입력 형식입니다. 모든 최상위 입력과 중첩 요청은 추가 필드를 거부합니다.

| 도구 | 입력과 역할 |
|---|---|
| `hwpx_health` | 빈 객체. 백엔드 상태와 대기열 확인 |
| `hwpx_open` | `request.source_path`, 선택 항목 `request.session_label`. 원본을 업로드해 서버 관리 복사본 생성 |
| `hwpx_status` | `session_id`. 문서 상태와 처리 결과 재확인 정보 조회 |
| `hwpx_find` | `session_id`, `request.query`, 선택 항목 `around`, `with_page`, `proof_match` |
| `hwpx_where` | `session_id`. 현재 Hancom 위치 확인 |
| `hwpx_command` | `session_id`, `request.op`. 아래 제한된 명령만 허용 |
| `hwpx_proof` | `session_id`, `request.kind`: `frame` 또는 `page`. 페이지 방식은 `page`와 선택 항목 `dpi` 사용 |
| `hwpx_save` | `session_id`. 관리 복사본 저장 |
| `hwpx_close` | `session_id`. 문서를 닫고 서버의 관리 복사본·임시 산출물 삭제 |

열기 결과의 `session_id`를 이후 모든 호출의 최상위 필드로 전달하세요. 중첩 `request`에 세션 ID를 넣으면 거부합니다. `document_id`는 이 관리 복사본의 ID이며, 원본 파일의 해시나 전역 문서 번호가 아닙니다. 닫은 뒤에도 명시적인 세션 ID로 상태 기록을 조회할 수 있습니다.

권장 순서는 `open → status → find/where → command 또는 proof → save → 다운로드 → close → status`입니다. `save` 결과의 `download_path`는 백엔드 기준 경로입니다. 닫기 전에 기존 REST 다운로드 기능으로 보관할 파일을 받으세요. 어댑터의 `proof` 폴더에 복사된 페이지 증명은 닫아도 남으므로 필요 없을 때 직접 정리합니다.

`hwpx_command`는 `context`, `selection_proof`, `readback`, `cell_format_exact`, `command_reconcile`만 받습니다. 셀 정렬 변경은 기존 대상 ID, 해시, 페이지, 구역 앵커와 `confirm_layout=true`를 요구하고 백엔드의 원본 일치·증명 검사를 그대로 통과해야 합니다. `pyhwpx_call`, `hwp_action`, 임의 Python·셸·명령 이름은 허용하지 않습니다. 이 어댑터는 기존 비변경용 `safe-schema` 자체를 확장하거나 쓰기 권한으로 해석하지 않습니다.

페이지는 `1..10000`, DPI는 `72..600`으로 제한됩니다. 한 페이지를 렌더링한 결과는 전체 문서 검토 통과를 뜻하지 않습니다. 최종 문서는 모든 페이지를 Hancom 기반으로 렌더링하고 확인해야 합니다.

## 실패·시간 초과·취소

결과는 `ok`, `operation`, `session_id`, `document_id`, `result`, `error`를 포함합니다. 같은 내용이 MCP의 텍스트와 `structuredContent`에 들어갑니다. 백엔드 거부와 도구 입력 오류는 `isError=true`입니다. JSON-RPC 문법·메서드·요청 메타데이터 오류는 별도의 프로토콜 오류입니다.

`BACKEND_OUTCOME_UNKNOWN`이면 작업을 반복하지 마세요. 클라이언트의 대기가 끝나도 이미 제출된 Hancom 작업은 계속될 수 있습니다. `hwpx_status`에서 처리 중인 명령 ID를 확인한 뒤 `hwpx_command`의 `command_reconcile`로 결과를 재확인합니다. 문서 열기 응답을 받기 전에 연결이 끊겨 세션 ID가 없다면 백엔드 운영자가 기존 `/local-cli/status`에서 세션과 처리 기록을 먼저 확인해야 합니다.

연결 취소는 롤백이 아닙니다. 어댑터는 변경 명령을 자동 재전송하지 않습니다. 백엔드 대기 제한은 `HWPX_MCP_TIMEOUT`으로 설정하며 기본 120초, 허용 범위는 0.1..600초입니다. 페이지 렌더링은 별도로 60초를 제한합니다. 취소된 렌더링의 임시 파일은 잠시 남을 수 있으며 완료된 `manifest.json`이 없는 파일을 증명으로 사용하면 안 됩니다.

## 종료와 보안

시험 문서를 명시적으로 닫은 뒤 이 터미널에서 만든 어댑터만 종료합니다.

```powershell
Stop-Process -Id $mcpProcess.Id
Remove-Item Env:HWPX_MCP_TOKEN
```

기존 백엔드, Hancom 세션, 다른 Python 프로세스를 일괄 종료하지 마세요. 어댑터 종료만으로 백엔드 문서가 닫히지는 않습니다.

`Host`와 `Origin`은 설정한 포트의 `127.0.0.1` 또는 `localhost`만 허용합니다. CORS는 열지 않습니다. 공개 주소 바인딩은 제공하지 않습니다. 백엔드가 별도 인증을 요구하면 `HWPX_MCP_BACKEND_TOKEN`을 설정하며 MCP 토큰과 다른 값을 사용합니다. 비루프백 백엔드에는 HTTPS가 필요하지만 이번 검증은 격리된 로컬 백엔드에 한정됩니다.

## 검증 명령

```powershell
.mcp-venv\Scripts\python.exe -m unittest discover -s mcp_tests -v
```

이 테스트는 실제 소켓과 공식 SDK 클라이언트를 사용하지만 REST 백엔드는 시험용 대체 서버입니다. Hancom 실행 증거와 혼동하지 마세요. 전체 기존 검사와 네이티브 문서 실행 결과는 패키지에 동봉된 검증 목록에서 별도로 구분합니다. 설치 프로그램, BOOK, 공개 저장소 배포는 이 문서의 실행 범위가 아닙니다.
