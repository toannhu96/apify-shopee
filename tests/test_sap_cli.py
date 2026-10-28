import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

GOLDEN = json.loads((Path(__file__).parent / 'fixtures/sap_golden.json').read_text())


def run_cli(command, text):
    return subprocess.run([sys.executable, '-m', 'shopee_affiliate.sap', command],
        input=text, capture_output=True, text=True, timeout=5)


def test_reconstruct_matches_original_sdk():
    request = {'ri': GOLDEN['headers']['x-sap-ri'],
        'payload_base64': base64.b64encode(bytes.fromhex(GOLDEN['payload_hex'])).decode()}
    result = run_cli('reconstruct', json.dumps(request))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == GOLDEN['headers']


def test_inspect_reports_metadata_without_payload_or_headers():
    request = {'ri': GOLDEN['headers']['x-sap-ri'], 'sec': GOLDEN['headers']['x-sap-sec']}
    result = run_cli('inspect', json.dumps(request))
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['checksum_valid'] is True
    assert data['payload_bytes'] == len(bytes.fromhex(GOLDEN['payload_hex']))
    assert data['version'] == 20190001
    assert data['flatbuffer']['field_count'] == 57
    assert request['ri'] not in result.stdout
    assert request['sec'] not in result.stdout


@pytest.mark.parametrize('text', ['private-value', '[]', '{}', '{"ri":"private-value"}',
    '{"ri":"one","ri":"two","payload_base64":""}',
    '{"ri":NaN,"payload_base64":""}', ' ' * 131073])
def test_cli_rejects_bad_input_without_echoing(text):
    result = run_cli('reconstruct', text)
    assert result.returncode == 1
    assert not result.stdout
    assert 'private-value' not in result.stderr
    assert 'Traceback' not in result.stderr


def test_build_from_fields_matches_original_sdk_end_to_end():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_payload_golden.json').read_text())
    request = {'ri': fixture['headers']['x-sap-ri'],
               'vectors': fixture['vectors'], 'fields': fixture['fields']}
    result = run_cli('build', json.dumps(request))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == fixture['headers']


@pytest.mark.parametrize('command', ['build', 'reconstruct', 'inspect'])
def test_main_private_file(tmp_path, capsys, command):
    from shopee_affiliate.sap import main
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_payload_golden.json').read_text())
    values = {
        'build': {'ri': fixture['headers']['x-sap-ri'], 'fields': fixture['fields'], 'vectors': fixture['vectors']},
        'reconstruct': {'ri': fixture['headers']['x-sap-ri'], 'payload_base64': base64.b64encode(bytes.fromhex(fixture['expected_payload_hex'])).decode()},
        'inspect': {'ri': fixture['headers']['x-sap-ri'], 'sec': fixture['headers']['x-sap-sec']},
    }
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(values[command]))
    assert main([command, '--input', str(path)]) == 0
    result = capsys.readouterr()
    assert not result.err
    assert json.loads(result.out) == (fixture['headers'] if command != 'inspect' else {
        'version': 20190001, 'checksum_valid': True,
        'payload_bytes': len(bytes.fromhex(fixture['expected_payload_hex'])),
        'timestamp_seconds': int.from_bytes(bytes.fromhex(fixture['headers']['x-sap-ri'])[:4], 'little'),
        'ri_protocol_version': 3, 'flatbuffer': {'root_offset': 124, 'field_count': 57, 'object_size': 172}})


@pytest.mark.parametrize('raw', [b'private-value', b'[]', b'{"ri":1,"ri":2}',
    b'{"ri":NaN}', b'\xff', b' ' * 131073])
def test_main_invalid_stdin(monkeypatch, capsys, raw):
    from io import BytesIO
    from types import SimpleNamespace
    from shopee_affiliate import sap
    monkeypatch.setattr(sap.sys, 'stdin', SimpleNamespace(buffer=BytesIO(raw)))
    assert sap.main(['build']) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert 'private-value' not in captured.err


def test_unreadable_input(tmp_path, capsys):
    from shopee_affiliate.sap import main
    assert main(['build', '--input', str(tmp_path / 'missing.json')]) == 1
    assert not capsys.readouterr().out
