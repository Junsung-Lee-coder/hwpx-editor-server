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
| `hwpx_command` | `session_id`, `request.op`. 아래 제한된 명령만 허용 |
| `hwpx_proof` | `session_id`, `request.kind`: `frame`, `page`, `review`. 페이지 방식은 `page`와 선택 항목 `dpi` 사용. `review`는 아래 "결과 확인"을 참고 |
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

글자 서식: `{"op": "character_format_exact", "match": "고유한 문구", "expected_page": 1, "font_family": "맑은 고딕", "size_pt": 13.5, "bold": true, "italic": false, "underline": true, "text_color": "#C03020", "confirm_layout": true}`. 항목은 하나 이상 지정하며 지정한 항목만 바뀝니다. `size_pt`는 포인트(0.1~4096, 0.1 단위), `text_color`는 `#RRGGBB`입니다. `font_family`는 문서에 이미 있는 TTF 글꼴이나 이 Windows 세션의 글꼴 목록에 있는 글꼴만 받습니다. Hancom은 없는 글꼴 이름도 받아 렌더링할 때 다른 글꼴로 바꾸므로 변경 전에 `font_not_available`로 거부합니다. 글꼴 목록을 읽지 못하면 `font_availability_unknown`입니다. Hancom 내장 HFT 글꼴은 같은 이름이 이 목록에 없으면 문서에서 쓰고 있어도 거부됩니다. `underline=true`는 아래쪽 밑줄입니다.

문단 서식: `{"op": "paragraph_format_exact", "match": "문단 안의 고유한 문구", "expected_page": 1, "align": "center", "left_indent_pt": 20, "right_indent_pt": 10, "first_line_indent_pt": 5, "line_spacing_type": "percent", "line_spacing_value": 180, "confirm_layout": true}`. 

- `align`: `left`, `center`, `right`, `justify`
- `left_indent_pt`, `right_indent_pt`: 왼쪽·오른쪽 여백(포인트, 0~570.2, 0.1 단위)
- `first_line_indent_pt`: 포인트(±570.2, 0.1 단위). 양수는 첫 줄을 그만큼 들여 쓰고, 음수(내어쓰기)는 첫 줄을 왼쪽 여백에 두고 다음 줄부터 그만큼 들여 씁니다.
- `line_spacing_type`과 `line_spacing_value`는 함께 지정합니다. `percent`는 글자에 따른 비율(1~500의 정수), `fixed`는 고정 값, `at_least`는 최소, `between_lines`는 여백만 지정이며 이 셋의 값은 포인트(0.1 단위)입니다.

`match`로 지정하면 그 글자가 든 문단 전체에 적용되고 이웃 문단은 바뀌지 않습니다. 요청한 값이 이미 모두 적용되어 있으면 네이티브 호출 없이 `status`가 `already_set`, `dirty=false`이고 Undo 단계도 늘지 않습니다.

결과의 `steps[0].result`에는 `target`(문단 번호, 위치 종류, 네이티브 범위), `requested`, `result.before`, `result.after`가 있어 범위별 글꼴·크기·굵기·기울임·밑줄·글자색이나 문단 값을 실행 전후로 볼 수 있습니다. `result.native_actions`는 실행한 네이티브 동작입니다.

지원하는 위치는 본문과 표 셀의 글자, 인라인 그림이나 표 옆의 글자, 표만 든 문단, 빈 문단, 번호·글머리표 문단입니다. 다음은 변경 전에 거부하며 `refusal_code`로 이유를 알려 줍니다. 캐럿과 그 전의 선택은 되돌리고, 세션은 보류되지 않으며, 저장되지 않은 이전 변경도 그대로 남습니다.

| `refusal_code` | 뜻 |
|---|---|
| `target_not_found`, `target_ambiguous` | 글자나 문단 번호가 없거나, 글자가 두 곳 이상에 있음 |
| `target_text_mismatch` | `paragraph_index`의 문단이 `expected_text`와 다름 |
| `target_wrong_page` | 대상이 `expected_page`에 없음. 결과의 `found_page`가 실제 쪽 |
| `target_crosses_control` | 글자 범위가 표·그림 같은 개체를 가로지름 |
| `target_has_controls` | 여러 문단 범위에 표·그림 같은 개체가 든 문단이 있음. 문단마다 따로 지정하면 됩니다 |
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

