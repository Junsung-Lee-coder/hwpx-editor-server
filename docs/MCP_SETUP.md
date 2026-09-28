# HWPX MCP 어댑터

기존 HWPX REST 서버를 MCP 클라이언트에서 호출하는 별도 프로세스입니다. 설치 프로그램이나 기존 서버의 시작 방식을 바꾸지 않습니다. 이 패키지는 로컬 개발·검증용 소스이며 운영 환경의 설치 절차를 대신하지 않습니다. 각 환경에서 실행한 검사 결과를 확인하세요.

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
| `hwpx_close` | `session_id`. 다운로드가 끝난 뒤 문서를 닫고 서버의 관리 복사본·임시 산출물 삭제 |
| `hwpx_cell_margins_get` | `session_id`, `request`. 아래 설명과 같이 표 셀 네 변의 여백을 새로 읽어 반환 |

열기 결과의 `session_id`를 이후 모든 호출의 최상위 필드로 전달하세요. 중첩 `request`에 세션 ID를 넣으면 거부합니다. `document_id`는 이 관리 복사본의 ID이며, 원본 파일의 해시나 전역 문서 번호가 아닙니다. 닫은 뒤에도 명시적인 세션 ID로 상태 기록을 조회할 수 있습니다.

권장 순서는 `open → status → find/where → command 또는 proof → save → 다운로드 → close → status`입니다. `save` 결과의 `download_path`는 인증된 REST 다운로드 경로이며 백엔드의 서버 파일 경로가 아닙니다. 닫기는 관리 복사본과 서버 임시 산출물을 삭제하므로, 닫기 전에 해당 경로로 보관할 파일을 받으세요. 어댑터의 `proof` 폴더에 복사된 페이지 증명은 닫아도 남으므로 필요 없을 때 직접 정리합니다.

관리 세션 판정에는 `metadata.local_cli_v1.opened_via == "local_cli_v1"`가 반드시 필요합니다. `bridge`나 `closed_via` 표시는 열기 출처를 대신하지 않는 추가 수명주기 정보이므로, 해당 표시만 있는 세션은 명령을 받을 수 없습니다. `state`가 `closed` 또는 `closed_cleanup_pending`인 세션은 상태 조회만 허용되며 새 명령·저장·증명·닫기 요청은 `SESSION_CLOSED`로 거부됩니다.

다운로드 경로는 관리 세션 바인딩과 해당 산출물의 크기·SHA-256 기록이 모두 있고, 산출물이 같은 관리 루트의 일반 파일임을 확인한 경우에만 상태에 표시됩니다. 서버의 절대 파일 경로는 응답에 포함하지 않습니다. 전송 직전에 경로를 다시 여는 대신 관리 루트와 파일을 안전하게 확인한 파일 스트림을 먼저 열고 custody hash를 재검증하므로, 검증 뒤 파일을 바꾸거나 경로를 심은 경우에는 전송하지 않습니다. Windows에서는 응답이 끝날 때까지 열린 파일에 대한 교체도 운영체제가 막습니다.

`hwpx_command`는 `context`, `selection_proof`, `readback`, `cell_format_exact`, `command_reconcile`만 받습니다. `cell_format_exact`는 기존 대상 ID, 해시, 페이지, 구역 앵커와 `confirm_layout=true`를 요구하며, 형식 선택자는 `vertical_align`, `cell_margin_mm`, `cell_margin_hu` 중 하나만 지정합니다. 셀 여백 변경은 선언된 단위로 네 변(left/right/top/bottom)을 모두 명시한 단일 네이티브 호출을 사용하고, 요청에서 독립적으로 계산한 네 변의 값과 네이티브 전후 getter가 정확히 일치해야 성공으로 기록됩니다. 유효하지 않거나 신선하지 않은 네이티브 getter, 같은 값을 다시 요청한 no-op, 요청값과 다른 여백은 거부되며, 동작 후 판정 실패는 변경 가능성을 보존합니다. 셀 정렬은 별도의 네이티브 전후 readback을 확보하지 못하면 성공으로 기록하지 않습니다. `pyhwpx_call`, `hwp_action`, 임의 Python·셸·명령 이름은 허용하지 않습니다. 이 어댑터는 기존 비변경용 `safe-schema` 자체를 확장하거나 쓰기 권한으로 해석하지 않습니다.

