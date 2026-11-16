from __future__ import annotations

from hashlib import sha256
from typing import Any

from .intake import canonical_bytes
from .normalize import canonical_decimal, normalize_period, period_index
from .registry import ContractError, Registry, precise


class Adjudicator:
    """Derives the single external series from published per-source observations.

    Comparability is decided jointly by the indicator contract (canonical unit,
    cadence, precision), batch quality evidence and the release sequence.
    Conflicting observations are never picked by source name; they stay withheld
    until a manual adjudication records the decision together with its evidence.
    An adjudication stays valid only while the participant set it was decided
    against still matches the current release, so a later source correction
    invalidates it and the period returns to the withheld list.
    """

    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry

    def _resolve(self, version: int | None) -> int | None:
        if version is not None:
            self.store.release(version)
            return version
        latest = self.store.latest_release()
        return latest["version"] if latest else None

    @staticmethod
    def digest(evidence: dict[str, Any]) -> str:
        return sha256(canonical_bytes(evidence)).hexdigest()

    def _participants(self, entries: list[dict[str, Any]], indicator_key: str,
                      period: str) -> list[dict[str, Any]]:
        selected = [row for row in entries
                    if row["indicator"] == indicator_key and row["period"] == period]
        selected.sort(key=lambda row: (row["source"], row["batch_id"], row["line"]))
        participants = []
        for entry in selected:
            batch = self.store.batch(entry["batch_id"])
            measurement = next((row for row in self.store.measurements(entry["batch_id"])
                                if row["line"] == entry["line"]), None)
            participants.append({
                "source": entry["source"],
                "batch_id": entry["batch_id"],
                "line": entry["line"],
                "value": entry["value"],
                "unit": entry["unit"],
                "raw_value": measurement["raw_value"] if measurement else None,
                "raw_unit": measurement["raw_unit"] if measurement else None,
                "factor": measurement["factor"] if measurement else None,
                "offset": measurement["offset"] if measurement else None,
                "external_id": batch["external_id"],
                "batch_revision": batch["revision"],
                "batch_status": batch["status"],
                "quality": batch["findings"],
            })
        return participants

    def evaluate(self, indicator_key: str, period: str, version: int,
                 entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        indicator = self.registry.indicator(indicator_key)
        normalize_period(period, indicator.cadence)
        if entries is None:
            entries = self.store.release_entries(version)
        participants = self._participants(entries, indicator_key, period)
        problems = []
        for participant in participants:
            if participant["unit"] != indicator.canonical_unit:
                problems.append({"code": "unit_mismatch", "source": participant["source"],
                                 "batch_id": participant["batch_id"],
                                 "detail": f"published unit {participant['unit']} is not "
                                           f"canonical unit {indicator.canonical_unit}"})
            if participant["batch_status"] != "published":
                problems.append({"code": "batch_not_published", "source": participant["source"],
                                 "batch_id": participant["batch_id"],
                                 "detail": f"batch status is {participant['batch_status']}"})
            if participant["raw_value"] is None:
                problems.append({"code": "measurement_missing", "source": participant["source"],
                                 "batch_id": participant["batch_id"],
                                 "detail": "stored measurement line is missing"})
        distinct = sorted({participant["value"] for participant in participants})
        if not participants:
            status = "absent"
        elif problems:
            status = "not_comparable"
        elif len(participants) == 1:
            status = "single"
        elif len(distinct) == 1:
            status = "agree"
        else:
            status = "conflict"
        return {"indicator": indicator_key, "period": period, "version": version,
                "status": status, "distinct_values": distinct,
                "problems": problems, "participants": participants}

    def evidence(self, indicator_key: str, period: str, version: int,
                 entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        indicator = self.registry.indicator(indicator_key)
        evaluation = self.evaluate(indicator_key, period, version, entries)
        return {"indicator": {"key": indicator.key,
                              "canonical_unit": indicator.canonical_unit,
                              "cadence": indicator.cadence,
                              "precision": indicator.precision},
                "period": period,
                "participants": evaluation["participants"]}

    def _periods(self, entries: list[dict[str, Any]], indicator_key: str,
                 cadence: str) -> list[str]:
        periods = {row["period"] for row in entries if row["indicator"] == indicator_key}
        return sorted(periods, key=lambda period: period_index(period, cadence))

    def applicable(self, indicator_key: str, period: str, version: int,
                   entries: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        digest = self.digest(self.evidence(indicator_key, period, version, entries))
        for record in reversed(self.store.adjudications(indicator_key, period)):
            if record["release_version"] <= version and record["evidence_digest"] == digest:
                return record
        return None

    @staticmethod
    def _summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        return {"id": record["id"], "decision": record["decision"],
                "rationale": record["rationale"], "evidence_digest": record["evidence_digest"],
                "release_version": record["release_version"], "decided_at": record["decided_at"]}

    def conflicts(self, indicator_key: str, version: int | None = None) -> dict[str, Any]:
        indicator = self.registry.indicator(indicator_key)
        resolved = self._resolve(version)
        report = {"indicator": indicator_key, "version": resolved,
                  "conflict_count": 0, "conflicts": []}
        if resolved is None:
            return report
        entries = self.store.release_entries(resolved)
        for period in self._periods(entries, indicator_key, indicator.cadence):
            evaluation = self.evaluate(indicator_key, period, resolved, entries)
            if evaluation["status"] not in {"conflict", "not_comparable"}:
                continue
            record = self.applicable(indicator_key, period, resolved, entries)
            prior = self.store.adjudications(indicator_key, period)
            if record is not None:
                resolution = "withheld" if record["decision"]["kind"] == "reject_all" else "resolved"
            elif prior:
                resolution = "reopened"
            else:
                resolution = "unresolved"
            report["conflicts"].append({
                "period": period, "status": evaluation["status"],
                "distinct_values": evaluation["distinct_values"],
                "problems": evaluation["problems"],
                "participants": evaluation["participants"],
                "resolution": resolution,
                "adjudication": self._summary(record),
                "prior_adjudications": [item["id"] for item in prior]})
        report["conflict_count"] = len(report["conflicts"])
        return report

    def adjudicate(self, indicator_key: str, period: str, *, value: Any = None,
                   reject: bool = False, rationale: str | None = None,
                   version: int | None = None) -> dict[str, Any]:
        indicator = self.registry.indicator(indicator_key)
        resolved = self._resolve(version)
        if resolved is None:
            raise ContractError("no_release", "no release has been published")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ContractError("rationale_invalid", "rationale must be nonempty text")
        rationale = rationale.strip()
        if len(rationale) > 500:
            raise ContractError("rationale_invalid", "rationale must be at most 500 characters")
        if reject and value is not None:
            raise ContractError("decision_shape", "choose a value or reject all, not both")
        if not reject and value is None:
            raise ContractError("decision_shape", "adjudication requires a chosen value or reject")
        evaluation = self.evaluate(indicator_key, period, resolved)
        if evaluation["status"] == "absent":
            raise ContractError("no_observations",
                                f"no published observations for {indicator_key} {period}")
        if evaluation["status"] in {"single", "agree"}:
            raise ContractError("no_conflict", "published observations do not conflict")
        if reject:
            decision = {"kind": "reject_all"}
        else:
            candidate = canonical_decimal(precise(value, "value"), indicator.precision)
            if candidate not in evaluation["distinct_values"]:
                raise ContractError("value_not_observed",
                                    "chosen value must equal one of the compared observations")
            decision = {"kind": "choose", "value": candidate, "unit": indicator.canonical_unit}
        evidence = self.evidence(indicator_key, period, resolved)
        record = self.store.insert_adjudication(indicator_key, period, resolved, decision,
                                                rationale, evidence, self.digest(evidence))
        return {**record, "status": "active"}

    def external_series(self, indicator_key: str, version: int | None = None) -> dict[str, Any]:
        indicator = self.registry.indicator(indicator_key)
        resolved = self._resolve(version)
        result = {"indicator": indicator_key, "version": resolved,
                  "unit": indicator.canonical_unit, "cadence": indicator.cadence,
                  "rows": [], "withheld": [],
                  "summary": {"published": 0, "withheld": 0}}
        if resolved is None:
            return result
        entries = self.store.release_entries(resolved)
        for period in self._periods(entries, indicator_key, indicator.cadence):
            evaluation = self.evaluate(indicator_key, period, resolved, entries)
            status = evaluation["status"]
            participants = evaluation["participants"]
            if status == "single":
                result["rows"].append({"period": period, "value": participants[0]["value"],
                                       "unit": indicator.canonical_unit, "basis": "single_source",
                                       "participants": participants, "adjudication": None})
            elif status == "agree":
                result["rows"].append({"period": period, "value": evaluation["distinct_values"][0],
                                       "unit": indicator.canonical_unit, "basis": "values_agree",
                                       "participants": participants, "adjudication": None})
            elif status == "not_comparable" or status == "conflict":
                record = self.applicable(indicator_key, period, resolved, entries)
                if record is not None and record["decision"]["kind"] == "reject_all":
                    result["withheld"].append({"period": period, "reason": "adjudicated_withheld",
                                               "adjudication": self._summary(record),
                                               "participants": participants})
                elif record is not None:
                    result["rows"].append({"period": period,
                                           "value": record["decision"]["value"],
                                           "unit": record["decision"]["unit"],
                                           "basis": "adjudicated",
                                           "participants": participants,
                                           "adjudication": self._summary(record)})
                elif status == "not_comparable":
                    result["withheld"].append({"period": period, "reason": "not_comparable",
                                               "problems": evaluation["problems"],
                                               "participants": participants})
                else:
                    prior = self.store.adjudications(indicator_key, period)
                    result["withheld"].append({
                        "period": period,
                        "reason": "adjudication_stale" if prior else "unresolved_conflict",
                        "distinct_values": evaluation["distinct_values"],
                        "participants": participants,
                        "prior_adjudications": [item["id"] for item in prior]})
        result["summary"] = {"published": len(result["rows"]), "withheld": len(result["withheld"])}
        return result

    def adjudications(self, indicator_key: str | None = None) -> list[dict[str, Any]]:
        records = self.store.adjudications(indicator_key)
        latest = self.store.latest_release()
        active_ids = set()
        if latest is not None:
            entries = self.store.release_entries(latest["version"])
            keys = sorted({(row["indicator"], row["period"]) for row in records})
            for key, period in keys:
                record = self.applicable(key, period, latest["version"], entries)
                if record is not None:
                    active_ids.add(record["id"])
        newest: dict[tuple[str, str], int] = {}
        for row in records:
            key = row["indicator"], row["period"]
            newest[key] = max(newest.get(key, 0), row["id"])
        result = []
        for row in records:
            if row["id"] in active_ids:
                status = "active"
            elif row["id"] == newest[(row["indicator"], row["period"])]:
                status = "stale"
            else:
                status = "superseded"
            result.append({**row, "status": status})
        return result
