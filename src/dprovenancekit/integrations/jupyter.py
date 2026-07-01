"""Jupyter / IPython integration for DProvenanceKit.

Provides a cell magic `%%trace_run` that executes the cell inside a trace
run, and automatically displays the DProvenanceKit trace timeline inline.
"""

from __future__ import annotations

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
            # Simple fallback HTML table of events for Jupyter inline view
            events = store.get_run(run.run_id).events if store.get_run(run.run_id) else []
            html_rows = []
            for i, ev in enumerate(events):
                payload_str = str(ev.payload); payload_str = (payload_str[:100] + '...') if len(payload_str) > 100 else ev.payload
                html_rows.append(
                    f"<tr><td style='padding:4px; border:1px solid #ccc;'>{i}</td>"
                    f"<td style='padding:4px; border:1px solid #ccc;'>{ev.engine_name}</td>"
                    f"<td style='padding:4px; border:1px solid #ccc;'>{ev.payload.type_identifier}</td>"
                    f"<td style='padding:4px; border:1px solid #ccc;'><pre style='margin:0;'>{payload_str}</pre></td></tr>"
                )
            
            table = f"""
            <div style="font-family: sans-serif; border: 1px solid #ccc; padding: 10px; border-radius: 4px; margin-top: 10px;">
                <h3>Trace Timeline (Run: {run.run_id})</h3>
                <table style='width:100%; border-collapse: collapse;'>
                    <tr style='background: #eee;'>
                        <th style='padding:4px; border:1px solid #ccc; text-align: left;'>Seq</th>
                        <th style='padding:4px; border:1px solid #ccc; text-align: left;'>Engine</th>
                        <th style='padding:4px; border:1px solid #ccc; text-align: left;'>Type</th>
                        <th style='padding:4px; border:1px solid #ccc; text-align: left;'>Payload</th>
                    </tr>
                    {''.join(html_rows)}
                </table>
            </div>
            """
            display(HTML(table))


def load_ipython_extension(ipython):
    ipython.register_magics(DProvenanceMagics)
