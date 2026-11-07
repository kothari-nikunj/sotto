import importlib.util
from pathlib import Path
import sys


def load_shared_lib(name, *, anchor):
    """Import the single shared implementation in the image or source checkout."""
    here = Path(anchor).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))  # sibling private tools have ordinary module imports
    # Image code is /app/trigger-receiver beside /app/sotto-skills. The checkout
    # uses runtime/trigger-receiver beside ../../sotto-chief-of-staff instead.
    # Never fall back to a writable runtime home for receiver implementation.
    for root in (here, here.parent / 'sotto-skills' / '_shared' / 'lib',
                 here.parent.parent / 'sotto-chief-of-staff' / '_shared' / 'lib'):
        candidate = root / (name + '.py')
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location(name, candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError(f'{name}.py is missing from the runtime')
