"""Offline acceptance checks for the two sets of catalog fixtures."""

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
SHARED = {
    "title": "Название",
    "format": "Формат",
    "target_audience": "Целевая аудитория",
    "source_url": "Ссылка на источник",
}
REQUIRED = {
    "internal": SHARED | {
        "opportunity_type": "Тип",
        "organizing_unit": "Подразделение-организатор",
        "period": "Дата/период",
        "required_competencies": "Требуемые компетенции",
        "cost_or_terms": "Стоимость/условия",
        "application_deadline": "Дедлайн подачи заявки",
        "development_track": "Трек индплана",
        "contact": "Контактное лицо",
        "status": "Статус",
    },
    "external": SHARED | {
        "organizer": "Организатор",
        "event_date": "Дата проведения",
        "venue": "Место проведения",
        "participation_fee": "Стоимость участия",
        "submission_deadline": "Дедлайн подачи заявки/тезисов",
        "registration_deadline": "Дедлайн регистрации",
        "publication_indexing": "Индексация публикаций",
        "language": "Язык мероприятия",
        "topic": "Тематика",
        "scale": "Масштаб",
        "participant_count": "Кол-во участников",
        "first_held_year": "С какого года проводится",
    },
}
DEADLINES = {
    "internal": ("application_deadline",),
    "external": ("submission_deadline", "registration_deadline"),
}
CASE_REASONS = {
    "full": set(), "incomplete": {"unknown"}, "conflict": {"conflict"},
    "not_applicable": {"pending_24"},
}


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"Missing UTC offset: {value}")
    return parsed.astimezone(timezone.utc)


class CatalogContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogs = {
            name: json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"),
                             object_pairs_hook=unique_keys)
            for name in REQUIRED
        }

    def assert_text(self, value):
        self.assertIsInstance(value, str)
        self.assertNotIn(value.strip().casefold(), {"", "n/a", "unknown", "неизвестно", "неприменимо"})

    def assert_known(self, field, value):
        if field == "period":
            self.assertEqual(set(value), {"start", "end"})
            self.assertLessEqual(date.fromisoformat(value["start"]), date.fromisoformat(value["end"]))
        elif field == "event_date":
            date.fromisoformat(value)
        elif field.endswith("_deadline"):
            instant(value)
        elif field in {"participant_count", "first_held_year"}:
            self.assertIs(type(value), int)
            self.assertGreaterEqual(value, 0 if field == "participant_count" else 1)
        elif field == "participation_fee":
            self.assertEqual(set(value), {"amount", "currency"})
            self.assertIs(type(value["amount"]), int)
            self.assertGreaterEqual(value["amount"], 0)
            self.assert_text(value["currency"])
        elif field == "required_competencies":
            self.assertIsInstance(value, list)
            self.assertTrue(value)
            for item in value:
                self.assert_text(item)
        elif field == "contact":
            self.assertEqual(set(value), {"name", "email"})
            self.assert_text(value["name"])
            self.assertRegex(value["email"], r"^[^@\s]+@[^@\s]+\.invalid$")
        else:
            self.assert_text(value)

    def test_named_fields_and_classifier(self):
        for catalog, data in self.catalogs.items():
            required = REQUIRED[catalog]
            with self.subTest(catalog=catalog):
                self.assertDictEqual(data["required_fields"], required)
                self.assertLessEqual(CASE_REASONS.keys(), data["cards"].keys())
            for case, card in data["cards"].items():
                with self.subTest(catalog=catalog, case=case):
                    self.assertSetEqual(set(card["fields"]), set(required))
                    if catalog == "external":
                        self.assert_text(card["event_type"])
                    else:
                        self.assertNotIn("event_type", card)

    def test_synthetic_provenance(self):
        for catalog, data in self.catalogs.items():
            for key, source in data["sources"].items():
                with self.subTest(catalog=catalog, source=key):
                    for field in ("provider", "record_id", "edition_ref", "revision", "excerpt"):
                        self.assert_text(source[field])
                    url = urlsplit(source["url"])
                    self.assertEqual(url.scheme, "https")
                    self.assertTrue(url.hostname and url.hostname.endswith(".invalid"))
            for case, card in data["cards"].items():
                with self.subTest(catalog=catalog, case=case):
                    self.assertEqual(card["fields"]["source_url"], data["sources"][card["source"]]["url"])

    def blocker(self, field, value, sources, primary):
        if not isinstance(value, dict) or "state" not in value:
            self.assert_known(field, value)
            return None

        state = value["state"]
        if state == "conflict":
            self.assertEqual(set(value), {"state", "candidates"})
            candidates = value["candidates"]
            self.assertGreaterEqual(len(candidates), 2)
            origins, alternatives = set(), set()
            for candidate in candidates:
                source = sources[candidate["source"]]
                self.assertEqual(source["edition_ref"], primary["edition_ref"])
                origins.add((source["provider"], source["record_id"], source["edition_ref"], source["revision"]))
                answer = candidate["value"]
                self.assert_known(field, answer)
                # Two offsets can describe the same instant.
                if field.endswith("_deadline"):
                    alternatives.add(instant(answer))
                else:
                    alternatives.add(json.dumps(answer, sort_keys=True, ensure_ascii=False))
            self.assertGreaterEqual(len(origins), 2)
            self.assertGreaterEqual(len(alternatives), 2)
            return "conflict"

        self.assertIn(state, {"unknown", "not_applicable"})
        self.assertEqual(set(value) - {"raw"}, {"state", "note"})
        self.assert_text(value["note"])
        if "raw" in value:
            self.assert_text(value["raw"])
        return "pending_24" if state == "not_applicable" else "unknown"

    def test_completeness_and_moderation(self):
        for catalog, data in self.catalogs.items():
            for case, card in data["cards"].items():
                with self.subTest(catalog=catalog, case=case):
                    blockers = {}
                    primary = data["sources"][card["source"]]
                    for field, value in card["fields"].items():
                        reason = self.blocker(field, value, data["sources"], primary)
                        if reason:
                            blockers[field] = reason
                    if case in CASE_REASONS:
                        self.assertEqual(set(blockers.values()), CASE_REASONS[case])
                    if case == "not_applicable":
                        self.assertEqual(len(blockers), 1)
                    expected = card["expected"]
                    self.assertDictEqual(expected["blockers"], blockers)
                    self.assertEqual(expected["known_fields"], len(card["fields"]) - len(blockers))
                    self.assertEqual(expected["route"], "moderation" if blockers else "pass_completeness")

    def test_deadlines_at_fixed_instant(self):
        for catalog, data in self.catalogs.items():
            as_of = instant(data["as_of"])
            for case, card in data["cards"].items():
                actual = {}
                for field in DEADLINES[catalog]:
                    with self.subTest(catalog=catalog, case=case, field=field):
                        value = card["fields"][field]
                        if isinstance(value, dict):
                            actual[field] = {"status": value["state"], "utc": None}
                        else:
                            deadline = instant(value)
                            actual[field] = {
                                "status": "open" if as_of < deadline else "elapsed",
                                "utc": deadline.isoformat().replace("+00:00", "Z"),
                            }
                with self.subTest(catalog=catalog, case=case):
                    self.assertDictEqual(card["expected"]["deadlines"], actual)

    def test_external_boundary_examples(self):
        cards = self.catalogs["external"]["cards"]
        full = cards["full"]["fields"]
        partial = cards["incomplete"]["fields"]
        self.assertEqual(full["participant_count"], 0)
        self.assertEqual(full["participation_fee"]["amount"], 0)
        self.assertEqual(full["publication_indexing"], "Без индексации")
        for field in ("participant_count", "participation_fee", "submission_deadline"):
            self.assertEqual(partial[field]["state"], "unknown")
        self.assert_text(partial["registration_deadline"])
        deadlines = cards["full"]["expected"]["deadlines"]
        self.assertEqual(deadlines["submission_deadline"]["status"], "elapsed")
        self.assertEqual(deadlines["registration_deadline"]["status"], "open")
        offsets = {datetime.fromisoformat(full[field]).utcoffset() for field in DEADLINES["external"]}
        self.assertEqual(len(offsets), 2)

    def test_import_identity(self):
        for catalog, data in self.catalogs.items():
            with self.subTest(catalog=catalog):
                cards, steps = data["cards"], data["imports"]
                by_identity = {}
                for step in steps:
                    source = data["sources"][cards[step["card"]]["source"]]
                    identity = (source["provider"], source["record_id"], source["edition_ref"])
                    key = step["expected_key"]
                    self.assert_text(key)
                    if identity in by_identity:
                        self.assertEqual(key, by_identity[identity])
                    else:
                        self.assertNotIn(key, by_identity.values())
                        by_identity[identity] = key
                    self.assertEqual(step["expected_count"], len(by_identity))
                self.assertEqual({step["card"] for step in steps}, set(cards))
                self.assertEqual([step["card"] for step in steps[:3]], ["full", "full", "incomplete"])
                self.assertEqual([step["expected_count"] for step in steps[:3]], [1, 1, 2])

                first, following = (data["sources"][cards[case]["source"]] for case in ("full", "incomplete"))
                for attr in ("provider", "record_id", "url"):
                    self.assertEqual(first[attr], following[attr])
                self.assertNotEqual(first["edition_ref"], following["edition_ref"])
                old, new = (cards[case]["fields"] for case in ("full", "incomplete"))
                if catalog == "internal":
                    old_date, new_date = old["period"]["start"], new["period"]["start"]
                else:
                    self.assertEqual(old["first_held_year"], new["first_held_year"])
                    old_date, new_date = old["event_date"], new["event_date"]
                self.assertEqual(date.fromisoformat(old_date).year + 1, date.fromisoformat(new_date).year)


if __name__ == "__main__":
    unittest.main()
