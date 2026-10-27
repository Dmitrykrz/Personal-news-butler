"""extract_json against replies Claude really sent. Run: python -m unittest discover -s tests"""
import unittest

from newsagent.claude import ClaudeError, extract_json

# 2026-10-26 shortlist: Claude corrected itself after the first answer
SELF_CORRECTED = """{"shortlist": ["c7", "c1", "c3", "c5", "c4", "c6", "c13", "c12", "c11", "c40", "c33", "c80", "c66"]}

Wait — I need to correct that: I included items that don't fit. Final answer:

{"shortlist": ["c7", "c1", "c3", "c5", "c6", "c13", "c11", "c40"]}"""

# 2026-10-24 final (trimmed): the last string is closed with a curly quote
CURLY_CLOSE = ('{"items":[{"candidate":"c2","title":"A Nintendo NES turned into a desktop PC",'
               '"summary":"The NES only handles I/O.","url":"https://www.hackster.io/news/x","kind":"news"}],'
               '"notes":"Skipped the Denisovan fossils item (article text unavailable).”}')


class ExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"shortlist": ["c1"]}', "shortlist"), {"shortlist": ["c1"]})

    def test_fence_and_prose(self):
        reply = 'Here you go:\n```json\n{"shortlist": ["c1", "c2"]}\n```\nHope that helps {really}.'
        self.assertEqual(extract_json(reply, "shortlist")["shortlist"], ["c1", "c2"])

    def test_self_correction_takes_last(self):
        self.assertEqual(extract_json(SELF_CORRECTED, "shortlist")["shortlist"],
                         ["c7", "c1", "c3", "c5", "c6", "c13", "c11", "c40"])

    def test_curly_closing_quote(self):
        result = extract_json(CURLY_CLOSE, "items")
        self.assertEqual(result["items"][0]["candidate"], "c2")
        self.assertTrue(result["notes"].endswith("unavailable)."))

    def test_curly_quotes_inside_text_survive(self):
        reply = '{"items": [], "notes": "He called it “bold”, then left."}'
        self.assertEqual(extract_json(reply, "items")["notes"], "He called it “bold”, then left.")

    def test_curly_quote_repair_keeps_inner_quotes(self):
        reply = '{"items": [], "notes": "He called it “bold”, then left.”}'
        self.assertEqual(extract_json(reply, "items")["notes"], "He called it “bold”, then left.")

    def test_raw_newline_in_string(self):
        self.assertEqual(extract_json('{"items": [], "notes": "line one\nline two"}', "items")["notes"],
                         "line one\nline two")

    def test_nested_object_with_key_is_not_mistaken(self):
        reply = '{"story": "s", "outlets": [{"id": "lemonde", "query": "q"}]}'
        self.assertEqual(extract_json(reply, "outlets")["story"], "s")

    def test_error_does_not_include_reply(self):
        with self.assertRaises(ClaudeError) as ctx:
            extract_json('{"items": [{"title": "secret article text', "items")
        self.assertNotIn("secret", str(ctx.exception))

    def test_missing_key(self):
        with self.assertRaises(ClaudeError):
            extract_json('{"other": 1}', "items")


if __name__ == "__main__":
    unittest.main()
