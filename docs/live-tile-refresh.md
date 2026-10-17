# Live split-pane refresh repair

## Spec
External writes to an idle open non-primary Desktop tile must become visible from one sessions.changed event, even while sidebar list refresh is pending. Never mutate chat data or interrupt a live turn. Retain transcript signature gating, owner routing, stale-read checks and pending user messages.

## Evidence and reviewed choice
Independent read-only diagnosis executed the actual hook with deferred list publication. Current code left the tile old after the list became new. Removing the sidebar fingerprint pre-fetch gate updated the tile. Awaiting lists also passed but depends on successful list publication and does not cover the window-return path. Choose removal of the metadata shortcut. An unchanged sidebar row is not evidence that the transcript is unchanged.

## Implementation plan
1. Ship an idempotent guarded repair for the source pre-fetch gate and the equivalent known packaged-renderer guard. Back up before writing. Unknown renderer shapes are reported, never guessed. Hook it into the existing installer and uninstall restoration.
2. Add an installer self-check covering old/fixed/unknown source and bundle shapes, repeat installation, and restoration. Add a hook-level delayed-list regression to the Hermes test file. The existing test asserting no transcript read for unchanged sidebar metadata must now expect a read with no repaint when transcript signature is unchanged.
3. Prove red on old source, green on repaired source. Run both background-sync suites single-worker, all router self-checks, diff whitespace check, and check packaged JS syntax. Exercise the actual packaged reconciliation function with controlled dependencies, before and after repair.
4. Deploy via installer, preserve the running app and all chats. Existing Downloads repair batch already calls the installer and requires Desktop reopen. Push the companion repair only after verification. State clearly that a running renderer must reload once before the persistent change activates; live on-screen Discord repaint is not proven by unit tests.

## Limits
Busy tiles are deliberately skipped. Do not weaken live-turn protection as part of this repair. This does not add a new polling mechanism or change mirroring/routing. The installer cannot generically rebuild a packaged renderer; it only recognizes the metadata guard's exact structural shape inside the tile reconciler.
