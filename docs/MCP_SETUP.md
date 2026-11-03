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

소스 폴더에서 실행합니다. 먼저 폐기 가능한 전용 백엔드의 `/health`와 `/runtime-readiness`를 확인하세요. 문서를 열려면 같은 대화형 데스크톱에서 전용 `python -m app.worker`가 함께 실행되어 준비 상태(`ready=true`)를 보고해야 합니다. 아래의 `18767`은 예시 전용 백엔드 포트입니다. 어댑터가 백엔드를 만들거나 실행해 주지는 않습니다. 운영 중인 문서 세션에 연결하지 마세요.

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

`input` 폴더는 미리 만들고 시험용 HWP/HWPX 복사본만 넣으세요. `source_path`는 클라이언트가 아닌 어댑터 컴퓨터의 절대 경로입니다. 입력은 25 MiB 이하로 제한됩니다(자세한 내용은 아래 "크기 제한"). 토큰을 화면에 출력하거나 설정 파일·저장소에 기록하지 마세요. 다른 터미널의 클라이언트에는 안전한 방법으로 같은 토큰을 전달해야 합니다.

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
| `hwpx_command` | `session_id`, `request.op`. 백엔드 명령 하나. op마다 닫힌 스키마가 있으며 목록은 아래 "백엔드 기능 대응" 참고 |
| `hwpx_bundle` | `session_id`, `request.steps`(1~20개). `hwpx_command`와 같은 단계 스키마로 백엔드 명령 묶음 하나를 순서대로 실행 |
| `hwpx_image` | `session_id`, `request.action`: `insert`, `insert_at_anchor`, `figure_section`. 입력·산출물 폴더의 그림을 관리 문서에 넣음 |
| `hwpx_job` | `request.action`. 파일 복사본에서 도는 백엔드 대기열 작업(변환, 편집, 작성, 자리표시자)과 상태·메타데이터·결과 회수 |
| `hwpx_artifact` | `session_id`, `request.kind`: `screenshot`, `recovery`. 백엔드가 보관 중인 최신 화면 캡처나 복구 파일을 산출물 폴더로 복사 |
| `hwpx_capabilities` | 빈 객체. 모든 백엔드 명령·경로·대기열 op의 MCP 경로 또는 제외 이유와 백엔드의 현재 기능·준비 상태 |
| `hwpx_proof` | `session_id`, `request.kind`: `frame`, `page`, `review`. 페이지 방식은 `page`와 선택 항목 `dpi` 사용. `frame`은 캡처한 PNG를 산출물 폴더로 복사해 `frame_proof`에 경로와 SHA-256을 줌. `review`는 아래 "결과 확인"을 참고 |
| `hwpx_save` | `session_id`. 관리 복사본을 저장하고 저장된 파일을 산출물 폴더로 복사. 결과의 `saved_output`에 경로, SHA-256, 열었던 파일과의 비교가 들어 있음. 복사에 실패하면 저장은 성공으로 두고 `saved_output.retrieved=false`와 이유, 재시도 방법을 알려 줌 |
| `hwpx_close` | `session_id`. 저장까지 끝난 깨끗한 문서를 닫고 Hancom 프로세스 종료를 확인한 뒤 서버 관리 파일을 삭제. 그 전에 저장된 내용이 산출물 폴더에 온전히 있는지 확인하고, 없으면 복사한 뒤 닫음. 복사할 수 없으면 `SAVED_OUTPUT_NOT_RETRIEVED`로, 저장되지 않은 변경이 있으면 `SAVE_REQUIRED`로 거부하며 아무것도 지우지 않음. 결과가 불명인 세션도 거부하고 그대로 유지 |
| `hwpx_cell_margins_get` | `session_id`, `request`. 아래 설명과 같이 표 셀 네 변의 여백을 새로 읽어 반환 |

열기 결과의 `session_id`를 이후 모든 호출의 최상위 필드로 전달하세요. 중첩 `request`에 세션 ID를 넣으면 거부합니다. `document_id`는 이 관리 복사본의 ID이며, 원본 파일의 해시나 전역 문서 번호가 아닙니다. 닫은 뒤에도 명시적인 세션 ID로 상태 기록을 조회할 수 있습니다.

권장 순서는 `open → proof(review) → command → proof(review) → save → close → open(saved_output.path) → proof(review)`입니다. 저장된 파일은 끝까지 받아 패키지를 확인한 뒤에만 `saved/` 폴더에 원자적으로 게시되므로, 부분 파일이 `.hwpx`로 남지 않습니다. 복사가 실패하면 `hwpx_save`를 다시 호출해 재시도하세요. 게시된 파일은 닫은 뒤에도 그 경로로 다시 열어 저장 결과를 확인할 수 있습니다(`source_path`는 입력 폴더 또는 산출물 폴더 아래만 허용). `save` 결과의 `download_path`는 인증된 REST 다운로드 경로이며 백엔드의 서버 파일 경로가 아닙니다. 닫기 거부는 관리 복사본과 서버 임시 산출물을 삭제하지 않습니다. 어댑터의 산출물 폴더에 복사된 증명과 저장 파일은 닫아도 남으므로 필요 없을 때 직접 정리합니다.

## 결과 확인

`hwpx_proof`의 `{"kind": "review"}`는 문서를 바꾸거나 저장하지 않고 다음을 한 번에 돌려줍니다. 선택 항목은 `dpi`(72~300, 기본 100), `max_pages`(기본 30), `inline_images`(MCP 응답에 바로 넣을 미리보기 수, 기본 3), `compare_to`(비교 기준, 예: `<session_id>/review-1`)입니다.

- `review_status`: 먼저 읽을 값입니다. `complete`(전체 확인), `partial`(일부만 확인), `unverified`(확인하지 못함), `mismatch`(확인한 범위에서 불일치)로 나뉘며, `coverage`에 문단·쪽·PDF 글자별 범위가 있습니다. 요약 첫 줄도 같은 내용입니다. 인쇄된 번호를 대조하는 것은 본문의 개요 번호뿐입니다. 자동 번호, 글머리표, 표 셀 안 개요 번호가 있는 문서는 `coverage.list_numbering`에 종류별 개수가 들어가고 `partial`이 되며, 요약 첫 줄과 `summary.md`에 해당 문단을 밝힙니다. `coverage.list_numbering.state`는 문서 전체를 읽어 이런 문단이 없을 때만 `none`이고, 있으면 `unverified`, 문단 목록이 잘렸으면 읽은 부분의 개수와 함께 `partial`, 구조를 읽지 못했으면 개수 없이 `unverified`입니다. 번호 없는 일반 문단만 있는 문서와 그림이 없는 문서는 다른 범위가 모두 확인되면 `complete`입니다.
- `summary_text`: 확인 범위, 쪽 수, 번호 일치 여부, 쪽 번호, 비교 결과, 저장 여부를 담은 짧은 요약. MCP 응답의 두 번째 텍스트로도 들어 있고, 그 뒤에 쪽 미리보기 이미지가 이어집니다.
- `numbering`: 번호 문단별 수준, Hancom이 만든 PDF에 실제 찍힌 번호(`rendered_label`), 수준 순서로 계산한 기대 번호(`expected_label`), 상태(`status`: `match`, `mismatch`, `outside_rendered_pages`, `unverified`). `numbering_all_match`는 문서 전체를 확인해 모두 일치할 때만 `true`, 불일치가 있으면 `false`, 그 밖에는 `null`입니다.
- `page_numbers`: 쪽 번호 컨트롤의 위치와 시작 번호, 쪽별 인쇄 여부. `all_found`는 모든 쪽을 읽은 경우에만 `true`이고, 일부만 읽었거나 PDF 글자를 읽지 못하면 `null`입니다.
- `comparison`: 기준 시점 대비 추가·삭제·번호 변경·서식 변경 문단과 머리말·꼬리말, 스타일, 그림 데이터, 번호 정의, 표·그림 개수, 표 셀 내용. 모든 문단의 번호 종류와 수준을 비교합니다. 자동 번호, 글머리표, 표 셀 안 개요 번호는 인쇄된 번호를 읽지 않으므로 종류, 수준, 번호 정의가 같으면 `numbers_not_compared`로 세고 `state`는 `partial`입니다. 증거는 `unchanged`, `changed`, `unproven`으로 구분하고, `state`는 비교 범위를 알려 줍니다. 문서에 그림 데이터 목록이 정말 없다는 것을 구조로 확인한 경우 값은 `absent`이고, 읽지 못한 경우에만 `unproven`이 됩니다. `compare_to`를 주지 않으면 그 세션의 첫 확인이 기준입니다.

문서가 너무 커서 구조를 읽을 수 없으면(HWPML2X 3,200만 자 초과 등) `review_status`가 `unverified`가 되고 PDF와 미리보기는 그대로 돌려줍니다. 이 실패는 문서를 변경됨으로 표시하거나 세션을 보류시키지 않습니다.
- `pdf`, `pages`, `summary_path`, `json_path`, `saved_output`: 산출물 폴더의 PDF, 쪽별 PNG, 읽기 쉬운 `summary.md`, 전체 기계 판독용 `review.json`, 마지막 저장 파일.

## 쪽 번호

`page_number_apply`의 `start_number`는 생략하면 1입니다. Hancom 13.0.0.3457은 시작 번호 1에 새 번호 조판을 만들지 않으므로, 첫 쪽이 1로 인쇄되는 근거는 문서 자체입니다. 그래서 변경 전에 문서의 시작 값을 확인합니다.

| 문서 | 요청 | 처리 |
|---|---|---|
| 새 번호 조판이 없고 구역 시작이 0 또는 1 | 1 또는 생략 | 쪽 번호를 넣습니다. `readback.new_number_source`는 `implicit_first_page` |
| 새 번호 조판이 없음 | 2 이상 | 쪽 번호와 새 번호 조판을 넣습니다. `new_number_source`는 `hwpml2x_page_restart` |
| 구역 시작이 2 이상이고 새 번호 조판이 없음 | 1 또는 생략 | 변경 전에 `existing_section_page_start`로 거부합니다. 넣어도 그 구역의 시작 번호가 인쇄되기 때문입니다. 시작 번호를 명시하면 됩니다 |
| 첫 문단에 요청과 같은 번호의 새 번호 조판이 있음 | 같은 번호 | 쪽 번호를 넣고 그 조판은 그대로 둡니다 |
| 그 밖의 새 번호 조판이 있음(뒤쪽 문단의 조판 포함) | 모두 | 변경 전에 `existing_page_restart`로 거부합니다. Hancom이 첫 쪽의 조판을 요청 값으로 고쳐 쓰고 되돌리기로 돌아오지 않는데, 이 확인은 조판이 첫 쪽에 있는지 가리지 않기 때문입니다 |
| 쪽 번호가 이미 있음 | 모두 | 변경 전에 `existing_page_number`로 거부합니다 |

거부되면 세션은 보류되지 않고 저장하지 않은 이전 변경도 그대로 남습니다. 실제 인쇄된 번호는 `hwpx_proof`의 `review`가 PDF에서 확인합니다.

공개 REST `/local-cli/command-bundle`과 CLI `bundle-run`의 한 요청 안에서도 같습니다. 앞 단계의 편집이 성공하고 뒤 단계가 변경 전의 정상 의미상 거부(예: `table_read`의 `table_not_found`)이면, 앞 단계와 전체 `dirty`는 유지하고 거부된 단계만 `mutation_may_have_persisted=false` 및 정확한 `refusal_code`로 반환합니다. 뒤 단계는 실행하지 않으며 이후 편집·저장·저장본 재열기가 가능합니다. 이 구분은 이미 성공한 편집을 취소하거나 미저장 문서 닫기를 허용하지 않습니다. 현재 단계가 mutator를 발행했거나 기술적 읽기/반환/보고 결과가 불명확한 경우의 보류와 저장 보호는 그대로입니다. MCP `hwpx_command`는 한 단계씩 보내므로 같은 요청의 이 경계를 검증하려면 REST/CLI 다단계 묶음을 사용해야 합니다.

