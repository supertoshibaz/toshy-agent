
# Managed installs keep user packages outside immutable version directories.
from core.paths import APP_PATHS
if APP_PATHS.root != APP_PATHS.code_root:
    __path__.insert(0, str(APP_PATHS.modules / 'custom'))
