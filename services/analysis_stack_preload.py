"""Import the analysis stack (pandas, scipy, analysis.*, services.analysis_service).

Imported by ``main.preload_analysis_stack_later`` on a worker thread shortly after
the main window appears, so that neither startup nor the first "Analyze" pays the
~1.5 s import cost on the Tk thread.  Importing this module has no other effect.
"""

from __future__ import annotations

import services.analysis_service  # noqa: F401  (pulls in pandas, scipy, analysis.*)