## 보호된 편집 묶음의 보조 관찰

표 읽기·편집·생성, 정확한 글자·문단 서식, 문단 삽입·삭제와 번호 명령이 있는 묶음은 보조 커서/문서 snapshot을 추가로 읽지 않습니다. 응답의 `before`/`after`에 `observation_status=omitted_guarded_envelope`가 표시되며, 생략된 문서 변경 상태는 `null`이지 깨끗하다는 증거가 아닙니다. 명령 자체의 대상·변경 전후·문서 보존 증거는 그대로 제공됩니다. 이전 저장 전 변경도 유지합니다.

이 묶음 안의 명시적 `where`는 엄격하게 읽은 커서·선택만 `partial_guarded_cursor`로 반환하며, 서식·주변 글·문서·쪽·셀 관찰의 생략을 명시합니다. 독립 `where`의 기존 상세 관찰은 유지됩니다. 기술 실패 뒤에는 다음 묶음 명령이나 진단용 SDK 읽기를 실행하지 않고 실제 편집 세션을 보류합니다.

## 번호 문단 편집

번호 체계 `1.` → `가.` → `1)` → `가)`는 `official_numbering_apply`로 지정합니다. 번호는 Hancom이 매기므로 문단을 넣거나 지우면 뒤따르는 번호가 자동으로 다시 매겨집니다.

- 같은 수준 이어 쓰기와 삽입: `{"op": "paragraph_insert_exact", "match": "기준 문단의 고유한 문구", "expected_page": 1, "text": "새 문단", "position": "after", "confirm_layout": true}`. `position`은 `after`(기본) 또는 `before`이며 새 문단은 기준 문단의 번호 수준을 물려받습니다. 첫 본문 문단 앞, 쪽·단 나눔으로 시작하는 문단 앞, 같은 글이 이미 있는 경우는 변경 전에 거부합니다.
- 삭제: `{"op": "paragraph_remove_exact", "match": "지울 문단의 고유한 문구", "expected_page": 1, "expected_text": "그 문단의 전체 글", "confirm_remove": true}`. `match`는 위치만 찾고, `expected_text`가 문단 전체 글과 정확히 같아야 지웁니다(`target_text_mismatch`). 문단 안의 탭은 `\t`, 줄 바꿈은 `\n`, 묶음 빈칸은 U+00A0, 고정폭 빈칸은 U+2007로 적어야 같은 글로 봅니다. 공백으로 바꾸거나 일부만 적은 글은 거부합니다. `hwpx_proof` 확인의 `review.json`에 있는 문단 `text`가 같은 표기를 씁니다(문단당 300자까지). 이 네 가지 밖의 요소가 글 안에 있는 문단은 개체가 있는 문단과 같이 거부합니다. 그림·표 같은 개체가 들어 있는 문단, 표 셀 안 문단, 구역의 마지막 문단, 쪽·단 나눔이 걸린 문단 또는 그 바로 앞 문단은 변경 전에 거부하며 `refusal_code`로 이유를 알려 줍니다.
- 되돌리기와 다시 실행: `{"op": "undo"}`, `{"op": "redo"}`. 이 세션에서 추적한 편집 범위 안에서만 동작합니다. 되돌릴 편집이 없으면 네이티브 호출 없이 `no_undo_available`·`no_redo_available`로 거부하고 문서는 변경됨으로 표시되지 않습니다. 너무 커서 호출 전후를 비교할 수 없는 문서도 네이티브 호출 없이 `undo_evidence_unavailable`·`redo_evidence_unavailable`로 거부하며, 저장되지 않은 이전 변경은 그대로 남습니다. 결과의 `confirmation`에 호출 전후 문서를 비교한 실제 변화가 들어 있습니다. 개요 번호 정의를 바꾼 뒤 수준까지 따로 지정한 경우(네이티브 동작 두 번)는 Undo 한 번으로 되돌릴 수 없으므로 `undo_not_counted`로 거부합니다.

네이티브 호출 전에 문서를 증거로 쓸 수 없다고 판단한 요청(너무 큰 문서, 불완전한 구조, 대상 없음, 여러 문단에 일치하는 `match`(`target_ambiguous`), 표 셀 안 문단(`target_not_in_body`), 컨트롤 수 한계(`control_enumeration_unavailable`), 공식과 다른 개요 번호 정의를 바꿔도 안전하다고 증명할 수 없는 문서(자동 번호 문단이 있거나 다른 구역과 정의가 얽힌 경우 `outline_usage_unprovable`, 같은 구역에 다른 개요 문단이 있으면 `foreign_outline_definition`) 등)은 캐럿과 문서가 그대로임을 확인한 뒤 `refusal_code`와 함께 거부되며 변경 가능으로 표시하거나 세션을 보류시키지 않습니다. 같은 세션에서 다른 대상으로 계속 작업하거나 닫을 수 있고, 그 전에 있던 저장되지 않은 변경은 그대로 남습니다. 네이티브 호출이 시작된 뒤 결과를 알 수 없으면 지금처럼 보류합니다. 네이티브 읽기 자체가 예외로 실패한 경우에도 거부로 바꾸지 않고, 다음 SDK 호출 없이 세션을 보류하되 편집이 있었던 것으로 표시하지 않습니다. 닫을 때 문서 변경 상태를 읽지 못한 경우도 같습니다. 보류된 세션의 오류는 `details.session_held=true`와 `reason=native_custody_held`로 알려 주며, 저장을 요구하는 `SAVE_REQUIRED`와 복사 실패인 `SAVED_OUTPUT_NOT_RETRIEVED`는 보류가 아니면 `session_held=false`이고 저장하거나 복사를 다시 시도하면 닫을 수 있습니다.

## 글자·문단 서식

GUI에서 범위를 직접 선택해 둘 필요가 없습니다. 명령마다 글자를 문서에서 다시 찾아 선택하고, 선택한 블록의 내용이 요청한 글자와 정확히 같은지 확인한 뒤 네이티브 동작 한 번을 실행합니다. 그래서 서식 → 확인(`review`, `context`, `find`, `where`, `readback`) → 서식을 같은 글자에 이어서 실행해도 그 글자에만 적용됩니다. 실행 후 문서 전체의 글자와 문단을 요청과 비교하며, 요청하지 않은 속성은 섞인 범위의 각 부분에서 그대로 유지됩니다.

대상은 두 명령이 같습니다. `expected_page`(대상이 있는 쪽)와 `confirm_layout=true`는 항상 필요합니다.

- `match`: 문서에서 한 곳에만 있는 글자. 앞뒤 공백을 포함해 그대로 비교합니다. 탭은 `\t`, 줄 바꿈은 `\n`, 묶음 빈칸은 U+00A0, 고정폭 빈칸은 U+2007로 적습니다.
- `through_match`: 선택 항목. 범위가 끝나는 곳의 글자로, 이것도 한 곳에만 있어야 합니다. 여러 문단에 걸친 범위는 본문에서만 쓸 수 있습니다.
- `paragraph_index`와 `expected_text`: `match` 대신 문단 하나를 지정합니다. 번호는 `review.json`의 문단 `index`이고 `expected_text`는 그 문단의 전체 글입니다(빈 문단은 빈 문자열). 전체 글이 같은 문단이 여러 개여도 본문 문단은 구별됩니다. `review.json`의 `text`는 문단당 300자까지이므로 더 긴 문단은 `match`를 쓰세요.

글자 서식: `{"op": "character_format_exact", "match": "고유한 문구", "expected_page": 1, "font_family": "맑은 고딕", "size_pt": 13.5, "bold": true, "italic": false, "underline": true, "text_color": "#C03020", "confirm_layout": true}`. 항목은 하나 이상 지정하며 지정한 항목만 바뀝니다. `size_pt`는 포인트(0.1~4096, 0.1 단위), `text_color`는 `#RRGGBB`입니다. `font_family`는 문서에 이미 있는 TTF 글꼴이나 이 Windows 세션의 글꼴 목록에 있는 글꼴만 받습니다. Hancom은 없는 글꼴 이름도 받아 렌더링할 때 다른 글꼴로 바꾸므로 변경 전에 `font_not_available`로 거부합니다. 글꼴 목록을 읽지 못하면 `font_availability_unknown`입니다. Hancom 내장 HFT 글꼴은 같은 이름이 이 목록에 없으면 문서에서 쓰고 있어도 거부됩니다. `underline=true`는 아래쪽 밑줄이며, 위쪽 밑줄이 있는 범위에 주면 아래쪽 밑줄로 바뀝니다.

문단 서식: `{"op": "paragraph_format_exact", "match": "문단 안의 고유한 문구", "expected_page": 1, "align": "center", "left_indent_pt": 20, "right_indent_pt": 10, "first_line_indent_pt": 5, "line_spacing_type": "percent", "line_spacing_value": 180, "confirm_layout": true}`. 

- `align`: `left`, `center`, `right`, `justify`
- `left_indent_pt`, `right_indent_pt`: 왼쪽·오른쪽 여백(포인트, 0~570.2, 0.1 단위)
- `first_line_indent_pt`: 포인트(±570.2, 0.1 단위). 양수는 첫 줄을 그만큼 들여 쓰고, 음수(내어쓰기)는 첫 줄을 왼쪽 여백에 두고 다음 줄부터 그만큼 들여 씁니다.
- `line_spacing_type`과 `line_spacing_value`는 함께 지정합니다. `percent`는 글자에 따른 비율(1~500의 정수), `fixed`는 고정 값, `at_least`는 최소, `between_lines`는 여백만 지정이며 이 셋의 값은 포인트(0.1 단위)입니다.

받는 범위의 양 끝 값(크기 0.1·4096pt, 여백·첫 줄 570.2pt, 줄 간격 1·500%와 0.1·841.8pt, 여백만 지정 0pt)은 Hancom 2024 13.0.0.3457에서 값이 그대로 저장되고 다시 열어도 유지되는 것을 확인했습니다. 페이지보다 큰 값도 그대로 저장되며 쪽 수가 늘어납니다.

**번호와 글머리표의 모양.** 번호 문단의 인쇄된 번호나 글머리표는 번호 정의가 모양을 지정하지 않으면 문단 마지막 글자 묶음의 글자 모양을 따릅니다(자동 번호, 개요 번호, 글머리표에서 측정). 그래서 범위가 문단 끝에 닿으면 번호 모양도 함께 바뀌려 합니다. 범위가 그 전에 끝나면 영향이 없습니다.

- 기본값 `number_mark: "preserve"`: 번호 모양을 그대로 둡니다. 범위가 문단 끝에 닿으면 문단 끝 캐럿에서 네이티브 동작을 한 번 더 실행해 이전 값을 돌려놓습니다. 글자는 바뀌지 않으며 결과의 `native_actions`에 `Execute:CharShape:paragraph-end`가 추가됩니다. 번호 정의가 모양을 지정한 문단은 영향이 없어 추가 동작이 없습니다.
- `number_mark: "follow"`: 번호 모양도 같은 변경을 따르게 합니다. Hancom이 물려주는 그대로이며, 번호 모양만 따로 지정하는 기능은 아닙니다.
- 여러 문단에 걸친 범위가 번호 문단의 끝에 닿으면 기본값에서는 변경 전에 `target_changes_number_mark`로 거부합니다. 범위의 마지막 문단 끝에서 정확히 끝나는 경우도 같습니다. 그 끝 앞에서 범위를 마치거나, 문단마다 따로 지정하거나, `follow`를 주세요. 번호가 쓰는 글꼴이 TTF가 아니어서 돌려놓을 수 없을 때도 같은 코드로 거부합니다.
- 결과의 `result.number_marks`에 번호 문단별 `before`, `after`, `changed`, 모양의 출처 `source`(`paragraph_end`, `definition`, `unknown`)가 있습니다.

`match`로 지정하면 그 글자가 든 문단 전체에 적용되고 이웃 문단은 바뀌지 않습니다. 요청한 값이 이미 모두 적용되어 있으면 네이티브 호출 없이 `status`가 `already_set`, `dirty=false`이고 Undo 단계도 늘지 않습니다.

