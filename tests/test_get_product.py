from unittest.mock import patch

import pytest

import get_product


def test_cli_forwards_ids_and_browser_options():
    with patch("get_product.probe.main", return_value=0) as run:
        assert get_product.main([
            "26609048170", "123", "--profile-dir", "/tmp/test-profile",
            "--out", "/tmp/test-output", "--headless",
        ]) == 0
    arguments = run.call_args.args[0]
    parsed = get_product.probe.build_parser().parse_args(arguments)
    assert parsed.command == "detail"
    assert parsed.item_id == ["26609048170", "123"]
    assert parsed.profile_dir == "/tmp/test-profile"
    assert parsed.out == "/tmp/test-output"
    assert parsed.headless


@pytest.mark.parametrize("status", [1, 2, 3])
def test_cli_preserves_failure_exit_status(status):
    with patch("get_product.probe.main", return_value=status):
        assert get_product.main(["26609048170"]) == status


def test_cli_requires_product_id():
    with pytest.raises(SystemExit) as exc:
        get_product.main([])
    assert exc.value.code == 2
