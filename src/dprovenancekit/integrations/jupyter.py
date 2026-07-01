"""Jupyter / IPython integration for DProvenanceKit.

Provides a cell magic `%%trace_run` that executes the cell inside a trace
run, and automatically displays the DProvenanceKit trace timeline inline.
"""

from __future__ import annotations

import uuid

try:
    from IPython.core.magic import Magics, magics_class, cell_magic
    from IPython.display import display, HTML
except ImportError:
    raise ImportError(
        "IPython is required for this integration. "
        "Install it with: pip install dprovenancekit[jupyter]"
    )


@magics_class
class DProvenanceMagics(Magics):
    @cell_magic
    def trace_run(self, line, cell):
        """Execute a cell within a trace run, and output the trace summary.
        
        Usage:
            %%trace_run [kit_variable_name] [store_variable_name]
        """
        args = line.strip().split()
        kit_var = args[0] if len(args) > 0 else "kit"
        store_var = args[1] if len(args) > 1 else "store"
        
        user_ns = self.shell.user_ns
        
        if kit_var not in user_ns or store_var not in user_ns:
            print(f"Error: Could not find '{kit_var}' and/or '{store_var}' in the namespace.")
            print("Did you instantiate them in a previous cell?")
            return
            
        kit = user_ns[kit_var]
        store = user_ns[store_var]
        
        with kit.run(context_id="jupyter-cell", store=store) as run:
            # Execute the cell in the user's namespace
            result = self.shell.run_cell(cell)
            
        # Only render if there wasn't a syntax error in the cell itself
        if result.error_in_exec is None:
            # Lazy import to avoid circular dependency
            from ..visualizer import render_trace_html
            from ..graph import TraceGraph
            
            run_obj = store.get_run(run.run_id)
            if not run_obj:
                display(HTML("<p>No events recorded.</p>"))
                return
                
            nodes = {ev.id: ev for ev in run_obj.events}
            
            edges = []
            if hasattr(store, '_edges'):
                for e in store._edges:
                    if e.source_id in nodes and e.target_id in nodes:
                        edges.append(e)
            
            graph = TraceGraph(nodes=nodes, edges=edges)
            html_content = render_trace_html(graph, title=f"Trace: {run.run_id}")
            
            display(HTML(f"<div style='height: 500px; overflow: hidden; border-radius: 8px; border: 1px solid #334155;'>{html_content}</div>"))


def load_ipython_extension(ipython):
    ipython.register_magics(DProvenanceMagics)