결과의 `steps[0].result`에는 `target`(문단 번호, 위치 종류, 네이티브 범위), `requested`, `result.before`, `result.after`가 있어 범위별 글꼴·크기·굵기·기울임·밑줄·글자색이나 문단 값을 실행 전후로 볼 수 있습니다. `result.native_actions`는 실행한 네이티브 동작이고, `result.joined_controls`는 범위와 함께 글자 값을 받은 보이지 않는 컨트롤입니다.

**범위 옆의 보이지 않는 컨트롤.** Hancom 13.0.0.3457에서 측정한 대로 처리합니다.

| 범위와 컨트롤의 관계 | 컨트롤 | 처리 |
|---|---|---|
| 문단의 첫 글자에서 시작하고 그 앞에 이 컨트롤만 있음 | 구역 설정, 단 설정, 머리말, 꼬리말, 쪽 번호, 새 번호, 책갈피, 감추기, 숨은 설명, 찾아보기 표시 | 컨트롤도 범위의 글자 값을 받습니다. 컨트롤의 데이터와 인쇄된 쪽 번호는 그대로이며, 실행 후 이를 비교합니다 |
| 범위가 컨트롤 바로 앞에서 끝남 | 머리말, 꼬리말, 쪽 번호, 새 번호, 책갈피, 감추기, 찾아보기 표시 | 위와 같습니다. 문단 끝의 책갈피까지 닿으면 번호 모양 규칙도 문단 끝에 닿은 범위와 같습니다 |
| 앞에 다른 글자가 있고 범위가 컨트롤 바로 뒤에서 시작함 | 위의 컨트롤 | 컨트롤은 그대로입니다 |
| 범위에 닿음 | 그림, 표, 사각형, 수식, 각주, 미주, 자동 번호 | 개체는 자기 값을 유지합니다 |
| 범위에 닿음 | 측정하지 않은 컨트롤(타원 등), 범위 바로 뒤의 숨은 설명, 여러 문단 범위 앞의 숨은 설명 | 변경 전에 `target_beside_unproven_control`로 거부합니다 |

페이지 번호를 넣은 문서의 제목 전체나 앞부분을 지정해도 세션이 보류되지 않으며, 제목과 본문을 한 문단 범위로 지정할 수 있습니다. 여러 문단에 걸친 문단 서식은 첫 문단의 첫 글자 앞에 있는 위 컨트롤만 건너뜁니다(숨은 설명 제외).

지원하는 위치는 본문과 표 셀의 글자, 인라인 그림이나 표 옆의 글자, 표만 든 문단, 빈 문단, 번호·글머리표 문단입니다. 다음은 변경 전에 거부하며 `refusal_code`로, 거부 응답의 `message`에는 문장으로 이유를 알려 줍니다. 캐럿과 그 전의 선택은 되돌리고, 세션은 보류되지 않으며, 저장되지 않은 이전 변경도 그대로 남습니다.

| `refusal_code` | 뜻 |
|---|---|
| `target_not_found`, `target_ambiguous` | 글자나 문단 번호가 없거나, 글자가 두 곳 이상에 있음 |
| `target_text_mismatch` | `paragraph_index`의 문단이 `expected_text`와 다름 |
| `target_wrong_page` | 대상이 `expected_page`에 없음. 결과의 `found_page`가 실제 쪽 |
| `target_crosses_control` | 글자 범위가 표·그림 같은 개체를 가로지름 |
| `target_crosses_invisible_control` | 글자 범위가 책갈피·쪽 번호·구역 설정 같은 보이지 않는 컨트롤이나 필드 표시를 가로지름 |
| `target_has_controls` | 여러 문단 범위에 표·그림 같은 개체가 든 문단이 있음. 문단마다 따로 지정하면 됩니다 |
| `target_has_invisible_controls` | 여러 문단 범위에서 첫 문단의 첫 글자 앞이 아닌 곳에 보이지 않는 컨트롤이 있거나, 첫 글자 앞에 숨은 설명이 있음. 문단마다 따로 지정하면 됩니다 |
| `target_beside_unproven_control` | 이 편집이 건드리지 않았다고 증명할 수 없는 컨트롤이 범위에 닿음. 위 표 참고. 범위와 컨트롤 사이에 글자를 한 자 이상 두면 됩니다 |
| `control_enumeration_unavailable` | 문서의 컨트롤 목록을 끝까지 읽지 못함. 하이퍼링크 같은 필드가 있는 문서가 여기에 해당합니다(Hancom이 필드 끝의 식별자를 빈 값으로 돌려줌) |
| `target_changes_number_mark` | 여러 문단 범위가 번호 문단의 끝에 닿거나(마지막 문단의 끝 포함) 번호 모양을 돌려놓을 수 없음. 위 번호 설명 참고 |
| `target_in_header_footer` | 글자가 머리말이나 꼬리말에 있음 |
| `target_unsupported_context` | 표 셀이 아닌 개체 안의 글자(글상자, 각주, 캡션 등) |
| `target_range_unsupported` | `through_match`가 `match`보다 앞에 있거나, 표 셀과 여러 문단에 걸침 |
| `target_empty` | 글자 서식의 대상에 글자가 없음 |
| `target_unselectable` | 글자가 없고 뒤따르는 문단도 없는 본문 문단 |
| `target_unlocatable` | 네이티브 문서에서 찾지 못한 위치. 글자가 없는 표 셀 문단이 여기에 해당 |
| `selection_unverified` | 선택한 블록이 요청한 글자와 같다고 확인되지 않음 |
| `font_not_available`, `font_availability_unknown` | 위 글꼴 설명 참고 |
| `preservation_unprovable` | 문서가 너무 커서 비교 근거를 읽지 못함 |

네이티브 동작이 시작된 뒤 결과가 요청과 다르거나 알 수 없으면(요청 밖의 글자·문단 변경, 저장되지 않은 값, 읽기 실패) 되돌리거나 다시 시도하지 않고 위 번호 문단 편집과 같이 세션을 보류합니다.

되돌리기와 다시 실행은 받아들인 서식 명령 하나당 한 번씩이며, 번호 모양을 돌려놓은 명령은 네이티브 두 단계를 한 번에 되돌립니다(`native_undo_steps: 2`). 결과의 `confirmation.expected_state`가 `{"compared": true, "matches": true}`이면 문서가 그 명령 전(되돌리기) 또는 후(다시 실행) 상태와 같다는 뜻입니다. `matches`가 `false`이면 `undo_state_mismatch`·`redo_state_mismatch`로 실패하고 이후 되돌리기는 `undo_not_counted`로 거부됩니다. 책갈피 같은 컨트롤 양쪽의 글자에 같은 값을 준 뒤 렌더링하면 Hancom이 두 글자 묶음을 합치므로, 그 뒤 다른 명령을 되돌릴 때 이렇게 알립니다.

확인은 `hwpx_proof`의 `review`로 합니다. `summary.md`의 "서식 변경 상세"는 행마다 문단(대괄호 안 숫자는 `review.json`의 `index`, 그 뒤에 글 일부, 글이 없으면 `(빈 문단)`, `(표만 있는 문단)`처럼 표시하고, 개체 정보를 읽지 못했으면 `(글 없음, 내용 확인 못함)`으로 표시), 범위, 항목, 이전 값, 이후 값을 보여 줍니다. 긴 글은 `…`로 줄였다고 표시하고, 응답에 글이 없는 범위는 `(앞부분 밖 글자 20개)`, 공백뿐인 범위는 `(공백 1개)`처럼 적습니다. 밑줄 위치는 `위쪽`, `아래쪽`, `없음`으로 비교합니다. 이 표는 200행까지이고 넘으면 그렇게 알리며, 전체는 `comparison.changes`에 있습니다. `coverage.format.state`는 바뀐 서식을 값으로 모두 설명하면 `complete`, 이 항목들이 아닌 다른 서식이 바뀌었거나(이름 있는 값과 함께 바뀐 경우 포함) 목록이 잘렸으면 `partial`, 값을 읽지 못했으면 `unverified`이며 뒤의 두 경우 `review_status`도 `partial`이 됩니다. 정의의 번호가 달라졌을 뿐 내용이 같으면 변경으로 보지 않습니다. 같은 확인이 번호 변경으로 보고한 문단의 번호 종류·수준 차이는 설명된 것으로 보며, 번호 정의가 바뀌었거나 다른 값도 함께 바뀌었으면 `partial`로 남습니다. "표 셀 등 본문 밖 문단"의 "글 동일"은 글만 비교한 결과이며, 서식이 바뀐 셀 문단은 그 수를 함께 적습니다.

번호 모양은 `coverage.number_mark`에 있습니다. `numbered`, `compared`, `changed`, `new`, `not_compared`가 개수이고 `state`는 다음과 같습니다.

| `state` | 뜻 | `review_status`에 미치는 영향 |
|---|---|---|
| `none` | 번호 문단이 없음 | 없음 |
| `compared` | 모든 번호 문단을 비교했고 바뀌지 않음 | 없음 |
| `changed` | 모든 번호 문단을 비교했고 바뀐 것이 있음(`comparison.changes`에 `number_mark_changed`로 표시) | 없음 |
| `new_only` | 모든 번호 문단이 기준 시점에 번호가 없었거나 새로 추가되어 비교할 이전 모양이 없음 | `partial` |
| `mixed` | 일부는 비교했고(`changed`에 바뀐 수) 일부는 새 번호임 | `partial` |
| `not_compared` | 비교할 기준이나 값이 없는 번호 문단이 있음. 세션의 첫 확인과 정의를 읽지 못한 번호가 여기에 해당 | `partial` |

새 번호의 모양은 비교되지 않았으므로 `compared`로 세지 않으며, 요약과 `summary.md`에 새 번호 개수를 적습니다. 인쇄된 번호 글자·종류·수준 확인은 별개이며 `numbering`에 있습니다. 번호를 붙인 뒤의 확인을 기준으로 다시 확인하면 그 번호는 `compared`가 됩니다. 이 비교는 서식 값으로 계산한 것이며 인쇄된 모양은 미리보기로 확인합니다. 저장되지 않은 변경이 없고 저장본이 있으면 `persisted_format`이 Hancom을 거치지 않고 HWPX 파일을 직접 읽어 비교한 결과를 줍니다(`match`, `partial`, `mismatch`, `unverified`). `mismatch`이면 `review_status`도 `mismatch`입니다. 저장본 비교에는 번호 모양도 포함됩니다. `review.json`의 각 문단에는 `index`, `context`(`body`, `table_cell`, `other`)와 `format`(글자 묶음별 값, 문단 값, 번호 모양, 개체 목록)이 있습니다. 글자 묶음 값이 아주 많은 문서는 응답 크기 안에서 뒤쪽 문단의 묶음을 생략하고 `runs_truncated`로 알립니다.

## 표

표는 문서 안의 정확한 하나를 지정해서 편집합니다. GUI에서 표를 클릭해 둘 필요가 없습니다. 명령마다 표를 네이티브 식별자로 다시 찾아 들어가고, 셀로 이동하거나 셀 블록을 만든 뒤 블록이 요청한 셀과 정확히 같은지 확인하고 네이티브 동작을 실행합니다. 실행 후 대상 표의 모든 셀과 다른 표, 표 밖 문단, 머리말·꼬리말, 스타일, 그림 데이터를 비교합니다. 그래서 `review`, `context`, `find`를 사이에 끼워도 대상이 바뀌지 않습니다.

### 표 찾기와 읽기

