# Make this package's uniquely-named test-support module (mw_fakes) importable under
# pytest's importlib mode, which (unlike prepend/append mode) does not put the test
# directory on sys.path. The helper is named `mw_fakes` (not `_fakes`) so it cannot
# collide with a same-named helper in another workspace package's tests dir.
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