되돌리기와 다시 실행은 받아들인 서식 명령 하나당 한 번씩입니다. 결과의 `confirmation.expected_state`가 `{"compared": true, "matches": true}`이면 문서가 그 명령 전(되돌리기) 또는 후(다시 실행) 상태와 같다는 뜻입니다. `matches`가 `false`이면 `undo_state_mismatch`·`redo_state_mismatch`로 실패하고 이후 되돌리기는 `undo_not_counted`로 거부됩니다.

확인은 `hwpx_proof`의 `review`로 합니다. 요약과 `summary.md`의 "서식 변경 상세"에 바뀐 범위, 항목, 이전 값, 이후 값이 있고, `comparison.changes`의 `format_changed` 항목에 `ranges`와 `paragraph`로 같은 내용이 들어 있습니다. `coverage.format.state`는 바뀐 서식을 값으로 모두 설명하면 `complete`, 이 항목들이 아닌 다른 서식이 바뀌었거나 목록이 잘렸으면 `partial`, 값을 읽지 못했으면 `unverified`이며 뒤의 두 경우 `review_status`도 `partial`이 됩니다. 저장되지 않은 변경이 없고 저장본이 있으면 `persisted_format`이 Hancom을 거치지 않고 HWPX 파일을 직접 읽어 비교한 결과를 줍니다(`match`, `partial`, `mismatch`, `unverified`). `mismatch`이면 `review_status`도 `mismatch`입니다. `review.json`의 각 문단에는 `index`, `context`(`body`, `table_cell`, `other`)와 `format`(글자 묶음별 값과 문단 값)이 있습니다.

## 크기 제한

- 열기: `hwpx_open`은 25 MiB 이하의 파일만 받고, 넘으면 `SOURCE_SIZE_LIMIT`로 거부합니다. 산출물 폴더의 저장본을 다시 열 때도 같은 제한입니다.
- 저장본 복사: `hwpx_save`와 `hwpx_close`는 저장본을 256 MiB까지 산출물 폴더로 복사합니다. 25 MiB를 넘는 저장본은 복사되지만 `hwpx_open`으로 다시 열어 확인할 수는 없습니다.
- 256 MiB 초과: 복사하지 않고 `saved_output.code`에 `ARTIFACT_SIZE_LIMIT`를 알려 줍니다. 복사되지 않은 내용이 있으면 `hwpx_close`는 `SAVED_OUTPUT_NOT_RETRIEVED`로 계속 거부하고 아무것도 지우지 않으므로, 이 어댑터만으로는 그 세션을 닫을 수 없습니다.
- 저장본 검사: 복사한 파일을 메모리에 올리지 않고 내려받아 검사합니다. 메모리에 올리는 것은 구역과 헤더의 글뿐이며 패키지 전체에서 합계 64 MiB까지입니다. 이 글 합계, 압축을 푼 패키지 전체 512 MiB 또는 멤버 4,096개를 넘으면 저장본은 그대로 두고 `saved_output.package` 없이 `package_error`를 `package_limit`로 알려 주며, 열었던 파일과의 비교는 제공하지 않습니다. 검사 중 메모리 사용은 이 글의 복사본과 문자열 변환을 합쳐 최대 약 400 MiB입니다. 열 때도 입력 파일에 같은 검사를 합니다.
- 누적: 저장할 때마다 `saved` 폴더에 `001-`, `002-`처럼 새 파일이 생기고, 확인할 때마다 `review-N` 폴더에 PDF와 PNG가 생깁니다. 어댑터는 이 파일들을 지우지 않으므로 필요 없는 산출물은 직접 정리해야 합니다.