- `{"op": "table_inventory"}`: 문서의 모든 표를 `table_id`(예: `tbl:1011`), `page`, `rows`, `cols`, `merged_cells`, `expected_hash`, 표 앞 문장(`before_text`), 앞쪽 셀 글(`first_cells`), 표 너비와 쪽의 관계(`layout`), 표에 저장된 크기(`stored_size`의 `width_hu`, `height_hu`), 중첩 표면 `nested_in`과 `nested_cell`로 돌려줍니다. 내용이 같은 표도 식별자가 다르므로 구별됩니다. 본문의 표만 나옵니다. 머리말·꼬리말의 표는 이 명령들로 편집하지 않으며(`target_in_header_footer`), 확인(`review`)에서 값을 비교합니다.
- `{"op": "table_read", "table_id": "tbl:1011", "max_cells": 500}`: 셀마다 `address`, `row`, `col`, `row_span`, `col_span`, `text`(문단은 줄바꿈으로 잇고 탭과 연속 공백을 그대로 유지), `paragraphs`(글자 묶음별 글꼴·크기·굵기·기울임·밑줄·글자색과 문단 값), `objects`(중첩 표 안의 개체까지), `content_sha256`, `border`(`left`, `right`, `top`, `bottom`마다 `line`, `width_mm`, `color`), `fill`을 돌려줍니다. 글은 2000자까지 나오고, 더 길면 `text_truncated=true`입니다. `max_cells`는 1~2000(기본 2000)이고, 잘리면 `cells_truncated=true`입니다.

병합된 셀은 첫 칸의 주소로 부릅니다. A1:B1을 합친 셀은 `A1`이고, `B1`을 지정하면 `cell_covered_by_merge`로 거부합니다.

### 공통 입력

편집 명령에는 읽은 값을 그대로 넣습니다: `table_id`, `expected_hash`, `expected_page`, `expected_rows`, `expected_cols`, `confirm_layout=true`. 읽은 뒤 표가 바뀌었으면 `table_state_stale`로 편집 전에 거부합니다. 해시는 격자, 셀 위치와 너비, 글과 서식, 선, 채우기를 포함하며, 렌더링에 따라 달라지는 셀 높이는 뺍니다. 편집 결과의 `after.expected_hash`를 다음 명령에 쓸 수 있습니다.

### 행·열·병합·나누기

`table_edit_exact`의 `action`은 다음과 같습니다. 주소와 번호는 1부터 셉니다.

| `action` | 입력 | 동작 |
|---|---|---|
| `insert_row_above`, `insert_row_below` | `row`, 선택 `count`(1~50) | 그 행 위나 아래에 행 추가. 수가 몇이든 네이티브 동작 하나, Undo 하나 |
| `insert_col_left`, `insert_col_right` | `col`, 선택 `count` | 그 열 왼쪽이나 오른쪽에 열 추가 |
| `delete_row`, `delete_col` | `row` 또는 `col`, 선택 `count`, `expected_removed_cells` | 정확히 그 줄 삭제 |
| `merge_cells` | `cell`, `end_cell` | 사각 범위 병합 |
| `split_cell` | `cell`, `split_rows`, `split_cols`(1~20) | 셀 나누기 |
| `unmerge_cell` | `cell` | 병합 셀을 합쳐진 칸 수대로 나누기 |

삭제할 때는 지워지는 셀 중 내용이 있는 셀을 모두 적어야 합니다: `"expected_removed_cells": [{"address": "A2", "text": "전체 글"}, {"address": "B2", "text": "그림 옆 글", "objects": ["picture"]}]`. `table_read`의 `text`, `objects`와 정확히 같아야 하며 다르면 `removed_content_mismatch`로 거부합니다. `text`가 잘린 셀은 글 대신 `{"address": "A2", "content_sha256": "..."}`로 적습니다. 이 값은 그 셀의 글, 글자 모양과 개체를 함께 가리키고, 표 해시와 주소에 묶입니다. 중첩 표에 든 그림도 `objects`에 적습니다. 다른 곳에서 함께 쓰는 그림 데이터는 그대로 남고, 마지막으로 쓰던 그림이 지워지면 데이터도 함께 빠집니다. 빈 줄을 지울 때는 비워 둡니다. 결과의 `changes.removed_cells`와 `removed_blank_cells`에 지워진 셀이 나옵니다.

Hancom 13.0.0.3457에서 측정한 결과이며, 결과의 `changes`와 `after`로 그대로 알려 줍니다.

- **병합:** 각 셀의 문단을 읽는 순서대로 이으며 글자·문단 서식과 그림을 유지합니다. 문단 하나가 비어 있기만 한 셀은 문단을 보태지 않고(`dropped_blank_cells`), 문단이 여럿인 셀의 빈 문단은 남습니다. 합쳐진 셀의 왼쪽·위 선과 채우기는 첫 셀에서, 오른쪽·아래 선은 범위 반대쪽 모서리 셀에서 옵니다. 어떤 셀도 시작하지 않게 된 격자선은 사라지므로 행이나 열 수가 줄 수 있습니다(`removed_grid_lines`).
- **나누기:** 내용은 전부 첫 칸에 남고 다른 칸은 비어 있습니다. 원래 셀별로 다시 나눠 주지 않습니다. 너비는 똑같이 나뉩니다. 그래서 너비가 다른 열을 합친 셀을 풀면 원래 경계로 돌아가지 않고 격자선이 하나 늘어납니다(`added_grid_lines`). 같은 줄의 다른 셀은 병합 범위가 넓어집니다. 나뉜 칸의 채우기는 같고, 칸 사이 선은 원래 셀의 오른쪽·아래 선, 그쪽이 표 가장자리이면 왼쪽·위 선입니다.
- **행·열 삽입:** 새 줄은 기준 줄의 병합 모양과 채우기와 선을 물려받고 글은 비어 있습니다. 새 줄을 가로지르는 병합 셀은 넓어집니다(`widened_cells`).
- **표 가장자리:** 표 바깥쪽으로 줄을 더하면 테두리 값은 새 줄로 가고 옛 줄의 그쪽 선은 자기 반대쪽 값이 됩니다. 가장자리 줄을 지우면 새로 가장자리가 되는 셀이 그 값을 받습니다. 바뀐 값은 `outer_edge_moved`에 있습니다.
- **병합 셀을 지나는 삭제:** 병합 셀이 삭제 줄을 지나면 줄어들고 내용은 남습니다(`shortened_cells`). 줄 여러 개를 한 번에 지울 때 병합 셀이 그 묶음의 경계를 가로지르면 `merged_cell_crosses_lines`로 거부합니다. 한 줄씩 지우거나 먼저 푸세요.

### 선

`table_style_exact`의 `scope`는 `cell`(`cell`), `range`(`cell`, `end_cell`), `table`입니다. `borders`에 다음 묶음을 하나 이상 넣습니다.

```json
{"op": "table_style_exact", "table_id": "tbl:1011", "expected_hash": "sha256:...", "expected_page": 1, "expected_rows": 5, "expected_cols": 4, "scope": "range", "cell": "B2", "end_cell": "C3",
 "borders": [{"edges": ["outline"], "line": "double", "width_mm": 0.5, "color": "#1F3864"},
             {"edges": ["inner_horizontal"], "line": "dot", "width_mm": 0.2, "color": "#808080"},
             {"edges": ["inner_vertical"], "line": "none"}],
 "fill": "#FFF2CC", "confirm_layout": true}
```

- `edges`: 대상의 바깥 변 `top`, `bottom`, `left`, `right`, 네 변을 함께 `outline`, 안쪽 선 `inner_horizontal`, `inner_vertical`, 둘을 함께 `inner`, 전부 `all`. 한 변은 한 번만 지정합니다. `all`과 `inner`는 그 대상에 있는 선만 뜻합니다. 한 셀의 `all`은 네 변이고, 한 행의 `all`은 네 변과 세로 안쪽 선입니다. 묶음 하나가 가리키는 선이 대상에 하나도 없으면 `edge_not_in_target`입니다. `inner_horizontal`이나 `inner_vertical`을 이름으로 적었는데 대상에 그 선이 없으면, `top` 같은 다른 변과 한 묶음이어도 편집 전에 `edge_not_in_target`으로 거부합니다.
- `line`: `solid`, `dash`, `dot`, `dash_dot`, `dash_dot_dot`, `long_dash`, `circle`, `double`, `slim_thick`, `thick_slim`, `slim_thick_slim`, 그리고 선을 지우는 `none`. 이 열두 값은 Hancom 공식 선 종류 표의 0~11입니다.
- `width_mm`: 0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 1, 1.5, 2, 3, 4, 5. `color`는 `#RRGGBB`입니다. `none`에는 두 값을 주지 않으며, Hancom은 저장된 굵기와 색을 그대로 두고 종류만 없음으로 합니다.

**공유 변.** 맞닿은 두 셀의 경계는 두 셀이 각각 값을 가집니다. 바깥 변을 바꾸면 Hancom이 맞은편 셀의 마주 보는 변도 함께 씁니다. 결과의 `changes.edges`에 행마다 `cell`, `side`, `before`, `after`, `role`(`requested` 또는 `shared_neighbour`)이 있어 어느 셀의 어느 변이 바뀌었는지 볼 수 있습니다. 예외가 하나 있습니다. 맞은편 셀이 그 변보다 넓은 병합 셀이면 그 셀의 값은 그대로이고 `changes.neighbours_not_written`에 나옵니다. 범위가 그 셀의 변 전체를 덮으면 그 셀도 함께 씁니다. 이 밖의 셀이나 변이 바뀌면 성공으로 보고하지 않습니다.

**더 넓은 이웃 아래의 선.** 더 넓은 병합 셀과 마주 보는 변은 두 셀의 선이 겹쳐 인쇄됩니다. 측정한 결과는 다음과 같습니다.

| 요청한 선 (이웃은 0.12mm 실선) | 인쇄 결과 | 처리 |
|---|---|---|
| `none` | 이웃의 선이 그대로 보임 | `edge_held_by_wider_neighbour`로 거부 |
| 이웃보다 가는 실선 (0.1mm) | 요청한 선 옆으로 이웃의 선이 보임 | 거부, `over_wider_neighbour: true`면 실행 |
| 점선 계열 | 끊긴 곳으로 이웃의 선이 보임 | 거부, `over_wider_neighbour: true`면 실행 |
| 이웃과 같거나 더 굵은 실선 | 요청한 선만 보임 | 실행 |

선을 지우려면 넓은 셀의 그 변을 대상으로 합니다. 그러면 그 변을 마주 보는 셀 전체의 선이 함께 지워지며, 일부 구간만 지우는 방법은 없습니다. 이웃의 선이 `none`이면 거부하지 않습니다. `over_wider_neighbour`로 실행하면 결과의 `changes.prints_over`에 함께 인쇄되는 이웃과 그 선이 나옵니다.

### 채우기

`fill`은 `#RRGGBB` 또는 `none`입니다. 흰색 `#FFFFFF`는 채우기이고 `none`은 채우기 없음이며, `table_read`와 저장 파일에서 구별됩니다. 대상 셀의 글, 서식, 선은 그대로입니다. 무늬·그러데이션·그림 채우기가 있는 셀에는 `fill_kind_unsupported`로 거부합니다. `borders`와 `fill`을 함께 주면 네이티브 동작 두 번이고 Undo 한 번으로 둘 다 되돌아갑니다. 요청한 값이 이미 있으면 네이티브 호출 없이 `result.status`가 `already_set`이고 `dirty=false`입니다.

### 표 너비

표의 `layout`은 너비(`width_hu`, `width_mm`), 본문 폭(`text_width_hu`), 왼쪽 시작(`left_hu`), 오른쪽 끝(`right_edge_hu`), 쪽 폭(`page_width_hu`), 넘친 값(`overflow_hu`, `overflow_mm`, `off_page_mm`), 판정(`status`)을 담습니다. `status`는 `fits`, `past_margin`(본문 오른쪽 여백을 넘음), `off_page`(쪽 밖으로 나가 인쇄에서 잘림), `wider_than_parent`(중첩 표가 담긴 셀의 안쪽 폭보다 넓음), `unverified`(`reason`에 이유)입니다. 글자처럼 취급하지 않는 표, 다단, 제본 여백, 셀 간격, 상대 너비, 표 앞에 글이 있는 문단, 가운데·오른쪽 정렬 문단은 `unverified`입니다.

