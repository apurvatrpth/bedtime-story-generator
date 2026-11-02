"""
Bedtime story generator for children ages 5-10, using Claude Haiku 5.5 as storyteller and judge.

Before submitting the assignment, describe here in a few sentences what you would have built next if you spent 2 more hours on this project:

With two more hours I would build an evaluation harness: a fixed set of about 20 requests (vague,
specific, scary, multi-character, and prompt-injection attempts) run in one batch, recording judge
scores before and after revision, so every prompt change can be measured instead of judged by eye.
I would calibrate the judge by scoring a few stories myself and checking that it agrees, and try a
different model family as judge to reduce self-preference bias, since writer and judge are both Haiku today.
On the product side I would add a "story universe" that saves a child's favorite characters and
preferences (for example "no dogs, she's scared of them") to a small JSON profile, so tomorrow's
story can bring back Bob the cat, plus streaming output so the parent can start reading sooner.
"""

import argparse
import sys

import anthropic

from llm import StoryError
from pipeline import StorySession, create_story, revise_with_feedback

EXAMPLE_REQUEST = "A story about a girl named Alice and her best friend Bob, who happens to be a cat."
DEFAULT_AGE = 7


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a bedtime story for ages 5-10.")
    parser.add_argument("--request", help="the story request (otherwise you are asked)")
    parser.add_argument("--age", type=int, help="the listener's age, 5-10 (otherwise you are asked)")
    parser.add_argument("--no-feedback", action="store_true", help="skip the 'any changes?' step")
    parser.add_argument("--verbose", action="store_true", help="show the judge's notes for every draft")
    return parser.parse_args()


def ask_age() -> int:
    answer = input(f"How old is the listener? (5-10, press Enter for {DEFAULT_AGE}) ").strip()
    return int(answer) if answer.isdigit() else DEFAULT_AGE


def show_judge_notes(session: StorySession) -> None:
    print("\n--- Judge's notes, draft by draft ---")
    for draft in session.history:
        print(f"\n{draft.label}: {draft.verdict.scorecard()}")
        for name in draft.verdict.scores():
            print(f"  {name}: {getattr(draft.verdict, name).note}")
        for concern in draft.verdict.safety_concerns:
            print(f"  SAFETY: {concern}")
        for instruction in draft.verdict.revision_instructions:
            print(f"  -> {instruction}")


def show_story(session: StorySession, verbose: bool) -> None:
    if verbose:
        show_judge_notes(session)
    best = session.best
    print("\n" + "=" * 70)
    print(best.story)
    print("=" * 70)
    print(f"({best.label}, {len(best.story.split())} words, judge: {best.verdict.scorecard()})")


def run(args: argparse.Namespace) -> None:
    request = args.request or input("What kind of story do you want to hear? ").strip()
    if not request:
        request = EXAMPLE_REQUEST
        print(f"No idea? Here's one: {request}")
    age = args.age if args.age is not None else ask_age()
    age = max(5, min(10, age))

    session = create_story(request, age, log=print)
    show_story(session, args.verbose)

    if args.no_feedback:
        return
    while True:
        feedback = input("\nWant any changes? Describe them, or press Enter to finish: ").strip()
        if not feedback:
            print("Story generation completed.")
            return
        revise_with_feedback(session, feedback, log=print)
        show_story(session, args.verbose)


def main() -> None:
    # Stories use curly quotes and dashes; keep them printable on any Windows console.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        run(parse_args())
    except StoryError as error:
        sys.exit(f"Sorry: {error}")
    except anthropic.AuthenticationError:
        sys.exit("Sorry: the API key was rejected. Check ANTHROPIC_API_KEY.")
    except anthropic.APIConnectionError:
        sys.exit("Sorry: could not reach the Anthropic API. Check your internet connection.")
    except anthropic.APIStatusError as error:
        sys.exit(f"Sorry: the API returned an error ({error.status_code}): {error.message}")
    except (KeyboardInterrupt, EOFError):
        print("\nGoodnight!")


if __name__ == "__main__":
    main()
