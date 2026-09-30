"""설정(YAML) 로드·검증 테스트."""
import shutil
from pathlib import Path

import pytest
import yaml

from config_sync.loader import DEFAULT_CONFIG_DIR, ConfigError, load_config
from config_sync.validate import validate_bundle
from stats_engine.formula import parse

EXPECTED_SPORTS = {"baseball", "basketball", "volleyball", "football"}
EXPECTED_LEAGUES = {"KBO", "KBL", "VLEAGUE_M", "VLEAGUE_W", "KLEAGUE1"}


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, dst)
    return dst


def _edit_sport(config_dir: Path, sport: str, mutate) -> None:
    path = config_dir / "sports" / f"{sport}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_repository_config_is_valid():
    bundle = load_config(DEFAULT_CONFIG_DIR)
    validate_bundle(bundle)
    assert set(bundle.sports) == EXPECTED_SPORTS
    assert set(bundle.leagues) == EXPECTED_LEAGUES
    # 수집 허용은 약관·robots.txt 를 확인해 기록한 소스만 (현재 KBO 의 GitHub 공개 데이터 두 곳)
    allowed = {s.code for s in bundle.sources.values() if s.collection_allowed}
    assert allowed == {"kbo_gh_schedule", "kbo_gh_stats"}
    assert all(bundle.sources[c].terms_note and "확인" in bundle.sources[c].terms_note for c in allowed)


def test_required_metrics_are_defined():
    """요구사항에 명시된 대표 지표가 정의되어 있는지."""
    bundle = load_config(DEFAULT_CONFIG_DIR)
    required = {
        "baseball": {"bat.AVG", "bat.OPS", "pit.ERA", "pit.WHIP", "pit.FIP", "bat.WOBA"},
        "basketball": {"PTS", "REB", "AST", "FG_PCT", "FG3_PCT", "TS_PCT", "PER", "PM"},
        "volleyball": {"ATK_PCT", "BLK_PT", "SRV_ACE", "RCV_EFF", "DIG_SUC"},
        "football": {"GLS", "AST", "SHT", "SOT", "PAS_PCT", "TKL", "MIN", "XG"},
    }
    for sport, codes in required.items():
        defined = {s.code for s in bundle.sports[sport].stats}
        assert codes <= defined, f"{sport}: 누락 {codes - defined}"


def test_defaults_are_resolved():
    bundle = load_config(DEFAULT_CONFIG_DIR)
    stats = {s.code: s for s in bundle.sports["baseball"].stats}
    avg = stats["bat.AVG"]
    assert avg.category == "bat" and avg.is_derived and avg.aggregation == "formula"
    assert avg.decimals == 3 and avg.depends_on == ("bat.AB", "bat.H")
    hr = stats["bat.HR"]
    assert not hr.is_derived and hr.aggregation == "sum" and hr.levels == ("match", "season")
    assert stats["std.W"].scope == "team" and stats["std.W"].levels == ("season",)


def test_derived_stats_evaluate_with_sample_box_score():
    """시드 수식이 실제 값으로 계산 가능한지 (계산 순서대로 평가)."""
    bundle = load_config(DEFAULT_CONFIG_DIR)
    stats = {s.code: s for s in bundle.sports["baseball"].stats}
    values = {"bat.PA": 5, "bat.AB": 4, "bat.H": 2, "bat.DBL": 1, "bat.TPL": 0, "bat.HR": 1,
              "bat.BB": 1, "bat.IBB": 0, "bat.HBP": 0, "bat.SF": 0, "bat.SO": 1}
    for code in ("bat.TB", "bat.AVG", "bat.OBP", "bat.SLG", "bat.OPS"):
        values[code] = parse(stats[code].formula).evaluate(values)
    assert values["bat.TB"] == 6  # 2루타 1 + 홈런 1 = 2 + 4
    assert values["bat.AVG"] == pytest.approx(0.5)
    assert values["bat.OBP"] == pytest.approx(0.6)
    assert values["bat.SLG"] == pytest.approx(1.5)
    assert values["bat.OPS"] == pytest.approx(2.1)


def _expect_error(config_dir: Path, fragment: str) -> None:
    with pytest.raises(ConfigError) as exc:
        validate_bundle(load_config(config_dir))
    assert any(fragment in e for e in exc.value.errors), exc.value.errors


def test_unknown_stat_reference(config_copy):
    _edit_sport(config_copy, "baseball", lambda d: d["stats"].append(
        {"code": "bat.XXX", "name_ko": "테스트", "formula": "bat.NOPE / bat.AB"}))
    _expect_error(config_copy, "정의되지 않은 지표 bat.NOPE")


def test_cycle_detection(config_copy):
    def mutate(d):
        d["stats"] += [
            {"code": "bat.CA", "name_ko": "A", "formula": "bat.CB + 1"},
            {"code": "bat.CB", "name_ko": "B", "formula": "bat.CA + 1"},
        ]
    _edit_sport(config_copy, "baseball", mutate)
    _expect_error(config_copy, "순환 참조")


def test_level_mismatch(config_copy):
    # 시즌 전용 지표를 경기 레벨 지표가 참조하면 오류
    _edit_sport(config_copy, "basketball", lambda d: d["stats"].append(
        {"code": "BAD", "category": "sco", "name_ko": "테스트", "formula": "PTS_PG * 2"}))
    _expect_error(config_copy, "레벨 값이 없습니다")


def test_undeclared_league_constant(config_copy):
    _edit_sport(config_copy, "baseball", lambda d: d["stats"].append(
        {"code": "pit.BAD", "name_ko": "테스트", "formula": "pit.ER + lg.UNKNOWN"}))
    _expect_error(config_copy, "정의되지 않은 리그 상수 lg.UNKNOWN")


def test_prefix_must_match_category(config_copy):
    _edit_sport(config_copy, "baseball", lambda d: d["stats"].append(
        {"code": "bat.ZZ", "category": "pit", "name_ko": "테스트"}))
    _expect_error(config_copy, "접두어와 category")


def test_unknown_context_variable(config_copy):
    _edit_sport(config_copy, "baseball", lambda d: d["qualification_rules"].append(
        {"code": "bad_rule", "category": "bat", "name_ko": "테스트", "rule": "bat.PA > ctx.NOPE"}))
    _expect_error(config_copy, "ctx.NOPE")


def test_unknown_yaml_key_is_rejected(config_copy):
    _edit_sport(config_copy, "volleyball", lambda d: d["stats"][0].update({"higer_is_better": False}))
    _expect_error(config_copy, "higer_is_better")


def test_league_schedule_validation(config_copy):
    path = config_copy / "leagues" / "kbo.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["schedules"][0]["cron"] = "every day"
    data["schedules"][1]["source"] = "nowhere"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        validate_bundle(load_config(config_copy))
    joined = "\n".join(exc.value.errors)
    assert "cron 형식 오류" in joined and "소스 nowhere" in joined
