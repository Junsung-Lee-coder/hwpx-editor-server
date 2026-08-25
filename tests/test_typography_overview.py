from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.command_packages.commands.typography_overview.run import _parse_hwpml_typography, run_step, validate_step
from local_cli_v1.output_parser import format_typography_overview_human, summarize_typography_overview


SAMPLE_HWPML = '''<?xml version="1.0" encoding="UTF-16"?>
<HWPML>
  <HEAD>
    <FONTFACE Lang="Hangul"><FONT Id="0" Face="Gulim" /></FONTFACE>
    <FONTFACE Lang="Latin"><FONT Id="1" Face="Arial" /></FONTFACE>
    <CHARSHAPE Id="1" Height="1000" UseFontSpace="false" Bold="1"><FONTID Hangul="0" Latin="1" /></CHARSHAPE>
    <CHARSHAPE Id="2" Height="900" FaceNameHangul="Batang" Italic="1" />
  </HEAD>
  <BODY>
    <SECTION>
      <P><TEXT CharShape="1"><CHAR>제목 텍스트</CHAR></TEXT></P>
      <P><TEXT CharShape="2"><CHAR>본문 텍스트입니다</CHAR></TEXT></P>
    </SECTION>
  </BODY>
</HWPML>
'''


def test_parse_hwpml_typography_counts_font_size_and_bold() -> None:
    warnings: list[str] = []
    report = _parse_hwpml_typography(SAMPLE_HWPML, max_samples=10, max_sections=10, max_styles=10, warnings=warnings)

    assert report['backend'] == 'hancom_gettextfile_hwpml2x'
    assert report['summary']['total_chars'] > 0
    assert report['summary']['unique_font_families'] == 2
    assert report['summary']['unique_font_sizes'] == 2
    assert report['summary']['bold_char_ratio'] > 0
    fonts = {row['value']: row['chars'] for row in report['global_fonts']}
    assert 'Gulim' in fonts
    assert 'Batang' in fonts
    assert 'false' not in fonts
    sizes = {row['value']: row['chars'] for row in report['global_sizes']}
    assert '10.0' in sizes
    assert '9.0' in sizes
    assert report['sections'][0]['style_variant_count'] == 2


def test_parse_hwpml_typography_reports_font_size_distribution_per_font() -> None:
    hwpml = '''<?xml version="1.0" encoding="UTF-16"?>
<HWPML>
  <HEAD>
    <FONTFACE Lang="Hangul"><FONT Id="0" Face="Gulim" /><FONT Id="1" Face="HYHeadLine" /></FONTFACE>
    <CHARSHAPE Id="1" Height="1000" Bold="0"><FONTID Hangul="0" /></CHARSHAPE>
    <CHARSHAPE Id="2" Height="1400" Bold="1"><FONTID Hangul="0" /></CHARSHAPE>
    <CHARSHAPE Id="3" Height="1800" Bold="1"><FONTID Hangul="1" /></CHARSHAPE>
  </HEAD>
  <BODY>
    <SECTION>
      <P><TEXT CharShape="1"><CHAR>굴림 본문 본문</CHAR></TEXT></P>
      <P><TEXT CharShape="2"><CHAR>굴림 큰 제목</CHAR></TEXT></P>
      <P><TEXT CharShape="3"><CHAR>헤드라인 제목</CHAR></TEXT></P>
    </SECTION>
  </BODY>
</HWPML>
'''
    warnings: list[str] = []
    report = _parse_hwpml_typography(hwpml, max_samples=10, max_sections=10, max_styles=10, warnings=warnings)

    distributions = {row['font_family']: row for row in report['font_size_distribution']}
    assert set(distributions) == {'Gulim', 'HYHeadLine'}
    gulim_sizes = {row['value']: row['chars'] for row in distributions['Gulim']['sizes']}
    assert gulim_sizes == {'10.0': len('굴림 본문 본문'), '14.0': len('굴림 큰 제목')}
    assert distributions['Gulim']['size_count'] == 2
    assert distributions['Gulim']['bold_char_ratio'] > 0
    headline_sizes = {row['value']: row['chars'] for row in distributions['HYHeadLine']['sizes']}
    assert headline_sizes == {'18.0': len('헤드라인 제목')}
    assert distributions['HYHeadLine']['bold_char_ratio'] == 1.0


def test_validate_step_caps_positive_limits() -> None:
    step = validate_step(
        service=None,
        index=1,
        step={'scope': 'document', 'max_samples': 999, 'max_sections': 999, 'max_styles': 999},
        manifest={},
        error_type=ValueError,
    )

    assert step['max_samples'] == 200
    assert step['max_sections'] == 200
    assert step['max_styles'] == 200


class FakeHwp:
    def GetTextFile(self, fmt: str, option: str) -> str:
        assert fmt == 'HWPML2X'
        return SAMPLE_HWPML


class FakeService:
    def _bundle_page_evidence(self, hwp):
        return {'page': 1, 'method': 'fake'}


class FakeHandle:
    def __init__(self, root: Path):
        self.hwp = FakeHwp()
        self.session_root = root


def test_run_step_writes_remote_program_artifact(tmp_path: Path) -> None:
    result, dirty, warnings = run_step(
        service=FakeService(),
        handle=FakeHandle(tmp_path),
        step={'scope': 'document', 'max_samples': 10, 'max_sections': 10, 'max_styles': 10},
        binding=None,
        manifest={'version': 'local-cli/typography-overview/v1-package'},
    )

    assert dirty is False
    assert result['ok'] is True
    assert result['read_only'] is True
    artifact = Path(result['artifact']['path'])
    assert artifact.exists()
    assert artifact.read_text(encoding='utf-8').strip().startswith('{')
    assert warnings == result['warnings']


def test_typography_overview_output_parser_surfaces_font_size_distribution() -> None:
    raw_payload = {
        'ok': True,
        'steps': [
            {
                'op': 'typography_overview',
                'ok': True,
                'result': {
                    'ok': True,
                    'read_only': True,
                    'backend': 'hancom_gettextfile_hwpml2x',
                    'summary': {'total_chars': 20, 'total_spans': 2, 'unique_font_families': 1, 'unique_font_sizes': 2, 'unique_style_variants': 2},
                    'global_fonts': [{'value': 'Gulim', 'chars': 20, 'ratio': 1.0}],
                    'global_sizes': [{'value': '10.0', 'chars': 12, 'ratio': 0.6}, {'value': '14.0', 'chars': 8, 'ratio': 0.4}],
                    'font_size_distribution': [
                        {
                            'font_family': 'Gulim',
                            'chars': 20,
                            'ratio': 1.0,
                            'size_count': 2,
                            'bold_char_ratio': 0.4,
                            'sizes': [
                                {'value': '10.0', 'chars': 12, 'ratio': 0.6, 'document_ratio': 0.6},
                                {'value': '14.0', 'chars': 8, 'ratio': 0.4, 'document_ratio': 0.4},
                            ],
                        }
                    ],
                },
            }
        ],
    }

    summary = summarize_typography_overview(raw_payload)
    assert summary['font_size_distribution'][0]['font_family'] == 'Gulim'
    assert summary['font_size_distribution'][0]['sizes'][1]['value'] == '14.0'
    human = format_typography_overview_human(raw_payload)
    assert 'font size distribution:' in human
    assert 'Gulim: 10.0(12, 60.0%), 14.0(8, 40.0%); bold=40.0%' in human
