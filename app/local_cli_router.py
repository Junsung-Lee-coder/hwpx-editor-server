from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import quote_from_bytes

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.models import CellMarginsGetRequest
from app.local_cli_service import LocalCliService, as_http_error


class LocalCliFindRequest(BaseModel):
    query: str
    session_id: str | None = None
    around: int = Field(default=0, ge=0, le=5)
    with_page: bool = False
    proof_match: int | None = Field(default=None, ge=1)


class LocalCliInfoRequest(BaseModel):
    target: str
    session_id: str | None = None


class LocalCliTargetRequest(BaseModel):
    target: str
    session_id: str | None = None


class LocalCliDirectionRequest(BaseModel):
    direction: str
    count: int = Field(default=1, ge=1)
    session_id: str | None = None


class LocalCliTextRequest(BaseModel):
    text: str
    session_id: str | None = None
    allow_insert_at_caret: bool = False


class LocalCliAnchorInsertRequest(BaseModel):
    target: str
    text: str
    position: str = 'before-anchor'
    session_id: str | None = None


class LocalCliFigureSectionRequest(BaseModel):
    target_heading: str
    heading: str
    intro: str | None = None
    caption: str | None = None
    body: str | None = None
    session_id: str | None = None


class LocalCliReplaceRequest(BaseModel):
    target: str
    text: str
    session_id: str | None = None


class LocalCliCellReplaceRequest(BaseModel):
    anchor: str | None = None
    cell: str | None = None
    text: str | None = None
    text_file: str | None = None
    expect_cell: str | None = None
    expect_old: str | None = None
    expect_new: str | None = None
    expected_page: int | None = Field(default=None, gt=0)
    session_id: str | None = None


class LocalCliFontSizeRequest(BaseModel):
    size_pt: float = Field(gt=0)
    session_id: str | None = None


class LocalCliBoldRequest(BaseModel):
    enabled: bool
    session_id: str | None = None


class LocalCliFontRequest(BaseModel):
    face_name: str
    session_id: str | None = None


class LocalCliCloseRequest(BaseModel):
    session_id: str | None = None


class LocalCliCommandReconcileRequest(BaseModel):
    command_id: str
    session_id: str | None = None


class LocalCliTableRequest(BaseModel):
    cols: int = Field(ge=1)
    rows: int = Field(ge=1)
    session_id: str | None = None


class LocalCliListRequest(BaseModel):
    count: int = Field(ge=1)
    session_id: str | None = None


class LocalCliPyCallRequest(BaseModel):
    method_path: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


class LocalCliActionRequest(BaseModel):
    action_name: str
    session_id: str | None = None


class LocalCliCommandBundleRequest(BaseModel):
    steps: list[dict[str, Any]] = Field(default_factory=list)
    session_id: str | None = None



_RFC5987_ATTR_SAFE = "!#$&+-.^_`|~"


def _ascii_fallback_filename(filename: str) -> str:
    raw_name = str(filename or 'download')
    suffix_match = re.search(r'(\.[A-Za-z0-9]{1,32})$', raw_name)
    suffix = suffix_match.group(1) if suffix_match else ''
    stem = raw_name[:-len(suffix)] if suffix else raw_name
    ascii_stem = unicodedata.normalize('NFKD', stem).encode('ascii', errors='ignore').decode('ascii')
    ascii_stem = re.sub(r'[^A-Za-z0-9!#$&+.^_`|~ -]', '_', ascii_stem)
    ascii_stem = re.sub(r'\s+', ' ', ascii_stem).strip(' .')
    if not ascii_stem or ascii_stem in {'.', '..'} or ascii_stem.startswith('.'):
        ascii_stem = 'download'
    return ascii_stem + suffix


def _content_disposition(filename: str) -> str:
    raw_name = str(filename or 'download')
    encoded_name = quote_from_bytes(raw_name.encode('utf-8'), safe=_RFC5987_ATTR_SAFE)
    return (
        f'attachment; filename="{_ascii_fallback_filename(raw_name)}"; '
        f"filename*=UTF-8''{encoded_name}"
    )


