"""Mid-call rolling summary (spec 34, Plan 6.8)."""

from worker.memory import ROLLING_TRIGGER_TURNS, RollingMemory, roll_summary


class TestRollingMemory:
    def test_trigger_after_enough_user_turns(self) -> None:
        memory = RollingMemory(base_instructions="You are the hotel agent.")
        assert memory.should_roll() is False
        for index in range(ROLLING_TRIGGER_TURNS):
            memory.record_user_turn(f"turn {index}")
        assert memory.should_roll() is True

    def test_compose_includes_retrieval_and_summary(self) -> None:
        memory = RollingMemory(base_instructions="Be brief.")
        memory.summary = "The caller asked about checkout."
        text = memory.compose_instructions(retrieved="[1] Checkout is at 11:00.")
        assert "Be brief." in text
        assert "Checkout is at 11:00." in text
        assert "Earlier in this call" in text
        assert "The caller asked about checkout." in text

    def test_empty_user_turn_is_ignored(self) -> None:
        memory = RollingMemory(base_instructions="x")
        memory.record_user_turn("   ")
        assert memory.user_turns == 0

    def test_roll_summary_uses_rolling_prompt(self) -> None:
        import inspect

        src = inspect.getsource(roll_summary)
        assert "system_prompt=_ROLLING_PROMPT" in src
        assert "_request_summary" in src
