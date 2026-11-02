"""The story pipeline: analyze -> write -> judge -> revise (loop) -> optional listener feedback."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Literal

from pydantic import BaseModel, Field

import prompts
from llm import call_model, call_structured

PASS_SCORE = 8      # every criterion must reach this for a draft to pass
MAX_REVISIONS = 2   # judge-driven rewrites before we stop and keep the best draft

Log = Callable[[str], None]


# Schemas for the analyzer and judge outputs

class StoryBrief(BaseModel):
    title_idea: str
    category: Literal["adventure", "friendship", "animals", "magic", "silly", "feelings"]
    characters: list[str]
    setting: str
    must_include: list[str] = Field(description="Specific details from the request that must appear in the story")
    gentle_theme: str
    safety_adjustment: str = Field(description="How an unsuitable request was reframed; empty if not needed")


class CriterionScore(BaseModel):
    # The note comes before the score so the judge gives evidence before it decides.
    note: str
    score: int = Field(description="1-10")


class JudgeVerdict(BaseModel):
    request_fidelity: CriterionScore
    age_fit: CriterionScore
    structure: CriterionScore
    engagement: CriterionScore
    bedtime_landing: CriterionScore
    read_aloud: CriterionScore
    safety_concerns: list[str]
    strengths: list[str]
    revision_instructions: list[str]

    def scores(self) -> dict[str, int]:
        criteria = ["request_fidelity", "age_fit", "structure", "engagement", "bedtime_landing", "read_aloud"]
        return {name: max(1, min(10, getattr(self, name).score)) for name in criteria}

    def passed(self) -> bool:
        # The pass decision is made in code, not by the model, so it stays consistent.
        return not self.safety_concerns and min(self.scores().values()) >= PASS_SCORE

    def average(self) -> float:
        values = self.scores().values()
        return sum(values) / len(values)

    def scorecard(self) -> str:
        cells = "  ".join(f"{name}={score}" for name, score in self.scores().items())
        status = "PASS" if self.passed() else "needs work"
        safety = f"  safety: {len(self.safety_concerns)} concern(s)" if self.safety_concerns else ""
        return f"avg {self.average():.1f} [{status}]  {cells}{safety}"


class PairwiseVerdict(BaseModel):
    # Reasoning first, then the decision, same as the rubric judge.
    comparison: str
    winner: Literal["A", "B", "tie"]
    reason: str = Field(description="One sentence on why the winner is better")


@dataclass
class Draft:
    story: str
    verdict: JudgeVerdict
    label: str  # e.g. "draft 1", "draft 1 revision 2", "feedback 1"

    def is_safe(self) -> bool:
        return not self.verdict.safety_concerns


@dataclass
class StorySession:
    """Everything we know about one story, so listener feedback can build on it."""
    request: str
    age: int
    brief: StoryBrief
    best: Draft
    history: list[Draft] = field(default_factory=list)
    listener_changes: list[str] = field(default_factory=list)


# One function per role

def _band(age: int) -> dict:
    return prompts.AGE_GUIDES[prompts.age_band(age)]


def _join(items: list[str], empty: str = "none") -> str:
    return "; ".join(items) if items else empty


def _bullets(items: list[str], empty: str = "- (none)") -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty


def analyze_request(request: str, age: int) -> StoryBrief:
    category_list = "\n".join(f"- {name}: {guide}" for name, guide in prompts.CATEGORY_GUIDES.items())
    prompt = prompts.ANALYZER_PROMPT.format(age=age, category_list=category_list, request=request)
    return call_structured(prompt, StoryBrief, system=prompts.ANALYZER_SYSTEM, effort="low")


def write_story(request: str, age: int, brief: StoryBrief) -> str:
    band = _band(age)
    prompt = prompts.STORY_PROMPT.format(
        age=age,
        age_label=band["label"],
        title_idea=brief.title_idea,
        category=brief.category,
        characters=_join(brief.characters),
        setting=brief.setting,
        must_include=_join(brief.must_include),
        theme=brief.gentle_theme,
        safety_adjustment=brief.safety_adjustment or "not needed",
        category_guide=prompts.CATEGORY_GUIDES[brief.category],
        language=band["language"],
        words=band["words"],
        request=request,
    )
    return call_model(prompt, system=prompts.STORYTELLER_SYSTEM)


def judge_story(story: str, session_request: str, age: int, brief: StoryBrief,
                listener_changes: list[str]) -> JudgeVerdict:
    band = _band(age)
    prompt = prompts.JUDGE_PROMPT.format(
        age=age,
        age_label=band["label"],
        words=band["words"],
        word_count=len(story.split()),
        must_include=_join(brief.must_include),
        category=brief.category,
        extra_requests=_join(listener_changes),
        safety_adjustment=brief.safety_adjustment or "not needed",
        request=session_request,
        story=story,
    )
    return call_structured(prompt, JudgeVerdict, system=prompts.JUDGE_SYSTEM)


def revise_story(story: str, age: int, brief: StoryBrief, verdict: JudgeVerdict,
                 listener_changes: list[str]) -> str:
    prompt = prompts.REVISION_PROMPT.format(
        words=_band(age)["words"],
        word_count=len(story.split()),
        must_include=_join(brief.must_include),
        listener_changes=_join(listener_changes),
        strengths=_bullets(verdict.strengths),
        # Safety concerns are always fixed, even if the judge did not repeat them as instructions.
        instructions=_bullets(verdict.safety_concerns + verdict.revision_instructions),
        story=story,
    )
    return call_model(prompt, system=prompts.STORYTELLER_SYSTEM)


def apply_listener_feedback(story: str, age: int, brief: StoryBrief, feedback: str) -> str:
    prompt = prompts.FEEDBACK_PROMPT.format(
        age=age,
        words=_band(age)["words"],
        word_count=len(story.split()),
        must_include=_join(brief.must_include),
        feedback=feedback,
        story=story,
    )
    return call_model(prompt, system=prompts.STORYTELLER_SYSTEM)


def compare_stories(story_a: str, story_b: str, age: int, brief: StoryBrief,
                    listener_changes: list[str]) -> PairwiseVerdict:
    band = _band(age)
    prompt = prompts.PAIRWISE_PROMPT.format(
        age=age,
        age_label=band["label"],
        words=band["words"],
        must_include=_join(brief.must_include),
        extra_requests=_join(listener_changes),
        safety_adjustment=brief.safety_adjustment or "not needed",
        story_a=story_a,
        story_b=story_b,
    )
    return call_structured(prompt, PairwiseVerdict, system=prompts.PAIRWISE_SYSTEM)


def pick_better_draft(champion: Draft, challenger: Draft, session: StorySession) -> tuple[Draft, str]:
    """Decide which draft to keep. Returns the winner and a short explanation for the log."""
    # Safety comes first and is decided in code.
    if champion.is_safe() != challenger.is_safe():
        winner = champion if champion.is_safe() else challenger
        return winner, f"{winner.label} kept: the other draft has safety concerns"

    # LLM judges tend to favor one position, so ask twice with the order swapped, in parallel.
    args = (session.age, session.brief, session.listener_changes)
    with ThreadPoolExecutor(max_workers=2) as pool:
        champion_first = pool.submit(compare_stories, champion.story, challenger.story, *args)
        challenger_first = pool.submit(compare_stories, challenger.story, champion.story, *args)
        first, second = champion_first.result(), challenger_first.result()

    # Translate each answer back to a draft ("A" means a different draft in each order).
    pick_1 = {"A": champion, "B": challenger}.get(first.winner)
    pick_2 = {"A": challenger, "B": champion}.get(second.winner)

    # In the first comparison A is the champion and B the challenger; use their names in the log.
    reason = first.reason.replace("Story A", champion.label).replace("Story B", challenger.label)
    if pick_1 is challenger and pick_2 is challenger:
        return challenger, f"{challenger.label} wins in both orders: {reason}"
    if pick_1 is champion and pick_2 is champion:
        return champion, f"{champion.label} wins in both orders: {reason}"
    # A tie, or the answer flipped when the order was swapped: not a real preference.
    return champion, f"no clear winner when the order is swapped, so {champion.label} stays"


# Orchestration

def _improve_until_pass(session: StorySession, log: Log, label_prefix: str) -> None:
    """Revise the current best draft, then let the pairwise judge decide whether the revision replaces it."""
    for round_number in range(1, MAX_REVISIONS + 1):
        best = session.best
        if best.verdict.passed():
            break
        log(f"Revising {best.label} with the judge's notes ({round_number}/{MAX_REVISIONS})...")
        story = revise_story(best.story, session.age, session.brief, best.verdict, session.listener_changes)
        verdict = judge_story(story, session.request, session.age, session.brief, session.listener_changes)
        revision = Draft(story, verdict, f"{label_prefix} revision {round_number}")
        session.history.append(revision)
        log(f"  {revision.label}: {verdict.scorecard()}")

        log(f"  comparing {revision.label} with {best.label}...")
        session.best, explanation = pick_better_draft(best, revision, session)
        log(f"  {explanation}")


def create_story(request: str, age: int, log: Log = lambda _: None) -> StorySession:
    log("Planning the story...")
    brief = analyze_request(request, age)
    log(f"  category: {brief.category} | must include: {_join(brief.must_include)}")
    if brief.safety_adjustment:
        log(f"  made bedtime-friendly: {brief.safety_adjustment}")

    log("Writing the first draft...")
    story = write_story(request, age, brief)
    verdict = judge_story(story, request, age, brief, [])
    draft = Draft(story, verdict, "draft 1")
    log(f"  {draft.label}: {verdict.scorecard()}")

    session = StorySession(request=request, age=age, brief=brief, best=draft, history=[draft])
    _improve_until_pass(session, log, label_prefix="draft 1")
    return session


def revise_with_feedback(session: StorySession, feedback: str, log: Log = lambda _: None) -> StorySession:
    session.listener_changes.append(feedback)
    log("Rewriting with your changes...")
    story = apply_listener_feedback(session.best.story, session.age, session.brief, feedback)
    verdict = judge_story(story, session.request, session.age, session.brief, session.listener_changes)
    label = f"feedback {len(session.listener_changes)}"
    draft = Draft(story, verdict, label)
    session.history.append(draft)
    log(f"  {draft.label}: {verdict.scorecard()}")
    # The listener asked for this change, so it becomes the new baseline even if it scores lower.
    session.best = draft
    _improve_until_pass(session, log, label_prefix=label)
    return session