**열을 넣으면 표가 넓어집니다.** 새 열은 옆 열과 같은 너비로 생기고 다른 열은 줄지 않으므로, 본문 폭을 다 쓰는 표는 쪽 밖으로 나갈 수 있습니다. 응답의 `after.layout`과 `warnings`, 그리고 확인 결과가 표와 쪽을 밝힙니다. 너비는 요청이 있을 때만 바꿉니다.

```json
{"op": "table_width_exact", "table_id": "tbl:1011", "expected_hash": "sha256:...", "expected_page": 1, "expected_rows": 2, "expected_cols": 5, "fit": "text_width", "confirm_layout": true}
{"op": "table_width_exact", "table_id": "tbl:1011", "expected_hash": "sha256:...", "expected_page": 1, "expected_rows": 2, "expected_cols": 3, "column_widths_mm": [30, 60, 50], "confirm_layout": true}
```

- `fit: "text_width"`: 열 비율을 유지한 채 표를 본문 폭에 맞춥니다. `unverified`인 표와 중첩 표는 `table_geometry_unknown`으로 거부합니다.
- `column_widths_mm`: 모든 격자 열의 너비를 왼쪽부터 줍니다. 병합 셀은 그 열들의 합이 됩니다. 열 수가 다르면 `column_widths_mismatch`, 합이 본문 폭을 넘으면 `table_would_overflow`, 5mm보다 좁은 열은 `column_too_narrow`입니다.
- 일반 표는 바뀌는 열마다, 병합 셀이 가로지르는 표는 바뀌는 셀마다 네이티브 동작 하나입니다. 응답의 `undo.native_editing_actions`에 개수가 있고, Undo·Redo 한 번으로 전부 되돌립니다. 같은 값이면 호출 없이 `already_set`입니다. 표 전체 너비를 한 번에 바꾸는 네이티브 값은 Hancom이 무시하므로 쓰지 않습니다.

### 새 표

`{"op": "table_create_exact", "match": "기준 문단의 고유한 문구", "expected_page": 1, "rows": 2, "cols": 3, "confirm_layout": true}`는 그 본문 문단 뒤에 표 문단과 빈 문단 하나를 만듭니다. 결과의 `table`에 다음 편집에 쓸 값이 있습니다. 선택 항목 `cells`로 행별 글을 넣을 수 있습니다. 빈 글은 빈 셀이고, 줄바꿈은 새 문단이 되며, 탭과 연속 공백은 그대로입니다. 그 밖의 제어 문자는 편집 전에 `cells_text_unsupported`로 거부합니다. 표 문단은 기준 문단의 문단 모양을 물려받으므로 번호 문단 뒤에는 `target_numbered`로 거부합니다. 표 안, 머리말·꼬리말, 없는 문구, 여러 문단에 있는 문구, 다른 쪽도 거부합니다.

### 되돌리기

명령 하나가 Undo·Redo 한 번입니다. 결과의 `confirmation.expected_state`가 `{"compared": true, "matches": true}`이면 명령 전후 상태와 같습니다. 측정된 예외는 다음과 같습니다.

- 병합 셀을 가로지르는 삽입을 되돌리면 격자, 글, 선, 표 해시와 인쇄 결과는 돌아오지만 그 병합 셀에 저장된 높이가 달라 `undo_state_mismatch`로 알립니다.
- 빈 새 표의 Redo는 표가 돌아오지만 Hancom이 겹침 순서 값을 새로 주어 `redo_state_mismatch`로 알립니다.
- 줄을 넣거나 지운 뒤의 Undo·Redo에서 표의 저장 크기 값이 셀보다 뒤처지는 일이 간혹 있습니다. 한 번은 Redo 뒤 `width_hu`가 54000이 아니라 45000이었습니다. 언제 일어나는지는 밝히지 못했고, 일어나면 `redo_state_mismatch`로 알립니다.
- 이렇게 알릴 때 `confirmation.expected_state.differences`에 표, 셀, 항목, 기록된 값, 현재 값이 나옵니다. 비교에 이미 읽은 내용으로 만들며 네이티브 호출을 더하지 않습니다. 기록된 값이 없는 편집(문서가 아주 크거나 오래전 편집)에는 `differences`가 없습니다.
- 이렇게 알린 뒤에는 그 지점 너머로 되돌리지 않습니다(`undo_not_counted`). 문서와 세션은 그대로 쓸 수 있습니다.
- `cells`를 준 새 표는 Undo 단위를 셀 수 없어 `undo_not_counted`로 거부합니다.

### 표 확인 결과

`hwpx_proof`의 `review`에서 표에 관해 읽을 값입니다.

- `coverage.layout`: 넘치는 표(`overflow`)와 확인하지 못한 표(`unverified`)를 표와 쪽으로 밝힙니다. 어느 쪽이든 있으면 `review_status`는 `complete`가 아니고 첫 줄에 이름이 나옵니다.
- `comparison.tables.changed`: 선, 채우기, 글, 병합, 표 너비(`width`), 셀 너비(`width_changes`), 표 속성(`frame_changes`), 셀 여백·세로 정렬 등(`cell_properties`)을 전후 값으로 보여 줍니다. 표 해시가 바뀌었는데 이름 붙일 값이 없으면 `other_properties=true`이고 `partial`입니다.
- `comparison.tables.stored_sizes`: 해시가 같은 표에서 저장된 표 크기만 바뀐 경우를 표와 전후 값으로 보여 줍니다. 한글은 편집 뒤 레이아웃에 따라 다른 표의 저장 높이도 다시 계산하므로 이런 표가 여럿 나올 수 있고, 내용·선·채우기가 같다는 뜻입니다. 바뀐 표의 저장 높이는 `stored_height`입니다. 되돌리기가 `size.height`를 차이로 알리면 이 항목으로 값을 볼 수 있습니다.
- `persisted_tables`: 저장 파일을 직접 읽어 격자, 병합, 전체 글, 선, 채우기, 표·셀 너비를 비교합니다. 저장된 표 높이는 내용 비교와 따로 `stored_size_differences`에 적고, 다르면 `partial`입니다. 실제 한글에서 저장 직후에는 둘이 같았습니다. 머리말·꼬리말의 표도 비교합니다. 실행 중 문서가 알리지 않은 머리말·꼬리말 표는 `unverified_tables`로 이름을 밝히고 `partial`입니다. 무늬와 그러데이션은 양쪽에서 읽은 값으로 비교하고, 그림 채우기처럼 양쪽 값을 맞출 수 없으면 `fills_not_compared`로 세고 `partial`입니다.
- `coverage.package_media`: 저장 파일과 원본 파일의 그림 데이터입니다. `complete`, `partial`(어떤 그림도 쓰지 않던 원본 이미지가 저장본에 없음), `removed_by_edit`(이 세션의 삭제로 빠진 그림), `unverified`(빠진 그림 데이터를 어느 삭제와도 대조하지 못함), `mismatch`(아직 문서에 있는 그림의 데이터가 빠짐, 데이터 없는 참조), `not_compared`, `not_saved`입니다. 대조는 개수가 아니라 그림 데이터 자체의 SHA-256으로 합니다. 이전 확인과 지금 사이에 지워진 그림의 데이터이고, 남은 그림 중 그 데이터를 쓰는 것이 없을 때만 삭제로 빠졌다고 봅니다. 실행 중 문서의 Base64 데이터를 풀어 얻은 값이 저장 파일의 BinData와 같은 것을 실제 한글에서 확인했습니다. 빠진 데이터는 `unexplained_lost`(문서에 남은 그림)와 `untraceable_lost`(대조할 수 없음)로도 나옵니다. Hancom은 어떤 그림도 쓰지 않는 이미지를 문서를 열 때 읽어 들이지 않으므로, 열린 문서만 비교하는 `보존 확인`은 이를 알 수 없습니다.

선과 채우기를 값과 인쇄로 볼 때 다음을 구별합니다. 흰색 `#FFFFFF`와 `none`은 저장 값으로 구별되지만 흰 종이에서는 같게 인쇄됩니다. 이중선과 삼중선은 0.4mm 이하 굵기를 100dpi 미리보기로 보면 한 줄처럼 보일 수 있으므로, 모양을 눈으로 확인하려면 1mm 이상으로 그리거나 PDF를 확대합니다. 이 값들은 저장 값으로 확인합니다.

### 거부와 결과 불명

| `refusal_code` | 뜻 |
|---|---|
| `table_not_found`, `table_identity_ambiguous` | 이 식별자의 표가 없거나 둘 이상 |
| `table_state_stale`, `table_grid_mismatch`, `target_wrong_page` | 읽은 뒤 표가 바뀌었거나 행·열·쪽이 다름. 쪽은 `found_page`로 알림 |
| `cell_out_of_range`, `cell_covered_by_merge`, `range_too_small`, `range_cuts_merged_cell` | 주소나 범위가 맞지 않음 |
| `cell_not_merged`, `merged_split_rows_unsupported` | 병합 셀이 아니거나, 병합 셀을 그 칸 수와 다른 행 수로 나누려 함. 이 경우는 렌더링 높이에 따라 결과가 달라 예측할 수 없음 |
| `removed_content_mismatch`, `table_would_be_empty`, `merged_cell_crosses_lines`, `line_inside_merged_cell` | 삭제·삽입 줄 문제 |
| `edge_not_in_target`, `fill_kind_unsupported` | 선·채우기 입력 문제 |
| `table_too_large`, `table_geometry_unknown`, `cells_shape_mismatch` | 500줄 초과, 열 너비를 셀에서 계산할 수 없음, `cells` 모양이 다름 |
| `target_table_not_entered`, `target_cell_unreachable`, `selection_unverified`, `preservation_unprovable`, `control_enumeration_unavailable` | 대상에 들어가지 못했거나 비교 근거를 읽지 못함 |
| `target_numbered`, `target_not_found`, `target_ambiguous`, `target_not_in_body`, `target_in_header_footer` | 새 표의 기준 문단 문제 |

거부되면 캐럿과 선택을 되돌리고 문서가 그대로임을 확인합니다. 세션은 보류되지 않고 저장하지 않은 이전 변경도 남습니다. 네이티브 동작이 시작된 뒤 결과가 예측과 다르면 되돌리거나 다시 시도하지 않고 세션을 보류하며, `error.details.message`에 어느 셀의 어느 값이 달랐는지 알려 줍니다. 문서 글은 넣지 않습니다.

### 확인

`hwpx_proof`의 `review`는 표를 식별자로 비교합니다. `comparison.tables`의 `changed`에 표마다 `grid`(행·열 전후), `added_cells`, `removed_cells`, `moved_cells`, `span_changes`, `content_changes`, `edges`, `fills`가 전후 값과 함께 있고, `unchanged`에 해시가 같은 표가, `added`, `removed`에 새로 생기거나 없어진 표가 있습니다. `summary.md`의 "표 변경" 표는 표(쪽), 셀, 항목, 이전, 이후를 보여 줍니다. 표 편집으로 표 문단의 개체 값이 바뀐 것은 이 비교가 설명하므로 알 수 없는 서식 변경으로 세지 않습니다. `coverage.tables.state`는 `complete`, 표가 없으면 `none`, 값을 읽지 못하면 `unverified`, 크기 한도로 잘렸거나 이름 없는 값이 바뀌었으면 `partial`이며 뒤의 두 경우 `review_status`도 낮아집니다. 저장본이 있으면 `persisted_tables`가 HWPX 파일을 Hancom 없이 직접 읽어 표마다 격자, 병합, 글, 너비, 선, 채우기를 비교합니다(`match`, `partial`, `mismatch`, `unverified`). `mismatch`이면 `review_status`도 `mismatch`입니다. HWPX 파일은 인쇄가 긴 점선인 선 `dash`를 `DOT`로, 점선인 `dot`을 `DASH`로 적습니다. 이 비교는 그 이름을 맞춰 읽습니다.

## 백엔드 기능 대응

