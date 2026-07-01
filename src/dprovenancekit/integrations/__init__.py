"""Framework integrations for DProvenanceKit.

Each integration is an *optional* adapter that translates some external agent
framework's execution into DProvenanceKit trace events. Integrations live here, never
in the core package, and the core never imports them — so ``import dprovenancekit``
stays dependency-free (pure standard library) while an adapter may require a
third-party package.

Import an adapter explicitly, e.g.::

    from dprovenancekit.integrations.langchain import DProvenanceTracer

The LangChain adapter requires ``langchain-core`` (``pip install dprovenancekit[langchain]``).
"""

from __future__ import annotations

# git-blob-rewrite
