# Third-party site packs

Keep packs you did not write here, **not** in `../packs/`. That directory is original work under
AGPL-3.0-or-later; this one is where datasets with their own terms live (for example the
WhatsMyName list, which is CC BY-SA 4.0: a pack derived from it is share-alike, so it stays in
its own licensed directory with attribution and is never merged into the bundled set).

Each pack file must carry `"licence"` (and should carry `"attribution"`); the loader refuses a pack
without one, and every run reports the licence of each pack it used. Point the plugin at this
directory with the `packs_dir` config. Nothing is shipped here.