백엔드에 있는 문서 편집 기능은 MCP 도구로 부를 수 있습니다. `hwpx_capabilities`가 백엔드 명령 묶음 op, REST 경로, 대기열 편집 op 하나하나에 대해 MCP 경로(`exposed`), 같은 기능을 하는 다른 도구(`equivalent`), 노출하지 않는 이유(`excluded`), 백엔드 자체가 막아 둔 기능(`backend_unsupported`)을 돌려줍니다. 이 분류는 `hwpx_mcp/parity.py`에 있고, `tests/test_mcp_backend_parity.py`가 백엔드 소스(명령 패키지 manifest, 서버 허용 목록, 라우터, 대기열 실행기)에서 목록과 필드를 직접 읽어 분류되지 않은 op·경로·필드가 생기면 실패합니다.

필드 이름과 범위는 백엔드 검증기와 같습니다. MCP가 먼저 거부하는 것은 백엔드도 거부하는 값뿐이고, 나머지는 바꾸지 않고 전달합니다. 백엔드의 거부 코드, 부분 결과, 결과 불명과 세션 보류는 그대로 돌려줍니다.

### `hwpx_command`와 `hwpx_bundle`의 op

| 구분 | op | 주요 입력 |
|---|---|---|
| 쪽·구역·다단 | `layout_inspect` | 없음. 캐럿 위치의 용지·여백·다단 값을 `layout_exact`의 `expected_before` 단위로 읽음 |
| | `layout_exact` | `kind`(`page_setup`, `columns`, `section_insert`, `section_delete`, `hanging_indent`), `expected_pos`(`hwpx_where`의 `[list, para, pos]`), `confirm_layout=true`. `page_setup`은 `paper_width_mm`·`paper_height_mm`(10~1000), `landscape`, 네 여백·머리말/꼬리말 길이·제본 여백(0~300), `apply_to`(`current_section`, `whole_document`)와 바꾸는 항목의 현재 값 `expected_before`. `columns`는 `count`(1~10), `gap_mm`, `same_width`, `apply_to`(`current_section`, `from_caret_new`). `section_delete`는 `section_index`(2 이상)와 `pos`가 0인 `expected_pos`. `hanging_indent`는 `marker_text` |
| 개체 넣기 | `object_insert_exact` | `kind`(`footnote`, `endnote`, `memo`, `hyperlink`, 책갈피, `equation`, `line`, `rectangle`, `ellipse`, `header`, `footer`. 책갈피의 정확한 값은 입력 스키마의 `kind` 열거값), `expected_pos`, `confirm_mutation=true`. 종류별로 `text`, `url`(http·https·mailto)과 `display_text`, `name`, `script`, `width_mm`·`height_mm`·`treat_as_char`, `apply_to`(`both`, `even`, `odd`) |
| 컨트롤(기존 컨트롤 목록 방식) | `control_inventory`, `table_frame_inventory`, `exact_control_select_proof`, `table_cell_structure_exact` | `section_anchor` 또는 `page_from`. 목록이 돌려준 `target_id`(`ctrl/...`)·`expected_hash`·`expected_page` |
| | `control_delete_exact`, `control_move_resize_exact`, `control_join_previous_exact` | 위 대상 값과 `confirm_remove=true` 또는 `confirm_layout=true`. 크기는 `scale_percent`(5~200), 이동은 `move_dx_mm`·`move_dy_mm`(±300) |
| 표(기존 컨트롤 목록 방식) | `cell_format_exact`, `cell_row_fit_exact`, `table_split_exact`, `table_structure_exact`, `table_column_width_exact` | 위 대상 값. 행 높이는 `row_height_percent`·`row_height_hu`·`row_height_mm`·`resize_up_steps`·`resize_down_steps`·`line_spacing`·`char_height_percent` 중 하나. 표 나누기는 `down_rows` |
| 표 만들기·바꾸기 | `native_table_insert`, `anchor_range_replace_native_table`, `selected_text_delete_exact` | 셀 글(`cells`)과 셀마다의 `sha256:` 해시(`flat_value_hashes`), 비어 있지 않은 셀 수와 앞부분 미리보기. 범위 교체는 4열 표만 받음 |
| 문단 | `paragraph_style_apply_exact` | `match`, `expected_page`, `keep_with_next`, `widow_orphan`, `pagebreak_before`(0 또는 1), `confirm_layout=true` |
| | `paragraph_delete_exact`, `paragraph_join_previous_exact`, `paragraph_join_next_exact`, `paragraph_rehome_exact` | 대상 문구와 쪽, 앞뒤 문단 확인 문구, 확인 값 |
| 글 넣기·선택 | `anchor_insert`, `set_text_file`, `get_selected_text` | `anchor_insert`는 `target`, `position`(`before-anchor`, `after-anchor`, `after-paragraph`, `before-heading`), `text` 또는 `fragments` |
| 읽기 | `style_inspect`, `typography_overview`, `where` | 문서를 바꾸지 않음 |
| 저수준 | `hwp_action`, `pyhwpx_call` | 백엔드 명령 묶음 허용 목록의 이름만. `hwp_action`은 이동·셀 선택 12개, `pyhwpx_call`은 위치·선택·글 읽기와 캐럿 이동 15개 |
| 기존 정확한 명령 | 위 "번호 문단 편집", "글자·문단 서식", "표" 절의 op | 변경 없음. `readback`의 `scope`는 백엔드와 같이 `caret`, `selection`, `page`, `document`를 받음(기본 `document`) |

백엔드는 번호·문단 삽입/삭제·글자/문단 서식·`table_*` 정확한 명령에만 "변경 전 거부" 경로가 있습니다. 위 표의 다른 op(쪽·구역, 개체 넣기, 기존 컨트롤·표 방식, 문단 구조, 글 넣기, 저수준, `style_inspect`·`typography_overview` 같은 읽기 포함)는 백엔드 오류가 나면 잘못된 대상이나 오래된 해시였더라도 결과 불명으로 보고하고 세션을 보류합니다. 실제 한글 2024에서 선택 없이 `get_selected_text`를 부른 경우와 구역 정의 컨트롤을 `table_cell_structure_exact`에 준 경우가 그랬습니다. 그래서 MCP는 표 전용 기존 단계(`cell_format_exact`, `cell_row_fit_exact`, `table_split_exact`, `table_structure_exact`, `table_column_width_exact`, `table_cell_structure_exact`)에 표 컨트롤 ID(`ctrl/<n>/tbl/<id>`)만 받고, `get_selected_text`는 `select_target` 등으로 선택을 만든 뒤 부릅니다. 각 op의 입력 스키마 설명에도 같은 내용이 있습니다.

`paragraph_rehome_exact`는 먼저 `insert_before_match` 앞에 `insert_text`와 줄 바꿈을 넣고, 그다음 문서 처음부터 `delete_match`를 찾아 `delete_expected_page`에서 처음 나온 것을 지웁니다. 이어서 공백을 모두 뺀 문서 글을 단계 전과 비교해 다르면 Undo를 두 번 시도하고 실패로 끝나므로, 보이는 글의 순서가 그대로인 이동만 통과합니다(한글 2024에서 문단 하나를 다른 두 문단 뒤로 옮기자 실패하고 세션이 보류됨). 옮길 글보다 앞에 있는 문구 앞으로 옮기면 방금 넣은 사본을 지우고도 성공으로 보고하며, 순서는 그대로이고 `insert_before_match` 앞에 줄 바꿈만 남습니다(한글 2024에서 확인). 입력 스키마 설명에도 같은 내용이 있습니다.

`hwpx_command`만 받는 op(백엔드 직접 경로, 명령 묶음이 아님): `target_info`, `move_to_target`, `select_target`(대상 글 또는 직전 `hwpx_find`의 결과 번호), `cell_info`, `cell_move`·`cursor_move`(`direction`, `count`), `type_text`(`text`, `allow_insert_at_caret`), `figure_section`, `cell_replace`(`anchor`는 대상 표 안의 글로, 그 글을 찾아 캐럿을 그 표로 옮김. `cell`은 그 표 안의 칸이며 `anchor` 없이 쓰면 캐럿이 이미 그 표 안에 있어야 함. `text`, 확인 값), `font_size`, `bold`, `font_family`, `bullet`, `table_insert`, `list_items`(`count`는 1만. 항목을 여러 개 넣으려면 여러 줄 입력이 필요한데 백엔드가 막아 두었으므로, 2 이상은 세션에 손대기 전에 거부), `numbering_readback`, 그리고 기존 `undo`, `redo`, `command_reconcile`. 캐럿이나 선택에 따라 결과가 달라지는 기존 경로이므로 `hwpx_proof`의 `review`로 결과를 확인합니다. 백엔드는 `move_to_target`, `select_target`, `cell_info`, `cell_move`, `cursor_move`를 "변경 가능"으로 세므로, 그 뒤 `hwpx_close`는 실제로 쓰는 저장을 요구합니다. 바뀐 것이 없는 문서의 저장은 아무것도 쓰지 않는 확인으로 끝나 이 요구를 풀지 않으므로, 편집 없이 위치만 옮긴 세션은 닫기가 계속 거부됩니다(한글 2024에서 확인). 위치만 필요하면 `hwpx_find`를 쓰고, 이미 그렇게 됐다면 실제 편집 뒤 `hwpx_save`로 저장하면 닫을 수 있습니다.

예: 2단으로 바꾸기

```json
{"session_id": "<세션>", "request": {"op": "layout_exact", "kind": "columns", "expected_pos": [0, 0, 0], "count": 2, "gap_mm": 8, "same_width": true, "apply_to": "current_section", "expected_before": {"count": 1}, "confirm_layout": true}}
```

`hwpx_bundle`은 같은 단계를 1~20개 묶어 한 번에 보냅니다. 백엔드는 첫 실패 단계에서 멈추며, 의미상 거부는 앞 단계의 편집을 유지하고 기술 실패는 세션을 보류합니다(위 "쪽 번호" 절의 묶음 규칙과 같음).

### 그림: `hwpx_image`

`source_path`는 어댑터 컴퓨터의 입력·산출물 폴더 아래 `.png`, `.jpg`, `.jpeg`, `.bmp` 파일(25 MiB 이하)만 받습니다. `insert`는 캐럿 위치, `insert_at_anchor`는 `target` 문구 옆(`position`), `figure_section`은 `target_heading` 뒤에 `heading`, `intro`, 그림, `caption`, `body`를 넣습니다. 선택 항목은 `width`, `height`, `sizeoption`, `treat_as_char`, `embedded`, `fit_cell`입니다. 결과의 `picture`에 보낸 파일의 SHA-256이 있습니다.

### 대기열 작업: `hwpx_job`

열린 세션과 별개로, 백엔드 작업자가 파일 복사본을 편집하고 PDF로 변환합니다. 입력 파일은 입력·산출물 폴더 아래 `.hwpx`, `.hwp`만 받습니다.

| `action` | 내용 |
|---|---|
| `convert` | PDF 변환 |
| `edit_and_convert` | `instructions.operations`의 대기열 편집 op를 차례로 적용(아래). 선택 항목 `instructions.validation`(`preserve_page_count`, `max_page_count`, `max_page_increase`) |
| `compile_authoring`, `author_and_convert` | `authoring_markdown`을 계획으로 바꾸거나 바로 실행. `validation`, `cleanup_placeholders`(`none`, `unused`, `all`), `policy_override` |
| `clear_placeholders`, `clear_placeholders_and_convert` | 자리표시자 정리 계획 또는 실행 |
| `compile_placeholder_fills`, `fill_placeholders_and_convert` | `placeholder_fills`(`placeholders`, `tables`의 `r1c1` 형식 셀, `table_cells`) |
| `confirm_state`, `confirm_brief`, `confirm_action` | 작성 계획의 확인 상태와 `confirm`(`inspect_more`, `abort`, `regenerate`, `approve`). `approve`에 `enqueue_convert=true`면 실행 작업을 등록 |
| `status`, `metadata` | 작업 상태와 `kind`별 기록(`edit_summary`, `execution_result`, `validation_artifact`, `validation_report`, `bundle_manifest`, `evidence_event`, `evidence_index`, `failure_summary`, `runtime_status`, `observation_status`) |
| `retrieve` | 성공한 작업의 PDF와 편집된 파일을 산출물 폴더의 `jobs/<job_id>/retrieve-N/`로 받고, 모든 쪽을 PNG로 만들며(`dpi`, `max_pages`), 편집 파일의 패키지를 제출한 파일과 비교 |

