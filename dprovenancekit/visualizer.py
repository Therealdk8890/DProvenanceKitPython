"""Visual Debugger UI for DProvenanceKit.

Renders a TraceGraph into a standalone, zero-dependency HTML file using
inline CSS and Vanilla JS for interactive exploration of AI execution traces.
"""

from __future__ import annotations

import json

from .graph import TraceGraph


_CSS = """
:root {
  --bg-dark: #0f111a;
  --bg-panel: #1a1d27;
  --bg-hover: #262a39;
  --text-main: #e2e8f0;
  --text-muted: #94a3b8;
  --accent: #3b82f6;
  --accent-light: #60a5fa;
  --border: #334155;
  --node-circle: #3b82f6;
  --font-sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background-color: var(--bg-dark);
  color: var(--text-main);
  font-family: var(--font-sans);
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.header {
  padding: 16px 24px;
  background-color: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header h1 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #fff;
  letter-spacing: 0.5px;
}
.header .badge {
  background: var(--accent);
  color: #fff;
  padding: 4px 12px;
  border-radius: 9999px;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
.main-layout {
  display: flex;
  flex: 1;
  overflow: hidden;
}
.sidebar {
  width: 320px;
  background-color: var(--bg-panel);
  border-right: 1px solid var(--border);
  overflow-y: auto;
  padding: 16px 0;
  display: flex;
  flex-direction: column;
}
.inspector {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  background-color: var(--bg-dark);
}
.timeline-item {
  position: relative;
  padding: 12px 24px 12px 48px;
  cursor: pointer;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  transition: background-color 0.2s;
}
.timeline-item:hover {
  background-color: var(--bg-hover);
}
.timeline-item.active {
  background-color: var(--bg-hover);
  border-left: 3px solid var(--accent);
}
.timeline-item::before {
  content: '';
  position: absolute;
  left: 24px;
  top: 20px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background-color: var(--node-circle);
  box-shadow: 0 0 0 4px var(--bg-panel);
  z-index: 2;
}
.timeline-item::after {
  content: '';
  position: absolute;
  left: 28px;
  top: 30px;
  bottom: -20px;
  width: 2px;
  background-color: var(--border);
  z-index: 1;
}
.timeline-item:last-child::after {
  display: none;
}
.node-engine {
  font-size: 12px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}
.node-type {
  font-size: 14px;
  font-weight: 500;
  color: #fff;
}
.node-meta {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
  font-family: var(--font-mono);
}
.inspector-title {
  font-size: 20px;
  font-weight: 600;
  color: #fff;
  margin: 0 0 16px 0;
  display: flex;
  align-items: center;
  gap: 12px;
}
.inspector-title .chip {
  font-size: 11px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 2px 8px;
  border-radius: 4px;
  color: var(--text-muted);
}
.json-view {
  background-color: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.5;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.string { color: #a5d6ff; }
.number { color: #79c0ff; }
.boolean { color: #ff7b72; }
.null { color: #ff7b72; }
.key { color: #7ee787; }
.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-muted);
  font-size: 14px;
}
.section-heading {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 24px 0 12px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}
.edge-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.edge-item {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 8px;
  font-size: 13px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.edge-type {
  color: var(--accent-light);
  font-family: var(--font-mono);
  font-size: 11px;
}
.edge-link {
  color: var(--text-muted);
  cursor: pointer;
  text-decoration: underline;
}
.edge-link:hover {
  color: #fff;
}
"""

