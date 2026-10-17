"""Repair Desktop's stale-sidebar shortcut, including unpacked shipped JS.

No transcript writes or new polling. Each eligible tile reads its stored tail;
the existing transcript signature still prevents unchanged paints.
"""
from pathlib import Path
import re
import shutil

SOURCE = Path("src/app/contrib/hooks/use-background-sync.ts")
GUARD = """    if (listRow) {
      if (signatureRef.current.get(rowFingerprintKey) === sessionListFingerprint(listRow)) {
        continue
      }
    }
"""
OLD_COMMENT = """    // Pre-fetch gate (#95767): when the session's sidebar row is listed and its
    // message_count / last_active / preview fingerprint is unchanged, the
    // 120-row transcript fetch is skipped entirely — a no-change tick costs
    // nothing. Tiles with no row (hidden bot chats) keep fetching every tick.
"""
NEW_COMMENT = """    // Router live-refresh repair: sidebar metadata can lag sessions.changed.
    // Always read the stored tail; its signature below still gates publication.
"""
# Rolldown emits the source guard as a conditional around try/catch. Match only
# this exact shape inside the tile reconciler, not arbitrary .current.get calls.
BUNDLE_GUARD = re.compile(
    r"if\(!\(([\w$]+)&&([\w$]+)\.current\.get\(([\w$]+)\)"
    r"===([\w$]+)\(\1\)\)\)try\{"
)
BACKUP_SUFFIX = ".hdr-live-backup"


def repaired_source(text: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    guard = GUARD.replace("\n", newline)
    old_comment = OLD_COMMENT.replace("\n", newline)
    new_comment = NEW_COMMENT.replace("\n", newline)
    if guard not in text:
        if new_comment in text:
            return text
        if "tileRowFingerprintKey" not in text:
            return text  # No known defective shortcut on this Hermes version.
        raise ValueError("Desktop live refresh: source anchor not found; left unchanged")
    if text.count(guard) != 1:
        raise ValueError("Desktop live refresh: ambiguous source guard")
    return text.replace(guard, "", 1).replace(old_comment, new_comment, 1)


def repaired_bundle(text: str) -> str:
    matches = []
    for match in BUNDLE_GUARD.finditer(text):
        # Bind to the actual tile loop: requestSequenceRef and tile identity
        # before the guard, passive transcript read shortly after it.
        prefix = text[max(0, match.start() - 1000):match.start()]
        suffix = text[match.end():match.end() + 1600]
        if "requestSequenceRef:" in prefix and "storedSessionId" in prefix and "{passive:!0}" in suffix:
            matches.append(match)
    if not matches:
        return text
    if len(matches) != 1:
        raise ValueError("Desktop live refresh: ambiguous packaged guard")
    match = matches[0]
    return text[:match.start()] + "try{" + text[match.end():]


def _write_repair(path: Path, transform) -> bool:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    fixed = transform(text)
    if fixed == text:
        return False
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_bytes(fixed.encode("utf-8"))
    print(f"  repaired live tile refresh: {path}")
    return True


def apply_desktop_live_refresh(home: Path) -> None:
    desktop = home / "hermes-agent/apps/desktop"
    source = desktop / SOURCE
    if source.exists():
        _write_repair(source, repaired_source)
    # The app loads these files directly from app.asar.unpacked. Editing TS
    # alone does not change the packaged renderer, and needs no rebuild here.
    for assets in desktop.glob("release/*/resources/app.asar.unpacked/dist/assets"):
        for bundle in assets.glob("*.js"):
            if "tile-meta:" in bundle.read_text(encoding="utf-8"):
                changed = _write_repair(bundle, repaired_bundle)
                if not changed and BUNDLE_GUARD.search(bundle.read_text(encoding="utf-8")):
                    raise ValueError(f"Desktop live refresh: unrecognized bundle shape: {bundle}")
    print("  Desktop live-refresh repair checked; reopen Desktop once to load changed renderer code")


def revert_desktop_live_refresh(home: Path) -> None:
    desktop = home / "hermes-agent/apps/desktop"
    source = desktop / SOURCE
    paths = [source.with_name(source.name + BACKUP_SUFFIX)]
    paths.extend(desktop.glob("release/*/resources/app.asar.unpacked/dist/assets/*" + BACKUP_SUFFIX))
    for path in paths:
        if not path.exists():
            continue
        original = path.with_name(path.name[:-len(BACKUP_SUFFIX)])
        shutil.copy2(path, original)
        path.unlink()
        print(f"  restored: {original}")