대기열 편집 op: `replace_all`, `replace_text_safe`, `replace_paragraph_safe`, `paragraph_replace_native`, `replace_paragraph_range_safe`, `paragraph_range_replace_native`, `replace_between_anchors_safe`, `clone_text_style`, `clone_paragraph_shape`, `clone_paragraph_layout`, `insert_after_text`, `insert_before_text`, `insert_at_document_end`, `style_text`, `style_text_in_paragraph`, `align_paragraph`, `paragraph_shape`, `list_paragraph`, `replace_empty_native_list_scaffold`, `cursor_replace_text`, `cursor_delete_range`, `cursor_insert_text`, `cursor_snapshot`, `control_delete_by_anchor`, `table_cell_action`, `table_cell_clear_text`, `table_cell_replace_text`, `table_patch_cells`. 각 op의 필드는 `hwpx_job` 입력 스키마에 있습니다.

`replace_paragraph_safe`와 `paragraph_replace_native`는 `find`가 든 문단을 문단 나눔까지 함께 지우고 `replace`를 입력합니다. 다음 문단과 나뉜 상태를 유지하려면 `replace`를 `"\r\n"`으로 끝내거나 `keep_paragraph_break: true`로 문단 내용만 바꿉니다. 둘 다 아니면 다음 문단이 바뀐 글에 붙고 작업은 성공으로 끝납니다(한글 2024에서 확인, 마지막 문단은 지울 문단 나눔이 없음). 작성 계획(`compile_authoring`, `author_and_convert`)은 대상 문단의 내용만 바꾸므로 그 뒤의 문단(예: 다음 제목)은 글과 문단 모양이 그대로 남습니다.

작업 ID는 이 어댑터가 제출한 것만 조회·확인·회수할 수 있으며, 다른 ID는 백엔드에 보내지 않고 `JOB_NOT_OWNED`로 거부합니다. 성공하지 않은 작업은 `JOB_NOT_READY`로 거부하고 아무것도 받지 않습니다. 제출 중 연결이 끊기면 `BACKEND_OUTCOME_UNKNOWN`이며 다시 보내지 않습니다.

### 노출하지 않는 백엔드 기능

| 백엔드 | 이유 |
|---|---|
| `/local-cli/action`, `/local-cli/pycall` | 아무 HAction 이름·pyhwpx 경로를 받는 무제한 경로. 명령 묶음의 허용 목록 부분은 `hwp_action`, `pyhwpx_call`로 노출 |
| 대기열 op `native_action` | 아무 HAction과 매개변수 집합을 실행하며 백엔드에 허용 목록이 없음 |
| 대기열 op `page_number`, `official_numbering` | 대기열 실행기가 스스로 거부(위 번호 설명 참고). `page_number_apply`, `official_numbering_apply`를 사용 |
| `/local-cli/replace` | 백엔드가 501로 막아 둠 |
| `cell-replace`의 `text_file` | 백엔드 컴퓨터의 임의 경로를 읽음. 내용을 `text`로 보냄 |
| `/interactive/session/*` 기록 경로(`open`, `find`, `choose`, `enter`, `lock`, `verify-pre`, `apply`, `verify-post`, `undo`, `close`)와 확인 증거 경로 | 문서를 편집하지 않는 운영자 기록이며, 관리 세션의 수명 기록을 덮어쓸 수 있음. 상태 조회는 모든 세션 도구가 사용 |
| `/observation-viewer/*` | 사람이 보는 화면 스트림. 네이티브 화면은 `hwpx_proof`의 `frame` |
| `*-from-path` 경로 | 같은 처리를 하는 업로드 경로를 `hwpx_job`이 사용하며, 백엔드 컴퓨터의 임의 경로는 노출하지 않음 |
| 명령 묶음 op `export_pdf`, `save_document` | `hwpx_proof`, `hwpx_save`가 같은 백엔드 동작 뒤에 파일을 받아 확인하므로 그쪽을 사용 |

새 문서 만들기는 백엔드 기능이 아니라 CLI의 `hwpx new`(로컬 파일 생성)입니다.

## 크기 제한

- 열기: `hwpx_open`은 25 MiB 이하의 파일만 받고, 넘으면 `SOURCE_SIZE_LIMIT`로 거부합니다. 산출물 폴더의 저장본을 다시 열 때도 같은 제한입니다.
- 저장본 복사: `hwpx_save`와 `hwpx_close`는 저장본을 256 MiB까지 산출물 폴더로 복사합니다. 25 MiB를 넘는 저장본은 복사되지만 `hwpx_open`으로 다시 열어 확인할 수는 없습니다.
- 256 MiB 초과: 복사하지 않고 `saved_output.code`에 `ARTIFACT_SIZE_LIMIT`를 알려 줍니다. 복사되지 않은 내용이 있으면 `hwpx_close`는 `SAVED_OUTPUT_NOT_RETRIEVED`로 계속 거부하고 아무것도 지우지 않으므로, 이 어댑터만으로는 그 세션을 닫을 수 없습니다.
- 저장본 검사: 복사한 파일을 메모리에 올리지 않고 내려받아 검사합니다. 메모리에 올리는 것은 구역과 헤더의 글뿐이며 패키지 전체에서 합계 64 MiB까지입니다. 이 글 합계, 압축을 푼 패키지 전체 512 MiB 또는 멤버 4,096개를 넘으면 저장본은 그대로 두고 `saved_output.package` 없이 `package_error`를 `package_limit`로 알려 주며, 열었던 파일과의 비교는 제공하지 않습니다. 검사 중 메모리 사용은 이 글의 복사본과 문자열 변환을 합쳐 최대 약 400 MiB입니다. 열 때도 입력 파일에 같은 검사를 합니다.
- 누적: 저장할 때마다 `saved` 폴더에 `001-`, `002-`처럼 새 파일이 생기고, 확인할 때마다 `review-N` 폴더에 PDF와 PNG가 생깁니다. 어댑터는 이 파일들을 지우지 않으므로 필요 없는 산출물은 직접 정리해야 합니다.


관리 세션 판정에는 `metadata.local_cli_v1.opened_via == "local_cli_v1"`가 반드시 필요합니다. `bridge`나 `closed_via` 표시는 열기 출처를 대신하지 않는 추가 수명주기 정보이므로, 해당 표시만 있는 세션은 명령을 받을 수 없습니다. `state`가 `closed` 또는 `closed_cleanup_pending`인 세션은 상태 조회만 허용되며 새 명령·저장·증명·닫기 요청은 `SESSION_CLOSED`로 거부됩니다.

다운로드 경로는 관리 세션 바인딩과 해당 산출물의 크기·SHA-256 기록이 모두 있고, 산출물이 같은 관리 루트의 일반 파일임을 확인한 경우에만 상태에 표시됩니다. 서버의 절대 파일 경로는 응답에 포함하지 않습니다. 전송 직전에 경로를 다시 여는 대신 관리 루트와 파일을 안전하게 확인한 파일 스트림을 먼저 열고 custody hash를 재검증하므로, 검증 뒤 파일을 바꾸거나 경로를 심은 경우에는 전송하지 않습니다. Windows에서는 응답이 끝날 때까지 열린 파일에 대한 교체도 운영체제가 막습니다.

`hwpx_command`가 받는 op 전체는 아래 "백엔드 기능 대응"에 있습니다. 이 문단은 그중 번호·셀 서식 명령의 세부 규칙입니다. `page_number_apply`는 `position`(`top-left`, `top-center`, `top-right`, `bottom-left`, `bottom-center`, `bottom-right`)과 `confirm_layout=true`를 요구하고, 선택 항목으로 `start_number`(1~65535, 기본 1), `side_char`(`none` 또는 `dash`, 기본 `none`)를 받습니다. 앞·뒤 문구는 네이티브 대시(`-`) 장식(`dash`)만 지원하고 임의의 앞·뒤 문구와 머리말·꼬리말 확인 우회 항목은 받지 않습니다. 실행 중인 문서의 메모리 상 HWPML2X 구조 스냅샷(본문, 글자·문단·표·그림 서식, 머리말·꼬리말, 스타일, 번호 정의, 바이너리 데이터 참조)을 읽을 수 있을 때만 변경하고, 읽을 수 없으면 변경 전에 거부하며, 실행 후 새 쪽 번호 컨트롤과 그 설정 외의 구조가 달라지거나 스냅샷을 읽을 수 없으면 정리(되돌리기) 없이 결과 불명으로 보고합니다(inserted로 보고하지 않음). `official_numbering_apply`는 `match`, `expected_page`, `level`(1~4), `confirm_layout=true`를 모두 요구하며 일치하는 문단 하나에 네이티브 개요 번호 수준을 적용합니다. 현재 구역의 개요 번호 정의가 `1.` → `가.` → `1)` → `가)` 4수준과 일치하는지 읽어서 확인하고, 일치하지 않으면 그 구역에 다른 개요 문단이 없다는 것을 증명할 수 있을 때만 4수준을 정의하며, 확인하지 못한 형식은 성공으로 보고하지 않습니다. 이미 확인된 정의가 있으면 네이티브 ParagraphShape 동작 한 번(HeadingType, Level)으로 적용하고, 같은 구조 스냅샷을 변경 전에 읽을 수 없거나 대상 문단을 하나로 식별할 수 없으면 변경 전에 거부하며, 변경 후 대상 문단의 번호 외 구조가 달라지면 결과 불명으로 보고합니다. 두 명령 모두 자동 저장하지 않으며, 저장 전에 렌더링 증명이 필요합니다. 두 동작은 보호된 대화형 `command_bundle`(CLI/MCP)과 호출자가 SDK 수명을 소유하는 라이브러리에서만 지원합니다. 대기열 편집 작업(`/edit-and-convert`, `/edit-and-convert-from-path`, 작업자 `edit_and_convert_with_pyhwpx`)은 실패 시 SDK를 종료하고 COM을 해제하므로 결과 불명 상태에서 문서를 유지할 수 없어, `page_number`/`official_numbering` 작업 연산이 하나라도 있으면 `edit_and_convert_with_pyhwpx`가 자신의 COM 초기화·SDK 생성·열기·편집 이전에 작업 전체를 거부합니다(`/capabilities`의 `queued_job_unsupported_ops`). 다만 이를 감싸는 `process_one_command`는 그 전에 네이티브 내보내기 사전 점검을 수행하며, 이 점검이 빈 탐침 인스턴스를 생성하고 종료하므로 자식 프로세스 전체가 SDK 작업을 전혀 하지 않는다는 뜻은 아닙니다. 그러므로 API·CLI·MCP 동일 입력 동등성은 충족되지 않습니다. 또한 결과 불명이 된 뒤에는 캐럿을 이동하지 않으며, 자동 Undo·닫기·종료를 하지 않습니다. `page_number_apply`와 `official_numbering_apply`에서 네이티브 변경 호출(`PageNumPos`, `OutlineNumber`, `ParagraphShape`)이 시작된 뒤 발생한 일반 예외(읽기·집계·비교 오류 포함)는 원인을 유지한 채 결과 불명(`mutation_may_have_persisted=true`)으로 보고합니다. `KeyboardInterrupt`·`SystemExit` 같은 종료 신호는 호출자가 소유한 라이브러리에서는 삼키지 않고 그대로 전파합니다. 정확한 내장 종료 신호에만 호환용 결과 불명 표시를 붙이고 사용자 하위 클래스의 속성은 읽거나 변경하지 않습니다. 소유 런타임의 trusted producer intent가 결과 불명과 STA 보존을 별도로 결정합니다. 보호된 라이브러리 소유 실행 경계(실시간 세션의 소유 스레드에서 실행되는 `command_bundle`)에서는 이런 결과 불명(반환된 결과든 종료 신호든)이 해당 세션을 보류 상태로 만듭니다. 명령은 `failed`(`may_have_mutated=true`, 원래 신호 종류는 원인 체인에 보존)로 기록되고, 호출자는 `ok=false`, `dirty=true`, `native_custody_held=true`인 미완료 결과를 받으며, 같은 소유 스레드와 세션 등록은 유지됩니다. 이 경로에서는 Clear·Quit·CoUninitialize, 캐럿 이동, Undo, 저장·내보내기, 재시도, 새 네이티브 호출을 하지 않고, 시간 초과로 표시하지도 않으며, 이후의 실행·닫기(강제 포함)·정리·증거 없는 reconcile/acknowledge는 보류를 해제하지 못합니다. 상태 조회는 읽기 전용으로 남습니다. 운영자 승인에 의한 해제 절차는 이 저장소에 구현되어 있지 않으므로 보류된 세션은 해당 프로세스가 끝날 때까지 유지됩니다. Linux 어댑터 테스트는 어댑터 계약만 확인합니다. `cell_format_exact`는 기존 대상 ID, 해시, 페이지, 구역 앵커와 `confirm_layout=true`를 요구하며, 형식 선택자는 `vertical_align`, `cell_margin_mm`, `cell_margin_hu`, `fill_color`(`#RRGGBB`), `border`(`none`) 중 하나만 지정합니다. `around`는 백엔드가 함께 찾아야 하는 주변 문구이며, 예전 어댑터가 정수 기본값으로 보내던 `0`은 지정하지 않은 것으로 받습니다. 셀 여백 변경은 선언된 단위로 네 변(left/right/top/bottom)을 모두 명시한 단일 네이티브 호출을 사용하고, 요청에서 독립적으로 계산한 네 변의 값과 네이티브 전후 getter가 정확히 일치해야 성공으로 기록됩니다. 유효하지 않거나 신선하지 않은 네이티브 getter, 같은 값을 다시 요청한 no-op, 요청값과 다른 여백은 거부되며, 동작 후 판정 실패는 변경 가능성을 보존합니다. 셀 정렬은 별도의 네이티브 전후 readback을 확보하지 못하면 성공으로 기록하지 않습니다. `pyhwpx_call`과 `hwp_action`은 백엔드 명령 묶음의 정확한 허용 목록에 있는 이름만 받고, 임의 Python·셸·명령 이름과 백엔드의 무제한 `/local-cli/pycall`·`/local-cli/action` 경로는 노출하지 않습니다. 이 어댑터는 기존 비변경용 `safe-schema` 자체를 확장하거나 쓰기 권한으로 해석하지 않습니다.

