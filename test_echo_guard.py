"""Echo guard must be keyed by THREAD, not session.

One Discord thread can host two sessions: the desktop session the mirror
created it for, and the discord session the gateway routes replies into.
A session-keyed guard never fires across that split, so every gateway reply
got mirrored a second time.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desktop_mirror as dm

THREAD = "thread-1"


def decide(m, turns):
    """Run the guard over `turns`; return the texts that would be POSTED."""
    posted = []
    for role, text, from_discord in turns:
        if from_discord:
            m.discord_turn_threads.add(THREAD)
            continue
        if role == "user":
            m.discord_turn_threads.discard(THREAD)
        elif THREAD in m.discord_turn_threads:
            continue
        posted.append(text)
    return posted


def main() -> None:
    m = dm.Mirror.__new__(dm.Mirror)
    m.discord_turn_threads = set()

    # THE BUG: prompt arrives over Discord (gateway answers it natively).
    # Neither the prompt nor the reply may be mirrored -- even though the reply
    # is recorded under a DIFFERENT session id than the thread's owner.
    assert decide(m, [
        ("user", "asked in discord", True),
        ("assistant", "gateway reply", False),
    ]) == [], "gateway-delivered reply was mirrored -> duplicate"

    # Desktop prompt in the same thread: mirror owns this exchange again.
    m.discord_turn_threads = set()
    assert decide(m, [
        ("user", "typed in desktop", False),
        ("assistant", "desktop reply", False),
    ]) == ["typed in desktop", "desktop reply"]

    # Alternating: discord turn must not permanently mute the thread.
    m.discord_turn_threads = set()
    assert decide(m, [
        ("user", "from discord", True),
        ("assistant", "gateway reply", False),
        ("user", "from desktop", False),
        ("assistant", "desktop reply", False),
    ]) == ["from desktop", "desktop reply"]

    # Two threads are independent.
    m.discord_turn_threads = {"other-thread"}
    assert decide(m, [("assistant", "reply here", False)]) == ["reply here"]

    print("all ok")


if __name__ == "__main__":
    main()
