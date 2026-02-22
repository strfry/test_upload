from __future__ import annotations

import unittest

from scambaiter.telethon_lookup import match_dialogs, normalize_text, resolve_unique_dialog


class TelethonLookupTests(unittest.TestCase):
    def test_normalize_text_removes_diacritics_and_symbols(self) -> None:
        self.assertEqual("celine allard", normalize_text("Céline 🌹 Allard"))

    def test_match_dialogs_matches_title_token(self) -> None:
        rows = [
            {"chat_id": 1, "title": "Julia 🌹", "username": "Julia83918"},
            {"chat_id": 2, "title": "Robert Nowak", "username": "Robert1245775"},
        ]
        matches = match_dialogs(rows, "julia")
        self.assertEqual(1, len(matches))
        self.assertEqual(1, int(matches[0]["chat_id"]))

    def test_match_dialogs_matches_username_token(self) -> None:
        rows = [
            {"chat_id": 1, "title": "Unknown", "username": "Julia83918"},
            {"chat_id": 2, "title": "Julia", "username": "other"},
        ]
        matches = match_dialogs(rows, "julia839")
        self.assertEqual(1, len(matches))
        self.assertEqual(1, int(matches[0]["chat_id"]))

    def test_match_dialogs_sorts_deterministically_by_score_then_chat_id(self) -> None:
        rows = [
            {"chat_id": 9, "title": "Julia Rose", "username": None},
            {"chat_id": 3, "title": "Julia Rose", "username": None},
        ]
        matches = match_dialogs(rows, "julia rose")
        self.assertEqual([3, 9], [int(item["chat_id"]) for item in matches])

    def test_resolve_unique_dialog_none(self) -> None:
        status, matches = resolve_unique_dialog(
            rows=[{"chat_id": 1, "title": "Alice", "username": "alice1"}],
            query="julia",
        )
        self.assertEqual("none", status)
        self.assertEqual([], matches)

    def test_resolve_unique_dialog_single(self) -> None:
        status, matches = resolve_unique_dialog(
            rows=[{"chat_id": 1, "title": "Julia", "username": "julia1"}],
            query="julia",
        )
        self.assertEqual("single", status)
        self.assertEqual(1, len(matches))

    def test_resolve_unique_dialog_multiple(self) -> None:
        status, matches = resolve_unique_dialog(
            rows=[
                {"chat_id": 1, "title": "Julia", "username": "julia1"},
                {"chat_id": 2, "title": "Julia Rose", "username": "rosejulia"},
            ],
            query="julia",
        )
        self.assertEqual("multiple", status)
        self.assertEqual(2, len(matches))

    def test_match_dialogs_empty_query(self) -> None:
        rows = [{"chat_id": 1, "title": "Julia", "username": None}]
        self.assertEqual([], match_dialogs(rows, ""))


if __name__ == "__main__":
    unittest.main()
