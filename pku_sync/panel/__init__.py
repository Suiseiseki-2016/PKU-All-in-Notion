"""The localhost panel package (server extra).

`webapi`/`pipelines` import fastapi/uvicorn and stay out of the default
import path: `pku-sync --help` on a machine without the extra must not
fail. Only `pku-sync panel` (and the panel tests) import them.
"""
