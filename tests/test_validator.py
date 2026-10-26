"""Focused checks for important SQL safety rules."""

import unittest

from sql_mcp import validator


class HiddenWriteTests(unittest.TestCase):
    def test_select_with_hidden_delete_is_detected(self):
        parsed = validator.parse_one(
            "WITH removed AS (DELETE FROM film RETURNING film_id) "
            "SELECT film_id FROM removed"
        )

        self.assertTrue(parsed["ok"])
        self.assertIsNotNone(
            validator.select_hides_a_write(parsed["expression"])
        )


if __name__ == "__main__":
    unittest.main()
