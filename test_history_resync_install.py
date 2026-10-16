"""apply_history_resync patches a pristine prompt_turn.py once, idempotently, and still parses."""
import ast, os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import install

PRISTINE = ("def f():\n" + install.PT_ANCHOR_1 + "    x = 1\n"
            "    if r:\n" + install.PT_ANCHOR_2)
home = Path(tempfile.mkdtemp())
tg = home / "hermes-agent" / "tui_gateway"; tg.mkdir(parents=True)
install.agent_dir = lambda h: h / "hermes-agent"
(tg / "prompt_turn.py").write_text(PRISTINE, encoding="utf-8")
for _ in range(2):  # second run must be a no-op
    install.apply_history_resync(home)
t = (tg / "prompt_turn.py").read_text(encoding="utf-8")
ast.parse(t)
assert t.count("adopt_foreign_writes(session") == 1 and t.count("record_tail(session") == 1
assert (tg / "history_resync.py").exists()
install.revert_patches(home)
assert (tg / "prompt_turn.py").read_text(encoding="utf-8") == PRISTINE
shutil.rmtree(home)

# Modern Hermes already adopts foreign rows, excluding its own staged user row.
# Remove our older hooks from existing installs, and never add them to clean ones.
modern = (PRISTINE.replace(install.PT_ANCHOR_1,
                          "    _adopt_out_of_band_turns(session)\n" + install.PT_ANCHOR_1)
          + "\ndef _adopt_out_of_band_turns(session):\n    pass\n")
for previously_patched in (False, True):
    home = Path(tempfile.mkdtemp())
    tg = home / "hermes-agent" / "tui_gateway"
    tg.mkdir(parents=True)
    original = modern
    if previously_patched:
        original = original.replace(install.PT_ANCHOR_1, install.PT_INSERT_1 + install.PT_ANCHOR_1)
        original = original.replace(install.PT_ANCHOR_2, install.PT_ANCHOR_2 + install.PT_INSERT_2)
    (tg / "prompt_turn.py").write_text(original, encoding="utf-8")
    for _ in range(2):
        install.apply_history_resync(home)
        repaired = (tg / "prompt_turn.py").read_text(encoding="utf-8")
        ast.parse(repaired)
        assert repaired == modern, "native adoption must not run beside our obsolete resync hooks"
    shutil.rmtree(home)
print("ok: legacy install, native no-op, migration, idempotency")
