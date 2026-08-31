"""
Duplicate-forecast and publish-policy guards.

This module is stdlib-only on purpose: unit tests and the offline dry-run
must run without forecasting-tools, API keys, or any Metaculus write.

Hard rails:
- resolve_publish_intent() is False unless Charles passes --submit *and*
  nothing forces a dry-run.
- assert_submit_allowed() / install_dry_run_network_guard() refuse every
  Metaculus forecast/comment POST when dry-run is active.
- decide_forecasts() is idempotent for both single-question posts and
  group posts (each sub-question is skipped independently).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping, Sequence
from urllib.parse import urlparse


_TRUTHY = {"1", "true", "yes", "on"}

# Metaculus write endpoints used by this template and by forecasting-tools.
_METACULUS_WRITE_PATH_FRAGMENTS = (
    "/questions/forecast",
    "/comments/create",
    "/forecasts/",
)

QuestionKind = Literal["single", "group_member", "conditional"]


class SubmitBlocked(RuntimeError):
    """Raised when a Metaculus write is attempted while dry-run is active."""


# Process-wide latch. False until Charles explicitly opts in via --submit
# *and* no dry-run override is set. Default False is the safety property.
_publish_allowed: bool = False
_network_guard_installed: bool = False
_original_session_request = None
_blocked_writes: list[str] = []


def set_publish_allowed(allowed: bool) -> None:
    global _publish_allowed
    _publish_allowed = bool(allowed)


def publish_is_allowed() -> bool:
    return _publish_allowed


def env_forces_dry_run(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    raw = (environ.get("METACULUS_DRY_RUN") or "").strip().lower()
    return raw in _TRUTHY


def resolve_publish_intent(
    *,
    submit: bool = False,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """
    Return True only when a submit is explicitly requested and nothing
    overrides it. Default (no flags, no env) is dry-run.
    """
    if dry_run or env_forces_dry_run(environ):
        return False
    return bool(submit)


def assert_submit_allowed(url: str | None = None) -> None:
    if _publish_allowed:
        return
    detail = f" ({url})" if url else ""
    raise SubmitBlocked(
        f"Dry-run: refusing to submit to Metaculus{detail}. "
        "Charles submits with --submit (and METACULUS_DRY_RUN unset)."
    )


def is_metaculus_write(method: str, url: str) -> bool:
    """True for POST/PUT/PATCH/DELETE to Metaculus forecast/comment endpoints."""
    if (method or "").upper() in {"GET", "HEAD", "OPTIONS"}:
        return False
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if "metaculus.com" not in host:
        return False
    path = parsed.path or ""
    return any(fragment in path for fragment in _METACULUS_WRITE_PATH_FRAGMENTS)


def blocked_write_log() -> list[str]:
    return list(_blocked_writes)


def install_dry_run_network_guard() -> None:
    """
    Patch requests.Session.request so a forgotten publish=True cannot POST
    a forecast or comment. Safe to call more than once. No-op if requests
    is not installed.
    """
    global _network_guard_installed, _original_session_request
    if _network_guard_installed:
        return
    try:
        import requests
    except ImportError:
        return

    _original_session_request = requests.sessions.Session.request

    def _guarded_request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        if is_metaculus_write(str(method), str(url)):
            _blocked_writes.append(f"{method} {url}")
            assert_submit_allowed(str(url))
        return _original_session_request(self, method, url, *args, **kwargs)

    requests.sessions.Session.request = _guarded_request  # type: ignore[method-assign]
    _network_guard_installed = True


def uninstall_dry_run_network_guard() -> None:
    """Test helper: restore the original requests.Session.request."""
    global _network_guard_installed, _original_session_request
    if not _network_guard_installed:
        return
    try:
        import requests

        if _original_session_request is not None:
            requests.sessions.Session.request = _original_session_request  # type: ignore[method-assign]
    except ImportError:
        pass
    _network_guard_installed = False
    _original_session_request = None


def question_already_forecasted(question: Mapping[str, Any] | None) -> bool:
    """
    True if this question payload already has a standing forecast from us.

    Metaculus puts the bot's own forecast under question.my_forecasts:
      - latest.forecast_values is non-null for an active forecast
      - history is a non-empty list of past forecasts
    Missing/malformed my_forecasts means "not forecasted" (same as the SDK).
    """
    if not question:
        return False
    mine = question.get("my_forecasts")
    if not isinstance(mine, dict):
        return False
    latest = mine.get("latest")
    if isinstance(latest, dict) and latest.get("forecast_values") is not None:
        return True
    history = mine.get("history")
    return isinstance(history, list) and len(history) > 0


def _status(question: Mapping[str, Any]) -> str:
    return str(question.get("status") or "").lower()


def _title(question: Mapping[str, Any], fallback: str = "") -> str:
    return str(question.get("title") or question.get("label") or fallback or "")


def _question_type(question: Mapping[str, Any]) -> str:
    return str(question.get("type") or "unknown")


def _question_id(question: Mapping[str, Any]) -> int | None:
    raw = question.get("id")
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class QuestionTarget:
    """One forecastable unit: a single question or one group sub-question."""

    question_id: int
    post_id: int
    title: str
    question_type: str
    status: str
    already_forecasted: bool
    kind: QuestionKind
    question: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class ForecastDecision:
    """Idempotent plan for one or more posts."""

    targets: tuple[QuestionTarget, ...]
    skipped: tuple[QuestionTarget, ...]

    @property
    def target_ids(self) -> tuple[int, ...]:
        return tuple(t.question_id for t in self.targets)

    @property
    def skipped_ids(self) -> tuple[int, ...]:
        return tuple(t.question_id for t in self.skipped)


def _group_question_list(group: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("questions", "sub_questions"):
        items = group.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _conditional_legs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for key in ("question_yes", "question_no"):
        leg = payload.get(key)
        if isinstance(leg, dict):
            legs.append(leg)
    return legs


def iter_questions_from_post(post: Mapping[str, Any]) -> list[QuestionTarget]:
    """
    Expand a Metaculus post into forecastable question targets.

    - Single: post.question
    - Group:  post.group_of_questions.questions (or sub_questions)
    - Conditional: post.conditional or a question with yes/no legs
    Notebooks and empty posts yield nothing.
    """
    post_id_raw = post.get("id")
    try:
        post_id = int(post_id_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []

    post_title = str(post.get("title") or "")
    targets: list[QuestionTarget] = []

    group = post.get("group_of_questions")
    if isinstance(group, dict):
        for question in _group_question_list(group):
            qid = _question_id(question)
            if qid is None:
                continue
            targets.append(
                QuestionTarget(
                    question_id=qid,
                    post_id=post_id,
                    title=_title(question, post_title),
                    question_type=_question_type(question),
                    status=_status(question),
                    already_forecasted=question_already_forecasted(question),
                    kind="group_member",
                    question=dict(question),
                )
            )
        return targets

    conditional = post.get("conditional")
    question = post.get("question")
    if isinstance(conditional, dict):
        payload = conditional
        kind: QuestionKind = "conditional"
    elif isinstance(question, dict) and (
        question.get("type") == "conditional"
        or "question_yes" in question
        or "question_no" in question
    ):
        payload = question
        kind = "conditional"
    elif isinstance(question, dict):
        qid = _question_id(question)
        if qid is None:
            return []
        return [
            QuestionTarget(
                question_id=qid,
                post_id=post_id,
                title=_title(question, post_title),
                question_type=_question_type(question),
                status=_status(question),
                already_forecasted=question_already_forecasted(question),
                kind="single",
                question=dict(question),
            )
        ]
    else:
        return []

    if kind == "conditional":
        legs = _conditional_legs(payload)
        if legs:
            for leg in legs:
                qid = _question_id(leg)
                if qid is None:
                    continue
                targets.append(
                    QuestionTarget(
                        question_id=qid,
                        post_id=post_id,
                        title=_title(leg, post_title),
                        question_type=_question_type(leg) or _question_type(payload),
                        status=_status(leg) or _status(payload),
                        already_forecasted=question_already_forecasted(leg),
                        kind="conditional",
                        question=dict(leg),
                    )
                )
            return targets
        qid = _question_id(payload)
        if qid is None:
            return []
        already = question_already_forecasted(payload) or any(
            question_already_forecasted(leg) for leg in _conditional_legs(payload)
        )
        return [
            QuestionTarget(
                question_id=qid,
                post_id=post_id,
                title=_title(payload, post_title),
                question_type=_question_type(payload) or "conditional",
                status=_status(payload),
                already_forecasted=already,
                kind="conditional",
                question=dict(payload),
            )
        ]
    return targets


def get_question_dict_from_post(
    post: Mapping[str, Any], question_id: int
) -> dict[str, Any] | None:
    for target in iter_questions_from_post(post):
        if target.question_id == question_id:
            return target.question
    return None


def decide_forecasts(
    posts: Sequence[Mapping[str, Any]],
    *,
    skip_already: bool = True,
    include_statuses: Iterable[str] = ("open",),
) -> ForecastDecision:
    """
    Choose which questions to forecast.

    Single and group posts use the same rule: an open question is a target
    unless skip_already and it already has our forecast. Group members are
    independent, so a half-forecasted group only retries the missing legs.
    A second pass over the same posts is a no-op once those legs are marked.
    """
    allowed = {status.lower() for status in include_statuses}
    targets: list[QuestionTarget] = []
    skipped: list[QuestionTarget] = []
    seen: set[int] = set()

    for post in posts:
        for target in iter_questions_from_post(post):
            if target.question_id in seen:
                continue
            seen.add(target.question_id)
            if allowed and target.status and target.status not in allowed:
                skipped.append(target)
                continue
            if skip_already and target.already_forecasted:
                skipped.append(target)
                continue
            targets.append(target)
    return ForecastDecision(targets=tuple(targets), skipped=tuple(skipped))


def should_skip_sdk_question(question: Any, *, skip_already: bool) -> bool:
    """
    Extra skip check for forecasting-tools MetaculusQuestion objects.
    Uses already_forecasted when set; falls back to the raw API JSON.
    """
    if not skip_already:
        return False
    flag = getattr(question, "already_forecasted", None)
    if flag is True:
        return True
    if flag is False:
        return False
    api_json = getattr(question, "api_json", None) or {}
    if isinstance(api_json, dict):
        raw = api_json.get("question")
        if isinstance(raw, dict) and question_already_forecasted(raw):
            return True
        qid = getattr(question, "id_of_question", None)
        if qid is not None:
            raw = get_question_dict_from_post(api_json, int(qid))
            if raw is not None:
                return question_already_forecasted(raw)
    return False


def filter_sdk_questions(
    questions: Sequence[Any], *, skip_already: bool
) -> tuple[list[Any], list[Any]]:
    kept: list[Any] = []
    skipped: list[Any] = []
    for question in questions:
        if should_skip_sdk_question(question, skip_already=skip_already):
            skipped.append(question)
        else:
            kept.append(question)
    return kept, skipped


def mark_questions_forecasted(
    posts: Sequence[Mapping[str, Any]],
    question_ids: Iterable[int],
) -> list[dict[str, Any]]:
    """
    Deep-copy posts and stamp my_forecasts onto the given question ids.
    Used by the offline dry-run to prove a second pass is a no-op.
    """
    wanted = {int(qid) for qid in question_ids}
    copies = json.loads(json.dumps(list(posts)))
    stamp = {
        "latest": {"forecast_values": [0.4, 0.6]},
        "history": [{"forecast_values": [0.4, 0.6]}],
    }

    def _stamp(question: dict[str, Any]) -> None:
        qid = _question_id(question)
        if qid in wanted:
            question["my_forecasts"] = dict(stamp)

    for post in copies:
        if not isinstance(post, dict):
            continue
        question = post.get("question")
        if isinstance(question, dict):
            _stamp(question)
            for leg in _conditional_legs(question):
                _stamp(leg)
        group = post.get("group_of_questions")
        if isinstance(group, dict):
            for member in _group_question_list(group):
                _stamp(member)
        conditional = post.get("conditional")
        if isinstance(conditional, dict):
            _stamp(conditional)
            for leg in _conditional_legs(conditional):
                _stamp(leg)
    return copies
