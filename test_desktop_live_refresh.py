"""Small offline installer check. Optional: --desktop PATH runs Hermes UI tests."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

from desktop_live_refresh import (
    SOURCE, GUARD, OLD_COMMENT, NEW_COMMENT, BACKUP_SUFFIX,
    apply_desktop_live_refresh, repaired_bundle, repaired_source,
    revert_desktop_live_refresh,
)


def self_check():
    prefix = 'async function io({requestSequenceRef:e,signatureRef:t}){let storedSessionId="x";'
    compiled = prefix + 'if(!(d&&t.current.get(f)===Ha(d)))try{await m(i,l,{passive:!0})}catch{}}'
    fixed = repaired_bundle(compiled)
    assert fixed != compiled and 'if(!(d&&' not in fixed
    assert repaired_bundle(fixed) == fixed
    assert repaired_bundle('if(!(d&&t.current.get(f)===Ha(d)))try{}catch{}') == 'if(!(d&&t.current.get(f)===Ha(d)))try{}catch{}'
    assert repaired_source('upstream without the shortcut') == 'upstream without the shortcut'
    try:
        repaired_source('unknown version with tileRowFingerprintKey')
    except ValueError:
        pass
    else:
        raise AssertionError('unknown source must fail closed')
    original = OLD_COMMENT + '    const rowFingerprintKey = tileRowFingerprintKey(id);\n' + GUARD
    assert GUARD not in repaired_source(original)
    assert NEW_COMMENT in repaired_source(original)
    with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temp:
        home = Path(temp)
        desktop = home / 'hermes-agent/apps/desktop'
        source = desktop / SOURCE
        bundle = desktop / 'release/win-unpacked/resources/app.asar.unpacked/dist/assets/updates-test.js'
        source.parent.mkdir(parents=True)
        bundle.parent.mkdir(parents=True)
        source.write_text(original, encoding='utf-8')
        bundle.write_text('const meta="tile-meta:";' + compiled, encoding='utf-8')
        before = {p: p.read_bytes() for p in (source, bundle)}
        apply_desktop_live_refresh(home)
        after = {p: p.read_bytes() for p in before}
        assert all(after[p] != before[p] for p in before)
        apply_desktop_live_refresh(home)
        assert all(p.read_bytes() == after[p] for p in before)
        revert_desktop_live_refresh(home)
        assert all(p.read_bytes() == before[p] for p in before)
        assert not list(desktop.rglob('*' + BACKUP_SUFFIX))
    print('PASS source/bundle repair, unknown anchors, idempotency, restoration')


def desktop_check(desktop):
    # Copy the existing suite so its real imports/setup exercise the installed
    # source without editing upstream tests. Delete the temporary suite on exit.
    hooks = desktop / 'src/app/contrib/hooks'
    original = (hooks / 'use-background-sync.test.ts').read_text(encoding='utf-8')
    source = (desktop / SOURCE).read_text(encoding='utf-8')
    if GUARD not in source:
        # Replace just the obsolete optimization assertion with authoritative
        # transcript-signature gating. All unrelated upstream tests stay intact.
        start = original.index("  it('skips the tile fetch when the sidebar row fingerprint is unchanged'")
        end = original.index("  it('still fetches when the sidebar row moved'", start)
        case = original[start:end]
        case = case.replace("    const updateSessionState = vi.fn()", "    signatureRef.current.set(`tile:${TILE_STORED_ID}`, sessionMessagesSignature([{ content: 'q', role: 'user', timestamp: 1 }, { content: 'a', role: 'assistant', timestamp: 2 }] as never))\n    const updateSessionState = vi.fn()")
        case = case.replace('expect(getLatestSessionMessages).not.toHaveBeenCalled()', 'expect(getLatestSessionMessages).toHaveBeenCalledTimes(1)')
        original = original[:start] + case + original[end:]
    test = hooks / 'router-live-tile-regression.test.ts'
    assert not test.exists(), 'refusing to overwrite an existing test'
    test.write_text(original + '\n' + (Path(__file__).parent / 'tests/live_tile_refresh.test.ts.inc').read_text(encoding='utf-8'), encoding='utf-8')
    node = os.environ.get('NODE_EXE', 'node')
    cmd = [node, str(desktop.parent.parent / 'node_modules/vitest/vitest.mjs'), 'run', '--project', 'ui', '--maxWorkers', '1', '--no-file-parallelism', str(test), 'src/app/contrib/hooks/use-background-sync.test.tsx']
    if '--red' in sys.argv:
        cmd += ['-t', 'router cross-process tile refresh regression']
    try:
        result = subprocess.run(cmd, cwd=desktop, timeout=240)
        return result.returncode
    finally:
        test.unlink()


if __name__ == '__main__':
    self_check()
    if '--desktop' in sys.argv:
        raise SystemExit(desktop_check(Path(sys.argv[sys.argv.index('--desktop') + 1])))