`hwpx_cell_margins_get`는 셀 서식을 바꾸지 않고 표 셀 네 변의 여백을 읽는 독립 도구입니다. `hwpx_find`가 돌려준 문서 생성 값, `hwpx_command`의 `readback`이 돌려준 컨트롤 목록의 대상 ID·해시·앵커 페이지, 그리고 같은 세션에서 새로 확인한 셀 위치를 `request`에 모두 담아야 합니다. 요청 필드는 `document_id`(세션 ID와 같은 32자리 소문자 16진수), `expected_document_generation`, `target_id`, `expected_hash`, `expected_page`, `expected_cell_page`, `page_from`, `page_to`, `cell_pos`, `cell_addr`, `section_anchor`이며 `max_controls`(기본 100)만 선택 항목입니다. 값은 JSON 배열로 직접 전달합니다. `cell_pos`는 네이티브 위치 3개 값, `cell_addr`는 0부터 시작하는 `[열, 행]`이고 A1이 `[0, 0]`입니다. A1 표기 문자열, 숫자 문자열, 다른 필드, 기본값 대체는 모두 거부합니다. `section_anchor`는 요청한 셀 문단에 문자 그대로 한 번만 나타나는 문장 조각이어야 합니다. 찾기 결과의 `section` 표시(`live-text`)는 메타데이터일 뿐 앵커 문구가 아니므로 그대로 쓰면 거부됩니다.

읽는 순서는 다음과 같습니다. 먼저 세션과 문서, 디스크 관리 복사본의 크기·SHA-256이 요청과 일치하는지 확인하고, 네이티브 텍스트를 새로 읽어 생성 값을 다시 계산해 요청 값과 비교합니다. 컨트롤 목록에서 대상 표를 정확히 하나만 고른 뒤 해시와 앵커 페이지를 맞춰 보고, 캐럿을 요청 위치로 한 번 옮겨 셀 주소, 바로 위 표, 렌더링 페이지, 문단 안의 앵커를 같은 시점에 확인합니다. 그다음 `HAction.GetDefault('TablePropertyDialog')`로 네이티브 값을 새로 읽어 왼쪽·오른쪽·위·아래 순서의 hwpunit 값을 돌려줍니다. mm 표시가 필요하면 `hwpunit × 25.4 ÷ 7200`으로 바꿀 수 있지만, 비교는 반환된 네이티브 값으로 합니다. 값을 읽은 뒤에는 문서·표·셀·페이지·생성 값이 그대로인지 다시 확인하고, 캐럿을 원래 위치로 되돌린 뒤 문서 변경 상태가 읽기 전과 같은지 확인합니다. 이 확인 중 하나라도 어긋나면 실패로 처리하고 값을 돌려주지 않습니다. 이미 수정된 문서에서도 `true → true`로 그대로면 성공하며, 이 도구가 변경을 만들었다는 뜻은 아닙니다.

이 도구는 결과를 캐시하지 않고, 읽기 성공이 저장·다시 열기 뒤의 값이라는 보장도 하지 않습니다. 저장 전 값과 다시 연 뒤 값을 비교하려면 `open → read → save → download → close → reopen → 새 대상으로 read` 순서로 각각 새 요청을 만들어 비교하세요. 시간 초과 뒤에는 이 도구를 다시 실행하지 말고 `hwpx_status`로 명령 상태를 확인한 뒤 `command_reconcile`로 결과를 확인합니다. 셀 안에 앵커 문구가 없는 빈 셀이나, 컨트롤 목록에 잡히지 않는 표, 선택 상태가 남아 있는 문서는 이 도구의 대상이 아닙니다.

페이지는 `1..10000`, DPI는 `72..600`으로 제한됩니다. 한 페이지를 렌더링한 결과는 전체 문서 검토 통과를 뜻하지 않습니다. 최종 문서는 모든 페이지를 Hancom 기반으로 렌더링하고 확인해야 합니다.

## 실패·시간 초과·취소

결과는 `ok`, `operation`, `session_id`, `document_id`, `result`, `error`를 포함합니다. 같은 내용이 MCP의 텍스트와 `structuredContent`에 들어갑니다. 백엔드 거부와 도구 입력 오류는 `isError=true`입니다. JSON-RPC 문법·메서드·요청 메타데이터 오류는 별도의 프로토콜 오류입니다.

`BACKEND_OUTCOME_UNKNOWN`이면 작업을 반복하지 마세요. 클라이언트의 대기가 끝나도 이미 제출된 Hancom 작업은 계속될 수 있습니다. `hwpx_status`에서 처리 중인 명령 ID를 확인한 뒤 같은 `session_id`와 그 명령 ID로 `hwpx_command`의 `command_reconcile`을 호출해 결과를 재확인합니다. 문서 열기 응답을 받기 전에 연결이 끊겨 세션 ID가 없다면 백엔드 운영자가 기존 `/local-cli/status`에서 세션과 처리 기록을 먼저 확인해야 합니다.

명령 시간 초과 뒤에는 해당 세션이 격리됩니다. 대기 중인 명령을 다시 보내거나 `close`를 호출하지 마세요. `command_reconcile`은 원래 Hancom 작업이 끝난 뒤 소유 STA에서 현재 문서 상태를 확인하고, 문서 전체의 변경 상태를 기록한 다음 네이티브 `SaveAs`가 정확히 성공한 경우에만 새 recovery 산출물을 보존합니다. 산출물의 존재·관리 루트·파일 크기·SHA-256을 확인해 커밋한 뒤에만 세션 소유권을 해제합니다.

