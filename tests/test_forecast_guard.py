"""Duplicate-forecast guard and group/single idempotency."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from forecast_guard import (
    decide_forecasts,
    filter_sdk_questions,
    get_question_dict_from_post,
    iter_questions_from_post,
    mark_questions_forecasted,
    question_already_forecasted,
    should_skip_sdk_question,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "posts.json"


def _posts() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["results"]


def _by_id(posts: list[dict], post_id: int) -> dict:
    return next(post for post in posts if post["id"] == post_id)


class TestAlreadyForecasted(unittest.TestCase):
    def test_missing_and_null_are_not_forecasted(self) -> None:
        self.assertFalse(question_already_forecasted(None))
        self.assertFalse(question_already_forecasted({}))
        self.assertFalse(question_already_forecasted({"my_forecasts": None}))
        self.assertFalse(question_already_forecasted({"my_forecasts": {}}))
        self.assertFalse(
            question_already_forecasted({"my_forecasts": {"latest": None, "history": []}})
        )

    def test_latest_forecast_values_count(self) -> None:
        self.assertTrue(
            question_already_forecasted(
                {"my_forecasts": {"latest": {"forecast_values": [0.2, 0.8]}}}
            )
        )

    def test_history_without_latest_counts(self) -> None:
        self.assertTrue(
            question_already_forecasted(
                {"my_forecasts": {"history": [{"forecast_values": [0.1, 0.9]}]}}
            )
        )

    def test_latest_null_values_without_history_does_not_count(self) -> None:
        self.assertFalse(
            question_already_forecasted(
                {"my_forecasts": {"latest": {"forecast_values": None}, "history": []}}
            )
        )


class TestPostExpansion(unittest.TestCase):
    def test_single_question_post(self) -> None:
        targets = iter_questions_from_post(_by_id(_posts(), 101))
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].kind, "single")
        self.assertEqual(targets[0].question_id, 1001)
        self.assertFalse(targets[0].already_forecasted)

    def test_group_expands_each_member(self) -> None:
        targets = iter_questions_from_post(_by_id(_posts(), 200))
        self.assertEqual([t.question_id for t in targets], [2001, 2002, 2003])
        self.assertTrue(all(t.kind == "group_member" for t in targets))
        self.assertTrue(targets[0].already_forecasted)
        self.assertFalse(targets[1].already_forecasted)

    def test_conditional_expands_yes_and_no_legs(self) -> None:
        targets = iter_questions_from_post(_by_id(_posts(), 300))
        self.assertEqual([t.question_id for t in targets], [3001, 3002])
        self.assertTrue(all(t.kind == "conditional" for t in targets))
        self.assertFalse(targets[0].already_forecasted)
        self.assertTrue(targets[1].already_forecasted)

    def test_notebook_yields_nothing(self) -> None:
        self.assertEqual(iter_questions_from_post(_by_id(_posts(), 400)), [])

    def test_lookup_by_question_id_works_for_group_members(self) -> None:
        post = _by_id(_posts(), 200)
        found = get_question_dict_from_post(post, 2002)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["title"], "Investment")
        self.assertIsNone(get_question_dict_from_post(post, 9999))


class TestDecideForecasts(unittest.TestCase):
    def test_skips_already_forecasted_single(self) -> None:
        decision = decide_forecasts(_posts())
        self.assertIn(1001, decision.target_ids)
        self.assertIn(1002, decision.skipped_ids)
        self.assertNotIn(1002, decision.target_ids)

    def test_group_only_retries_unforecasted_open_members(self) -> None:
        decision = decide_forecasts(_posts())
        self.assertIn(2002, decision.target_ids)
        self.assertIn(2001, decision.skipped_ids)
        self.assertIn(2003, decision.skipped_ids)
        self.assertNotIn(2001, decision.target_ids)
        self.assertNotIn(2003, decision.target_ids)

    def test_fully_forecasted_group_is_a_no_op(self) -> None:
        decision = decide_forecasts([_by_id(_posts(), 201)])
        self.assertEqual(decision.target_ids, ())
        self.assertEqual(set(decision.skipped_ids), {2101, 2102})

    def test_force_reforecast_includes_already_forecasted_open_questions(self) -> None:
        decision = decide_forecasts(_posts(), skip_already=False)
        self.assertIn(1002, decision.target_ids)
        self.assertIn(2001, decision.target_ids)
        self.assertNotIn(2003, decision.target_ids)

    def test_conditional_legs_are_independent(self) -> None:
        decision = decide_forecasts([_by_id(_posts(), 300)])
        self.assertEqual(decision.target_ids, (3001,))
        self.assertEqual(decision.skipped_ids, (3002,))

    def test_second_pass_is_idempotent_for_single_and_group(self) -> None:
        posts = _posts()
        first = decide_forecasts(posts)
        self.assertTrue(first.targets)
        after = mark_questions_forecasted(posts, first.target_ids)
        second = decide_forecasts(after)
        self.assertEqual(second.target_ids, ())
        self.assertTrue(set(first.target_ids).issubset(set(second.skipped_ids)))

        third = decide_forecasts(mark_questions_forecasted(after, second.target_ids))
        self.assertEqual(third.target_ids, ())

    def test_mutating_one_group_member_does_not_skip_its_sibling(self) -> None:
        post = copy.deepcopy(_by_id(_posts(), 200))
        post["group_of_questions"]["questions"][1]["my_forecasts"] = {
            "latest": {"forecast_values": [0.0, 1.0]}
        }
        decision = decide_forecasts([post])
        self.assertNotIn(2002, decision.target_ids)
        self.assertIn(2002, decision.skipped_ids)


class TestSdkQuestionFilter(unittest.TestCase):
    def test_uses_already_forecasted_flag(self) -> None:
        q = SimpleNamespace(already_forecasted=True, api_json={})
        self.assertTrue(should_skip_sdk_question(q, skip_already=True))
        self.assertFalse(should_skip_sdk_question(q, skip_already=False))

    def test_falls_back_to_api_json_for_group_member(self) -> None:
        post = _by_id(_posts(), 200)
        q = SimpleNamespace(
            already_forecasted=None,
            id_of_question=2001,
            api_json=post,
        )
        self.assertTrue(should_skip_sdk_question(q, skip_already=True))
        sibling = SimpleNamespace(
            already_forecasted=None,
            id_of_question=2002,
            api_json=post,
        )
        self.assertFalse(should_skip_sdk_question(sibling, skip_already=True))

    def test_filter_partitions_kept_and_skipped(self) -> None:
        questions = [
            SimpleNamespace(already_forecasted=True, api_json={}),
            SimpleNamespace(already_forecasted=False, api_json={}),
        ]
        kept, skipped = filter_sdk_questions(questions, skip_already=True)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(skipped), 1)


if __name__ == "__main__":
    unittest.main()