`hwpx_cell_margins_get`는 셀 서식을 바꾸지 않고 표 셀 네 변의 여백을 읽는 독립 도구입니다. `hwpx_find`가 돌려준 문서 생성 값, `hwpx_command`의 `readback`이 돌려준 컨트롤 목록의 대상 ID·해시·앵커 페이지, 그리고 같은 세션에서 새로 확인한 셀 위치를 `request`에 모두 담아야 합니다. 요청 필드는 `document_id`(세션 ID와 같은 32자리 소문자 16진수), `expected_document_generation`, `target_id`, `expected_hash`, `expected_page`, `expected_cell_page`, `page_from`, `page_to`, `cell_pos`, `cell_addr`, `section_anchor`이며 `max_controls`(기본 100)만 선택 항목입니다. 값은 JSON 배열로 직접 전달합니다. `cell_pos`는 네이티브 위치 3개 값, `cell_addr`는 0부터 시작하는 `[열, 행]`이고 A1이 `[0, 0]`입니다. A1 표기 문자열, 숫자 문자열, 다른 필드, 기본값 대체는 모두 거부합니다. `section_anchor`는 요청한 셀 문단에 문자 그대로 한 번만 나타나는 문장 조각이어야 합니다. 찾기 결과의 `section` 표시(`live-text`)는 메타데이터일 뿐 앵커 문구가 아니므로 그대로 쓰면 거부됩니다.

읽는 순서는 다음과 같습니다. 먼저 세션과 문서, 디스크 관리 복사본의 크기·SHA-256이 요청과 일치하는지 확인하고, 네이티브 텍스트를 새로 읽어 생성 값을 다시 계산해 요청 값과 비교합니다. 컨트롤 목록에서 대상 표를 정확히 하나만 고른 뒤 해시와 앵커 페이지를 맞춰 보고, 캐럿을 요청 위치로 한 번 옮겨 셀 주소, 바로 위 표, 렌더링 페이지, 문단 안의 앵커를 같은 시점에 확인합니다. 그다음 `HAction.GetDefault('TablePropertyDialog')`로 네이티브 값을 새로 읽어 왼쪽·오른쪽·위·아래 순서의 hwpunit 값을 돌려줍니다. mm 표시가 필요하면 `hwpunit × 25.4 ÷ 7200`으로 바꿀 수 있지만, 비교는 반환된 네이티브 값으로 합니다. 값을 읽은 뒤에는 문서·표·셀·페이지·생성 값이 그대로인지 다시 확인하고, 캐럿을 원래 위치로 되돌린 뒤 문서 변경 상태가 읽기 전과 같은지 확인합니다. 이 확인 중 하나라도 어긋나면 실패로 처리하고 값을 돌려주지 않습니다. 이미 수정된 문서에서도 `true → true`로 그대로면 성공하며, 이 도구가 변경을 만들었다는 뜻은 아닙니다.

이 도구는 결과를 캐시하지 않고, 읽기 성공이 저장·다시 열기 뒤의 값이라는 보장도 하지 않습니다. 저장 전 값과 다시 연 뒤 값을 비교하려면 저장·닫기 뒤 `saved_output.path`를 다시 열어 새 대상으로 읽어야 하며, 이 셀 여백 흐름 자체는 네이티브 검증되지 않았습니다. 시간 초과 뒤에는 이 도구를 다시 실행하지 말고 `hwpx_status`로 명령 상태를 확인한 뒤 `command_reconcile`로 결과를 확인합니다. 셀 안에 앵커 문구가 없는 빈 셀이나, 컨트롤 목록에 잡히지 않는 표, 선택 상태가 남아 있는 문서는 이 도구의 대상이 아닙니다.

페이지는 `1..10000`, DPI는 `72..600`으로 제한됩니다. 한 페이지를 렌더링한 결과는 전체 문서 검토 통과를 뜻하지 않습니다. 최종 문서는 모든 페이지를 Hancom 기반으로 렌더링하고 확인해야 합니다.

## 실패·시간 초과·취소

결과는 `ok`, `operation`, `session_id`, `document_id`, `result`, `error`를 포함합니다. 같은 내용이 MCP의 텍스트와 `structuredContent`에 들어갑니다. 백엔드 거부와 도구 입력 오류는 `isError=true`입니다. JSON-RPC 문법·메서드·요청 메타데이터 오류는 별도의 프로토콜 오류입니다.

`BACKEND_OUTCOME_UNKNOWN`이면 작업을 반복하지 마세요. 클라이언트의 대기가 끝나도 이미 제출된 Hancom 작업은 계속될 수 있습니다. `hwpx_status`에서 처리 중인 명령 ID를 확인한 뒤 같은 `session_id`와 그 명령 ID로 `hwpx_command`의 `command_reconcile`을 호출해 결과를 재확인합니다. 문서 열기 응답을 받기 전에 연결이 끊겨 세션 ID가 없다면 백엔드 운영자가 기존 `/local-cli/status`에서 세션과 처리 기록을 먼저 확인해야 합니다.

명령 시간 초과 뒤에는 해당 세션이 격리됩니다. 대기 중인 명령을 다시 보내거나 `close`를 호출하지 마세요. `command_reconcile`은 원래 Hancom 작업이 끝난 뒤 소유 STA에서 현재 문서 상태를 확인하고, 문서 전체의 변경 상태를 기록한 다음 네이티브 `SaveAs`가 정확히 성공한 경우에만 새 recovery 산출물을 보존합니다. 산출물의 존재·관리 루트·파일 크기·SHA-256 커밋은 full close 증명이 아니며 현재 런타임은 세션 소유권을 해제하지 않습니다.

복구가 아직 진행 중이면 `reconciled=false`, `reconciliation=pending`, `ok=false`가 HTTP 200으로 반환됩니다. 이 응답은 관찰 기록이지 성공이 아니며, 명령을 다시 실행하지도 않았다는 뜻입니다. recovery 산출물을 만들 수 없거나 파일 검증에 실패하면 결과는 알 수 없음으로 남고, 세션의 보류 상태도 유지됩니다. 이 경우 명령을 재실행하지 말고 응답과 백엔드 로그를 보존해 운영자가 판단해야 합니다. 복구가 완료된 뒤 같은 명령 ID로 다시 조회할 수 있으며, 서버는 저장된 의미적 성공·실패 결과를 그대로 반환합니다. 보관할 recovery 파일은 인증된 `/local-cli/session/{session_id}/artifact/recovery` 경로로 다운로드하세요. 상태 조회는 같은 명령이 아직 진행 중인 상태에서도 성공할 수 있으므로, 조회 성공을 편집 성공으로 해석하지 않습니다.

## 네이티브 한계와 관찰 한도

Hancom 객체는 `new=True`로 만들지만, 이 인자가 COM 호출이 실행되기 전에 새 네이티브 프로세스의 독점 소유권을 확보해 주지는 않습니다. 생성자 오류가 기존 Hancom 객체에 연결된 뒤나 새 프로세스가 시작된 뒤에 도착할 수 있고, 핸들을 받지 못한 프로세스에 대해 이 코드가 자동 정리를 확인할 방법은 없습니다. 외부 생성 도우미는 한 번만 시도하고 원래 오류를 그대로 반환하며, 재시도로 실패를 성공처럼 보이게 하지 않습니다. 네이티브 핸들을 받지 못했다면 자동 정리는 `unconfirmed`로 남고, 정리는 반환된 핸들을 닫고 해당하는 COM 해제까지 끝난 뒤에만 `confirmed`가 됩니다.

백그라운드 전용 실행은 지원하지 않습니다. Hancom 창은 로그인된 대화형 데스크톱에서 보이게 유지해야 하며, 최소화되거나 포커스를 잃은 창, 잠긴 데스크톱에서의 동작은 보장하지 않습니다.

결과 재확인의 관찰에는 한도가 있습니다. 시간 초과 뒤에는 복구 요청을 최대 한 번 보낼 수 있고 호출자의 대기는 최대 125초입니다. 그 뒤에는 자동 관찰을 중단하고, 결과가 정해지지 않았으면 세션 바인딩과 로그와 기존 산출물을 보존해 운영자가 판단합니다. 이 한도는 네이티브 프로세스가 끝났다는 보장이 아니며, 결과가 남아 있는 동안 정리는 확인되지 않은 상태로 유지됩니다.

연결 취소는 롤백이 아닙니다. 어댑터는 변경 명령을 자동 재전송하지 않습니다. 백엔드 대기 제한은 `HWPX_MCP_TIMEOUT`으로 설정하며 기본 120초, 허용 범위는 0.1..600초입니다. 페이지 렌더링은 별도로 60초를 제한합니다. 취소된 렌더링의 임시 파일은 잠시 남을 수 있으며 완료된 `manifest.json`이 없는 파일을 증명으로 사용하면 안 됩니다. 복구 전에는 원본 문서나 서버 관리 복사본을 직접 삭제하지 마세요.

## 종료와 보안

문서는 `hwpx_close`로 닫습니다. 저장되지 않은 변경이 있거나 결과 불명으로 보류된 세션은 닫기를 거부하며 소유 STA와 세션을 유지합니다. 그런 세션은 별도로 권한을 가진 운영자가 정확히 소유한 일회용 시험 인스턴스만 실제 네이티브 증거로 종료합니다. 그 뒤 이 터미널에서 만든 어댑터만 종료합니다.

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
