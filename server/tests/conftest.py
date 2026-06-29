import os
import sys

_HERE = os.path.dirname(__file__)
for _p in (os.path.join(_HERE, ".."), os.path.join(_HERE, "..", "..", "src")):
    _p = os.path.abspath(_p)
    if _p not in sys.path:
        sys.path.insert(0, _p)
