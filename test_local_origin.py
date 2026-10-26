"""Exercise the real mirror loop: local request IDs must not mute a thread."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
import desktop_mirror as dm

class StopPolling(BaseException):
    pass

def check():
    with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
        m = dm.Mirror.__new__(dm.Mirror)
        m.state_db = Path(tmp) / 'state.db'
        m.state_db.touch()
        m.cursor_path = Path(tmp) / 'cursor.json'
        m.cursor = 1
        m.poll_s = 0
        m.cwd_to_channel = {'project': 'channel'}
        m.session_threads = {'chat': 'thread'}
        m.discord_turn_threads = set()
        m._sweep_deletions = lambda: None
        m._target_channel = lambda t: 'channel'
        m._thread_for_turn = lambda *a: 'thread'
        turns = [
            ('user', 'local prompt', 'fef72710597d42ac948bb25b76b0c2f5'),
            ('assistant', 'local reply', None),
            ('user', 'discord prompt', '1563047539773808651'),
            ('assistant', 'discord reply', None),
            ('user', 'back in Desktop', '0717a9573b2a41c996381580baa1765c'),
            ('assistant', 'Desktop reply', None),
            ('user', '[Person] relayed without id', None),
            ('assistant', 'already delivered', None),
            ('user', 'legacy Desktop prompt', None),
            ('assistant', 'legacy reply', None),
        ]
        m._fetch_new_turns = lambda: [dict(id=i, session_id='chat', source='local',
            role=role, content=text, platform_message_id=mid, consumed_copy=0)
            for i, (role, text, mid) in enumerate(turns, 2)]
        sent = []
        m._send = lambda tid, text: sent.append(text)
        with patch.object(dm.time, 'sleep', side_effect=StopPolling):
            try:
                m.run()
            except StopPolling:
                pass
        assert [dm.unformat_turn(text) for text in sent] == [
            'local prompt', 'local reply', 'back in Desktop', 'Desktop reply',
            'legacy Desktop prompt', 'legacy reply'], sent

if __name__ == '__main__':
    check()
    print('ok: local UUID, Discord snowflake, relay fallback, mixed-origin thread, legacy Desktop')