_JS = """
function syntaxHighlight(jsonStr) {
    if (typeof jsonStr !== 'string') {
        jsonStr = JSON.stringify(jsonStr, undefined, 2);
    }
    jsonStr = jsonStr.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return jsonStr.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\\s*:)?|\\b(true|false|null)\\b|-?\\d+(?:\\.\\d*)?(?:[eE][+\\-]?\\d+)?)/g, function (match) {
        var cls = 'number';
        if (/^"/.test(match)) {
            if (/:$/.test(match)) {
                cls = 'key';
            } else {
                cls = 'string';
            }
        } else if (/true|false/.test(match)) {
            cls = 'boolean';
        } else if (/null/.test(match)) {
            cls = 'null';
        }
        return '<span class="' + cls + '">' + match + '</span>';
    });
}

function selectNode(nodeId) {
    document.querySelectorAll('.timeline-item').forEach(el => el.classList.remove('active'));
    const el = document.getElementById('node-' + nodeId);
    if (el) el.classList.add('active');

    const node = window.graphData.nodes[nodeId];
    if (!node) return;

    const inspector = document.getElementById('inspector');
    
    // Compute edges
    let derivedFrom = [];
    let informedBy = [];
    
    window.graphData.edges.forEach(edge => {
        if (edge.to === nodeId) {
            if (edge.type === 'DERIVED_FROM') derivedFrom.push(edge.from);
            if (edge.type === 'INFORMED_BY') informedBy.push(edge.from);
        }
    });

    let edgesHtml = '';
    
    if (derivedFrom.length > 0) {
        edgesHtml += '<div class="section-heading">Derived From</div><ul class="edge-list">';
        derivedFrom.forEach(sourceId => {
            const srcNode = window.graphData.nodes[sourceId];
            edgesHtml += `<li class="edge-item">
                <span class="edge-link" onclick="selectNode('${sourceId}')">${srcNode.type_identifier} (${srcNode.engine_name})</span>
                <span class="edge-type">DERIVED_FROM</span>
            </li>`;
        });
        edgesHtml += '</ul>';
    }

    if (informedBy.length > 0) {
        edgesHtml += '<div class="section-heading">Informed By</div><ul class="edge-list">';
        informedBy.forEach(sourceId => {
            const srcNode = window.graphData.nodes[sourceId];
            edgesHtml += `<li class="edge-item">
                <span class="edge-link" onclick="selectNode('${sourceId}')">${srcNode.type_identifier} (${srcNode.engine_name})</span>
                <span class="edge-type">INFORMED_BY</span>
            </li>`;
        });
        edgesHtml += '</ul>';
    }

    let payloadJsonStr = "";
    try {
        payloadJsonStr = JSON.stringify(node.payload, null, 2);
    } catch (e) {
        payloadJsonStr = String(node.payload);
    }

    inspector.innerHTML = `
        <div class="inspector-title">
            ${node.type_identifier}
            <span class="chip">${node.engine_name}</span>
            <span class="chip">Seq: ${node.sequence}</span>
        </div>
        ${edgesHtml}
        <div class="section-heading">Payload</div>
        <div class="json-view">${syntaxHighlight(payloadJsonStr)}</div>
        <div class="section-heading">Raw Node Metadata</div>
        <div class="json-view">${syntaxHighlight(JSON.stringify(node, null, 2))}</div>
    `;
}
"""

def render_trace_html(graph: TraceGraph, title: str = "Visual Debugger") -> str:
    """Render a standalone interactive HTML visualizer for a TraceGraph."""
    
    # Extract nodes sorted by sequence
    nodes_list = sorted(graph.nodes.values(), key=lambda n: n.sequence)
    
    # Serialize data for the JS frontend
    js_nodes = {}
    for n in nodes_list:
        payload = n.payload
        try:
            payload_dict = payload.to_dict()
        except AttributeError:
            payload_dict = str(payload)
            
        js_nodes[str(n.id)] = {
            "id": str(n.id),
            "run_id": str(n.run_id),
            "context_id": n.context_id,
            "engine_name": n.engine_name,
            "sequence": n.sequence,
            "type_identifier": payload.type_identifier if hasattr(payload, "type_identifier") else str(payload),
            "payload": payload_dict
        }
        
    js_edges = []
    for e in graph.edges:
        js_edges.append({
            "from": str(e.source_id),
            "to": str(e.target_id),
            "type": e.type.name
        })
        
    graph_data_json = json.dumps({"nodes": js_nodes, "edges": js_edges})

    # Generate timeline HTML
    timeline_html = []
    for n in nodes_list:
        type_id = n.payload.type_identifier if hasattr(n.payload, "type_identifier") else str(n.payload)
        timeline_html.append(f'''
        <div class="timeline-item" id="node-{n.id}" onclick="selectNode('{n.id}')">
            <div class="node-engine">{n.engine_name}</div>
            <div class="node-type">{type_id}</div>
            <div class="node-meta">seq: {n.sequence}</div>
        </div>
        ''')
        
    timeline_str = "".join(timeline_html)
    
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>{title}</title>
    <style>{_CSS}</style>
</head>
<body>
    <div class="header">
        <h1>{title}</h1>
        <span class="badge">DProvenanceKit</span>
    </div>
    <div class="main-layout">
        <div class="sidebar">
            {timeline_str}
        </div>
        <div class="inspector" id="inspector">
            <div class="empty-state">Select a node on the timeline to inspect its execution trace.</div>
        </div>
    </div>
    <script>
        window.graphData = {graph_data_json};
        {_JS}
    </script>
</body>
</html>
"""

__all__ = ["render_trace_html"]
