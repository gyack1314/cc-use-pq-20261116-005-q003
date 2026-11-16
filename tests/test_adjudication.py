from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from climateledger.cli import main
from climateledger.intake import write_manifest
from climateledger.registry import ContractError
from support import LedgerCase


class AdjudicationCase(LedgerCase):
    def publish_two_sources(self, first="428", second="430"):
        batch_a, _ = self.ingest("a_batch", self.rows(first), source="lab_a")
        self.service.publish(batch_a["id"], batch_a["revision"])
        batch_b, _ = self.ingest("b_batch", self.rows(second), source="lab_b")
        self.service.publish(batch_b["id"], batch_b["revision"])
        return batch_a, batch_b


class ExternalSeriesTests(AdjudicationCase):
    def test_single_source_basis(self):
        self.release()
        series = self.service.external_series("co2_ppm")
        self.assertEqual([row["basis"] for row in series["rows"]],
                         ["single_source", "single_source"])
        self.assertEqual(series["rows"][0]["participants"][0]["source"], "lab_a")
        self.assertEqual(series["withheld"], [])

    def test_agreeing_sources_publish_with_all_participants(self):
        self.publish_two_sources("428", "428")
        series = self.service.external_series("co2_ppm")
        self.assertEqual(series["withheld"], [])
        row = series["rows"][0]
        self.assertEqual(row["basis"], "values_agree")
        self.assertEqual(row["value"], "428.00")
        self.assertEqual({item["source"] for item in row["participants"]}, {"lab_a", "lab_b"})
        self.assertIsNone(row["adjudication"])
        self.assertEqual(self.service.conflicts("co2_ppm")["conflict_count"], 0)

    def test_unresolved_conflict_is_withheld_not_silently_published(self):
        self.publish_two_sources("428", "430")
        series = self.service.external_series("co2_ppm")
        self.assertEqual(series["rows"], [])
        self.assertEqual(series["summary"], {"published": 0, "withheld": 1})
        withheld = series["withheld"][0]
        self.assertEqual(withheld["reason"], "unresolved_conflict")
        self.assertEqual(withheld["period"], "2026-01")
        self.assertEqual({item["source"] for item in withheld["participants"]},
                         {"lab_a", "lab_b"})
        report = self.service.conflicts("co2_ppm")
        self.assertEqual(report["conflict_count"], 1)
        conflict = report["conflicts"][0]
        self.assertEqual(conflict["resolution"], "unresolved")
        self.assertEqual(conflict["distinct_values"], ["428.00", "430.00"])

    def test_per_source_exports_stay_reproducible_during_conflict(self):
        self.publish_two_sources("428", "430")
        bundle = self.service.export(2)
        self.assertTrue(self.service.verify(bundle))
        self.assertEqual({row["source"] for row in bundle["entries"]}, {"lab_a", "lab_b"})
        self.assertEqual(self.service.series("co2_ppm", 2, "lab_a")[0]["value"], "428.00")
        self.assertEqual(self.service.series("co2_ppm", 2, "lab_b")[0]["value"], "430.00")
        self.assertEqual(self.service.discrepancies(2, "co2_ppm")["disagreements"][0]["spread"],
                         "2.00")


