from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# The app imports itself as `document_processor_service.*`, which assumes the repo is checked
# out under that name. This checkout is `pdf_parser`, so register the package under the name
# the imports expect before any test module is collected.
_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = "document_processor_service"

if _PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _PACKAGE,
        _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)