def build_local_cli_router(*, settings: Any, interactive_sessions: Any, service: LocalCliService | None = None) -> APIRouter:
    router = APIRouter()
    service = service or LocalCliService(settings=settings, interactive_sessions=interactive_sessions)

    @router.get('/local-cli/status')
    def local_cli_status() -> dict[str, Any]:
        try:
            return service.status()
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/command-reconcile')
    def local_cli_command_reconcile(request: LocalCliCommandReconcileRequest) -> dict[str, Any]:
        try:
            return service.reconcile_command(command_id=request.command_id, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/open')
    async def local_cli_open(
        file: UploadFile = File(...),
        session_label: str | None = Form(None),
    ) -> dict[str, Any]:
        try:
            return await service.open_upload(file=file, session_label=session_label)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/find')
    def local_cli_find(request: LocalCliFindRequest) -> dict[str, Any]:
        try:
            return service.find(
                query=request.query,
                session_id=request.session_id,
                around=request.around,
                with_page=request.with_page,
                proof_match=request.proof_match,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/info')
    def local_cli_info(request: LocalCliInfoRequest) -> dict[str, Any]:
        try:
            return service.info(target=request.target, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/move')
    def local_cli_move(request: LocalCliTargetRequest) -> dict[str, Any]:
        try:
            return service.move(target=request.target, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/select')
    def local_cli_select(request: LocalCliTargetRequest) -> dict[str, Any]:
        try:
            return service.select(target=request.target, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/cell')
    def local_cli_cell(request: LocalCliCloseRequest) -> dict[str, Any]:
        try:
            return service.cell(session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/cellmove')
    def local_cli_cellmove(request: LocalCliDirectionRequest) -> dict[str, Any]:
        try:
            return service.cell_move(direction=request.direction, count=request.count, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/cursormove')
    def local_cli_cursormove(request: LocalCliDirectionRequest) -> dict[str, Any]:
        try:
            return service.cursor_move(direction=request.direction, count=request.count, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/type')
    def local_cli_type(request: LocalCliTextRequest) -> dict[str, Any]:
        try:
            return service.type_text(
                text=request.text,
                session_id=request.session_id,
                allow_insert_at_caret=request.allow_insert_at_caret,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/anchor-insert')
    def local_cli_anchor_insert(request: LocalCliAnchorInsertRequest) -> dict[str, Any]:
        try:
            return service.anchor_insert(
                target=request.target,
                text=request.text,
                position=request.position,
                session_id=request.session_id,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/figure-section')
    async def local_cli_figure_section(request: LocalCliFigureSectionRequest) -> dict[str, Any]:
        try:
            return await service.figure_section(
                target_heading=request.target_heading,
                heading=request.heading,
                intro=request.intro,
                caption=request.caption,
                body=request.body,
                session_id=request.session_id,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/figure-section-image')
    async def local_cli_figure_section_image(
        image: UploadFile = File(...),
        target_heading: str = Form(...),
        heading: str = Form(...),
        intro: str | None = Form(None),
        caption: str | None = Form(None),
        body: str | None = Form(None),
        width: float | None = Form(None),
        height: float | None = Form(None),
        sizeoption: int | None = Form(None),
        treat_as_char: str | None = Form(None),
        embedded: str | None = Form(None),
        fit_cell: bool = Form(False),
        session_id: str | None = Form(None),
    ) -> dict[str, Any]:
        try:
            return await service.figure_section(
                target_heading=target_heading,
                heading=heading,
                intro=intro,
                caption=caption,
                body=body,
                image_file=image,
                width=width,
                height=height,
                sizeoption=sizeoption,
                treat_as_char=treat_as_char,
                embedded=embedded,
                fit_cell=fit_cell,
                session_id=session_id,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/image')
    async def local_cli_image(
        file: UploadFile = File(...),
        width: float | None = Form(None),
        height: float | None = Form(None),
        sizeoption: int | None = Form(None),
        treat_as_char: str | None = Form(None),
        embedded: str | None = Form(None),
        fit_cell: bool = Form(False),
        session_id: str | None = Form(None),
    ) -> dict[str, Any]:
        try:
            return await service.image_upload(
                file=file,
                width=width,
                height=height,
                sizeoption=sizeoption,
                treat_as_char=treat_as_char,
                embedded=embedded,
                fit_cell=fit_cell,
                session_id=session_id,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/image-at-anchor')
    async def local_cli_image_at_anchor(
        file: UploadFile = File(...),
        target: str = Form(...),
        position: str = Form('before-anchor'),
        width: float | None = Form(None),
        height: float | None = Form(None),
        sizeoption: int | None = Form(None),
        treat_as_char: str | None = Form(None),
        embedded: str | None = Form(None),
        fit_cell: bool = Form(False),
        session_id: str | None = Form(None),
    ) -> dict[str, Any]:
        try:
            return await service.image_upload_at_anchor(
                file=file,
                target=target,
                position=position,
                width=width,
                height=height,
                sizeoption=sizeoption,
                treat_as_char=treat_as_char,
                embedded=embedded,
                fit_cell=fit_cell,
                session_id=session_id,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/replace')
    def local_cli_replace(request: LocalCliReplaceRequest) -> dict[str, Any]:
        try:
            return service.replace(target=request.target, text=request.text, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/cell-replace')
    def local_cli_cell_replace(request: LocalCliCellReplaceRequest) -> dict[str, Any]:
        try:
            return service.cell_replace(
                anchor=request.anchor,
                cell=request.cell,
                text=request.text,
                text_file=request.text_file,
                expect_cell=request.expect_cell,
                expect_old=request.expect_old,
                expect_new=request.expect_new,
                expected_page=request.expected_page,
                session_id=request.session_id,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/fontsize')
    def local_cli_fontsize(request: LocalCliFontSizeRequest) -> dict[str, Any]:
        try:
            return service.font_size(size_pt=request.size_pt, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/bold')
    def local_cli_bold(request: LocalCliBoldRequest) -> dict[str, Any]:
        try:
            return service.bold(enabled=request.enabled, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/font')
    def local_cli_font(request: LocalCliFontRequest) -> dict[str, Any]:
        try:
            return service.font_family(face_name=request.face_name, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/bullet')
    def local_cli_bullet(request: LocalCliTextRequest) -> dict[str, Any]:
        try:
            return service.bullet(text=request.text, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/undo')
    def local_cli_undo(request: LocalCliCloseRequest) -> dict[str, Any]:
        try:
            return service.undo(session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/redo')
    def local_cli_redo(request: LocalCliCloseRequest) -> dict[str, Any]:
        try:
            return service.redo(session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/close')
    def local_cli_close(request: LocalCliCloseRequest) -> dict[str, Any]:
        try:
            return service.close(session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/table')
    def local_cli_table(request: LocalCliTableRequest) -> dict[str, Any]:
        try:
            return service.table(cols=request.cols, rows=request.rows, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/list')
    def local_cli_list(request: LocalCliListRequest) -> dict[str, Any]:
        try:
            return service.list_items(count=request.count, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/pycall')
    def local_cli_pycall(request: LocalCliPyCallRequest) -> dict[str, Any]:
        try:
            return service.pycall(
                method_path=request.method_path,
                args=request.args,
                kwargs=request.kwargs,
                session_id=request.session_id,
            )
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/action')
    def local_cli_action(request: LocalCliActionRequest) -> dict[str, Any]:
        try:
            return service.action(action_name=request.action_name, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/command-bundle')
    def local_cli_command_bundle(request: LocalCliCommandBundleRequest) -> dict[str, Any]:
        try:
            return service.command_bundle(steps=request.steps, session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/screenshot')
    def local_cli_screenshot(request: LocalCliCloseRequest) -> dict[str, Any]:
        try:
            return service.screenshot(session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/save')
    def local_cli_save(request: LocalCliCloseRequest) -> dict[str, Any]:
        try:
            return service.save(session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/export')
    def local_cli_export(request: LocalCliCloseRequest) -> dict[str, Any]:
        try:
            return service.export(session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/where')
    def local_cli_where(request: LocalCliCloseRequest) -> dict[str, Any]:
        try:
            return service.where(session_id=request.session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.post('/local-cli/cell-margins-get')
    def local_cli_cell_margins_get(request: CellMarginsGetRequest) -> dict[str, Any]:
        try:
            return service.cell_margins_get(session_id=request.session_id, request=request)
        except Exception as exc:
            raise as_http_error(exc) from exc

    @router.get('/local-cli/session/{session_id}/artifact/{kind}')
    def local_cli_artifact(session_id: str, kind: str) -> StreamingResponse:
        try:
            download = service.open_artifact(kind=kind, session_id=session_id)
        except Exception as exc:
            raise as_http_error(exc) from exc

        try:
            media_type = {
                'screenshot': 'image/png',
                'export': 'application/pdf',
            }.get(kind, 'application/octet-stream')
            headers = {'Content-Disposition': _content_disposition(download.filename)}
            size_bytes = getattr(download, 'size_bytes', None)
            if isinstance(size_bytes, int) and not isinstance(size_bytes, bool) and size_bytes >= 0:
                headers['Content-Length'] = str(size_bytes)

            def body():
                try:
                    while True:
                        chunk = download.stream.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    download.close()

            return StreamingResponse(body(), media_type=media_type, headers=headers)
        except Exception:
            try:
                download.close()
            except Exception:
                pass
            raise

    return router