관리 세션 판정에는 `metadata.local_cli_v1.opened_via == "local_cli_v1"`가 반드시 필요합니다. `bridge`나 `closed_via` 표시는 열기 출처를 대신하지 않는 추가 수명주기 정보이므로, 해당 표시만 있는 세션은 명령을 받을 수 없습니다. `state`가 `closed` 또는 `closed_cleanup_pending`인 세션은 상태 조회만 허용되며 새 명령·저장·증명·닫기 요청은 `SESSION_CLOSED`로 거부됩니다.

다운로드 경로는 관리 세션 바인딩과 해당 산출물의 크기·SHA-256 기록이 모두 있고, 산출물이 같은 관리 루트의 일반 파일임을 확인한 경우에만 상태에 표시됩니다. 서버의 절대 파일 경로는 응답에 포함하지 않습니다. 전송 직전에 경로를 다시 여는 대신 관리 루트와 파일을 안전하게 확인한 파일 스트림을 먼저 열고 custody hash를 재검증하므로, 검증 뒤 파일을 바꾸거나 경로를 심은 경우에는 전송하지 않습니다. Windows에서는 응답이 끝날 때까지 열린 파일에 대한 교체도 운영체제가 막습니다.

`hwpx_command`는 `context`, `selection_proof`, `readback`, `cell_format_exact`, `page_number_apply`, `official_numbering_apply`, `paragraph_insert_exact`, `paragraph_remove_exact`, `character_format_exact`, `paragraph_format_exact`, `undo`, `redo`, `command_reconcile`만 받습니다. `page_number_apply`는 `position`(`top-left`, `top-center`, `top-right`, `bottom-left`, `bottom-center`, `bottom-right`)과 `confirm_layout=true`를 요구하고, 선택 항목으로 `start_number`(1~65535, 기본 1), `side_char`(`none` 또는 `dash`, 기본 `none`)를 받습니다. 앞·뒤 문구는 네이티브 대시(`-`) 장식(`dash`)만 지원하고 임의의 앞·뒤 문구와 머리말·꼬리말 확인 우회 항목은 받지 않습니다. 실행 중인 문서의 메모리 상 HWPML2X 구조 스냅샷(본문, 글자·문단·표·그림 서식, 머리말·꼬리말, 스타일, 번호 정의, 바이너리 데이터 참조)을 읽을 수 있을 때만 변경하고, 읽을 수 없으면 변경 전에 거부하며, 실행 후 새 쪽 번호 컨트롤과 그 설정 외의 구조가 달라지거나 스냅샷을 읽을 수 없으면 정리(되돌리기) 없이 결과 불명으로 보고합니다(inserted로 보고하지 않음). `official_numbering_apply`는 `match`, `expected_page`, `level`(1~4), `confirm_layout=true`를 모두 요구하며 일치하는 문단 하나에 네이티브 개요 번호 수준을 적용합니다. 현재 구역의 개요 번호 정의가 `1.` → `가.` → `1)` → `가)` 4수준과 일치하는지 읽어서 확인하고, 일치하지 않으면 그 구역에 다른 개요 문단이 없다는 것을 증명할 수 있을 때만 4수준을 정의하며, 확인하지 못한 형식은 성공으로 보고하지 않습니다. 이미 확인된 정의가 있으면 네이티브 ParagraphShape 동작 한 번(HeadingType, Level)으로 적용하고, 같은 구조 스냅샷을 변경 전에 읽을 수 없거나 대상 문단을 하나로 식별할 수 없으면 변경 전에 거부하며, 변경 후 대상 문단의 번호 외 구조가 달라지면 결과 불명으로 보고합니다. 두 명령 모두 자동 저장하지 않으며, 저장 전에 렌더링 증명이 필요합니다. 두 동작은 보호된 대화형 `command_bundle`(CLI/MCP)과 호출자가 SDK 수명을 소유하는 라이브러리에서만 지원합니다. 대기열 편집 작업(`/edit-and-convert`, `/edit-and-convert-from-path`, 작업자 `edit_and_convert_with_pyhwpx`)은 실패 시 SDK를 종료하고 COM을 해제하므로 결과 불명 상태에서 문서를 유지할 수 없어, `page_number`/`official_numbering` 작업 연산이 하나라도 있으면 `edit_and_convert_with_pyhwpx`가 자신의 COM 초기화·SDK 생성·열기·편집 이전에 작업 전체를 거부합니다(`/capabilities`의 `queued_job_unsupported_ops`). 다만 이를 감싸는 `process_one_command`는 그 전에 네이티브 내보내기 사전 점검을 수행하며, 이 점검이 빈 탐침 인스턴스를 생성하고 종료하므로 자식 프로세스 전체가 SDK 작업을 전혀 하지 않는다는 뜻은 아닙니다. 그러므로 API·CLI·MCP 동일 입력 동등성은 충족되지 않습니다. 또한 결과 불명이 된 뒤에는 캐럿을 이동하지 않으며, 자동 Undo·닫기·종료를 하지 않습니다. `page_number_apply`와 `official_numbering_apply`에서 네이티브 변경 호출(`PageNumPos`, `OutlineNumber`, `ParagraphShape`)이 시작된 뒤 발생한 일반 예외(읽기·집계·비교 오류 포함)는 원인을 유지한 채 결과 불명(`mutation_may_have_persisted=true`)으로 보고합니다. `KeyboardInterrupt`·`SystemExit` 같은 종료 신호는 호출자가 소유한 라이브러리에서는 삼키지 않고 그대로 전파합니다. 정확한 내장 종료 신호에만 호환용 결과 불명 표시를 붙이고 사용자 하위 클래스의 속성은 읽거나 변경하지 않습니다. 소유 런타임의 trusted producer intent가 결과 불명과 STA 보존을 별도로 결정합니다. 보호된 라이브러리 소유 실행 경계(실시간 세션의 소유 스레드에서 실행되는 `command_bundle`)에서는 이런 결과 불명(반환된 결과든 종료 신호든)이 해당 세션을 보류 상태로 만듭니다. 명령은 `failed`(`may_have_mutated=true`, 원래 신호 종류는 원인 체인에 보존)로 기록되고, 호출자는 `ok=false`, `dirty=true`, `native_custody_held=true`인 미완료 결과를 받으며, 같은 소유 스레드와 세션 등록은 유지됩니다. 이 경로에서는 Clear·Quit·CoUninitialize, 캐럿 이동, Undo, 저장·내보내기, 재시도, 새 네이티브 호출을 하지 않고, 시간 초과로 표시하지도 않으며, 이후의 실행·닫기(강제 포함)·정리·증거 없는 reconcile/acknowledge는 보류를 해제하지 못합니다. 상태 조회는 읽기 전용으로 남습니다. 운영자 승인에 의한 해제 절차는 이 저장소에 구현되어 있지 않으므로 보류된 세션은 해당 프로세스가 끝날 때까지 유지됩니다. Linux 어댑터 테스트는 어댑터 계약만 확인합니다. `cell_format_exact`는 기존 대상 ID, 해시, 페이지, 구역 앵커와 `confirm_layout=true`를 요구하며, 형식 선택자는 `vertical_align`, `cell_margin_mm`, `cell_margin_hu` 중 하나만 지정합니다. 셀 여백 변경은 선언된 단위로 네 변(left/right/top/bottom)을 모두 명시한 단일 네이티브 호출을 사용하고, 요청에서 독립적으로 계산한 네 변의 값과 네이티브 전후 getter가 정확히 일치해야 성공으로 기록됩니다. 유효하지 않거나 신선하지 않은 네이티브 getter, 같은 값을 다시 요청한 no-op, 요청값과 다른 여백은 거부되며, 동작 후 판정 실패는 변경 가능성을 보존합니다. 셀 정렬은 별도의 네이티브 전후 readback을 확보하지 못하면 성공으로 기록하지 않습니다. `pyhwpx_call`, `hwp_action`, 임의 Python·셸·명령 이름은 허용하지 않습니다. 이 어댑터는 기존 비변경용 `safe-schema` 자체를 확장하거나 쓰기 권한으로 해석하지 않습니다.

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
