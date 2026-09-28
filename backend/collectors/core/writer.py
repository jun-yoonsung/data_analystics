"""정규화 번들 → core 테이블 저장.

- 외부 ID → 내부 ID 해석 (ingest.external_id_map), 매핑이 없으면 자연키로 기존 행을 찾아 연결
- 자연키 기반 upsert. 값이 바뀐 경우에만 UPDATE 하고 inserted/updated/unchanged 를 센다
- DTO 필드가 None 이면 "제공되지 않음" 으로 보고 기존 값을 유지한다
- 기록 값은 카탈로그(stat_definition)로 검증해 모르는 키는 버리고 경고를 남긴다
- 참조를 해석할 수 없는 행(없는 팀·경기 등)은 버리고 rejected 로 센다
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from collectors.core import dto as d
from stats_engine.catalog import SportCatalog

PARTITIONED_TABLES = frozenset({"core.event"})


class Rejected(Exception):
    """해당 행을 저장할 수 없음 (참조 해석 실패 등)."""


@dataclass(frozen=True)
class LeagueInfo:
    id: int
    code: str
    sport_id: int
    sport_code: str
    timezone: str
    collector_key: str | None
    settings: dict


@dataclass
class WriteStats:
    """저장 결과 집계 + 파생 계산 대상."""

    counts: Counter = field(default_factory=Counter)          # "<table>.<outcome>" → 건수
    warnings: list[str] = field(default_factory=list)
    match_ids: set[int] = field(default_factory=set)          # 기록이 바뀐 경기
    stage_ids: set[int] = field(default_factory=set)          # 기록이 바뀐 스테이지

    def add(self, table: str, outcome: str, n: int = 1) -> None:
        self.counts[f"{table}.{outcome}"] += n

    def totals(self) -> dict:
        out: Counter = Counter()
        for key, n in self.counts.items():
            out[key.rsplit(".", 1)[1]] += n
        return {**out, "by_table": dict(sorted(self.counts.items()))}

    def merge(self, other: WriteStats) -> None:
        self.counts.update(other.counts)
        self.warnings.extend(other.warnings)
        self.match_ids |= other.match_ids
        self.stage_ids |= other.stage_ids


def _sql_value(col: str, value) -> tuple[str, object]:
    """dict/list 는 jsonb 로 캐스팅."""
    if isinstance(value, (dict, list)) and col not in ("participant_roles",):
        return f"CAST(:{col} AS jsonb)", json.dumps(value, ensure_ascii=False, default=str)
    return f":{col}", value


class BundleWriter:
    def __init__(self, conn: Connection, *, league: LeagueInfo, catalog: SportCatalog,
                 source_id: int, ingest_run_id: int | None) -> None:
        self.conn = conn
        self.league = league
        self.catalog = catalog
        self.source_id = source_id
        self.ingest_run_id = ingest_run_id
        self.tz = ZoneInfo(league.timezone)
        self._ids: dict[tuple[str, str], int] = {}
        self._seasons: dict[str, int] = {}
        self._stages: dict[tuple[str, str], tuple[int, int]] = {}
        self._periods: dict[tuple[int, str, int], int] = {}
        self._match_dates: dict[int, date] = {}
        self._partitions: set[int] = set()

    # ==================================================================
    # 진입점
    # ==================================================================
    def write(self, bundle: d.NormalizedBundle) -> WriteStats:
        ws = WriteStats(warnings=list(bundle.warnings))
        steps = [
            (bundle.seasons, self._season), (bundle.stages, self._stage), (bundle.venues, self._venue),
            (bundle.teams, self._team), (bundle.players, self._player), (bundle.matches, self._match),
            (bundle.lineups, self._lineup), (bundle.team_match_stats, self._team_match_stat),
            (bundle.player_match_stats, self._player_match_stat),
            (bundle.team_season_stats, self._team_season_stat),
            (bundle.player_season_stats, self._player_season_stat),
            (bundle.standings, self._standing), (bundle.rosters, self._roster), (bundle.events, self._event),
        ]
        for items, fn in steps:
            for item in items:
                try:
                    with self.conn.begin_nested():
                        fn(item, ws)
                except Rejected as exc:
                    ws.add(type(item).__name__, "rejected")
                    ws.warnings.append(f"{type(item).__name__} 거부: {exc}")
        self._extend_season_dates(bundle)
        return ws

    # ==================================================================
    # 공통 upsert
    # ==================================================================
    def _upsert(self, ws: WriteStats, table: str, conflict: str, key: dict, data: dict,
                merge_attrs: bool = False) -> int:
        """자연키 upsert. data 의 None 값은 제외(기존 값 유지). 반환: 행 ID."""
        data = {k: v for k, v in data.items() if v is not None}
        row = {**key, **data, "source_id": self.source_id, "ingest_run_id": self.ingest_run_id}
        cols, vals, params = [], [], {}
        for c, v in row.items():
            ph, pv = _sql_value(c, v)
            cols.append(c)
            vals.append(ph)
            params[c] = pv
        cols.append("collected_at")
        vals.append("now()")

        compare = list(data)
        sets = []
        for c in compare:
            if merge_attrs and c == "attrs":
                sets.append(f"attrs = t.attrs || EXCLUDED.attrs")
            else:
                sets.append(f"{c} = EXCLUDED.{c}")
        sets += ["source_id = EXCLUDED.source_id", "ingest_run_id = EXCLUDED.ingest_run_id",
                 "collected_at = EXCLUDED.collected_at"]
        new_vals = [("t.attrs || EXCLUDED.attrs" if merge_attrs and c == "attrs" else f"EXCLUDED.{c}")
                    for c in compare]
        # 삽입/갱신 구분: 일반 테이블은 xmax = 0, 파티션 테이블은 xmax 를 쓸 수 없어
        # created_at(트랜잭션 시작 시각 기본값)이 현재 트랜잭션 시각과 같은지로 판단
        inserted_expr = "(t.created_at = now())" if table in PARTITIONED_TABLES else "(xmax = 0)"
        where = (f"WHERE ({', '.join('t.' + c for c in compare)}) IS DISTINCT FROM ({', '.join(new_vals)})"
                 if compare else "WHERE false")
        sql = (f"INSERT INTO {table} AS t ({', '.join(cols)}) VALUES ({', '.join(vals)}) "
               f"ON CONFLICT {conflict} DO UPDATE SET {', '.join(sets)} {where} "
               f"RETURNING id, {inserted_expr} AS inserted")
        res = self.conn.execute(text(sql), params).first()
        if res is not None:
            ws.add(table, "inserted" if res.inserted else "updated")
            return res.id
        ws.add(table, "unchanged")
        cond = " AND ".join(f"{c} IS NOT DISTINCT FROM :{c}" for c in key)
        return self.conn.execute(text(f"SELECT id FROM {table} WHERE {cond}"),
                                 {c: params[c] for c in key}).scalar_one()

    def _update_by_id(self, ws: WriteStats, table: str, row_id: int, data: dict,
                      merge_attrs: bool = False) -> None:
        data = {k: v for k, v in data.items() if v is not None}
        if not data:
            ws.add(table, "unchanged")
            return
        params = {"id": row_id, "source_id": self.source_id, "ingest_run_id": self.ingest_run_id}
        sets, new_vals = [], []
        for c, v in data.items():
            ph, params[c] = _sql_value(c, v)
            expr = f"attrs || {ph}" if merge_attrs and c == "attrs" else ph
            sets.append(f"{c} = {expr}")
            new_vals.append(expr)
        sql = (f"UPDATE {table} SET {', '.join(sets)}, source_id = :source_id, "
               f"ingest_run_id = :ingest_run_id, collected_at = now() "
               f"WHERE id = :id AND ({', '.join(data)}) IS DISTINCT FROM ({', '.join(new_vals)})")
        n = self.conn.execute(text(sql), params).rowcount
        ws.add(table, "updated" if n else "unchanged")

    def _insert(self, ws: WriteStats, table: str, data: dict) -> int:
        data = {k: v for k, v in data.items() if v is not None}
        params, vals = {}, []
        for c, v in data.items():
            ph, params[c] = _sql_value(c, v)
            vals.append(ph)
        sql = (f"INSERT INTO {table} ({', '.join(data)}, source_id, ingest_run_id, collected_at) "
               f"VALUES ({', '.join(vals)}, :source_id, :ingest_run_id, now()) RETURNING id")
        ws.add(table, "inserted")
        return self.conn.execute(text(sql), {**params, "source_id": self.source_id,
                                             "ingest_run_id": self.ingest_run_id}).scalar_one()

    # ==================================================================
    # 외부 ID 매핑
    # ==================================================================
    def _lookup(self, entity_type: str, external_id: str) -> int | None:
        key = (entity_type, external_id)
        if key in self._ids:
            return self._ids[key]
        found = self.conn.execute(text("""
            UPDATE ingest.external_id_map SET last_seen_at = now()
            WHERE source_id = :s AND entity_type = :t AND external_id = :e RETURNING entity_id
        """), {"s": self.source_id, "t": entity_type, "e": external_id}).scalar()
        if found is not None:
            self._ids[key] = found
        return found

    def _map(self, entity_type: str, external_id: str, entity_id: int, confidence: str = "exact") -> None:
        self.conn.execute(text("""
            INSERT INTO ingest.external_id_map (source_id, entity_type, external_id, entity_id, confidence)
            VALUES (:s, :t, :e, :id, :c)
            ON CONFLICT (source_id, entity_type, external_id)
            DO UPDATE SET entity_id = EXCLUDED.entity_id, last_seen_at = now()
        """), {"s": self.source_id, "t": entity_type, "e": external_id, "id": entity_id, "c": confidence})
        self._ids[(entity_type, external_id)] = entity_id

    def _require(self, entity_type: str, external_id: str | None) -> int | None:
        if external_id is None:
            return None
        found = self._lookup(entity_type, external_id)
        if found is None:
            raise Rejected(f"{entity_type} 외부 ID '{external_id}' 를 찾을 수 없습니다")
        return found

    def _unmapped_candidates(self, table: str, entity_type: str, where: str, params: dict) -> list[int]:
        """이 소스에서 아직 매핑되지 않은 기존 엔티티 중 조건에 맞는 것."""
        return list(self.conn.execute(text(f"""
            SELECT x.id FROM {table} x
            WHERE {where} AND NOT EXISTS (
                SELECT 1 FROM ingest.external_id_map m
                WHERE m.source_id = :src AND m.entity_type = :etype AND m.entity_id = x.id)
            ORDER BY x.id
        """), {**params, "src": self.source_id, "etype": entity_type}).scalars())

    # ==================================================================
    # 리그 계층
    # ==================================================================
    def _season_id(self, label: str, ws: WriteStats | None = None) -> int:
        if label not in self._seasons:
            found = self.conn.execute(text("SELECT id FROM core.season WHERE league_id = :l AND label = :lb"),
                                      {"l": self.league.id, "lb": label}).scalar()
            if found is None:
                # 번들에 시즌 정의가 없어도 참조되면 생성 (시작 연도 = 라벨 앞 4자리)
                if not label[:4].isdigit():
                    raise Rejected(f"시즌 '{label}' 이 없고 라벨로 시작 연도를 알 수 없습니다")
                found = self.conn.execute(text("""
                    INSERT INTO core.season (league_id, label, start_year, source_id, collected_at)
                    VALUES (:l, :lb, :y, :s, now()) RETURNING id
                """), {"l": self.league.id, "lb": label, "y": int(label[:4]), "s": self.source_id}).scalar_one()
                if ws:
                    ws.add("core.season", "inserted")
            self._seasons[label] = found
        return self._seasons[label]

    def _stage_ids(self, season_label: str, stage_code: str) -> tuple[int, int]:
        key = (season_label, stage_code)
        if key not in self._stages:
            season_id = self._season_id(season_label)
            stage_id = self.conn.execute(text(
                "SELECT id FROM core.competition_stage WHERE season_id = :s AND code = :c"),
                {"s": season_id, "c": stage_code}).scalar()
            if stage_id is None:
                raise Rejected(f"스테이지 {season_label}/{stage_code} 가 정의되지 않았습니다")
            self._stages[key] = (stage_id, season_id)
        return self._stages[key]

    def _season(self, s: d.SeasonDTO, ws: WriteStats) -> None:
        sid = self._upsert(ws, "core.season", "(league_id, label)", {"league_id": self.league.id, "label": s.label},
                           {"start_year": s.start_year, "start_date": s.start_date, "end_date": s.end_date,
                            "status": s.status})
        self._seasons[s.label] = sid

    def _stage(self, s: d.StageDTO, ws: WriteStats) -> None:
        season_id = self._season_id(s.season_label, ws)
        stage_id = self._upsert(ws, "core.competition_stage", "(season_id, code)",
                                {"season_id": season_id, "code": s.code},
                                {"name_ko": s.name_ko, "stage_type": s.stage_type, "start_date": s.start_date,
                                 "end_date": s.end_date, "sort_order": s.sort_order})
        self._stages[(s.season_label, s.code)] = (stage_id, season_id)

    def _extend_season_dates(self, bundle: d.NormalizedBundle) -> None:
        """경기 일정으로 시즌 기간을 넓힌다 (비시즌 판단 기준)."""
        labels = {m.season_label for m in bundle.matches}
        for label in labels:
            if label in self._seasons:
                self.conn.execute(text("""
                    UPDATE core.season s SET
                        start_date = LEAST(coalesce(s.start_date, x.min_d), x.min_d),
                        end_date   = GREATEST(coalesce(s.end_date, x.max_d), x.max_d)
                    FROM (SELECT min(local_date) AS min_d, max(local_date) AS max_d
                          FROM core.match WHERE season_id = :sid) x
                    WHERE s.id = :sid AND x.min_d IS NOT NULL
                      AND (s.start_date IS DISTINCT FROM LEAST(coalesce(s.start_date, x.min_d), x.min_d)
                           OR s.end_date IS DISTINCT FROM GREATEST(coalesce(s.end_date, x.max_d), x.max_d))
                """), {"sid": self._seasons[label]})

    def _ensure_season_team(self, season_id: int, team_id: int) -> None:
        self.conn.execute(text("""
            INSERT INTO core.season_team (season_id, team_id, source_id, collected_at)
            VALUES (:s, :t, :src, now()) ON CONFLICT DO NOTHING
        """), {"s": season_id, "t": team_id, "src": self.source_id})

    # ==================================================================
    # 엔티티
    # ==================================================================
    def _mapped_entity(self, ws: WriteStats, entity_type: str, table: str, external_id: str, data: dict,
                       find_existing) -> int:
        entity_id = self._lookup(entity_type, external_id)
        if entity_id is not None:
            self._update_by_id(ws, table, entity_id, data, merge_attrs=True)
            return entity_id
        existing, confidence = find_existing()
        if existing is not None:
            self._map(entity_type, external_id, existing, confidence)
            self._update_by_id(ws, table, existing, data, merge_attrs=True)
            return existing
        entity_id = self._insert(ws, table, data)
        self._map(entity_type, external_id, entity_id)
        return entity_id

    def _venue(self, v: d.VenueDTO, ws: WriteStats) -> None:
        def find():
            ids = self._unmapped_candidates("core.venue", "venue", "x.name_ko = :n", {"n": v.name_ko})
            return (ids[0], "fuzzy") if len(ids) == 1 else (None, None)
        self._mapped_entity(ws, "venue", "core.venue", v.external_id,
                            {"name_ko": v.name_ko, "name_en": v.name_en, "city": v.city,
                             "capacity": v.capacity, "attrs": v.attrs}, find)

    def _team(self, t: d.TeamDTO, ws: WriteStats) -> None:
        def find():
            ids = self._unmapped_candidates("core.team", "team", "x.sport_id = :sp AND x.name_ko = :n",
                                            {"sp": self.league.sport_id, "n": t.name_ko})
            return (ids[0], "fuzzy") if len(ids) == 1 else (None, None)
        self._mapped_entity(ws, "team", "core.team", t.external_id,
                            {"sport_id": self.league.sport_id, "name_ko": t.name_ko, "name_en": t.name_en,
                             "short_name_ko": t.short_name_ko, "code": t.code, "city": t.city,
                             "attrs": t.attrs}, find)

    def _player(self, p: d.PlayerDTO, ws: WriteStats) -> None:
        def find():
            if p.birth_date is not None:
                ids = self._unmapped_candidates(
                    "core.player", "player", "x.sport_id = :sp AND x.name_ko = :n AND x.birth_date = :b",
                    {"sp": self.league.sport_id, "n": p.name_ko, "b": p.birth_date})
                if len(ids) == 1:
                    return ids[0], "fuzzy"
            # 동명이인 가능성: 새 선수로 만들고 검토 대기열에 올린다
            same_name = self._unmapped_candidates("core.player", "player", "x.sport_id = :sp AND x.name_ko = :n",
                                                  {"sp": self.league.sport_id, "n": p.name_ko})
            if same_name:
                self.conn.execute(text("""
                    INSERT INTO ingest.id_match_candidate
                        (source_id, entity_type, external_id, payload, candidate_ids, ingest_run_id)
                    VALUES (:s, 'player', :e, CAST(:p AS jsonb), :c, :r)
                    ON CONFLICT (source_id, entity_type, external_id) DO NOTHING
                """), {"s": self.source_id, "e": p.external_id, "p": p.model_dump_json(),
                       "c": same_name, "r": self.ingest_run_id})
                ws.warnings.append(f"선수 '{p.name_ko}'({p.external_id}) 동명이인 후보 {len(same_name)}명 — 검토 대기열 등록")
            return None, None
        self._mapped_entity(ws, "player", "core.player", p.external_id,
                            {"sport_id": self.league.sport_id, "name_ko": p.name_ko, "name_en": p.name_en,
                             "birth_date": p.birth_date, "nationality": p.nationality, "height_cm": p.height_cm,
                             "weight_kg": p.weight_kg, "attrs": p.attrs}, find)

    # ==================================================================
    # 경기
    # ==================================================================
    def _match(self, m: d.MatchDTO, ws: WriteStats) -> None:
        stage_id, season_id = self._stage_ids(m.season_label, m.stage_code)
        home = self._require("team", m.home_team_external_id)
        away = self._require("team", m.away_team_external_id)
        venue = self._require("venue", m.venue_external_id)
        local_date = m.scheduled_at.astimezone(self.tz).date()
        winner = m.winner
        if winner is None and m.status == "final" and m.home_score is not None and m.away_score is not None:
            winner = "home" if m.home_score > m.away_score else "away" if m.home_score < m.away_score else "draw"
        data = {
            "season_id": season_id, "stage_id": stage_id, "home_team_id": home, "away_team_id": away,
            "venue_id": venue, "scheduled_at": m.scheduled_at, "local_date": local_date,
            "game_number": m.game_number, "round_label": m.round_label, "status": m.status,
            "home_score": m.home_score, "away_score": m.away_score, "winner": winner,
            "result_type": m.result_type, "attendance": m.attendance, "duration_sec": m.duration_sec,
            "started_at": m.started_at, "ended_at": m.ended_at, "attrs": m.attrs,
        }

        def find():
            found = self.conn.execute(text("""
                SELECT id FROM core.match WHERE stage_id = :st AND home_team_id = :h AND away_team_id = :a
                AND local_date = :ld AND game_number = :g"""),
                {"st": stage_id, "h": home, "a": away, "ld": local_date, "g": m.game_number}).scalar()
            return (found, "exact") if found is not None else (None, None)

        match_id = self._mapped_entity(ws, "match", "core.match", m.external_id, data, find)
        self._match_dates[match_id] = local_date
        self._ensure_season_team(season_id, home)
        self._ensure_season_team(season_id, away)

        if m.periods is not None:
            for p in m.periods:
                self._period_id(match_id, p.period, ws, {
                    "home_score": p.home_score, "away_score": p.away_score,
                    "duration_sec": p.duration_sec, "stats": p.stats or None})

    def _period_id(self, match_id: int, ref: d.PeriodRef, ws: WriteStats, data: dict | None = None) -> int:
        key = (match_id, ref.code, ref.seq)
        if key in self._periods and data is None:
            return self._periods[key]
        info = self.catalog.periods.get(ref.code)
        if info is None:
            raise Rejected(f"정의되지 않은 구간 코드 {ref.code}")
        # ordinal: 구간 정의 순서 → 순번 (Q1~Q4 다음 OT1 = 5)
        existing = self.conn.execute(text("""
            SELECT pd.code, mp.seq FROM core.match_period mp
            JOIN config.period_definition pd ON pd.id = mp.period_def_id WHERE mp.match_id = :m
        """), {"m": match_id}).all()
        keys = sorted({(c, s) for c, s in existing} | {(ref.code, ref.seq)},
                      key=lambda cs: self.catalog.period_ordinal_key(*cs))
        ordinal = keys.index((ref.code, ref.seq)) + 1
        period_id = self._upsert(ws, "core.match_period", "(match_id, period_def_id, seq)",
                                 {"match_id": match_id, "period_def_id": info.id, "seq": ref.seq},
                                 {"ordinal": ordinal, **(data or {})})
        self._periods[key] = period_id
        return period_id

    def _stats(self, stats: dict, level: str, subject: str, ws: WriteStats, where: str) -> dict:
        clean, problems = self.catalog.validate_stats(stats, level, subject)
        for p in problems:
            ws.warnings.append(f"{where}: {p}")
        return clean

    def _position(self, code: str | None, ws: WriteStats, where: str) -> str | None:
        if code is None or code in self.catalog.positions:
            return code
        ws.warnings.append(f"{where}: 정의되지 않은 포지션 {code} (무시)")
        return None

    def _lineup(self, x: d.LineupDTO, ws: WriteStats) -> None:
        match_id = self._require("match", x.match_external_id)
        where = f"lineup {x.match_external_id}/{x.player_external_id}"
        self._upsert(ws, "core.match_lineup", "(match_id, player_id)",
                     {"match_id": match_id, "player_id": self._require("player", x.player_external_id)},
                     {"team_id": self._require("team", x.team_external_id), "is_starter": x.is_starter,
                      "order_no": x.order_no, "position_code": self._position(x.position_code, ws, where),
                      "shirt_no": x.shirt_no, "attrs": x.attrs})

    def _player_match_stat(self, x: d.PlayerMatchStatDTO, ws: WriteStats) -> None:
        match_id = self._require("match", x.match_external_id)
        where = f"player_match_stat {x.match_external_id}/{x.player_external_id}"
        period_id = self._period_id(match_id, x.period, ws) if x.period else None
        stats = self._stats(x.stats, "period" if x.period else "match", "player", ws, where)
        if not stats:
            raise Rejected(f"{where}: 유효한 지표가 없습니다")
        before = ws.counts["core.player_match_stat.unchanged"]
        self._upsert(ws, "core.player_match_stat", "ON CONSTRAINT uq_player_match_stat",
                     {"match_id": match_id, "player_id": self._require("player", x.player_external_id),
                      "period_id": period_id},
                     {"team_id": self._require("team", x.team_external_id),
                      "position_code": self._position(x.position_code, ws, where),
                      "is_starter": x.is_starter, "stats": stats})
        if ws.counts["core.player_match_stat.unchanged"] == before:
            self._mark_match_changed(match_id, ws)

    def _team_match_stat(self, x: d.TeamMatchStatDTO, ws: WriteStats) -> None:
        match_id = self._require("match", x.match_external_id)
        where = f"team_match_stat {x.match_external_id}/{x.team_external_id}"
        period_id = self._period_id(match_id, x.period, ws) if x.period else None
        stats = self._stats(x.stats, "period" if x.period else "match", "team", ws, where)
        if not stats:
            raise Rejected(f"{where}: 유효한 지표가 없습니다")
        before = ws.counts["core.team_match_stat.unchanged"]
        self._upsert(ws, "core.team_match_stat", "ON CONSTRAINT uq_team_match_stat",
                     {"match_id": match_id, "team_id": self._require("team", x.team_external_id),
                      "period_id": period_id},
                     {"stats": stats})
        if ws.counts["core.team_match_stat.unchanged"] == before:
            self._mark_match_changed(match_id, ws)

    def _mark_match_changed(self, match_id: int, ws: WriteStats) -> None:
        ws.match_ids.add(match_id)
        ws.stage_ids.add(self.conn.execute(text("SELECT stage_id FROM core.match WHERE id = :m"),
                                           {"m": match_id}).scalar_one())

    # ==================================================================
    # 시즌 기록·순위·명단
    # ==================================================================
    def _player_season_stat(self, x: d.PlayerSeasonStatDTO, ws: WriteStats) -> None:
        stage_id, season_id = self._stage_ids(x.season_label, x.stage_code)
        where = f"player_season_stat {x.season_label}/{x.player_external_id}"
        stats = self._stats(x.stats, "season", "player", ws, where)
        if not stats:
            raise Rejected(f"{where}: 유효한 지표가 없습니다")
        self._upsert(ws, "core.player_season_stat", "ON CONSTRAINT uq_player_season_stat",
                     {"stage_id": stage_id, "player_id": self._require("player", x.player_external_id),
                      "team_id": self._require("team", x.team_external_id), "origin": "collected"},
                     {"season_id": season_id, "stats": stats})
        ws.stage_ids.add(stage_id)

    def _team_season_stat(self, x: d.TeamSeasonStatDTO, ws: WriteStats) -> None:
        stage_id, season_id = self._stage_ids(x.season_label, x.stage_code)
        where = f"team_season_stat {x.season_label}/{x.team_external_id}"
        stats = self._stats(x.stats, "season", "team", ws, where)
        if not stats:
            raise Rejected(f"{where}: 유효한 지표가 없습니다")
        team_id = self._require("team", x.team_external_id)
        self._upsert(ws, "core.team_season_stat", "(stage_id, team_id, origin)",
                     {"stage_id": stage_id, "team_id": team_id, "origin": "collected"},
                     {"season_id": season_id, "stats": stats})
        self._ensure_season_team(season_id, team_id)
        ws.stage_ids.add(stage_id)

    def _standing(self, x: d.StandingDTO, ws: WriteStats) -> None:
        stage_id, season_id = self._stage_ids(x.season_label, x.stage_code)
        team_id = self._require("team", x.team_external_id)
        stats = self._stats(x.stats, "season", "team", ws, f"standing {x.team_external_id}")
        self._upsert(ws, "core.standing", "(stage_id, team_id, as_of_date)",
                     {"stage_id": stage_id, "team_id": team_id, "as_of_date": x.as_of_date},
                     {"rank": x.rank, "group_code": x.group_code, "stats": stats})
        self._ensure_season_team(season_id, team_id)

    def _roster(self, x: d.RosterEntryDTO, ws: WriteStats) -> None:
        where = f"roster {x.player_external_id}"
        self._upsert(ws, "core.roster_entry", "(player_id, team_id, season_id, valid_from)",
                     {"player_id": self._require("player", x.player_external_id),
                      "team_id": self._require("team", x.team_external_id),
                      "season_id": self._season_id(x.season_label, ws), "valid_from": x.valid_from},
                     {"valid_to": x.valid_to, "status": x.status, "jersey_no": x.jersey_no,
                      "position_code": self._position(x.position_code, ws, where)})

    # ==================================================================
    # 이벤트
    # ==================================================================
    def _event(self, x: d.EventDTO, ws: WriteStats) -> None:
        match_id = self._require("match", x.match_external_id)
        type_id = self.catalog.event_types.get(x.event_type)
        if type_id is None:
            raise Rejected(f"정의되지 않은 이벤트 타입 {x.event_type}")
        if match_id not in self._match_dates:
            self._match_dates[match_id] = self.conn.execute(
                text("SELECT local_date FROM core.match WHERE id = :m"), {"m": match_id}).scalar_one()
        match_date = self._match_dates[match_id]
        if match_date.year not in self._partitions:
            self.conn.execute(text("SELECT util.ensure_event_partition(:y)"), {"y": match_date.year})
            self._partitions.add(match_date.year)
        period_id = self._period_id(match_id, x.period, ws) if x.period else None
        event_id = self._upsert(ws, "core.event", "ON CONSTRAINT uq_event_seq",
                                {"match_id": match_id, "seq": x.seq, "match_date": match_date},
                                {"event_type_id": type_id, "period_id": period_id,
                                 "team_id": self._require("team", x.team_external_id),
                                 "clock_sec": x.clock_sec, "elapsed_sec": x.elapsed_sec,
                                 "score_home_after": x.score_home_after, "score_away_after": x.score_away_after,
                                 "description": x.description, "attrs": x.attrs})
        wanted = {(self._require("player", p.player_external_id), p.role, p.ord) for p in x.participants}
        current = {tuple(r) for r in self.conn.execute(text(
            "SELECT player_id, role, ord FROM core.event_participant WHERE event_id = :e AND match_date = :d"),
            {"e": event_id, "d": match_date})}
        if wanted != current:
            self.conn.execute(text("DELETE FROM core.event_participant WHERE event_id = :e AND match_date = :d"),
                              {"e": event_id, "d": match_date})
            for player_id, role, ord_ in sorted(wanted):
                self.conn.execute(text("""
                    INSERT INTO core.event_participant (event_id, match_date, player_id, role, ord)
                    VALUES (:e, :d, :p, :r, :o)"""),
                    {"e": event_id, "d": match_date, "p": player_id, "r": role, "o": ord_})
