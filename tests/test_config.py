import pytest

from searchiqs_scraper.config import Config, ConfigError, load_config


def test_defaults_are_valid():
    config = load_config({})
    assert config.max_pages == 1000 and config.delay_min == 1.0 and config.google_sheet_id is None


def test_env_overrides():
    config = load_config({
        "SEARCHIQS_DELAY_MIN": "0.5",
        "SEARCHIQS_MAX_PAGES": "20",
        "SEARCHIQS_OUTPUT_DIR": "out",
        "GOOGLE_SHEET_ID": " abc ",
    })
    assert config.delay_min == 0.5 and config.max_pages == 20
    assert str(config.output_dir) == "out" and config.google_sheet_id == "abc"


@pytest.mark.parametrize(
    "env",
    [
        {"SEARCHIQS_DELAY_MIN": "nan"},
        {"SEARCHIQS_REQUEST_TIMEOUT": "inf"},
        {"SEARCHIQS_CONNECT_TIMEOUT": "0"},
        {"SEARCHIQS_RUN_DEADLINE": "-5"},
        {"SEARCHIQS_MAX_PAGES": "0"},
        {"SEARCHIQS_MAX_PAGES": "2.5"},
        {"SEARCHIQS_READ_ATTEMPTS": "three"},
        {"SEARCHIQS_DELAY_MIN": "4"},
        {"SEARCHIQS_BACKOFF_BASE": "90"},
        {"SEARCHIQS_CONNECT_TIMEOUT": "90"},
    ],
)
def test_invalid_values_rejected(env):
    with pytest.raises(ConfigError):
        load_config(env)


def test_redacted_hides_google_settings(tmp_path):
    creds = tmp_path / "sa.json"
    creds.write_text("{}")
    shown = Config(google_credentials=creds, google_sheet_id="secret-id").redacted()
    assert shown["google_credentials"] == "set" and shown["google_sheet_id"] == "set"
    assert "secret-id" not in repr(shown) and str(tmp_path) not in repr(shown)


def test_require_sheets(tmp_path):
    with pytest.raises(ConfigError, match="GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID"):
        Config().require_sheets()
    with pytest.raises(ConfigError, match="does not point to a file"):
        Config(google_credentials=tmp_path / "absent.json", google_sheet_id="x").require_sheets()
    creds = tmp_path / "sa.json"
    creds.write_text("{}")
    Config(google_credentials=creds, google_sheet_id="x").require_sheets()


def test_dotenv_file_is_read(clean_env):
    (clean_env / ".env").write_text("SEARCHIQS_MAX_PAGES=7\n")
    assert load_config().max_pages == 7