복구가 아직 진행 중이면 `reconciled=false`, `reconciliation=pending`, `ok=false`가 HTTP 200으로 반환됩니다. 이 응답은 관찰 기록이지 성공이 아니며, 명령을 다시 실행하지도 않았다는 뜻입니다. recovery 산출물을 만들 수 없거나 파일 검증에 실패하면 결과는 알 수 없음으로 남고, 세션의 보류 상태도 유지됩니다. 이 경우 명령을 재실행하지 말고 응답과 백엔드 로그를 보존해 운영자가 판단해야 합니다. 복구가 완료된 뒤 같은 명령 ID로 다시 조회할 수 있으며, 서버는 저장된 의미적 성공·실패 결과를 그대로 반환합니다. 보관할 recovery 파일은 인증된 `/local-cli/session/{session_id}/artifact/recovery` 경로로 다운로드하세요. 상태 조회는 같은 명령이 아직 진행 중인 상태에서도 성공할 수 있으므로, 조회 성공을 편집 성공으로 해석하지 않습니다.

## 네이티브 한계와 관찰 한도

Hancom 객체는 `new=True`로 만들지만, 이 인자가 COM 호출이 실행되기 전에 새 네이티브 프로세스의 독점 소유권을 확보해 주지는 않습니다. 생성자 오류가 기존 Hancom 객체에 연결된 뒤나 새 프로세스가 시작된 뒤에 도착할 수 있고, 핸들을 받지 못한 프로세스에 대해 이 코드가 자동 정리를 확인할 방법은 없습니다. 외부 생성 도우미는 한 번만 시도하고 원래 오류를 그대로 반환하며, 재시도로 실패를 성공처럼 보이게 하지 않습니다. 네이티브 핸들을 받지 못했다면 자동 정리는 `unconfirmed`로 남고, 정리는 반환된 핸들을 닫고 해당하는 COM 해제까지 끝난 뒤에만 `confirmed`가 됩니다.

백그라운드 전용 실행은 지원하지 않습니다. Hancom 창은 로그인된 대화형 데스크톱에서 보이게 유지해야 하며, 최소화되거나 포커스를 잃은 창, 잠긴 데스크톱에서의 동작은 보장하지 않습니다.

결과 재확인의 관찰에는 한도가 있습니다. 시간 초과 뒤에는 복구 요청을 최대 한 번 보낼 수 있고 호출자의 대기는 최대 125초입니다. 그 뒤에는 자동 관찰을 중단하고, 결과가 정해지지 않았으면 세션 바인딩과 로그와 기존 산출물을 보존해 운영자가 판단합니다. 이 한도는 네이티브 프로세스가 끝났다는 보장이 아니며, 결과가 남아 있는 동안 정리는 확인되지 않은 상태로 유지됩니다.

연결 취소는 롤백이 아닙니다. 어댑터는 변경 명령을 자동 재전송하지 않습니다. 백엔드 대기 제한은 `HWPX_MCP_TIMEOUT`으로 설정하며 기본 120초, 허용 범위는 0.1..600초입니다. 페이지 렌더링은 별도로 60초를 제한합니다. 취소된 렌더링의 임시 파일은 잠시 남을 수 있으며 완료된 `manifest.json`이 없는 파일을 증명으로 사용하면 안 됩니다. 복구 전에는 원본 문서나 서버 관리 복사본을 직접 삭제하지 마세요.

## 종료와 보안

시험 문서를 명시적으로 닫은 뒤 이 터미널에서 만든 어댑터만 종료합니다.

```powershell
Stop-Process -Id $mcpProcess.Id
Remove-Item Env:HWPX_MCP_TOKEN
```

기존 백엔드, Hancom 세션, 다른 Python 프로세스를 일괄 종료하지 마세요. 어댑터 종료만으로 백엔드 문서가 닫히지는 않습니다.

`Host`와 `Origin`은 설정한 포트의 `127.0.0.1` 또는 `localhost`만 허용합니다. CORS는 열지 않습니다. 공개 주소 바인딩은 제공하지 않습니다. 백엔드에 `HWP_API_TOKEN`을 설정했다면 같은 값을 `HWPX_MCP_BACKEND_TOKEN`에 설정하며, MCP 토큰과 다른 값을 사용합니다. 비루프백 백엔드에는 HTTPS가 필요하지만 이번 검증은 격리된 로컬 백엔드에 한정됩니다.

## 검증 명령

```powershell
.mcp-venv\Scripts\python.exe -m unittest discover -s mcp_tests -v
```

이 테스트는 실제 소켓과 공식 SDK 클라이언트를 사용하지만 REST 백엔드는 시험용 대체 서버입니다. Hancom 실행 결과와 혼동하지 마세요. 전체 기존 검사와 네이티브 문서 실행 결과는 각각 실행 환경에서 별도로 확인합니다. 설치와 공개 배포는 이 문서의 실행 범위가 아닙니다.