class AdjudicationFlowTests(AdjudicationCase):
    def test_adjudicated_value_carries_batches_and_basis(self):
        batch_a, batch_b = self.publish_two_sources("428", "430")
        record = self.service.adjudicate("co2_ppm", "2026-01", value="430",
                                         rationale="采用校准证据更完整的批次")
        self.assertEqual(record["decision"],
                         {"kind": "choose", "value": "430.00", "unit": "ppm"})
        self.assertEqual(record["release_version"], 2)
        self.assertEqual(record["status"], "active")
        self.assertEqual(len(record["evidence_digest"]), 64)
        self.assertEqual(record["evidence"]["indicator"]["canonical_unit"], "ppm")
        series = self.service.external_series("co2_ppm")
        row = series["rows"][0]
        self.assertEqual(row["basis"], "adjudicated")
        self.assertEqual(row["value"], "430.00")
        self.assertEqual({item["batch_id"] for item in row["participants"]},
                         {batch_a["id"], batch_b["id"]})
        self.assertEqual(row["adjudication"]["id"], record["id"])
        self.assertEqual(row["adjudication"]["rationale"], "采用校准证据更完整的批次")
        self.assertEqual(row["adjudication"]["evidence_digest"], record["evidence_digest"])
        self.assertEqual(self.service.conflicts("co2_ppm")["conflicts"][0]["resolution"],
                         "resolved")

    def test_reject_all_withholds_period_as_decided(self):
        self.publish_two_sources("428", "430")
        record = self.service.adjudicate("co2_ppm", "2026-01", reject=True,
                                         rationale="双方均未附校准证据")
        self.assertEqual(record["decision"], {"kind": "reject_all"})
        series = self.service.external_series("co2_ppm")
        self.assertEqual(series["rows"], [])
        withheld = series["withheld"][0]
        self.assertEqual(withheld["reason"], "adjudicated_withheld")
        self.assertEqual(withheld["adjudication"]["id"], record["id"])
        self.assertEqual(self.service.conflicts("co2_ppm")["conflicts"][0]["resolution"],
                         "withheld")

    def test_chosen_value_must_match_a_compared_observation(self):
        self.publish_two_sources("428", "430")
        with self.assertRaisesRegex(ContractError, "value_not_observed"):
            self.service.adjudicate("co2_ppm", "2026-01", value="431", rationale="凭空取值")

    def test_adjudication_requires_an_actual_conflict(self):
        batch, _ = self.ingest("solo", self.rows(428))
        self.service.publish(batch["id"], 1)
        with self.assertRaisesRegex(ContractError, "no_conflict"):
            self.service.adjudicate("co2_ppm", "2026-01", value="428", rationale="无冲突")
        with self.assertRaisesRegex(ContractError, "no_observations"):
            self.service.adjudicate("co2_ppm", "2026-05", value="428", rationale="无观测")
        other, _ = self.ingest("other", self.rows(428), source="lab_b")
        self.service.publish(other["id"], 1)
        with self.assertRaisesRegex(ContractError, "no_conflict"):
            self.service.adjudicate("co2_ppm", "2026-01", value="428", rationale="值已一致")

    def test_decision_shape_and_rationale_are_validated(self):
        self.publish_two_sources("428", "430")
        with self.assertRaisesRegex(ContractError, "rationale_invalid"):
            self.service.adjudicate("co2_ppm", "2026-01", value="430", rationale="  ")
        with self.assertRaisesRegex(ContractError, "decision_shape"):
            self.service.adjudicate("co2_ppm", "2026-01", rationale="缺少决定")
        with self.assertRaisesRegex(ContractError, "decision_shape"):
            self.service.adjudicate("co2_ppm", "2026-01", value="430", reject=True,
                                    rationale="两者互斥")

    def test_identical_adjudication_is_idempotent(self):
        self.publish_two_sources("428", "430")
        first = self.service.adjudicate("co2_ppm", "2026-01", value="430", rationale="同一理由")
        second = self.service.adjudicate("co2_ppm", "2026-01", value="430.00", rationale="同一理由")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.adjudications("co2_ppm")), 1)
        events = self.service.events("adjudication:co2_ppm:2026-01")
        self.assertEqual(len(events), 1)

    def test_adjudication_is_logged_as_event(self):
        self.publish_two_sources("428", "430")
        record = self.service.adjudicate("co2_ppm", "2026-01", value="430", rationale="留痕")
        events = self.service.events("adjudication:co2_ppm:2026-01")
        self.assertEqual([event["kind"] for event in events], ["adjudicate"])
        self.assertEqual(events[0]["detail"]["id"], record["id"])
        self.assertEqual(events[0]["detail"]["evidence_digest"], record["evidence_digest"])

    def test_source_correction_invalidates_adjudication_until_resolved(self):
        self.publish_two_sources("428", "430")
        record = self.service.adjudicate("co2_ppm", "2026-01", value="430",
                                         rationale="采用 lab_b 初值")
        correction, _ = self.ingest("b_batch", self.rows("431"), source="lab_b")
        self.assertEqual(correction["revision"], 2)
        self.assertEqual(correction["status"], "review")
        self.service.approve(correction["id"], 2)
        self.service.publish(correction["id"], 2)
        series = self.service.external_series("co2_ppm")
        self.assertEqual(series["rows"], [])
        withheld = series["withheld"][0]
        self.assertEqual(withheld["reason"], "adjudication_stale")
        self.assertEqual(withheld["prior_adjudications"], [record["id"]])
        self.assertEqual(self.service.conflicts("co2_ppm")["conflicts"][0]["resolution"],
                         "reopened")
        self.assertEqual(self.service.adjudications("co2_ppm")[0]["status"], "stale")
        second = self.service.adjudicate("co2_ppm", "2026-01", value="431",
                                         rationale="采用更正后批次")
        series = self.service.external_series("co2_ppm")
        self.assertEqual(series["rows"][0]["value"], "431.00")
        self.assertEqual(series["rows"][0]["adjudication"]["id"], second["id"])
        statuses = {row["id"]: row["status"] for row in self.service.adjudications("co2_ppm")}
        self.assertEqual(statuses[record["id"]], "superseded")
        self.assertEqual(statuses[second["id"]], "active")

    def test_unrelated_release_keeps_adjudication_valid(self):
        self.publish_two_sources("428", "430")
        self.service.adjudicate("co2_ppm", "2026-01", value="430", rationale="不受无关发布影响")
        later, _ = self.ingest("a_later", self.rows(429, start=2), source="lab_a")
        self.assertEqual(later["status"], "accepted")
        self.service.publish(later["id"], 1)
        series = self.service.external_series("co2_ppm")
        rows = {row["period"]: row for row in series["rows"]}
        self.assertEqual(rows["2026-01"]["basis"], "adjudicated")
        self.assertEqual(rows["2026-02"]["basis"], "single_source")
        self.assertEqual(self.service.adjudications("co2_ppm")[0]["status"], "active")

    def test_historical_external_series_is_reproducible(self):
        self.release()
        before = self.service.external_series("co2_ppm", 1)
        other, _ = self.ingest("other", self.rows(430), source="lab_b")
        self.service.publish(other["id"], 1)
        self.service.adjudicate("co2_ppm", "2026-01", value="430", rationale="历史视图不变")
        after = self.service.external_series("co2_ppm", 1)
        self.assertEqual(before, after)
        self.assertEqual([row["basis"] for row in after["rows"]],
                         ["single_source", "single_source"])
        current = self.service.external_series("co2_ppm", 2)
        january = [row for row in current["rows"] if row["period"] == "2026-01"][0]
        self.assertEqual(january["basis"], "adjudicated")
        bundle = self.service.export(1)
        self.assertTrue(self.service.verify(bundle))
        self.assertEqual(len(bundle["entries"]), 2)

    def test_quality_evidence_travels_with_participants(self):
        rows = self.rows(428) + self.rows(430, start=3)
        batch_a, _ = self.ingest("a_gap", rows, source="lab_a")
        self.assertEqual(batch_a["status"], "review")
        self.service.approve(batch_a["id"], 1)
        self.service.publish(batch_a["id"], 1)
        batch_b, _ = self.ingest("b_gap", self.rows(429) + self.rows(431, start=3), source="lab_b")
        self.service.approve(batch_b["id"], 1)
        self.service.publish(batch_b["id"], 1)
        report = self.service.conflicts("co2_ppm")
        self.assertEqual(report["conflict_count"], 2)
        january = [item for item in report["conflicts"] if item["period"] == "2026-01"][0]
        quality = {item["source"]: [finding["code"] for finding in item["quality"]]
                   for item in january["participants"]}
        self.assertIn("missing_period", quality["lab_a"])
        record = self.service.adjudicate("co2_ppm", "2026-01", value="429",
                                         rationale="参考双方质量证据后采用 lab_b")
        evidence_sources = {item["source"] for item in record["evidence"]["participants"]}
        self.assertEqual(evidence_sources, {"lab_a", "lab_b"})

    def test_tampered_unit_makes_observations_not_comparable(self):
        self.publish_two_sources("428", "428")
        self.service.store.connection.execute(
            "UPDATE release_entries SET unit='ppb' WHERE source='lab_b' AND version=2")
        series = self.service.external_series("co2_ppm")
        self.assertEqual(series["rows"], [])
        withheld = series["withheld"][0]
        self.assertEqual(withheld["reason"], "not_comparable")
        self.assertEqual(withheld["problems"][0]["code"], "unit_mismatch")
        record = self.service.adjudicate("co2_ppm", "2026-01", value="428",
                                         rationale="人工核验两份原文一致")
        self.assertEqual(record["status"], "active")
        row = self.service.external_series("co2_ppm")["rows"][0]
        self.assertEqual(row["basis"], "adjudicated")


class AdjudicationCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "cli.sqlite3"

    def call(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.db), *args])
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def contract(self, filename, data):
        path = self.root / filename
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def publish_pair(self):
        indicator = self.contract("indicator.json", {"key": "co2_ppm", "title": "Synthetic",
            "canonical_unit": "ppm", "cadence": "month", "precision": 2})
        source_a = self.contract("source_a.json", {"key": "lab_a", "title": "Lab A",
            "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm"]})
        source_b = self.contract("source_b.json", {"key": "lab_b", "title": "Lab B",
            "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm"]})
        self.assertEqual(self.call("register-indicator", indicator)[0], 0)
        self.assertEqual(self.call("register-source", source_a)[0], 0)
        self.assertEqual(self.call("register-source", source_b)[0], 0)
        manifest_a = write_manifest(self.root / "a", source="lab_a", batch_id="a_batch",
                                    rows=[{"indicator": "co2_ppm", "period": "2026-01",
                                           "value": "428", "unit": "ppm"}])
        self.assertEqual(self.call("ingest", str(manifest_a))[0], 0)
        self.assertEqual(self.call("publish", "1", "1")[0], 0)
        manifest_b = write_manifest(self.root / "b", source="lab_b", batch_id="b_batch",
                                    rows=[{"indicator": "co2_ppm", "period": "2026-01",
                                           "value": "430", "unit": "ppm"}])
        self.assertEqual(self.call("ingest", str(manifest_b))[0], 0)
        self.assertEqual(self.call("publish", "2", "1")[0], 0)

    def test_cli_conflict_adjudicate_external_series(self):
        self.publish_pair()
        code, report, _ = self.call("conflicts", "co2_ppm")
        self.assertEqual(code, 0)
        self.assertEqual(report["conflicts"][0]["resolution"], "unresolved")
        code, series, _ = self.call("external-series", "co2_ppm")
        self.assertEqual(series["rows"], [])
        self.assertEqual(series["withheld"][0]["reason"], "unresolved_conflict")
        code, record, _ = self.call("adjudicate", "co2_ppm", "2026-01",
                                    "--value", "430", "--rationale", "人工核验采用 lab_b")
        self.assertEqual(code, 0)
        self.assertEqual(record["decision"]["value"], "430.00")
        code, series, _ = self.call("external-series", "co2_ppm")
        self.assertEqual(series["rows"][0]["basis"], "adjudicated")
        self.assertEqual(series["rows"][0]["value"], "430.00")
        code, listing, _ = self.call("adjudications", "--indicator", "co2_ppm")
        self.assertEqual(listing[0]["status"], "active")

    def test_cli_adjudicate_requires_a_decision(self):
        self.publish_pair()
        code, _, error = self.call("adjudicate", "co2_ppm", "2026-01",
                                   "--rationale", "缺少决定")
        self.assertEqual(code, 2)
        self.assertIn("decision_shape", error)
