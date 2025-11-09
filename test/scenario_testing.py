#!/usr/bin/env python3
"""Scenario tester that mirrors the production conversation flow."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Tuple

# Ensure project root on sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import ConversationApp, build_gpt_client, build_user, load_config

try:
    from db import (
        DB as DBConn,
        ensure_test_run_tables,
        insert_test_case,
        insert_test_run,
        update_test_run,
    )
except Exception:  # pragma: no cover - script fallback when deps missing
    DBConn = None
    ensure_test_run_tables = insert_message_from_gpt = insert_test_case = insert_test_run = update_test_run = None

DEFAULT_INTRO = "Hey! Quick check-in -- are you still seeing any pest activity?"


@dataclass
class TestConversationApp:
    app: ConversationApp
    intro_text: Optional[str]

    @property
    def phone(self) -> str:
        return self.app.phone

    @property
    def gpt(self):
        return self.app.gpt

    @property
    def user(self):
        return self.app.user

    @property
    def db_factory(self) -> Callable[[], DBConn]:
        return self.app.db_factory  # type: ignore[return-value]

    def reset_state(self) -> None:
        self.app.reset_state()

    def setup(self) -> None:
        self.app.reset_state()
        self.app.setup()
        intro = self.intro_text or self.app.intro_message or DEFAULT_INTRO
        if insert_message_from_gpt and intro:
            try:
                with self.db_factory() as db:
                    db.insert_message_from_gpt(self.phone, intro)
            except Exception:
                pass
        self.intro_text = intro

    def cleanup(self) -> None:
        try:
            self.app.reset_state()
        except Exception:
            pass

    def should_exit_stateful(self) -> bool:
        return self.app.should_exit_stateful()

    def handle_stop(self, text: str) -> None:
        self.app.handle_stop(text)

    def handle_user_turn(self, text: str) -> str:
        return self.app.handle_user_turn(text)


class BotTester:
    def __init__(self) -> None:
        self.cfg = load_config()
        if DBConn is None:
            raise RuntimeError("Database module unavailable; install dependencies before running scenarios.")

    def build_test_app(self, phone: Optional[str] = None) -> TestConversationApp:
        phone_number = phone or self.cfg["default_phone"]
        gpt = build_gpt_client(self.cfg)
        user = build_user(phone_number)
        app = ConversationApp(
            phone=phone_number,
            gpt=gpt,
            user=user,
            db_factory=DBConn,  # type: ignore[arg-type]
            intro_message=self.cfg.get("intro_message") or DEFAULT_INTRO,
        )
        harness = TestConversationApp(app=app, intro_text=self.cfg.get("intro_message") or DEFAULT_INTRO)
        harness.setup()
        return harness

    def test_scenario(
        self,
        name: str,
        steps: List[Tuple[str, str]],
        wait_time: float = 1.0,
    ) -> Tuple[bool, Optional[str], int]:
        print(f"\n[TEST] {name}")
        app = self.build_test_app()
        steps_verified = 0
        transcript: List[Tuple[str, str, str]] = []  # (Role, Text, FSM state after turn)
        verbose: List[str] = []

        try:
            initial_state = app.user.get_current_state()
        except Exception:
            initial_state = "unknown"
        transcript.append(("Bot", app.intro_text or DEFAULT_INTRO, initial_state))

        def print_sections() -> None:
            print("\n-- Transcript (clean) --")
            for role, text, state in transcript:
                print(f"{role}: {text}\t[{state}]")
            print("\n-- Details (verbose) --")
            for line in verbose:
                print(line)

        try:
            for i, (user_input, expected_state) in enumerate(steps):
                try:
                    if user_input.lower() in {"exit", "quit", "stop"}:
                        app.handle_stop(user_input)
                        current_state = app.user.get_current_state()
                        current_snapshot = app.user.get_fsm_snapshot()
                        transcript.append(("User", user_input, current_state))
                        verbose.append(
                            f"(stop) Expected={expected_state}, actual={current_state}, snapshot={current_snapshot}"
                        )
                        if current_state == expected_state:
                            steps_verified += 1
                            verbose.append("(stop) Matched expected state; exiting scenario.")
                        else:
                            print_sections()
                            return False, (
                                f"Step {i + 1}: expected state '{expected_state}' but observed '{current_state}'"
                            ), steps_verified
                        break

                    response = app.handle_user_turn(user_input)
                    time.sleep(wait_time)

                    current_snapshot = app.user.get_fsm_snapshot()
                    current_state = app.user.get_current_state()
                    transcript.append(("User", user_input, current_state))
                    transcript.append(("Bot", response, current_state))

                    verbose.append(
                        f"[{i + 1}] user='{user_input}' -> state='{current_state}', expected='{expected_state}', snapshot={current_snapshot}"
                    )

                    if current_state != expected_state:
                        print_sections()
                        return False, (
                            f"Step {i + 1}: expected state '{expected_state}' but observed '{current_state}'"
                        ), steps_verified

                    steps_verified += 1

                    if app.should_exit_stateful():
                        verbose.append("Conversation indicated exit condition; stopping further steps.")
                        break
                except Exception as exc:
                    print_sections()
                    return False, (f"Step {i + 1}: encountered exception {exc}"), steps_verified

            print_sections()
            print("  [PASS]")
            return True, None, steps_verified
        finally:
            app.cleanup()

    def _wait_for_stable_state(
        self,
        app: TestConversationApp,
        initial_wait: float,
        max_wait: float,
    ) -> str:
        time.sleep(initial_wait)
        start_time = time.time()
        previous_state: Optional[str] = None
        stable_count = 0

        while time.time() - start_time < max_wait:
            current_state = app.user.get_current_state()
            if current_state == previous_state:
                stable_count += 1
                if stable_count >= 2:
                    break
            else:
                stable_count = 0
            previous_state = current_state
            time.sleep(0.2)

        return app.user.get_current_state()

    def test_scenario_with_adaptive_wait(
        self,
        name: str,
        steps: List[Tuple[str, str]],
        initial_wait: float = 0.75,
        max_wait: float = 5.0,
    ) -> Tuple[bool, Optional[str], int]:
        print(f"\n[TEST] {name} (adaptive wait)")
        app = self.build_test_app()
        steps_verified = 0
        transcript: List[Tuple[str, str, str]] = []
        verbose: List[str] = []

        try:
            initial_state = app.user.get_current_state()
        except Exception:
            initial_state = "unknown"
        transcript.append(("Bot", app.intro_text or DEFAULT_INTRO, initial_state))

        def print_sections() -> None:
            print("\n-- Transcript (clean) --")
            for role, text, state in transcript:
                print(f"{role}: {text}\t[{state}]")
            print("\n-- Details (verbose) --")
            for line in verbose:
                print(line)

        try:
            for i, (user_input, expected_state) in enumerate(steps):
                try:
                    if user_input.lower() in {"exit", "quit", "stop"}:
                        app.handle_stop(user_input)
                        current_state = app.user.get_current_state()
                        current_snapshot = app.user.get_fsm_snapshot()
                        transcript.append(("User", user_input, current_state))
                        verbose.append(
                            f"(stop) Expected={expected_state}, actual={current_state}, snapshot={current_snapshot}"
                        )
                        if current_state == expected_state:
                            steps_verified += 1
                            verbose.append("(stop) Matched expected state; exiting scenario.")
                        else:
                            print_sections()
                            return False, (
                                f"Step {i + 1}: expected state '{expected_state}' but observed '{current_state}'"
                            ), steps_verified
                        break

                    response = app.handle_user_turn(user_input)
                    achieved_state = self._wait_for_stable_state(app, initial_wait, max_wait)
                    current_snapshot = app.user.get_fsm_snapshot()

                    transcript.append(("User", user_input, achieved_state))
                    transcript.append(("Bot", response, achieved_state))

                    verbose.append(
                        f"[{i + 1}] user='{user_input}' -> state='{achieved_state}', expected='{expected_state}', snapshot={current_snapshot}"
                    )

                    if achieved_state != expected_state:
                        print_sections()
                        return False, (
                            f"Step {i + 1}: expected state '{expected_state}' but observed '{achieved_state}'"
                        ), steps_verified

                    steps_verified += 1

                    if app.should_exit_stateful():
                        verbose.append("Conversation indicated exit condition; stopping further steps.")
                        break
                except Exception as exc:
                    print_sections()
                    return False, (f"Step {i + 1}: encountered exception {exc}"), steps_verified

            print_sections()
            print("  [PASS]")
            return True, None, steps_verified
        finally:
            app.cleanup()

    def run_tests(
        self,
        scenarios: List[Tuple[str, List[Tuple[str, str]]]],
        wait_time: float = 1.0,
        use_adaptive_wait: bool = False,
    ) -> bool:
        passed = failed = 0
        wait_method = "adaptive" if use_adaptive_wait else f"fixed ({wait_time}s)"
        print(f"[START] Running conversation flow tests with {wait_method} waiting...")

        results: List[dict] = []
        run_db = None
        run_id = None
        run_started = datetime.utcnow()
        if DBConn and ensure_test_run_tables and insert_test_run:
            try:
                run_db = DBConn()
                ensure_test_run_tables(run_db)
                run_id = insert_test_run(run_db, started_at=run_started, total_passed=0, total_failed=0)
            except Exception:
                run_db = None
                run_id = None

        for idx, (name, steps) in enumerate(scenarios, start=1):
            total_steps = len(steps)
            if use_adaptive_wait:
                success, error, steps_verified = self.test_scenario_with_adaptive_wait(name, steps)
            else:
                success, error, steps_verified = self.test_scenario(name, steps, wait_time)

            if success:
                passed += 1
            else:
                failed += 1
                print(f"    [FAIL] {error}")

            results.append(
                {
                    "index": idx,
                    "name": name,
                    "result": "PASS" if success else "FAIL",
                    "steps": f"{steps_verified}/{total_steps}",
                }
            )

            if run_db and run_id and insert_test_case:
                try:
                    insert_test_case(
                        run_db,
                        run_id=run_id,
                        name=name,
                        result=("PASS" if success else "FAIL"),
                        steps_verified=steps_verified,
                        total_steps=total_steps,
                        duration_seconds=None,
                        finished_at=datetime.utcnow(),
                    )
                except Exception:
                    pass

        print(f"\n[RESULTS] {passed} passed, {failed} failed")
        self._print_results_table(results)

        if run_db and run_id and update_test_run:
            try:
                update_test_run(
                    run_db,
                    run_id=run_id,
                    finished_at=datetime.utcnow(),
                    total_passed=passed,
                    total_failed=failed,
                )
            except Exception:
                pass
            finally:
                try:
                    run_db.close()
                except Exception:
                    pass

        if failed == 0:
            print("[SUCCESS] All tests passed!")
            return True
        print("[FAILURE] Some tests failed.")
        return False

    def _print_results_table(self, results: List[dict]) -> None:
        headers = ["#", "Scenario", "Result", "Steps"]
        rows = [[str(r["index"]), r["name"], r["result"], r["steps"]] for r in results]
        widths = [max(len(row[col]) for row in [headers] + rows) for col in range(len(headers))]

        def fmt(values: List[str]) -> str:
            return "| " + " | ".join(val.ljust(widths[idx]) for idx, val in enumerate(values)) + " |"

        sep = "+-" + "+-".join("-" * width for width in widths) + "-+"
        print("\n" + sep)
        print(fmt(headers))
        print(sep)
        for row in rows:
            print(fmt(row))
        print(sep + "\n")


def get_test_scenarios() -> List[Tuple[str, List[Tuple[str, str]]]]:
    return [
        ("Happy path spray request", [
            ("yes id like a spray", "interested"),
            ("1300 just about", "follow_up"),
            ("Okay", "done"),
        ]),
        ("Large sqft rejection recovery", [
            ("Yes i am", "interested"),
            ("1350000, just kidding i dont want this at all, GO AWAY", "not_interested"),
            ("no wait!", "confused"),
            ("i would like a spray", "action_sqft"),
            ("1305", "follow_up"),
            ("ok thanks", "complete_flow"),
        ]),
        ("Immediate rejection", [("no thanks", "not_interested")]),
        ("Immediate rejection with STOP", [("STOP", "user_stopped")]),
        ("User stops conversation", [
            ("yes I have pests", "interested"),
            ("stop", "stop"),
        ]),
        ("Confusion then clarification", [
            ("maybe", "confused"),
            ("yes I need help", "interested"),
            ("about 1200 sq ft", "follow_up"),
            ("sounds good", "done"),
        ]),
        ("Three confusions leading to pause", [
            ("huh?", "confused"),
            ("what do you mean?", "confused"),
            ("I don't understand", "pause"),
        ]),
        ("Direct sqft request from start", [
            ("I need 2000 square feet treated", "action_sqft"),
            ("looks good to me", "follow_up"),
            ("thanks", "done"),
        ]),
        ("Confusion recovery to sqft path", [
            ("Hmm i dont know", "confused"),
            ("actually yes, I need 1800 sq ft done", "action_sqft"),
            ("that works", "follow_up"),
            ("great", "done"),
        ]),
        ("Interested but then changes mind", [
            ("yes I'm interested", "interested"),
            ("actually never mind, not interested", "not_interested"),
        ]),
        ("Polite acknowledgment path", [
            ("yes please", "interested"),
            ("about 1400 square feet", "follow_up"),
            ("thank you so much", "done"),
        ]),
        ("Pause resume to completion", [
            ("I'm confused by this", "confused"),
            ("still not sure what's happening", "confused"),
            ("this is going nowhere", "pause"),
            ("resume", "start"),
            ("yes let's keep going", "interested"),
            ("it's about 1450 square feet", "action_sqft"),
            ("sounds fine", "follow_up"),
            ("thanks!", "done"),
        ]),
        ("Follow-up cancellation", [
            ("yes schedule me", "interested"),
            ("around 1900 square feet", "follow_up"),
            ("actually let's skip it", "not_interested"),
        ]),
        ("Paused user re-engages", [
            ("uh what?", "confused"),
            ("still not sure what you mean", "confused"),
            ("no idea here", "pause"),
            ("actually I do need help", "interested"),
            ("roughly 1100 square feet", "follow_up"),
            ("great appreciate it", "done"),
        ]),
    ]


def main() -> bool:
    tester = BotTester()
    scenarios = get_test_scenarios()
    success = tester.run_tests(scenarios, use_adaptive_wait=True)
    return success


if __name__ == "__main__":
    sys.exit(0 if main() else 1)