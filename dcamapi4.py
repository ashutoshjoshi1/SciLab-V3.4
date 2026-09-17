import importlib.util
from pathlib import Path


_SOURCE_PATH = Path(__file__).resolve().parent / "spectrometers dll files" / "dcamapi4.py"
_SPEC = importlib.util.spec_from_file_location("_bundled_dcamapi4", _SOURCE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load bundled DCAM API shim from '{_SOURCE_PATH}'.")

_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

for _name, _value in vars(_MODULE).items():
    if _name.startswith("__") and _name not in {"__doc__", "__all__"}:
        continue
    globals()[_name] = _value

__all__ = getattr(_MODULE, "__all__", [name for name in globals() if not name.startswith("_")])
