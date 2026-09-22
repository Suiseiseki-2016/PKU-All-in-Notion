This directory receives the release wheels when
installer/release/make_simple_index.py assembles the upload tree (the
committed index.html and SHA256SUMS.txt reference them by exact sha256).

The wheels themselves are BUILD ARTIFACTS, not committed files: build them
with `uv build --wheel` at the release commit. The 0.1.0 release wheel must
hash to the value pinned in SHA256SUMS.txt:

    2a1de66498f9fb126dcece549d86bacffbe187cbaafdbe352621d6dcf5095d86  wheels/pku_course_sync-0.1.0-py3-none-any.whl

(the build was verified byte-reproducible for this repo; if your build
hashes differently, regenerate the tree with make_simple_index.py and use
your hashes — see RUNBOOK.md step 4).
