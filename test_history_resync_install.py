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
shutil.rmtree(home); print("ok")
