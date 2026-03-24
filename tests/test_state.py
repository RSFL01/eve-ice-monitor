from pathlib import Path

from ice_monitor.state import MonitorState, load_state, save_state



def test_load_missing_state(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = load_state(state_file)
    assert state.baseline_npc_kills is None



def test_save_then_load_state(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    original = MonitorState(baseline_npc_kills=10, ice_belt_active=True)
    save_state(state_file, original)
    loaded = load_state(state_file)
    assert loaded.baseline_npc_kills == 10
    assert loaded.ice_belt_active is True
